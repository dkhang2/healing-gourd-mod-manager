import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List, Optional, Tuple, TypedDict


APP_NAME = "Healing Gourd Mod Manager"
IS_FROZEN = getattr(sys, "frozen", False)
BASE = Path(sys.executable).parent if IS_FROZEN else Path(__file__).parent
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DATA_DIR = LOCAL_APP_DATA / APP_NAME if IS_FROZEN else BASE
LIBRARY = DATA_DIR / "Library"
STATE_FILE = DATA_DIR / ".sekiro_mod_manager.json"
MIGRATION_MARKER = DATA_DIR / ".legacy-migration-complete"
ASSETS = DATA_DIR / "Assets"
DEFAULT_ICON_NAME = "healing-gourd.ico"
DEFAULT_BACKGROUND_NAME = "sekiro-background.png"
MAX_ARCHIVE_FILES = 20000
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
ARCHIVE_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
MOD_CONTENT_DIR_NAMES = {
    "asset", "assets", "chr", "event", "font", "map", "menu", "mtd", "msg",
    "movie", "obj", "other", "param", "parts", "script", "sfx", "sound",
}


def bundled_asset_path(name: str) -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", BASE)
    return Path(bundle_dir) / "Assets" / name


def resolve_background_image_path(background: Optional[str]) -> Optional[Path]:
    if background:
        image_path = ASSETS / background
    else:
        image_path = bundled_asset_path(DEFAULT_BACKGROUND_NAME)
    return image_path if image_path.is_file() else None


class ManagerState(TypedDict):
    version: int
    game_dir: Optional[str]
    mods: Dict[str, Dict[str, str]]
    display_names: Dict[str, str]
    appearance: Dict[str, Optional[str]]


def migrate_legacy_user_data() -> None:
    if not IS_FROZEN or STATE_FILE.exists() or MIGRATION_MARKER.exists():
        return

    legacy_dirs = [BASE]
    if BASE.name.casefold() == APP_NAME.casefold():
        legacy_dirs.append(BASE.parent)
    legacy_dir = next(
        (
            directory for directory in legacy_dirs
            if (directory / STATE_FILE.name).is_file()
            or (directory / "Library").is_dir()
            or (directory / "Assets").is_dir()
        ),
        None,
    )
    if legacy_dir is None:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for folder_name in ("Library", "Assets"):
        source = legacy_dir / folder_name
        if not source.is_dir():
            continue
        destination = DATA_DIR / folder_name
        for path in source.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            target = destination / path.relative_to(source)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

    legacy_state = legacy_dir / STATE_FILE.name
    if legacy_state.is_file() and not STATE_FILE.exists():
        temporary_state = STATE_FILE.with_suffix(".migration.tmp")
        shutil.copy2(legacy_state, temporary_state)
        os.replace(temporary_state, STATE_FILE)
    MIGRATION_MARKER.write_text("complete\n", encoding="utf-8")


def load_state() -> ManagerState:
    migrate_legacy_user_data()
    if not STATE_FILE.exists():
        return {
            "version": 1,
            "game_dir": None,
            "mods": {},
            "display_names": {},
            "appearance": {"background_image": None, "app_icon": None},
        }

    try:
        raw: object = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid manager state file: {STATE_FILE}") from error

    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError(f"Unsupported manager state file: {STATE_FILE}")

    game_dir = raw.get("game_dir")
    raw_mods = raw.get("mods")
    raw_display_names = raw.get("display_names", {})
    raw_appearance = raw.get("appearance", {})
    if (
        (game_dir is not None and not isinstance(game_dir, str))
        or not isinstance(raw_mods, dict)
        or not isinstance(raw_display_names, dict)
        or not isinstance(raw_appearance, dict)
    ):
        raise ValueError(f"Invalid manager state file: {STATE_FILE}")

    mods: Dict[str, Dict[str, str]] = {}
    for name, files in raw_mods.items():
        if not isinstance(name, str) or not isinstance(files, dict):
            raise ValueError(f"Invalid mod record in manager state file: {STATE_FILE}")
        if not all(isinstance(path, str) and isinstance(digest, str) for path, digest in files.items()):
            raise ValueError(f"Invalid file record in manager state file: {STATE_FILE}")
        mods[name] = files

    display_names: Dict[str, str] = {}
    for name, display_name in raw_display_names.items():
        if not isinstance(name, str) or not isinstance(display_name, str):
            raise ValueError(f"Invalid display-name record in manager state file: {STATE_FILE}")
        display_names[name] = display_name

    appearance: Dict[str, Optional[str]] = {}
    for key in ("background_image", "app_icon"):
        value = raw_appearance.get(key)
        if value is not None and (
            not isinstance(value, str)
            or Path(value).name != value
            or (key == "background_image" and Path(value).suffix.casefold() not in {".png", ".gif"})
            or (key == "app_icon" and value.casefold() != "app-icon.ico")
        ):
            raise ValueError(f"Invalid appearance setting in manager state file: {STATE_FILE}")
        appearance[key] = value

    return {
        "version": 1,
        "game_dir": game_dir,
        "mods": mods,
        "display_names": display_names,
        "appearance": appearance,
    }


def save_state(state: ManagerState) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary_file, STATE_FILE)


def validate_mod_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or name.endswith((" ", "."))
        or any(char in name for char in '<>:"/\\|?*')
        or name.split(".", 1)[0].upper() in ARCHIVE_RESERVED_NAMES
    ):
        raise ValueError("Mod name must be a non-empty folder name.")


