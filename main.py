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
from typing import Callable, Dict, List, Optional, Tuple, TypedDict

import save_manager


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
MAX_7Z_LISTING_SIZE = 64 * 1024 * 1024
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


def _validate_archive_member_path(raw_path: str, archive_format: str) -> PurePosixPath:
    normalized = raw_path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    parts = relative.parts
    if (
        not normalized
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
        raise ValueError(f"Unsafe path in {archive_format} archive: {raw_path}")
    return relative


def _install_staged_archive(
    staging: Path,
    top_level_names: set[str],
    has_root_files: bool,
    source: Path,
    library: Path,
    mod_name: Optional[str],
) -> str:
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
        raise ValueError("The archive did not produce any mod files.")
    shutil.move(str(content), str(destination))
    return name


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
            relative = _validate_archive_member_path(raw_path, "ZIP")
            parts = relative.parts

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

            return _install_staged_archive(
                staging,
                top_level_names,
                has_root_files,
                source,
                library,
                mod_name,
            )


def parse_7z_listing(output: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    in_listing = False
    for line in output.splitlines():
        if line.strip() == "----------":
            in_listing = True
            continue
        if not in_listing:
            continue
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if " = " not in line:
            raise ValueError("Could not safely parse the 7z archive listing.")
        key, value = line.split(" = ", 1)
        if key in current:
            raise ValueError("The 7z archive listing contains ambiguous member metadata.")
        current[key] = value
    if current:
        entries.append(current)
    if not entries:
        raise ValueError("The 7z archive is empty or could not be listed.")
    return entries


def extract_7z_archive(
    source: Path,
    library: Path,
    mod_name: Optional[str] = None,
) -> str:
    helper = bundled_asset_path("7za.exe")
    if not helper.is_file():
        raise ValueError(f"The bundled 7-Zip helper is missing: {helper}")

    with tempfile.TemporaryDirectory(prefix=".import-", dir=library) as temporary:
        temporary_dir = Path(temporary)
        listing_path = temporary_dir / "listing.txt"
        try:
            with listing_path.open("wb") as listing:
                result = subprocess.run(
                    [str(helper), "l", "-slt", "-sccUTF-8", str(source)],
                    stdout=listing,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Timed out while reading the 7z archive.") from error
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(error or "7-Zip could not read this archive.")
        listing_size = listing_path.stat().st_size
        if listing_size > MAX_7Z_LISTING_SIZE:
            raise ValueError("The 7z archive listing is too large to safely process.")
        try:
            output = listing_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("The 7z archive listing is not valid UTF-8.") from error
        entries = parse_7z_listing(output)
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValueError(f"The 7z archive contains more than {MAX_ARCHIVE_FILES} entries.")

        top_level_names: set[str] = set()
        seen_paths: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
        seen_prefixes: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
        expected_files: Dict[str, int] = {}
        has_root_files = False
        total_size = 0

        for entry in entries:
            raw_path = entry.get("Path")
            if raw_path is None:
                raise ValueError("The 7z archive listing is missing a member path.")
            relative = _validate_archive_member_path(raw_path, "7z")
            parts = relative.parts
            attributes = entry.get("Attributes", "")
            is_directory = attributes.startswith("D")
            if "Encrypted" in entry and entry["Encrypted"] not in {"-", "0"}:
                raise ValueError("Password-protected 7z archives are not supported.")
            if (
                "Symbolic Link" in entry.get("Type", "")
                or "symlink" in attributes.casefold()
                or "lrwx" in attributes.casefold()
            ):
                raise ValueError(f"Symbolic links are not allowed in 7z archives: {raw_path}")

            folded_parts = tuple(part.casefold() for part in parts)
            original_parts = tuple(parts)
            for depth in range(1, len(parts) + 1):
                folded_prefix = folded_parts[:depth]
                original_prefix = original_parts[:depth]
                previous_prefix = seen_prefixes.get(folded_prefix)
                if previous_prefix is not None and previous_prefix != original_prefix:
                    raise ValueError(f"Conflicting Windows paths in 7z archive: {raw_path}")
                seen_prefixes[folded_prefix] = original_prefix
            previous_parts = seen_paths.get(folded_parts)
            if previous_parts is not None and previous_parts != original_parts:
                raise ValueError(f"Duplicate Windows path in 7z archive: {raw_path}")
            if previous_parts is not None and not is_directory:
                raise ValueError(f"Duplicate file in 7z archive: {raw_path}")
            seen_paths[folded_parts] = original_parts
            top_level_names.add(parts[0])
            if not is_directory:
                try:
                    size = int(entry["Size"])
                except (KeyError, ValueError) as error:
                    raise ValueError(f"Invalid file size in 7z archive: {raw_path}") from error
                if size < 0:
                    raise ValueError(f"Invalid file size in 7z archive: {raw_path}")
                total_size += size
                if total_size > MAX_ARCHIVE_SIZE:
                    raise ValueError("The 7z archive expands to more than 2 GiB.")
                if len(parts) == 1:
                    has_root_files = True
                expected_files[relative.as_posix()] = size

        if not expected_files:
            raise ValueError("The 7z archive does not contain any files.")

        staging = temporary_dir / "payload"
        staging.mkdir()
        try:
            result = subprocess.run(
                [
                    str(helper),
                    "x",
                    "-y",
                    "-snl-",
                    "-snh-",
                    "-sccUTF-8",
                    "-bso0",
                    "-bsp0",
                    "-bse1",
                    f"-o{staging}",
                    str(source),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Timed out while extracting the 7z archive.") from error
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(error or "7-Zip could not extract this archive.")

        actual_files: Dict[str, int] = {}
        for path in staging.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symbolic links are not allowed in 7z archives: {path}")
            if path.is_file():
                relative = path.relative_to(staging).as_posix()
                try:
                    path.resolve().relative_to(staging.resolve())
                except ValueError as error:
                    raise ValueError(f"Unsafe extracted path in 7z archive: {relative}") from error
                actual_files[relative] = path.stat().st_size
        if actual_files != expected_files:
            raise ValueError("The extracted 7z contents did not match the archive listing.")

        return _install_staged_archive(
            staging,
            top_level_names,
            has_root_files,
            source,
            library,
            mod_name,
        )


def import_mod(source_folder: str, mod_name: Optional[str] = None) -> str:
    source = Path(source_folder).expanduser()
    archive_suffix = source.suffix.casefold()
    is_archive = source.is_file() and archive_suffix in {".zip", ".7z"}
    if not is_archive and not source.is_dir():
        raise ValueError(f"Source mod folder or ZIP/7z archive not found: {source}")

    LIBRARY.mkdir(parents=True, exist_ok=True)
    if is_archive:
        if archive_suffix == ".7z":
            name = extract_7z_archive(source, LIBRARY, mod_name)
        else:
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


def find_mod_conflicts(
    mod_name: str,
    state: Optional[ManagerState] = None,
) -> List[Tuple[str, List[str]]]:
    state = state or load_state()
    mod_folder = LIBRARY / mod_name
    active_paths: Dict[str, List[str]] = {}
    for active_name, files in state["mods"].items():
        if active_name == mod_name:
            continue
        for relative in files:
            active_paths.setdefault(relative.casefold(), []).append(active_name)

    conflicts: Dict[str, List[str]] = {}
    if not mod_folder.is_dir():
        return []
    for path in mod_folder.rglob("*"):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(mod_folder).as_posix()
            owners = active_paths.get(relative.casefold())
            if owners:
                conflicts[relative] = owners
    return sorted(conflicts.items(), key=lambda item: item[0].casefold())


def enable_mod(mod_name: str, allow_conflicts: bool = False) -> None:
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

    conflicts = find_mod_conflicts(mod_name, state)
    if conflicts and not allow_conflicts:
        details = "; ".join(
            f"{relative} (already provided by {', '.join(owners)})"
            for relative, owners in conflicts
        )
        raise ValueError(
            f"Mod file conflicts detected: {details}. "
            "Confirm the overwrite in the app or use --allow-conflicts."
        )

    game_dir = get_game_dir(state)
    mods_dir = game_dir / "mods"
    active_files: Dict[str, List[Tuple[str, str, str]]] = {}
    for active_name, files in state["mods"].items():
        for relative, digest in files.items():
            active_files.setdefault(relative.casefold(), []).append(
                (active_name, relative, digest),
            )

    planned: List[Tuple[Path, Path, str, bool]] = []
    seen_paths: set[str] = set()
    for source in source_files:
        relative = source.relative_to(mod_folder).as_posix()
        folded_relative = relative.casefold()
        if folded_relative in seen_paths:
            raise ValueError(f"Mod contains duplicate paths differing only by case: {relative}")
        seen_paths.add(folded_relative)
        destination = managed_path(mods_dir, relative)
        owners = active_files.get(folded_relative, [])
        if owners:
            top_owner, _, expected_hash = owners[-1]
            if destination.is_symlink() or not destination.is_file():
                raise ValueError(
                    f"Cannot safely override managed file; the active copy is missing: {destination}"
                )
            if file_hash(destination) != expected_hash:
                raise ValueError(
                    f"Cannot safely override managed file because it was modified outside "
                    f"the manager: {destination} (currently owned by {top_owner})"
                )
            planned.append((source, destination, relative, True))
        elif destination.exists() or destination.is_symlink():
            raise ValueError(f"Deployment conflict; file already exists or is managed: {destination}")
        else:
            planned.append((source, destination, relative, False))

    deployed_files: Dict[str, str] = {}
    mods_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mod-enable-", dir=mods_dir) as temporary:
        backup_dir = Path(temporary)
        backups: Dict[Path, Path] = {}
        try:
            for index, (_, destination, _, had_previous_file) in enumerate(planned):
                if had_previous_file:
                    backup = backup_dir / str(index)
                    shutil.copy2(destination, backup)
                    backups[destination] = backup

            for source, destination, relative, _ in planned:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                deployed_files[relative] = file_hash(destination)

            state["mods"][mod_name] = deployed_files
            save_state(state)
        except (OSError, shutil.Error):
            state["mods"].pop(mod_name, None)
            for _, destination, _, had_previous_file in planned:
                backup = backups.get(destination)
                if backup is not None and backup.is_file():
                    shutil.copy2(backup, destination)
                elif not had_previous_file and (destination.is_file() or destination.is_symlink()):
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
    active_files: Dict[str, List[Tuple[str, str, str]]] = {}
    for active_name, active_records in state["mods"].items():
        for relative, expected_hash in active_records.items():
            active_files.setdefault(relative.casefold(), []).append(
                (active_name, relative, expected_hash),
            )

    planned: List[Tuple[Path, Optional[Path]]] = []
    for relative, expected_hash in files.items():
        owners = active_files[relative.casefold()]
        if owners[-1][0] != mod_name:
            continue
        destination = managed_path(mods_dir, relative)
        if destination.is_symlink() or not destination.is_file():
            preserved.append(relative)
            continue
        if file_hash(destination) != expected_hash:
            preserved.append(relative)
            continue
        previous_owners = owners[:-1]
        if previous_owners:
            previous_name, previous_relative, previous_hash = previous_owners[-1]
            previous_source = managed_path(LIBRARY / previous_name, previous_relative)
            if (
                not previous_source.is_file()
                or previous_source.is_symlink()
                or file_hash(previous_source) != previous_hash
            ):
                preserved.append(relative)
                continue
            planned.append((destination, previous_source))
        else:
            planned.append((destination, None))

    original_mods = state["mods"].copy()
    backups: Dict[Path, Path] = {}
    if planned:
        with tempfile.TemporaryDirectory(prefix=".mod-disable-", dir=mods_dir) as temporary:
            backup_dir = Path(temporary)
            try:
                for index, (destination, _) in enumerate(planned):
                    backup = backup_dir / str(index)
                    shutil.copy2(destination, backup)
                    backups[destination] = backup

                for destination, source in planned:
                    if source is None:
                        destination.unlink()
                    else:
                        shutil.copy2(source, destination)

                del state["mods"][mod_name]
                save_state(state)
            except (OSError, shutil.Error):
                state["mods"].clear()
                state["mods"].update(original_mods)
                for destination, backup in backups.items():
                    if backup.is_file():
                        shutil.copy2(backup, destination)
                raise
    else:
        del state["mods"][mod_name]
        try:
            save_state(state)
        except OSError:
            state["mods"].clear()
            state["mods"].update(original_mods)
            raise

    for destination, source in planned:
        if source is not None:
            continue
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
    enable_command.add_argument(
        "--allow-conflicts",
        action="store_true",
        help="Allow the mod to override same-path files from other enabled mods",
    )

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
            enable_mod(args.name, args.allow_conflicts)
        elif args.command == "disable":
            disable_mod(args.name)
        elif args.command == "delete":
            if not args.yes:
                raise ValueError("Deletion requires confirmation; rerun with --yes.")
            delete_library_mod(args.name)
        return 0
    except (
        OSError,
        ValueError,
        shutil.Error,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
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
            style.configure(
                "Navigation.TButton",
                background="#202220",
                foreground="#d8d0c0",
                font=("Segoe UI", 10, "bold"),
                padding=(12, 12),
                anchor="w",
                justify="left",
                borderwidth=0,
            )
            style.map(
                "Navigation.TButton",
                background=[("active", "#30322f")],
                foreground=[("active", "#fff1dc")],
            )
            style.configure(
                "Navigation.Active.TButton",
                background="#4b2723",
                foreground="#fff1dc",
                font=("Segoe UI", 10, "bold"),
                padding=(12, 12),
                anchor="w",
                justify="left",
                borderwidth=0,
            )
            style.map(
                "Navigation.Active.TButton",
                background=[("active", "#5a302a")],
            )
            style.configure("Danger.TButton", background="#382321", foreground="#e8a39a")
            style.map("Danger.TButton", background=[("active", "#60302b"), ("disabled", "#25211f")])

            outer = ttk.Frame(root, padding=18)
            outer.pack(fill="both", expand=True)
            workspace = ttk.Frame(outer)
            workspace.pack(fill="both", expand=True)
            self.sidebar = ttk.Frame(workspace, style="Panel.TFrame", padding=(12, 14))
            self.sidebar.pack(side="left", fill="y", padx=(0, 16))
            self.sidebar.configure(width=204)
            self.sidebar.pack_propagate(False)
            ttk.Label(
                self.sidebar,
                text="HEALING GOURD",
                style="Eyebrow.TLabel",
                background="#1a1c1b",
            ).pack(anchor="w", padx=8)
            ttk.Label(
                self.sidebar,
                text="Mod Manager",
                font=("Segoe UI", 15, "bold"),
                background="#1a1c1b",
                foreground="#eee5d3",
            ).pack(anchor="w", padx=8, pady=(2, 18))
            ttk.Separator(self.sidebar).pack(fill="x", pady=(0, 14))
            ttk.Label(
                self.sidebar,
                text="WORKSPACE",
                style="Eyebrow.TLabel",
                background="#1a1c1b",
            ).pack(anchor="w", padx=8, pady=(0, 8))
            self.mod_navigation_button = ttk.Button(
                self.sidebar,
                text="Mod Manager\nMods, conflicts & library",
                command=lambda: self.show_workspace_page("mods"),
                style="Navigation.Active.TButton",
            )
            self.mod_navigation_button.pack(fill="x", pady=(0, 6))
            self.save_navigation_button = ttk.Button(
                self.sidebar,
                text="Save Manager\nBackups, profiles & saves",
                command=lambda: self.show_workspace_page("saves"),
                style="Navigation.TButton",
            )
            self.save_navigation_button.pack(fill="x")
            self.sidebar_navigation = {
                "mods": self.mod_navigation_button,
                "saves": self.save_navigation_button,
            }

            sidebar_actions = ttk.Frame(self.sidebar, style="Panel.TFrame")
            sidebar_actions.pack(side="bottom", fill="x", pady=(14, 0))
            ttk.Separator(sidebar_actions).pack(fill="x", pady=(0, 12))
            ttk.Button(
                sidebar_actions,
                text="Run Modded",
                command=self.run_modded,
                style="Accent.TButton",
            ).pack(fill="x", pady=(0, 6))
            ttk.Button(
                sidebar_actions,
                text="Customize UI",
                command=self.customize_ui,
                style="TButton",
            ).pack(fill="x")

            self.workspace_content = ttk.Frame(workspace)
            self.workspace_content.pack(side="left", fill="both", expand=True)
            self.header_canvas = tk.Canvas(
                self.workspace_content,
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
            self.refresh_header()

            self.workspace_pages = ttk.Frame(self.workspace_content)
            self.workspace_pages.pack(fill="both", expand=True)
            self.mod_manager_tab = ttk.Frame(self.workspace_pages, padding=(0, 0, 0, 4))
            self.save_manager_tab = ttk.Frame(self.workspace_pages, padding=10)
            self.mod_manager_tab.pack(fill="both", expand=True)
            self.active_workspace_page = "mods"
            self.save_manager_built = False
            self.refresh_save_manager: Optional[Callable[[], None]] = None

            game_frame = ttk.LabelFrame(self.mod_manager_tab, text="Sekiro install folder", padding=10)
            game_frame.pack(fill="x", pady=(0, 14))
            self.game_dir = tk.StringVar(value=load_state()["game_dir"] or "")
            self.game_entry = ttk.Entry(game_frame, textvariable=self.game_dir)
            self.game_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Button(game_frame, text="Auto-detect", command=self.detect_game_dir).pack(side="left")
            ttk.Button(game_frame, text="Browse...", command=self.browse_game_dir).pack(side="left")
            ttk.Button(game_frame, text="Save path", command=self.save_game_dir).pack(side="left", padx=(8, 0))
            self.game_indicator = tk.StringVar()
            self.game_indicator_label = tk.Label(
                game_frame, textvariable=self.game_indicator, anchor="w",
                background="#1a1c1b", foreground="#a79e8e", font=("Segoe UI", 9),
            )
            self.game_indicator_label.pack(fill="x", pady=(8, 0))
            self.game_dir.trace_add("write", self.update_game_indicator)

            ttk.Label(self.mod_manager_tab, text="MOD COLLECTION", style="Eyebrow.TLabel").pack(
                anchor="w", pady=(2, 8),
            )
            list_frame = ttk.Frame(self.mod_manager_tab)
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
            self.mods.tag_configure("conflict", foreground="#e08a63")
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.mods.yview)
            self.mods.configure(yscrollcommand=scrollbar.set)
            self.mods.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.mods.bind("<<TreeviewSelect>>", self.update_buttons)
            self.mod_items: Dict[str, Tuple[str, bool, Optional[str]]] = {}

            actions = ttk.Frame(self.mod_manager_tab)
            actions.pack(fill="x", pady=(14, 0))
            ttk.Button(actions, text="Import ZIP/7z...", command=self.import_zip,
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
            status_bar = tk.Frame(self.mod_manager_tab, background="#1a1c1b", padx=10, pady=8)
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

        def show_workspace_page(self, page: str) -> None:
            if page not in {"mods", "saves"}:
                raise ValueError(f"Unknown workspace page: {page}")
            self.active_workspace_page = page
            for child in self.workspace_pages.winfo_children():
                child.pack_forget()
            selected_page = self.mod_manager_tab if page == "mods" else self.save_manager_tab
            selected_page.pack(fill="both", expand=True)
            for name, button in self.sidebar_navigation.items():
                button.configure(
                    style="Navigation.Active.TButton" if name == page else "Navigation.TButton"
                )

            if page == "saves":
                if self.header_canvas.winfo_manager():
                    self.header_canvas.pack_forget()
                if not self.save_manager_built:
                    self.build_save_manager_tab()
                elif self.refresh_save_manager is not None:
                    self.refresh_save_manager()
            elif not self.header_canvas.winfo_manager():
                self.header_canvas.pack(fill="x", before=self.workspace_pages, pady=(0, 16))

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
                    conflicts = find_mod_conflicts(folder.name, state)
                    enabled = folder.name in state["mods"]
                    if conflicts:
                        status = (
                            f"Enabled · {len(conflicts)} overlap(s)"
                            if enabled
                            else f"Conflict · {len(conflicts)} file(s)"
                        )
                        tags = ("conflict",)
                    else:
                        status = "Enabled" if enabled else "Disabled"
                        tags = ("enabled",) if enabled else ()
                    item_id = f"library:{folder.name}"
                    self.mod_items[item_id] = (folder.name, True, None)
                    library_names.add(folder.name.casefold())
                    self.mods.insert(
                        "", "end", iid=item_id,
                        values=(folder.name, "Library", status, count),
                        tags=tags,
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
                title="Select a mod ZIP or 7z archive",
                filetypes=[
                    ("Mod archives", "*.zip *.7z"),
                    ("ZIP archives", "*.zip"),
                    ("7-Zip archives", "*.7z"),
                    ("All files", "*.*"),
                ],
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
            except (
                OSError,
                ValueError,
                shutil.Error,
                zipfile.BadZipFile,
                subprocess.SubprocessError,
            ) as error:
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
                save_settings = save_manager.load_settings(DATA_DIR)
                save_dir_value = save_settings["save_dir"]
                if not save_dir_value:
                    discovered = save_manager.discover_save_folders()
                    if len(discovered) == 1:
                        save_dir = discovered[0]
                        save_settings["save_dir"] = str(save_dir)
                        save_manager.save_settings(DATA_DIR, save_settings)
                    else:
                        raise ValueError(
                            "Set your Sekiro save folder in Save Manager before using Run Modded."
                        )
                else:
                    save_dir = Path(save_dir_value).expanduser()
                save_dir = save_manager.validate_save_dir(save_dir)
                if save_manager.is_game_running():
                    raise ValueError("Close Sekiro before creating an automatic save backup.")
                with contextlib.redirect_stdout(io.StringIO()):
                    backup_path = save_manager.create_backup(
                        save_dir,
                        DATA_DIR,
                        keep_backups=save_settings["keep_backups"],
                        label="Automatic backup before Run Modded",
                        profile=save_settings["active_profile"],
                    )
                    if nested_path is not None and is_nested_mod_folder(nested_path):
                        enabled_count = install_external_mod(game_dir, nested_name)
                    else:
                        enabled_count = launch_modded_game(game_dir)
                self.refresh()
                self.status.set(
                    f"Backup: {backup_path.name}  ·  Sekiro launched  ·  "
                    f"{enabled_count} manager mod(s) enabled"
                )
            except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                self.refresh()
                messagebox.showerror("Could not launch Sekiro", str(error), parent=self.root)

        def open_save_manager(self) -> None:
            self.show_workspace_page("saves")

        def build_save_manager_tab(self) -> None:
            try:
                settings = save_manager.load_settings(DATA_DIR)
            except (OSError, ValueError) as error:
                messagebox.showerror("Could not load save settings", str(error), parent=self.root)
                return
            dialog = self.save_manager_tab
            body = ttk.Frame(dialog, padding=8)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="SAVE MANAGER", style="Eyebrow.TLabel").pack(anchor="w")
            ttk.Label(
                body,
                text="Back up, restore, or switch between separate Sekiro saves.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(4, 12))
            save_dir_var = tk.StringVar(value=settings["save_dir"] or "")
            retention_var = tk.StringVar(value=str(settings["keep_backups"]))
            save_location_var = tk.StringVar()
            current_save_details_var = tk.StringVar(value="Current save details: not loaded")

            def update_save_location(*_args: object) -> None:
                del _args
                location = save_dir_var.get().strip()
                save_location_var.set(
                    f"Save folder: {location}" if location
                    else "Save folder not set. Choose Settings to find it."
                )
                current_save_details_var.set(
                    "Save details will refresh after you save the selected folder."
                )

            save_dir_var.trace_add("write", update_save_location)
            update_save_location()

            notebook = ttk.Notebook(body)
            notebook.pack(fill="both", expand=True)
            backups_tab = ttk.Frame(notebook, padding=14)
            profiles_tab = ttk.Frame(notebook, padding=14)
            settings_tab = ttk.Frame(notebook, padding=14)
            notebook.add(backups_tab, text="Backups")
            notebook.add(profiles_tab, text="Save profiles")
            notebook.add(settings_tab, text="Settings")

            ttk.Label(backups_tab, textvariable=save_location_var, style="Muted.TLabel").pack(
                anchor="w", pady=(0, 8),
            )
            ttk.Label(
                backups_tab,
                textvariable=current_save_details_var,
                style="Muted.TLabel",
                wraplength=680,
                justify="left",
            ).pack(anchor="w", pady=(0, 8))
            ttk.Label(
                backups_tab,
                text="A backup copies the entire save folder. Run Modded also creates one automatically.",
                style="Muted.TLabel",
                wraplength=680,
            ).pack(anchor="w", pady=(0, 12))
            label_row = ttk.Frame(backups_tab)
            label_row.pack(fill="x", pady=(0, 10))
            ttk.Label(label_row, text="Backup note (optional):").pack(side="left")
            label_var = tk.StringVar()
            ttk.Entry(label_row, textvariable=label_var).pack(
                side="left", fill="x", expand=True, padx=(8, 0),
            )

            backup_list_frame = ttk.Frame(backups_tab)
            backup_list_frame.pack(fill="both", expand=True)
            backup_list = ttk.Treeview(
                backup_list_frame,
                columns=("date", "label", "profile"),
                show="headings",
                selectmode="browse",
                height=10,
            )
            backup_list.heading("date", text="Date")
            backup_list.heading("label", text="Note")
            backup_list.heading("profile", text="Profile")
            backup_list.column("date", width=190, anchor="w")
            backup_list.column("label", width=280, anchor="w")
            backup_list.column("profile", width=150, anchor="w")
            backup_scrollbar = ttk.Scrollbar(
                backup_list_frame,
                orient="vertical",
                command=backup_list.yview,
            )
            backup_list.configure(yscrollcommand=backup_scrollbar.set)
            backup_list.pack(side="left", fill="both", expand=True)
            backup_scrollbar.pack(side="right", fill="y")
            backup_paths: Dict[str, Path] = {}
            backup_details_var = tk.StringVar(value="Select a backup to inspect its Sekiro save.")
            ttk.Label(
                backups_tab,
                textvariable=backup_details_var,
                style="Muted.TLabel",
                wraplength=680,
                justify="left",
            ).pack(anchor="w", pady=(8, 0))
            backup_count_var = tk.StringVar()
            ttk.Label(backups_tab, textvariable=backup_count_var, style="Muted.TLabel").pack(
                anchor="w", pady=(8, 0),
            )

            def browse_save_dir() -> None:
                selected = filedialog.askdirectory(
                    title="Select the folder containing Sekiro's save files",
                    initialdir=save_dir_var.get() or str(save_manager.default_appdata() / "Sekiro"),
                    parent=dialog,
                )
                if selected:
                    save_dir_var.set(selected)

            def detect_save_dir() -> None:
                candidates = save_manager.discover_save_folders()
                if len(candidates) == 1:
                    save_dir_var.set(str(candidates[0]))
                elif candidates:
                    messagebox.showinfo(
                        "Multiple save folders found",
                        "Choose the correct Steam account save folder:\n\n"
                        + "\n".join(str(path) for path in candidates),
                        parent=dialog,
                    )
                else:
                    messagebox.showinfo(
                        "Save folder not found",
                        "No %APPDATA%\\Sekiro\\<numeric ID>\\S0000.sl2 folder was found. "
                        "Use Browse to select the save folder yourself.",
                        parent=dialog,
                    )

            def get_save_settings() -> save_manager.SaveSettings:
                entered = save_dir_var.get().strip().strip('"')
                save_dir = Path(entered).expanduser().resolve() if entered else None
                if save_dir is None:
                    raise ValueError("Choose your Sekiro save folder in Settings first.")
                save_dir = save_manager.validate_save_dir(save_dir)
                try:
                    keep_backups = int(retention_var.get())
                except ValueError as error:
                    raise ValueError("Keep-backup count must be a whole number from 1 to 100.") from error
                if not save_manager.BACKUP_RETENTION_MIN <= keep_backups <= save_manager.BACKUP_RETENTION_MAX:
                    raise ValueError("Keep-backup count must be between 1 and 100.")
                current = save_manager.load_settings(DATA_DIR)
                current["save_dir"] = str(save_dir) if save_dir is not None else None
                current["keep_backups"] = keep_backups
                save_manager.save_settings(DATA_DIR, current)
                return current

            def save_folder_and_settings() -> Optional[save_manager.SaveSettings]:
                try:
                    result = get_save_settings()
                    self.status.set(f"Save folder saved: {result['save_dir']}")
                    update_current_save_details()
                    return result
                except (OSError, ValueError) as error:
                    messagebox.showerror("Could not save settings", str(error), parent=dialog)
                    return None

            def update_current_save_details() -> None:
                entered = save_dir_var.get().strip().strip('"')
                if not entered:
                    current_save_details_var.set(
                        "Current save details: choose a save folder in Settings."
                    )
                    return
                save_file = Path(entered).expanduser() / "S0000.sl2"
                try:
                    details = save_manager.format_sekiro_save_summary(save_file)
                except (OSError, ValueError) as error:
                    details = f"Save details unavailable: {error}"
                current_save_details_var.set(f"Current save:\n{details}")

            def update_backup_details(_event: object = None) -> None:
                del _event
                selection = backup_list.selection()
                backup_path = backup_paths.get(selection[0]) if selection else None
                if backup_path is None:
                    backup_details_var.set("Select a backup to inspect its Sekiro save.")
                    return
                try:
                    details = save_manager.format_sekiro_save_summary(
                        backup_path / "save" / "S0000.sl2"
                    )
                except (OSError, ValueError) as error:
                    details = f"Save details unavailable: {error}"
                backup_details_var.set(f"Selected backup:\n{details}")

            backup_list.bind("<<TreeviewSelect>>", update_backup_details)

            def refresh_backups() -> None:
                try:
                    backup_paths.clear()
                    for item in backup_list.get_children():
                        backup_list.delete(item)
                    backups = save_manager.list_backups(DATA_DIR)
                    for backup in backups:
                        backup_path = Path(str(backup["path"]))
                        created_at = str(backup["created_at"]).replace("T", " ").split(".")[0]
                        backup_paths[backup_path.name] = backup_path
                        backup_list.insert(
                            "",
                            "end",
                            iid=backup_path.name,
                            values=(
                                created_at,
                                backup["label"] or "—",
                                backup["profile"] or "—",
                            ),
                        )
                    backup_count_var.set(
                        f"{len(backups)} backup(s) · keeps newest {retention_var.get()}"
                    )
                    children = backup_list.get_children()
                    if children:
                        backup_list.selection_set(children[0])
                    update_backup_details()
                except (OSError, ValueError) as error:
                    messagebox.showerror("Could not read backups", str(error), parent=dialog)

            update_current_save_details()

            def backup_now() -> None:
                try:
                    current = save_folder_and_settings()
                    if current is None or not current["save_dir"]:
                        return
                    if save_manager.is_game_running():
                        raise ValueError("Close Sekiro before creating a save backup.")
                    backup = save_manager.create_backup(
                        Path(current["save_dir"]),
                        DATA_DIR,
                        keep_backups=current["keep_backups"],
                        label=label_var.get(),
                        profile=current["active_profile"],
                    )
                    label_var.set("")
                    refresh_backups()
                    self.status.set(f"Save backup created: {backup.name}")
                except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                    messagebox.showerror("Backup failed", str(error), parent=dialog)

            backup_actions = ttk.Frame(backups_tab)
            backup_actions.pack(fill="x", pady=(12, 0))
            ttk.Button(
                backup_actions,
                text="Backup now",
                command=backup_now,
                style="Accent.TButton",
            ).pack(side="left")
            ttk.Button(
                backup_actions,
                text="Refresh",
                command=refresh_backups,
            ).pack(side="left", padx=(8, 0))

            def restore_selected() -> None:
                selection = backup_list.selection()
                backup = backup_paths.get(selection[0]) if selection else None
                if backup is None:
                    messagebox.showinfo("Select a backup", "Choose a backup to restore.", parent=dialog)
                    return
                current = save_folder_and_settings()
                if current is None or not current["save_dir"]:
                    return
                backup_values = backup_list.item(selection[0], "values")
                description = " · ".join(str(value) for value in backup_values if value != "—")
                if not messagebox.askyesno(
                    "Restore save backup",
                    f"Restore this backup?\n\n{description}\n\n"
                    "The entire current save folder will be replaced. The current save "
                    "will first be copied into a new backup so you can undo this restore.\n\n"
                    + save_manager.game_cloud_warning()
                    + "\n\nContinue?",
                    parent=dialog,
                ):
                    return
                try:
                    save_manager.restore_backup(
                        backup,
                        Path(current["save_dir"]),
                        DATA_DIR,
                    )
                    refresh_backups()
                    update_current_save_details()
                    self.status.set(f"Save restored from: {backup.name}")
                    messagebox.showinfo("Save restored", "The save folder was restored and verified.", parent=dialog)
                except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                    messagebox.showerror("Restore failed", str(error), parent=dialog)

            ttk.Button(
                backup_actions,
                text="Restore selected...",
                command=restore_selected,
            ).pack(side="right")
            refresh_backups()

            ttk.Label(
                profiles_tab,
                text="Each profile stores a separate copy of your complete save folder. Switching saves "
                "the current profile first, then loads the selected profile.",
                style="Muted.TLabel",
                wraplength=680,
            ).pack(anchor="w", pady=(0, 14))
            active_profile_var = tk.StringVar()
            ttk.Label(
                profiles_tab,
                textvariable=active_profile_var,
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(0, 8))
            profile_row = ttk.Frame(profiles_tab)
            profile_row.pack(fill="x", pady=(0, 12))
            profile_list = ttk.Combobox(profile_row, state="readonly")
            profile_list.pack(side="left", fill="x", expand=True, padx=(0, 8))
            profile_details_var = tk.StringVar(
                value="Select a profile to inspect its Sekiro save."
            )
            ttk.Label(
                profiles_tab,
                textvariable=profile_details_var,
                style="Muted.TLabel",
                wraplength=680,
                justify="left",
            ).pack(anchor="w", pady=(0, 12))

            def update_profile_details(_event: object = None) -> None:
                del _event
                name = profile_list.get()
                if not name:
                    profile_details_var.set("Select a profile to inspect its Sekiro save.")
                    return
                save_file = save_manager.profile_root(DATA_DIR) / name / "save" / "S0000.sl2"
                try:
                    details = save_manager.format_sekiro_save_summary(save_file)
                except (OSError, ValueError) as error:
                    details = f"Save details unavailable: {error}"
                profile_details_var.set(f"Profile '{name}':\n{details}")

            profile_list.bind("<<ComboboxSelected>>", update_profile_details)

            def refresh_profiles() -> None:
                profiles = save_manager.list_profiles(DATA_DIR)
                profile_list.configure(values=profiles)
                profile_settings = save_manager.load_settings(DATA_DIR)
                active = profile_settings["active_profile"]
                profile_list.set(active if active in profiles else (profiles[0] if profiles else ""))
                active_profile_var.set(
                    f"Active save: {active}" if active
                    else "Active save: No profile (your regular Sekiro save)"
                )
                update_profile_details()

            def create_profile() -> None:
                current = save_folder_and_settings()
                if current is None or not current["save_dir"]:
                    return
                name = simpledialog.askstring(
                    "Create save profile",
                    "Name this profile (for example, Randomizer):",
                    parent=dialog,
                )
                if name is None:
                    return
                try:
                    profile = save_manager.create_profile(
                        Path(current["save_dir"]),
                        DATA_DIR,
                        name,
                    )
                    refresh_profiles()
                    self.status.set(f"Save profile created: {profile}")
                    messagebox.showinfo(
                        "Profile created",
                        f"'{profile}' is an independent copy of the current save. "
                        "The current game save was not changed.",
                        parent=dialog,
                    )
                except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                    messagebox.showerror("Could not create profile", str(error), parent=dialog)

            def switch_profile() -> None:
                name = profile_list.get()
                if not name:
                    messagebox.showinfo("Select a profile", "Create or select a save profile.", parent=dialog)
                    return
                current = save_folder_and_settings()
                if current is None or not current["save_dir"]:
                    return
                active = current["active_profile"] or "unprofiled save"
                if not messagebox.askyesno(
                    "Switch save profile",
                    f"Switch from '{active}' to '{name}'?\n\n"
                    "The current save will be backed up and saved to its active profile "
                    "before the selected profile is copied into the game save folder. "
                    "Close Sekiro before switching.",
                    parent=dialog,
                ):
                    return
                try:
                    profile = save_manager.switch_profile(
                        Path(current["save_dir"]),
                        DATA_DIR,
                        name,
                    )
                    refresh_profiles()
                    update_current_save_details()
                    refresh_backups()
                    self.status.set(f"Active save profile: {profile}")
                except (OSError, ValueError, shutil.Error, subprocess.SubprocessError) as error:
                    messagebox.showerror("Could not switch profile", str(error), parent=dialog)

            ttk.Button(
                profiles_tab,
                text="Create profile from current save...",
                command=create_profile,
                style="Accent.TButton",
            ).pack(anchor="w", pady=(0, 8))
            ttk.Button(
                profiles_tab,
                text="Switch to selected profile...",
                command=switch_profile,
            ).pack(anchor="w")
            ttk.Label(
                profiles_tab,
                text="Close Sekiro before creating or switching profiles.",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(16, 0))
            refresh_profiles()

            ttk.Label(
                settings_tab,
                text="Select the folder that contains S0000.sl2. It is usually "
                "%APPDATA%\\Sekiro\\<Steam ID>.",
                style="Muted.TLabel",
                wraplength=680,
            ).pack(anchor="w", pady=(0, 10))
            save_dir_row = ttk.Frame(settings_tab)
            save_dir_row.pack(fill="x")
            ttk.Entry(save_dir_row, textvariable=save_dir_var).pack(
                side="left", fill="x", expand=True, padx=(0, 8),
            )
            ttk.Button(save_dir_row, text="Detect", command=detect_save_dir).pack(side="left")
            ttk.Button(save_dir_row, text="Browse...", command=browse_save_dir).pack(
                side="left", padx=(6, 0),
            )

            ttk.Label(
                settings_tab,
                text="Keep newest backups:",
            ).pack(anchor="w", pady=(18, 6))
            retention_row = ttk.Frame(settings_tab)
            retention_row.pack(anchor="w")
            ttk.Spinbox(
                retention_row,
                from_=save_manager.BACKUP_RETENTION_MIN,
                to=save_manager.BACKUP_RETENTION_MAX,
                textvariable=retention_var,
                width=6,
            ).pack(side="left")
            ttk.Label(retention_row, text="(1–100)").pack(side="left", padx=(8, 0))
            ttk.Button(
                settings_tab,
                text="Save settings",
                command=save_folder_and_settings,
                style="Accent.TButton",
            ).pack(anchor="w", pady=(14, 20))
            ttk.Label(
                settings_tab,
                text=save_manager.game_cloud_warning(),
                style="Muted.TLabel",
                wraplength=680,
            ).pack(anchor="w")
            def refresh_save_manager_page() -> None:
                update_current_save_details()
                refresh_backups()
                refresh_profiles()

            self.refresh_save_manager = refresh_save_manager_page
            self.save_manager_built = True

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
            conflicts = find_mod_conflicts(name)
            allow_conflicts = False
            if conflicts:
                details = "\n".join(
                    f"• {relative}  (currently provided by {', '.join(owners)})"
                    for relative, owners in conflicts[:20]
                )
                if len(conflicts) > 20:
                    details += f"\n…and {len(conflicts) - 20} more conflicting file(s)."
                allow_conflicts = messagebox.askyesno(
                    "Same-file mod conflicts",
                    f"{name} shares {len(conflicts)} file(s) with enabled mods:\n\n"
                    f"{details}\n\n"
                    "If you continue, this mod's version will take priority for those "
                    "paths. Other files from both mods remain enabled; disabling this "
                    "mod restores the previous enabled version. This replaces files, "
                    "it does not merge them, and overhaul mods may still be incompatible.\n\n"
                    "Enable anyway?",
                    parent=self.root,
                )
                if not allow_conflicts:
                    return
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    globals()["enable_mod"](name, allow_conflicts=allow_conflicts)
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