def extract_mod_archive(
    source: Path,
    library: Path,
    mod_name: Optional[str] = None,
) -> str:
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if not entries:
            raise ValueError("The ZIP archive is empty.")
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValueError(f"The ZIP archive contains more than {MAX_ARCHIVE_FILES} entries.")
        if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_SIZE:
            raise ValueError("The ZIP archive expands to more than 2 GiB.")

        paths: List[Tuple[zipfile.ZipInfo, PurePosixPath, bool]] = []
        seen_paths: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
        seen_prefixes: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
        top_level_names: set[str] = set()
        has_root_files = False
        file_count = 0

        for entry in entries:
            raw_path = entry.filename.replace("\\", "/")
            relative = PurePosixPath(raw_path)
            windows_path = PureWindowsPath(raw_path)
            parts = relative.parts
            if (
                not raw_path
                or relative.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or not parts
                or any(part in {"", ".", ".."} for part in parts)
                or any(
                    any(char in part for char in '<>:"|?*')
                    or part.endswith((" ", "."))
                    or part.split(".", 1)[0].upper() in ARCHIVE_RESERVED_NAMES
                    for part in parts
                )
            ):
                raise ValueError(f"Unsafe path in ZIP archive: {entry.filename}")

            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symbolic links are not allowed in ZIP archives: {entry.filename}")
            if entry.flag_bits & 0x1:
                raise ValueError("Password-protected ZIP archives are not supported.")

            is_directory = entry.is_dir() or raw_path.endswith("/")
            folded_parts = tuple(part.casefold() for part in parts)
            original_parts = tuple(parts)
            for depth in range(1, len(parts) + 1):
                folded_prefix = folded_parts[:depth]
                original_prefix = original_parts[:depth]
                previous_prefix = seen_prefixes.get(folded_prefix)
                if previous_prefix is not None and previous_prefix != original_prefix:
                    raise ValueError(f"Conflicting Windows paths in ZIP archive: {entry.filename}")
                seen_prefixes[folded_prefix] = original_prefix
            previous_parts = seen_paths.get(folded_parts)
            if previous_parts is not None and previous_parts != original_parts:
                raise ValueError(f"Duplicate Windows path in ZIP archive: {entry.filename}")
            if previous_parts is not None and not is_directory:
                raise ValueError(f"Duplicate file in ZIP archive: {entry.filename}")
            seen_paths[folded_parts] = original_parts
            top_level_names.add(parts[0])
            if len(parts) == 1 and not is_directory:
                has_root_files = True
            if not is_directory:
                file_count += 1
            paths.append((entry, relative, is_directory))

        if file_count == 0:
            raise ValueError("The ZIP archive does not contain any files.")

        with tempfile.TemporaryDirectory(prefix=".import-", dir=library) as temporary:
            staging = (Path(temporary) / "payload").resolve()
            staging.mkdir()
            for entry, relative, is_directory in paths:
                target = staging.joinpath(*relative.parts)
                try:
                    target.resolve().relative_to(staging)
                except ValueError as error:
                    raise ValueError(f"Unsafe path in ZIP archive: {entry.filename}") from error
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    extracted_size = 0
                    with archive.open(entry) as archived_file, target.open("xb") as extracted_file:
                        while chunk := archived_file.read(1024 * 1024):
                            extracted_size += len(chunk)
                            if extracted_size > entry.file_size or extracted_size > MAX_ARCHIVE_SIZE:
                                raise ValueError(f"ZIP entry exceeds its declared size: {entry.filename}")
                            extracted_file.write(chunk)
                except NotImplementedError as error:
                    raise ValueError(f"Unsupported ZIP compression for: {entry.filename}") from error
                if extracted_size != entry.file_size:
                    raise ValueError(f"Unexpected extracted size for ZIP entry: {entry.filename}")

            wrapper_name = next(iter(top_level_names)) if len(top_level_names) == 1 else None
            if (
                wrapper_name is not None
                and not has_root_files
                and wrapper_name.casefold() not in MOD_CONTENT_DIR_NAMES
            ):
                content = staging / wrapper_name
                name = mod_name or wrapper_name
            else:
                content = staging
                name = mod_name or source.stem
            validate_mod_name(name)
            destination = library / name
            if destination.exists():
                raise ValueError(f"A mod with that name already exists: {name}")
            if not any(path.is_file() for path in content.rglob("*")):
                raise ValueError("The ZIP archive did not produce any mod files.")
            shutil.move(str(content), str(destination))
            return name


def import_mod(source_folder: str, mod_name: Optional[str] = None) -> str:
    source = Path(source_folder).expanduser()
    is_archive = source.is_file() and source.suffix.casefold() == ".zip"
    if not is_archive and not source.is_dir():
        raise ValueError(f"Source mod folder or ZIP archive not found: {source}")

    LIBRARY.mkdir(parents=True, exist_ok=True)
    if is_archive:
        name = extract_mod_archive(source, LIBRARY, mod_name)
    else:
        name = mod_name or source.name
        validate_mod_name(name)
        destination = LIBRARY / name
        if destination.exists():
            raise ValueError(f"A mod with that name already exists: {name}")
        shutil.copytree(source, destination)
    print(f"Imported: {name}")
    return name


def launch_game(game_dir: Path) -> subprocess.Popen[bytes]:
    if not is_game_dir(game_dir):
        raise ValueError(f"Could not find sekiro.exe in: {game_dir}")
    executable = game_dir / "sekiro.exe"
    return subprocess.Popen([str(executable)], cwd=str(game_dir))


def is_nested_mod_folder(path: Path) -> bool:
    if path.is_symlink() or path.name.casefold() in MOD_CONTENT_DIR_NAMES or not path.is_dir():
        return False
    try:
        return any(
            child.is_dir() and child.name.casefold() in MOD_CONTENT_DIR_NAMES
            for child in path.iterdir()
        )
    except OSError:
        return False


def install_external_mod(game_dir: Path, mod_name: str) -> int:
    validate_mod_name(mod_name)
    source = game_dir / "mods" / mod_name
    if not is_nested_mod_folder(source):
        raise ValueError(f"{mod_name} is not a nested mod folder under the game's mods directory.")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Nested mods containing symbolic links cannot be imported: {path}")
    if (LIBRARY / mod_name).exists():
        raise ValueError(
            f"A library mod named '{mod_name}' already exists. Select that library entry instead."
        )
    import_mod(str(source), mod_name)
    enable_mod(mod_name)
    return launch_modded_game(game_dir)


def launch_modded_game(game_dir: Path) -> int:
    launch_game(game_dir)
    return len(load_state()["mods"])


def list_mods() -> None:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    mods = sorted(path for path in LIBRARY.iterdir() if path.is_dir())
    state = load_state()

    print("Mods in library:")
    if not mods:
        print(" (none)")
        return

    for mod in mods:
        status = "enabled" if mod.name in state["mods"] else "disabled"
        print(f" - {mod.name} ({status})")


def get_game_dir(state: ManagerState) -> Path:
    configured_path = state["game_dir"]
    if configured_path:
        game_dir = Path(configured_path)
        if is_game_dir(game_dir):
            return game_dir

    entered_path = input("Sekiro install folder (the folder containing sekiro.exe): ").strip().strip('"')
    if not entered_path:
        raise ValueError("A Sekiro install folder is required.")

    game_dir = Path(entered_path).expanduser().resolve()
    if not is_game_dir(game_dir):
        raise ValueError(f"Could not find sekiro.exe in: {game_dir}")

    state["game_dir"] = str(game_dir)
    save_state(state)
    return game_dir


def is_game_dir(path: Path) -> bool:
    try:
        return path.is_dir() and any(
            entry.is_file() and entry.name.casefold() == "sekiro.exe"
            for entry in path.iterdir()
        )
    except OSError:
        return False


def steam_install_paths() -> List[Path]:
    try:
        import winreg
    except ImportError:
        return []

    registry_locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
    )
    paths: List[Path] = []
    for hive, key_name in registry_locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in ("SteamPath", "InstallPath"):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if isinstance(value, str) and value:
                        path = Path(value).expanduser()
                        if path not in paths:
                            paths.append(path)
        except OSError:
            continue
    return paths


def discover_game_dir(steam_paths: Optional[List[Path]] = None) -> Optional[Path]:
    steam_roots = steam_paths if steam_paths is not None else steam_install_paths()
    libraries: List[Path] = []

    for steam_root in steam_roots:
        if steam_root not in libraries:
            libraries.append(steam_root)
        library_file = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            contents = library_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value in re.findall(r'"(?:path|[0-9]+)"\s*"([^"]+)"', contents):
            library = Path(value.replace("\\\\", "\\"))
            if library not in libraries:
                libraries.append(library)

    for library in libraries:
        steamapps = library / "steamapps"
        manifest = steamapps / "appmanifest_814380.acf"
        install_dirs: List[str] = []
        try:
            manifest_contents = manifest.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'"installdir"\s*"([^"]+)"', manifest_contents, re.IGNORECASE)
            if match:
                install_dirs.append(match.group(1))
        except OSError:
            pass
        if "Sekiro" not in install_dirs:
            install_dirs.append("Sekiro")

        common = steamapps / "common"
        for install_dir in install_dirs:
            candidate = common / install_dir
            if is_game_dir(candidate):
                return candidate.resolve()
        try:
            for candidate in common.iterdir():
                if candidate.is_dir() and candidate.name.casefold() == "sekiro" and is_game_dir(candidate):
                    return candidate.resolve()
        except OSError:
            continue
    return None


def list_unmanaged_installed_mods(
    mods_dir: Path,
    managed_files: set[str],
) -> List[Tuple[str, int, str]]:
    if not mods_dir.is_dir():
        return []

    installed: List[Tuple[str, int, str]] = []
    for entry in sorted(mods_dir.iterdir(), key=lambda path: path.name.casefold()):
        if entry.is_symlink():
            continue
        if entry.is_file():
            relative = entry.relative_to(mods_dir).as_posix()
            if relative.casefold() not in managed_files:
                installed.append((entry.name, 1, "External"))
            continue
        if not entry.is_dir():
            continue

        files = [path for path in entry.rglob("*") if path.is_file() and not path.is_symlink()]
        unmanaged_count = sum(
            path.relative_to(mods_dir).as_posix().casefold() not in managed_files
            for path in files
        )
        if unmanaged_count:
            if is_nested_mod_folder(entry):
                status = "Nested - inactive"
            else:
                status = "Mixed" if unmanaged_count != len(files) else "External"
            installed.append((entry.name, unmanaged_count, status))
    return installed


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def managed_path(mods_dir: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError(f"Unsafe path in manager state: {relative_path}")

    root = mods_dir.resolve()
    destination = mods_dir.joinpath(*relative.parts)
    try:
        destination.resolve().relative_to(root)
    except ValueError as error:
        raise ValueError(f"Unsafe path in manager state: {relative_path}") from error
    return destination


def enable_mod(mod_name: str) -> None:
    validate_mod_name(mod_name)
    state = load_state()
    mod_folder = LIBRARY / mod_name
    if not mod_folder.is_dir():
        raise ValueError(f"Mod not found in library: {mod_name}")
    if mod_name in state["mods"]:
        raise ValueError(f"Mod is already enabled: {mod_name}")

    source_files: List[Path] = []
    for path in mod_folder.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Mods containing symbolic links cannot be deployed: {path}")
        if path.is_file():
            source_files.append(path)
    if not source_files:
        raise ValueError(f"Mod contains no files: {mod_name}")

    game_dir = get_game_dir(state)
    mods_dir = game_dir / "mods"
    records = state["mods"]
    owned_paths = {
        relative.casefold()
        for files in records.values()
        for relative in files
    }

    planned: List[Tuple[Path, Path, str]] = []
    for source in source_files:
        relative = source.relative_to(mod_folder).as_posix()
        destination = managed_path(mods_dir, relative)
        if relative.casefold() in owned_paths or destination.exists() or destination.is_symlink():
            raise ValueError(f"Deployment conflict; file already exists or is managed: {destination}")
        planned.append((source, destination, relative))

    deployed_files: Dict[str, str] = {}
    copied: List[Path] = []
    try:
        for source, destination, relative in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied.append(destination)
            shutil.copy2(source, destination)
            deployed_files[relative] = file_hash(destination)

        records[mod_name] = deployed_files
        save_state(state)
    except (OSError, shutil.Error):
        for destination in copied:
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
        raise

    print(f"Enabled: {mod_name}")


def disable_mod(mod_name: str) -> None:
    validate_mod_name(mod_name)
    state = load_state()
    files = state["mods"].get(mod_name)
    if files is None:
        raise ValueError(f"Mod is not enabled: {mod_name}")

    configured_path = state["game_dir"]
    if not configured_path:
        raise ValueError("No Sekiro install folder is configured.")
    mods_dir = Path(configured_path) / "mods"
    preserved: List[str] = []

    for relative, expected_hash in files.items():
        destination = managed_path(mods_dir, relative)
        if destination.is_symlink() or not destination.is_file():
            preserved.append(relative)
            continue
        if file_hash(destination) != expected_hash:
            preserved.append(relative)
            continue
        destination.unlink()
        parent = destination.parent
        while parent != mods_dir:
            try:
                parent.relative_to(mods_dir)
                parent.rmdir()
            except OSError:
                break
            except ValueError:
                break
            parent = parent.parent

    del state["mods"][mod_name]
    save_state(state)
    print(f"Disabled: {mod_name}")
    for relative in preserved:
        print(f"Preserved changed or missing file: {relative}", file=sys.stderr)


def delete_library_mod(mod_name: str) -> None:
    validate_mod_name(mod_name)
    mod_folder = LIBRARY / mod_name
    if mod_folder.is_symlink() or not mod_folder.is_dir():
        raise ValueError(f"Mod not found in library: {mod_name}")
    if mod_folder.resolve().parent != LIBRARY.resolve():
        raise ValueError(f"Mod folder is outside the library: {mod_name}")

    state = load_state()
    if mod_name in state["mods"]:
        disable_mod(mod_name)

    shutil.rmtree(mod_folder)
    print(f"Deleted from library: {mod_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Sekiro mods with the dinput8-style mods folder.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="List imported mods and their enabled status")

    import_command = commands.add_parser("import", help="Import a mod folder or ZIP archive")
    import_command.add_argument("source", help="Path to an extracted mod folder or ZIP archive")
    import_command.add_argument("--name", help="Name to use in the library (defaults to folder name)")

    enable_command = commands.add_parser("enable", help="Deploy a mod into Sekiro's mods folder")
    enable_command.add_argument("name", help="Name of the imported mod")

    disable_command = commands.add_parser("disable", help="Remove this manager's unchanged files for a mod")
    disable_command.add_argument("name", help="Name of the enabled mod")

    delete_command = commands.add_parser("delete", help="Delete a mod from the manager library")
    delete_command.add_argument("name", help="Name of the imported mod")
    delete_command.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion; unchanged deployed files for an enabled mod will also be removed",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            list_mods()
        elif args.command == "import":
            import_mod(args.source, args.name)
        elif args.command == "enable":
            enable_mod(args.name)
        elif args.command == "disable":
            disable_mod(args.name)
        elif args.command == "delete":
            if not args.yes:
                raise ValueError("Deletion requires confirmation; rerun with --yes.")
            delete_library_mod(args.name)
        return 0
    except (OSError, ValueError, shutil.Error, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    class ModManagerApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(APP_NAME)
            self.root.configure(background="#111211")

            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure("TFrame", background="#111211")
            style.configure("Panel.TFrame", background="#1a1c1b")
            style.configure("TLabel", background="#111211", foreground="#d8d0c0",
                            font=("Segoe UI", 10))
            style.configure("Title.TLabel", background="#171918", foreground="#eee5d3",
                            font=("Segoe UI", 23, "bold"))
            style.configure("Eyebrow.TLabel", background="#171918", foreground="#bd3b32",
                            font=("Segoe UI", 9, "bold"))
            style.configure("Muted.TLabel", background="#171918", foreground="#a79e8e",
                            font=("Segoe UI", 10))
            style.configure("TLabelframe", background="#1a1c1b", foreground="#d8d0c0",
                            bordercolor="#353733", relief="solid")
            style.configure("TLabelframe.Label", background="#1a1c1b", foreground="#bd3b32",
                            font=("Segoe UI", 9, "bold"))
            style.configure("TEntry", fieldbackground="#101211", foreground="#eee5d3",
                            insertcolor="#eee5d3", bordercolor="#454741", padding=7)
            style.configure("Treeview", background="#171918", fieldbackground="#171918",
                            foreground="#d8d0c0", rowheight=34, bordercolor="#353733",
                            font=("Segoe UI", 10))
            style.configure("Treeview.Heading", background="#252725", foreground="#bdb4a4",
                            font=("Segoe UI", 9, "bold"), padding=(8, 10), relief="flat")
            style.map("Treeview", background=[("selected", "#5a2724")],
                      foreground=[("selected", "#fff1dc")])
            style.map("Treeview.Heading", background=[("active", "#333532")])
            style.configure("TButton", background="#30322f", foreground="#e5ddcd",
                            font=("Segoe UI", 9, "bold"), padding=(12, 8), borderwidth=0)
            style.map("TButton", background=[("active", "#42443f"), ("disabled", "#222421")],
                      foreground=[("disabled", "#77766f")])
            style.configure("Accent.TButton", background="#9c302a", foreground="#fff0df")
            style.map("Accent.TButton", background=[("active", "#b63b33"), ("disabled", "#402724")])
            style.configure("Danger.TButton", background="#382321", foreground="#e8a39a")
            style.map("Danger.TButton", background=[("active", "#60302b"), ("disabled", "#25211f")])

            outer = ttk.Frame(root, padding=18)
            outer.pack(fill="both", expand=True)
            self.header_canvas = tk.Canvas(
                outer,
                height=146,
                background="#171918",
                highlightthickness=1,
                highlightbackground="#30322f",
                borderwidth=0,
            )
            self.header_canvas.pack(fill="x", pady=(0, 16))
            self.header_canvas.bind("<Configure>", self.draw_header)
            self.header_photo = None
            self.header_scaled_photo = None
            self.customize_button = ttk.Button(
                self.header_canvas,
                text="Customize UI",
                command=self.customize_ui,
                style="TButton",
            )
            self.refresh_header()

            game_frame = ttk.LabelFrame(outer, text="Sekiro install folder", padding=10)
            game_frame.pack(fill="x", pady=(0, 14))
            self.game_dir = tk.StringVar(value=load_state()["game_dir"] or "")
            self.game_entry = ttk.Entry(game_frame, textvariable=self.game_dir)
            self.game_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Button(game_frame, text="Auto-detect", command=self.detect_game_dir).pack(side="left")
            ttk.Button(game_frame, text="Browse...", command=self.browse_game_dir).pack(side="left")
            ttk.Button(game_frame, text="Save path", command=self.save_game_dir).pack(side="left", padx=(8, 0))
            ttk.Button(
                game_frame,
                text="Run Modded",
                command=self.run_modded,
                style="Accent.TButton",
            ).pack(side="left", padx=(8, 0))
            self.game_indicator = tk.StringVar()
            self.game_indicator_label = tk.Label(
                game_frame, textvariable=self.game_indicator, anchor="w",
                background="#1a1c1b", foreground="#a79e8e", font=("Segoe UI", 9),
            )
            self.game_indicator_label.pack(fill="x", pady=(8, 0))
            self.game_dir.trace_add("write", self.update_game_indicator)

            ttk.Label(outer, text="MOD COLLECTION", style="Eyebrow.TLabel").pack(
                anchor="w", pady=(2, 8),
            )
            list_frame = ttk.Frame(outer)
            list_frame.pack(fill="both", expand=True)
            self.mods = ttk.Treeview(
                list_frame,
                columns=("name", "source", "status", "files"),
                show="headings",
                selectmode="browse",
                height=6,
            )
            self.mods.heading("name", text="Mod")
            self.mods.heading("source", text="Location")
            self.mods.heading("status", text="Status")
            self.mods.heading("files", text="Files")
            self.mods.column("name", width=400, anchor="w")
            self.mods.column("source", width=160, anchor="center")
            self.mods.column("status", width=150, anchor="center")
            self.mods.column("files", width=90, anchor="center")
            self.mods.tag_configure("enabled", foreground="#c2a56d")
            self.mods.tag_configure("external", foreground="#c0b9a9")
            self.mods.tag_configure("mixed", foreground="#d29b62")
            self.mods.tag_configure("nested", foreground="#e08a63")
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.mods.yview)
            self.mods.configure(yscrollcommand=scrollbar.set)
            self.mods.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.mods.bind("<<TreeviewSelect>>", self.update_buttons)
            self.mod_items: Dict[str, Tuple[str, bool, Optional[str]]] = {}

            actions = ttk.Frame(outer)
            actions.pack(fill="x", pady=(14, 0))
            ttk.Button(actions, text="Import ZIP...", command=self.import_zip,
                       style="Accent.TButton").pack(side="left")
            ttk.Button(actions, text="Import folder...", command=self.import_folder).pack(
                side="left", padx=(8, 0),
            )
            self.rename_button = ttk.Button(
                actions, text="Rename label...", command=self.rename_installed_mod, state="disabled",
            )
            self.rename_button.pack(side="left", padx=(8, 0))
            self.enable_button = ttk.Button(actions, text="Enable", command=self.enable_mod, state="disabled")
            self.enable_button.pack(side="left", padx=(8, 0))
            self.disable_button = ttk.Button(actions, text="Disable", command=self.disable_mod, state="disabled")
            self.disable_button.pack(side="left", padx=(8, 0))
            self.delete_button = ttk.Button(actions, text="Delete", command=self.delete_mod,
                                            state="disabled", style="Danger.TButton")
            self.delete_button.pack(side="left", padx=(8, 0))
            ttk.Button(actions, text="Refresh", command=self.refresh).pack(side="right")

            self.status = tk.StringVar(value=f"LIBRARY  ·  {LIBRARY}")
            status_bar = tk.Frame(outer, background="#1a1c1b", padx=10, pady=8)
            status_bar.pack(fill="x", pady=(12, 0))
            ttk.Label(status_bar, textvariable=self.status, anchor="w",
                      background="#1a1c1b", foreground="#a79e8e",
                      font=("Segoe UI", 9)).pack(fill="x")
            self.root.bind("<Configure>", self.on_root_configure)
            if self.game_dir.get().strip() and is_game_dir(Path(self.game_dir.get()).expanduser()):
                self.update_game_indicator()
            else:
                self.detect_game_dir(show_message=False)
            self.refresh()
            self.fit_window_to_screen()

        def fit_window_to_screen(self) -> None:
            self.root.update_idletasks()
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            requested_width = max(900, self.root.winfo_reqwidth())
            requested_height = max(700, self.root.winfo_reqheight())
            width = min(requested_width, max(1, screen_width - 48))
            height = min(requested_height, max(1, screen_height - 80))
            self.root.minsize(min(760, width), min(560, height))
            x = max(0, (screen_width - width) // 2)
            y = max(0, (screen_height - height) // 2)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.update_idletasks()

        def on_root_configure(self, event: object) -> None:
            if getattr(event, "widget", None) is self.root:
                self.draw_header()

        def refresh_header(self) -> None:
            import tkinter as tk

            self.header_photo = None
            self.header_scaled_photo = None
            background = load_state()["appearance"]["background_image"]
            image_path = resolve_background_image_path(background)
            if image_path is not None:
                try:
                    self.header_photo = tk.PhotoImage(file=str(image_path))
                except tk.TclError:
                    messagebox.showwarning(
                        "Background image unavailable",
                        f"Could not load the background image:\n{image_path}",
                        parent=self.root,
                    )
            self.draw_header()
            self.apply_app_icon(show_error=False)

        def draw_header(self, _event: object = None) -> None:
            del _event
            canvas = self.header_canvas
            width = max(canvas.winfo_width(), 2)
            height = max(canvas.winfo_height(), 2)
            canvas.delete("all")
            canvas.configure(background="#171918")

            if self.header_photo is not None:
                image_width = self.header_photo.width()
                image_height = self.header_photo.height()
                scale = max(width / image_width, height / image_height)
                scale = min(8.0, max(0.125, scale))
                ratio = Fraction(scale).limit_denominator(8)
                self.header_scaled_photo = self.header_photo.zoom(ratio.numerator).subsample(
                    ratio.denominator,
                )
                canvas.create_image(width // 2, height // 2, image=self.header_scaled_photo)
                canvas.create_rectangle(
                    0, 0, width, height,
                    fill="#111211", stipple="gray50", outline="",
                )
            else:
                canvas.create_rectangle(0, 0, width, height, fill="#171918", outline="")
                canvas.create_line(width - 230, height, width, 0, fill="#242622", width=2)
                canvas.create_line(width - 190, height, width, 18, fill="#242622", width=1)

            canvas.create_rectangle(0, 0, 5, height, fill="#a5302a", outline="")
            canvas.create_text(
                24, 28, anchor="w", text="HEALING GOURD MOD MANAGER",
                fill="#bd3b32", font=("Segoe UI", 9, "bold"),
            )
            canvas.create_text(
                22, 65, anchor="w", text="Mod Arsenal",
                fill="#eee5d3", font=("Segoe UI", 23, "bold"),
            )
            canvas.create_text(
                25, 104, anchor="w", text="Manage your collection. Keep your blade sharp.",
                fill="#d2c8b7", font=("Segoe UI", 10),
            )
            canvas.create_window(
                width - 14, 16, anchor="ne", window=self.customize_button,
            )

        def customize_ui(self) -> None:
            dialog = tk.Toplevel(self.root)
            dialog.title("Customize appearance")
            dialog.configure(background="#111211")
            dialog.transient(self.root)
            dialog.resizable(False, False)

            body = ttk.Frame(dialog, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="PERSONALIZE YOUR MOD ARSENAL",
                      style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(
                body,
                text="Choose a banner image and a window icon for your manager.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(5, 14))

            ttk.Label(body, text="BACKGROUND IMAGE", style="Section.TLabel").pack(anchor="w")
            ttk.Label(
                body,
                text="A Sekiro banner is shown by default. Choose a PNG or GIF to replace it.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(3, 8))
            background_actions = ttk.Frame(body)
            background_actions.pack(fill="x", pady=(0, 16))
            ttk.Button(
                background_actions,
                text="Choose image...",
                command=lambda: self.choose_background_image(dialog),
                style="Accent.TButton",
            ).pack(side="left")
            ttk.Button(
                background_actions,
                text="Clear",
                command=lambda: self.clear_appearance_asset("background_image", dialog),
            ).pack(side="left", padx=(8, 0))

            ttk.Label(body, text="APP WINDOW ICON", style="Section.TLabel").pack(anchor="w")
            ttk.Label(
                body,
                text="Choose a Windows .ico file. To also update the EXE's file icon, rebuild the app.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(3, 8))
            icon_actions = ttk.Frame(body)
            icon_actions.pack(fill="x")
            ttk.Button(
                icon_actions,
                text="Choose icon...",
                command=lambda: self.choose_app_icon(dialog),
                style="Accent.TButton",
            ).pack(side="left")
            ttk.Button(
                icon_actions,
                text="Clear",
                command=lambda: self.clear_appearance_asset("app_icon", dialog),
            ).pack(side="left", padx=(8, 0))
            ttk.Button(body, text="Done", command=dialog.destroy).pack(anchor="e", pady=(18, 0))

            dialog.update_idletasks()
            dialog.geometry(
                f"+{self.root.winfo_rootx() + 70}+{self.root.winfo_rooty() + 70}",
            )
            dialog.grab_set()

        def choose_background_image(self, parent: tk.Toplevel) -> None:
            selected = filedialog.askopenfilename(
                title="Choose a background image",
                filetypes=[("PNG or GIF image", "*.png *.gif"), ("All files", "*.*")],
                parent=parent,
            )
            if not selected:
                return

            source = Path(selected)
            if source.suffix.casefold() not in {".png", ".gif"}:
                messagebox.showerror("Unsupported image", "Choose a PNG or GIF image.", parent=parent)
                return
            try:
                tk.PhotoImage(master=self.root, file=str(source))
                ASSETS.mkdir(parents=True, exist_ok=True)
                target = ASSETS / f"background{source.suffix.lower()}"
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                state = load_state()
                state["appearance"]["background_image"] = target.name
                save_state(state)
                self.refresh_header()
                self.status.set(f"BACKGROUND  ·  {source.name}")
            except (OSError, tk.TclError) as error:
                messagebox.showerror("Could not set background image", str(error), parent=parent)

        def choose_app_icon(self, parent: tk.Toplevel) -> None:
            selected = filedialog.askopenfilename(
                title="Choose an app icon",
                filetypes=[("Windows icon", "*.ico"), ("All files", "*.*")],
                parent=parent,
            )
            if not selected:
                return

            source = Path(selected)
            if source.suffix.casefold() != ".ico":
                messagebox.showerror("Unsupported icon", "Choose a Windows .ico file.", parent=parent)
                return

            target = ASSETS / "app-icon.ico"
            previous_icon = load_state()["appearance"]["app_icon"]
            previous_path = ASSETS / previous_icon if previous_icon else None
            try:
                self.root.iconbitmap(str(source))
                if previous_path and previous_path.is_file():
                    self.root.iconbitmap(str(previous_path))
                else:
                    self.root.iconbitmap(default="")
                ASSETS.mkdir(parents=True, exist_ok=True)
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                state = load_state()
                state["appearance"]["app_icon"] = target.name
                save_state(state)
                self.apply_app_icon()
                self.status.set(f"WINDOW ICON  ·  {source.name}")
            except (OSError, tk.TclError) as error:
                self.apply_app_icon(show_error=False)
                messagebox.showerror("Could not set app icon", str(error), parent=parent)

        def apply_app_icon(self, show_error: bool = True) -> None:
            icon_name = load_state()["appearance"]["app_icon"] or DEFAULT_ICON_NAME
            icon_path = ASSETS / icon_name
            if not icon_path.is_file() and getattr(sys, "_MEIPASS", None):
                icon_path = Path(getattr(sys, "_MEIPASS")) / "Assets" / DEFAULT_ICON_NAME
            if not icon_path.is_file():
                return
            try:
                self.root.iconbitmap(str(icon_path))
            except tk.TclError as error:
                if show_error:
                    messagebox.showerror("Could not load app icon", str(error), parent=self.root)

        def clear_appearance_asset(self, setting: str, parent: tk.Toplevel) -> None:
            state = load_state()
            asset_name = state["appearance"].get(setting)
            state["appearance"][setting] = None
            try:
                save_state(state)
                if asset_name:
                    asset_path = ASSETS / asset_name
                    if asset_path.is_file():
                        asset_path.unlink()
                if setting == "background_image":
                    for extension in (".png", ".gif"):
                        candidate = ASSETS / f"background{extension}"
                        if candidate.is_file():
                            candidate.unlink()
                    self.refresh_header()
                else:
                    self.apply_app_icon()
                self.status.set("APPEARANCE  ·  custom setting cleared")
            except (OSError, tk.TclError) as error:
                messagebox.showerror("Could not clear appearance setting", str(error), parent=parent)

        def selected_mod(self) -> Optional[str]:
            selection = self.mods.selection()
            return selection[0] if selection else None

        def update_buttons(self, event: object = None) -> None:
            del event
            name = self.selected_mod()
            if name is None:
                self.rename_button.configure(state="disabled")
                self.enable_button.configure(state="disabled")
                self.disable_button.configure(state="disabled")
                self.delete_button.configure(state="disabled")
                return
            item = self.mod_items.get(name)
            if item is None:
                self.rename_button.configure(state="disabled")
                self.enable_button.configure(state="disabled")
                self.disable_button.configure(state="disabled")
                self.delete_button.configure(state="disabled")
                return
            self.rename_button.configure(state="normal" if item[2] is not None else "disabled")
            if not item[1]:
                self.enable_button.configure(state="disabled")
                self.disable_button.configure(state="disabled")
                self.delete_button.configure(state="disabled")
                return
            self.delete_button.configure(state="normal")
            enabled = item[0] in load_state()["mods"]
            self.enable_button.configure(state="disabled" if enabled else "normal")
            self.disable_button.configure(state="normal" if enabled else "disabled")

        def refresh(self) -> None:
            try:
                state = load_state()
                previous_selection = self.selected_mod()
                LIBRARY.mkdir(parents=True, exist_ok=True)
                self.mods.delete(*self.mods.get_children())
                self.mod_items.clear()
                library_names: set[str] = set()
                for folder in sorted(path for path in LIBRARY.iterdir() if path.is_dir()):
                    count = sum(1 for path in folder.rglob("*") if path.is_file())
                    status = "Enabled" if folder.name in state["mods"] else "Disabled"
                    item_id = f"library:{folder.name}"
                    self.mod_items[item_id] = (folder.name, True, None)
                    library_names.add(folder.name.casefold())
                    self.mods.insert(
                        "", "end", iid=item_id,
                        values=(folder.name, "Library", status, count),
                        tags=("enabled",) if status == "Enabled" else (),
                    )

                game_dir = Path(self.game_dir.get().strip().strip('"')).expanduser()
                if is_game_dir(game_dir):
                    managed_files = {
                        relative.casefold()
                        for files in state["mods"].values()
                        for relative in files
                    }
                    installed = list_unmanaged_installed_mods(game_dir / "mods", managed_files)
                    visible_installed = 0
                    for name, count, status in installed:
                        if status == "Nested - inactive" and name.casefold() in library_names:
                            continue
                        visible_installed += 1
                        item_id = f"installed:{name}"
                        self.mod_items[item_id] = (name, False, name)
                        display_name = state["display_names"].get(name, name)
                        self.mods.insert(
                            "", "end", iid=item_id,
                            values=(display_name, "Game mods folder", status, count),
                            tags=(status.split(" ", 1)[0].casefold(),),
                        )
                    self.status.set(
                        f"LIBRARY  ·  {LIBRARY}  |  INSTALLED GAME MODS  ·  {visible_installed}"
                    )
                else:
                    self.status.set(
                        f"LIBRARY  ·  {LIBRARY}  |  Set a valid game folder to scan installed mods"
                    )
                if previous_selection in self.mod_items:
                    self.mods.selection_set(previous_selection)
                    self.mods.focus(previous_selection)
                self.update_buttons()
            except (OSError, ValueError) as error:
                messagebox.showerror("Could not load mods", str(error), parent=self.root)

        def browse_game_dir(self) -> None:
            selected = filedialog.askdirectory(
                title="Select the Sekiro install folder (contains sekiro.exe)",
                initialdir=self.game_dir.get() or str(Path.home()),
                parent=self.root,
            )
            if selected:
                self.game_dir.set(selected)
                self.save_game_dir()

        def update_game_indicator(self, _variable: str = "", _index: str = "", _mode: str = "") -> None:
            del _variable, _index, _mode
            entered = self.game_dir.get().strip().strip('"')
            if not entered:
                text, color = "Not set — choose or auto-detect your Sekiro install folder.", "#777777"
            else:
                path = Path(entered).expanduser()
                if is_game_dir(path):
                    text, color = "✓ Valid Sekiro folder — sekiro.exe found.", "#16803c"
                else:
                    text, color = "✗ Not a valid game folder — sekiro.exe was not found.", "#b42318"
            self.game_indicator.set(text)
            self.game_indicator_label.configure(fg=color)

        def detect_game_dir(self, show_message: bool = True) -> None:
            detected = discover_game_dir()
            if detected is None:
                self.update_game_indicator()
                if show_message:
                    messagebox.showinfo(
                        "Sekiro not found",
                        "I couldn't find Sekiro in your Steam libraries.\n"
                        "Use Browse to select the folder containing sekiro.exe.",
                        parent=self.root,
                    )
                return

            self.game_dir.set(str(detected))
            if self.save_game_dir():
                self.status.set(f"Steam detected Sekiro: {detected}")
                if show_message:
                    messagebox.showinfo("Sekiro found", f"Found Sekiro here:\n{detected}", parent=self.root)

        def save_game_dir(self) -> bool:
            entered = self.game_dir.get().strip().strip('"')
            if not entered:
                self.update_game_indicator()
                messagebox.showerror("Sekiro folder required", "Choose the folder containing sekiro.exe.",
                                     parent=self.root)
                return False
            path = Path(entered).expanduser().resolve()
            if not is_game_dir(path):
                self.update_game_indicator()
                messagebox.showerror("Invalid Sekiro folder", f"Could not find sekiro.exe in:\n{path}",
                                     parent=self.root)
                return False
            state = load_state()
            state["game_dir"] = str(path)
            save_state(state)
            self.game_dir.set(str(path))
            self.update_game_indicator()
            self.status.set(f"Sekiro folder saved: {path}")
            return True

        def ensure_game_dir(self) -> bool:
            entered = self.game_dir.get().strip().strip('"')
            if entered and is_game_dir(Path(entered).expanduser()):
                return self.save_game_dir()
            detected = discover_game_dir()
            if detected is not None:
                self.game_dir.set(str(detected))
                return self.save_game_dir()
            messagebox.showinfo("Select Sekiro folder", "Choose your Sekiro install folder to continue.",
                                parent=self.root)
            self.browse_game_dir()
            return bool(self.game_dir.get().strip() and is_game_dir(Path(self.game_dir.get()).expanduser()))

        def import_zip(self) -> None:
            source = filedialog.askopenfilename(
                title="Select a mod ZIP archive",
                filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
                parent=self.root,
            )
            if not source:
                return
            self.import_source(source)

        def import_folder(self) -> None:
            source = filedialog.askdirectory(title="Select an extracted mod folder", parent=self.root)
            if not source:
                return
            self.import_source(source)

        def import_source(self, source: str) -> None:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    name = globals()["import_mod"](source)
                self.refresh()
                self.status.set(f"Imported: {name}")
            except (OSError, ValueError, shutil.Error, zipfile.BadZipFile) as error:
                messagebox.showerror("Import failed", str(error), parent=self.root)

        def run_modded(self) -> None:
            if not self.ensure_game_dir():
                return

            game_dir = Path(self.game_dir.get().strip().strip('"')).expanduser().resolve()
            selected = self.selected_mod()
            item = self.mod_items.get(selected) if selected else None
            nested_name = item[0] if item is not None and not item[1] else None
            nested_path = game_dir / "mods" / nested_name if nested_name else None
            if nested_path is not None and is_nested_mod_folder(nested_path):
                if not messagebox.askyesno(
                    "Move nested mod into Mod Engine folder",
                    f"'{nested_name}' is stored as mods\\{nested_name}\\parts. "
                    "This Mod Engine setup loads files directly from mods\\parts, "
                    "so the nested copy will not work there.\n\n"
                    "Copy it into the manager library and deploy its files to the active "
                    "mods folder? The original nested folder will be kept as a backup.",
                    parent=self.root,
                ):
                    return
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    if nested_path is not None and is_nested_mod_folder(nested_path):
                        enabled_count = install_external_mod(game_dir, nested_name)
                    else:
                        enabled_count = launch_modded_game(game_dir)
                self.refresh()
                self.status.set(
                    f"Sekiro launched  ·  {enabled_count} manager mod(s) enabled"
                )
            except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                self.refresh()
                messagebox.showerror("Could not launch Sekiro", str(error), parent=self.root)

        def rename_installed_mod(self) -> None:
            selected = self.selected_mod()
            item = self.mod_items.get(selected) if selected else None
            if item is None or item[2] is None:
                return

            name = item[0]
            key = item[2]
            if key is None:
                return
            state = load_state()
            current_name = state["display_names"].get(key, name)
            display_name = simpledialog.askstring(
                "Rename installed mod",
                f"Display name for '{name}':",
                initialvalue=current_name,
                parent=self.root,
            )
            if display_name is None:
                return

            display_name = display_name.strip()
            if display_name:
                state["display_names"][key] = display_name
            else:
                state["display_names"].pop(key, None)
            save_state(state)
            self.refresh()
            self.status.set(f"Display name saved for {name}")

        def delete_mod(self) -> None:
            selected = self.selected_mod()
            item = self.mod_items.get(selected) if selected else None
            if item is None or not item[1]:
                return

            name = item[0]
            enabled = name in load_state()["mods"]
            message = f"Delete '{name}' from the manager library?"
            if enabled:
                message += (
                    "\n\nThis mod is enabled. The manager will remove only deployed files "
                    "that are unchanged. Modified files and external game mods will be preserved."
                )
            message += "\n\nThis cannot be undone."
            if not messagebox.askyesno("Delete mod", message, parent=self.root):
                return

            output = io.StringIO()
            errors = io.StringIO()
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    delete_library_mod(name)
                self.refresh()
                warnings = errors.getvalue().strip()
                self.status.set(f"Deleted: {name}")
                if warnings:
                    messagebox.showwarning(
                        "Mod deleted with preserved files",
                        warnings,
                        parent=self.root,
                    )
            except (OSError, ValueError, shutil.Error) as error:
                messagebox.showerror("Could not delete mod", str(error), parent=self.root)

        def enable_mod(self) -> None:
            selected = self.selected_mod()
            item = self.mod_items.get(selected) if selected else None
            if item is None or not item[1]:
                return
            name = item[0]
            if not self.ensure_game_dir():
                return
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    globals()["enable_mod"](name)
                self.refresh()
                self.status.set(f"Enabled: {name}")
            except (OSError, ValueError, shutil.Error) as error:
                messagebox.showerror("Could not enable mod", str(error), parent=self.root)

        def disable_mod(self) -> None:
            selected = self.selected_mod()
            item = self.mod_items.get(selected) if selected else None
            if item is None or not item[1]:
                return
            name = item[0]
            output = io.StringIO()
            errors = io.StringIO()
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    globals()["disable_mod"](name)
                self.refresh()
                warnings = errors.getvalue().strip()
                self.status.set(f"Disabled: {name}")
                if warnings:
                    messagebox.showwarning("Mod disabled with preserved files", warnings, parent=self.root)
            except (OSError, ValueError, shutil.Error) as error:
                messagebox.showerror("Could not disable mod", str(error), parent=self.root)

    root = tk.Tk()
    ModManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and not getattr(sys, "frozen", False):
        raise SystemExit(main())
    try:
        launch_gui()
    except Exception as error:
        if getattr(sys, "frozen", False):
            import tkinter.messagebox as messagebox
            messagebox.showerror("Healing Gourd Mod Manager failed to start", str(error))
        else:
            raise
