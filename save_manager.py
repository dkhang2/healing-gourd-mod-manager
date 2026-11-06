import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TypedDict


BACKUP_RETENTION_MIN = 1
BACKUP_RETENTION_MAX = 100
SEKIRO_SLOT_COUNT = 10
SEKIRO_BND4_HEADER_SIZE = 64
SEKIRO_BND4_ENTRY_SIZE = 32
SEKIRO_MAX_SAVE_FILE_SIZE = 128 * 1024 * 1024
SEKIRO_SLOT_MIN_SIZE = 0x34563
SEKIRO_STEAM_ID_OFFSET = 0x33E54
SEKIRO_JOURNEY_OFFSET = 0x33F34
SEKIRO_PLAYTIME_OFFSET = 0x33F80
SEKIRO_VITALITY_OFFSET = 0x34498
SEKIRO_ATTACK_POWER_OFFSET = 0x3449C


class SaveSettings(TypedDict):
    version: int
    save_dir: Optional[str]
    keep_backups: int
    active_profile: Optional[str]


class SekiroSaveSlot(TypedDict):
    slot: int
    journey: int
    playtime_seconds: int
    vitality: int
    attack_power: int


def settings_path(data_dir: Path) -> Path:
    return data_dir / "save-manager.json"


def backup_root(data_dir: Path) -> Path:
    return data_dir / "SaveBackups"


def profile_root(data_dir: Path) -> Path:
    return data_dir / "SaveProfiles"


def default_appdata() -> Path:
    configured = os.environ.get("APPDATA")
    return Path(configured) if configured else Path.home() / "AppData" / "Roaming"


def discover_save_folders(appdata: Optional[Path] = None) -> List[Path]:
    sekiro_root = (appdata or default_appdata()) / "Sekiro"
    if not sekiro_root.is_dir() or sekiro_root.is_symlink():
        return []
    return sorted(
        (
            folder.resolve()
            for folder in sekiro_root.iterdir()
            if folder.is_dir()
            and not folder.is_symlink()
            and folder.name.isdecimal()
            and (folder / "S0000.sl2").is_file()
        ),
        key=lambda path: path.name,
    )


def validate_save_dir(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Save folder does not exist or is a symbolic link: {path}")
    files = _file_manifest(path)
    if not files:
        raise ValueError(f"Save folder contains no files: {path}")
    return path.resolve()


def read_sekiro_slots(save_file: Path) -> List[SekiroSaveSlot]:
    if save_file.is_symlink() or not save_file.is_file():
        raise ValueError(f"Sekiro save file is unavailable: {save_file}")
    file_size = save_file.stat().st_size
    if not SEKIRO_BND4_HEADER_SIZE <= file_size <= SEKIRO_MAX_SAVE_FILE_SIZE:
        raise ValueError(f"Unexpected Sekiro save file size: {file_size} bytes.")
    data = save_file.read_bytes()
    if data[:4] != b"BND4":
        raise ValueError("This is not a supported Sekiro BND4 save file.")

    entry_count = int.from_bytes(data[12:16], "little")
    table_end = SEKIRO_BND4_HEADER_SIZE + SEKIRO_BND4_ENTRY_SIZE * entry_count
    if not SEKIRO_SLOT_COUNT <= entry_count <= 64 or table_end > len(data):
        raise ValueError("The Sekiro save has an invalid BND4 entry table.")

    slots: List[SekiroSaveSlot] = []
    entry_ranges: List[tuple[int, int]] = []
    for slot_index in range(SEKIRO_SLOT_COUNT):
        entry_offset = SEKIRO_BND4_HEADER_SIZE + SEKIRO_BND4_ENTRY_SIZE * slot_index
        entry_size = int.from_bytes(data[entry_offset + 8 : entry_offset + 16], "little")
        payload_offset = int.from_bytes(data[entry_offset + 16 : entry_offset + 20], "little")
        entry_end = payload_offset + entry_size
        if (
            entry_size < 16 + SEKIRO_SLOT_MIN_SIZE
            or payload_offset < table_end
            or entry_end > len(data)
        ):
            raise ValueError(f"Sekiro save slot {slot_index + 1} has invalid bounds.")
        if any(payload_offset < previous_end and entry_end > previous_start
               for previous_start, previous_end in entry_ranges):
            raise ValueError("Sekiro save slot data overlaps in the BND4 container.")
        entry_ranges.append((payload_offset, entry_end))

        entry = data[payload_offset:entry_end]
        if hashlib.md5(entry[16:], usedforsecurity=False).digest() != entry[:16]:
            raise ValueError(f"Sekiro save slot {slot_index + 1} failed its checksum.")
        payload = entry[16:]
        steam_id = int.from_bytes(
            payload[SEKIRO_STEAM_ID_OFFSET : SEKIRO_STEAM_ID_OFFSET + 8],
            "little",
        )
        if steam_id == 0:
            continue

        journey = payload[SEKIRO_JOURNEY_OFFSET]
        playtime_seconds = int.from_bytes(
            payload[SEKIRO_PLAYTIME_OFFSET : SEKIRO_PLAYTIME_OFFSET + 4],
            "little",
        )
        vitality = int.from_bytes(
            payload[SEKIRO_VITALITY_OFFSET : SEKIRO_VITALITY_OFFSET + 4],
            "little",
        )
        attack_power = int.from_bytes(
            payload[SEKIRO_ATTACK_POWER_OFFSET : SEKIRO_ATTACK_POWER_OFFSET + 4],
            "little",
        )
        if not 0 <= journey <= 99 or not 1 <= vitality <= 20 or not 1 <= attack_power <= 99:
            raise ValueError(f"Sekiro save slot {slot_index + 1} has implausible character stats.")
        slots.append({
            "slot": slot_index + 1,
            "journey": journey,
            "playtime_seconds": playtime_seconds,
            "vitality": vitality,
            "attack_power": attack_power,
        })

    if not slots:
        raise ValueError("No populated Sekiro save slots were found in this file.")
    return slots


def format_sekiro_save_summary(save_file: Path) -> str:
    summaries = []
    for slot in read_sekiro_slots(save_file):
        journey = slot["journey"]
        journey_label = (
            f"Journey {journey + 1} (NG+{journey})" if journey else "Journey 1 (NG)"
        )
        hours, remainder = divmod(slot["playtime_seconds"], 3600)
        minutes = remainder // 60
        summaries.append(
            f"Slot {slot['slot']}: {journey_label} · {hours}h {minutes:02}m · "
            f"Vitality {slot['vitality']} · Attack Power {slot['attack_power']}"
        )
    return "\n".join(summaries)


def _validate_storage_separation(save_dir: Path, data_dir: Path) -> None:
    save_dir = save_dir.resolve()
    data_dir = data_dir.resolve()
    try:
        save_dir.relative_to(data_dir)
    except ValueError:
        pass
    else:
        raise ValueError("The save folder cannot be inside the manager's data folder.")
    try:
        data_dir.relative_to(save_dir)
    except ValueError:
        return
    raise ValueError("The manager's data folder cannot be inside the Sekiro save folder.")


def load_settings(data_dir: Path) -> SaveSettings:
    path = settings_path(data_dir)
    if not path.exists():
        return {
            "version": 1,
            "save_dir": None,
            "keep_backups": 10,
            "active_profile": None,
        }
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid save manager settings: {path}") from error
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError(f"Unsupported save manager settings: {path}")
    save_dir = raw.get("save_dir")
    keep_backups = raw.get("keep_backups", 10)
    active_profile = raw.get("active_profile")
    if (
        (save_dir is not None and not isinstance(save_dir, str))
        or type(keep_backups) is not int
        or not BACKUP_RETENTION_MIN <= keep_backups <= BACKUP_RETENTION_MAX
        or (active_profile is not None and not isinstance(active_profile, str))
    ):
        raise ValueError(f"Invalid save manager settings: {path}")
    if active_profile is not None:
        try:
            validate_profile_name(active_profile)
        except ValueError as error:
            raise ValueError(f"Invalid save manager settings: {path}") from error
    return {
        "version": 1,
        "save_dir": save_dir,
        "keep_backups": keep_backups,
        "active_profile": active_profile,
    }


def save_settings(data_dir: Path, settings: SaveSettings) -> None:
    if not BACKUP_RETENTION_MIN <= settings["keep_backups"] <= BACKUP_RETENTION_MAX:
        raise ValueError(
            f"Keep-backup count must be between {BACKUP_RETENTION_MIN} and "
            f"{BACKUP_RETENTION_MAX}."
        )
    if settings["active_profile"] is not None:
        validate_profile_name(settings["active_profile"])
    data_dir.mkdir(parents=True, exist_ok=True)
    path = settings_path(data_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _file_manifest(folder: Path) -> Dict[str, Dict[str, object]]:
    manifest: Dict[str, Dict[str, object]] = {}
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Save folders containing symbolic links are not supported: {path}")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest[path.relative_to(folder).as_posix()] = {
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
    return manifest


def _copy_verified(source: Path, destination: Path) -> Dict[str, Dict[str, object]]:
    validate_save_dir(source)
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    source_manifest = _file_manifest(source)
    copied_manifest = _file_manifest(destination)
    if source_manifest != copied_manifest:
        raise OSError(f"Save copy verification failed: {destination}")
    return copied_manifest


def _read_manifest(path: Path, expected_kind: str) -> Dict[str, object]:
    try:
        manifest: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid save snapshot metadata: {path}") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or manifest.get("kind") != expected_kind
        or not isinstance(manifest.get("files"), dict)
    ):
        raise ValueError(f"Invalid save snapshot metadata: {path}")
    return manifest


def _verify_snapshot(snapshot: Path, manifest: Dict[str, object]) -> None:
    expected = manifest["files"]
    actual = _file_manifest(snapshot)
    if actual != expected:
        raise ValueError(f"Save snapshot failed integrity verification: {snapshot}")


def is_game_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq sekiro.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"Could not check whether Sekiro is running: {result.stderr.strip()}")
    for row in csv.reader(io.StringIO(result.stdout)):
        if row and row[0].casefold() == "sekiro.exe":
            return True
    return False


def _require_game_closed() -> None:
    if is_game_running():
        raise ValueError("Close Sekiro before restoring or switching save profiles.")


def _unique_timestamp_name(parent: Path, timestamp: str) -> str:
    candidate = timestamp
    suffix = 1
    while (parent / candidate).exists():
        suffix += 1
        candidate = f"{timestamp}-{suffix:02d}"
    return candidate


def create_backup(
    save_dir: Path,
    data_dir: Path,
    keep_backups: int = 10,
    label: str = "",
    profile: Optional[str] = None,
    prune: bool = True,
) -> Path:
    if not BACKUP_RETENTION_MIN <= keep_backups <= BACKUP_RETENTION_MAX:
        raise ValueError(
            f"Keep-backup count must be between {BACKUP_RETENTION_MIN} and "
            f"{BACKUP_RETENTION_MAX}."
        )
    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    root = backup_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
    folder_name = _unique_timestamp_name(root, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))

    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as temporary:
        staged = Path(temporary)
        snapshot = staged / "save"
        manifest = _copy_verified(save_dir, snapshot)
        metadata = {
            "version": 1,
            "kind": "backup",
            "created_at": timestamp,
            "label": label.strip(),
            "profile": profile,
            "files": manifest,
        }
        (staged / "backup.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        destination = root / folder_name
        os.replace(staged, destination)

    if prune:
        _prune_backups(data_dir, keep_backups)
    return destination


def list_backups(data_dir: Path) -> List[Dict[str, object]]:
    root = backup_root(data_dir)
    if not root.is_dir():
        return []
    backups: List[Dict[str, object]] = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
            continue
        metadata_path = folder / "backup.json"
        if not metadata_path.is_file():
            continue
        metadata = _read_manifest(metadata_path, "backup")
        created_at = metadata.get("created_at")
        label = metadata.get("label")
        profile = metadata.get("profile")
        if (
            not isinstance(created_at, str)
            or not isinstance(label, str)
            or (profile is not None and not isinstance(profile, str))
        ):
            raise ValueError(f"Invalid save backup metadata: {metadata_path}")
        backups.append({
            "path": folder,
            "created_at": created_at,
            "label": label,
            "profile": profile,
        })
    return sorted(
        backups,
        key=lambda backup: (str(backup["created_at"]), Path(backup["path"]).name),
        reverse=True,
    )


def _prune_backups(data_dir: Path, keep_backups: int) -> None:
    backups = list_backups(data_dir)
    for backup in backups[keep_backups:]:
        shutil.rmtree(backup["path"])


def _replace_directory(source: Path, destination: Path) -> None:
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError(f"Current save folder is unavailable: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".save-replace-", dir=destination.parent) as temporary:
        work = Path(temporary)
        replacement = work / "replacement"
        _copy_verified(source, replacement)
        displaced = work / "previous"
        os.replace(destination, displaced)
        try:
            os.replace(replacement, destination)
        except OSError:
            os.replace(displaced, destination)
            raise


def _validate_backup_path(path: Path, data_dir: Path) -> Path:
    root = backup_root(data_dir).resolve()
    candidate = path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Selected backup is outside the save backup folder: {path}") from error
    if candidate == root or not candidate.is_dir() or path.is_symlink():
        raise ValueError(f"Selected save backup is unavailable: {path}")
    metadata = _read_manifest(candidate / "backup.json", "backup")
    snapshot = candidate / "save"
    _verify_snapshot(snapshot, metadata)
    return candidate


def restore_backup(path: Path, save_dir: Path, data_dir: Path) -> Path:
    _require_game_closed()
    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    backup = _validate_backup_path(path, data_dir)
    settings = load_settings(data_dir)
    before_restore = create_backup(
        save_dir,
        data_dir,
        settings["keep_backups"],
        label="Before restore",
        profile=settings["active_profile"],
        prune=False,
    )
    try:
        _replace_directory(backup / "save", save_dir)
        if settings["active_profile"]:
            update_profile_from_current(save_dir, data_dir, settings["active_profile"])
    except (OSError, ValueError, shutil.Error):
        _replace_directory(before_restore / "save", save_dir)
        raise
    _prune_backups(data_dir, settings["keep_backups"])
    return backup


def validate_profile_name(name: str) -> str:
    name = name.strip()
    if (
        not name
        or name in {".", ".."}
        or name.endswith((" ", "."))
        or any(char in name for char in '<>:"/\\|?*')
        or name.split(".", 1)[0].upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
    ):
        raise ValueError("Profile name must be a valid folder name.")
    return name


def list_profiles(data_dir: Path) -> List[str]:
    root = profile_root(data_dir)
    if not root.is_dir():
        return []
    profiles: List[str] = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        _read_profile(folder)
        profiles.append(folder.name)
    return sorted(profiles, key=str.casefold)


def _read_profile(folder: Path) -> Dict[str, object]:
    metadata = _read_manifest(folder / "profile.json", "profile")
    if metadata.get("name") != folder.name:
        raise ValueError(f"Profile name does not match its folder: {folder}")
    _verify_snapshot(folder / "save", metadata)
    return metadata


def create_profile(save_dir: Path, data_dir: Path, name: str) -> str:
    _require_game_closed()
    name = validate_profile_name(name)
    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    root = profile_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    if destination.exists():
        raise ValueError(f"A save profile named '{name}' already exists.")
    settings = load_settings(data_dir)
    if settings["active_profile"]:
        update_profile_from_current(save_dir, data_dir, settings["active_profile"])

    with tempfile.TemporaryDirectory(prefix=".profile-", dir=root) as temporary:
        staged = Path(temporary) / "profile"
        staged.mkdir()
        manifest = _copy_verified(save_dir, staged / "save")
        metadata = {
            "version": 1,
            "kind": "profile",
            "name": name,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": manifest,
        }
        (staged / "profile.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        os.replace(staged, destination)

    settings["active_profile"] = name
    try:
        save_settings(data_dir, settings)
    except OSError:
        shutil.rmtree(destination)
        raise
    return name


def update_profile_from_current(save_dir: Path, data_dir: Path, name: str) -> None:
    name = validate_profile_name(name)
    root = profile_root(data_dir)
    profile = root / name
    _read_profile(profile)
    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)

    with tempfile.TemporaryDirectory(prefix=".profile-update-", dir=profile) as temporary:
        work = Path(temporary)
        replacement = work / "save"
        manifest = _copy_verified(save_dir, replacement)
        metadata = _read_profile(profile)
        metadata["files"] = manifest
        metadata["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata_path = work / "profile.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        old_save = work / "previous-save"
        old_metadata = work / "previous-profile.json"
        old_save_moved = False
        new_save_moved = False
        old_metadata_moved = False
        try:
            os.replace(profile / "save", old_save)
            old_save_moved = True
            os.replace(replacement, profile / "save")
            new_save_moved = True
            os.replace(profile / "profile.json", old_metadata)
            old_metadata_moved = True
            os.replace(metadata_path, profile / "profile.json")
        except OSError:
            if old_metadata_moved:
                if (profile / "profile.json").exists():
                    (profile / "profile.json").unlink()
                os.replace(old_metadata, profile / "profile.json")
            if new_save_moved and (profile / "save").exists():
                shutil.rmtree(profile / "save")
            if old_save_moved:
                os.replace(old_save, profile / "save")
            raise


def switch_profile(save_dir: Path, data_dir: Path, name: str) -> str:
    _require_game_closed()
    name = validate_profile_name(name)
    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    settings = load_settings(data_dir)
    current_name = settings["active_profile"]
    if current_name == name:
        return name

    destination_profile = profile_root(data_dir) / name
    destination_metadata = _read_profile(destination_profile)
    _verify_snapshot(destination_profile / "save", destination_metadata)
    create_backup(
        save_dir,
        data_dir,
        settings["keep_backups"],
        label=f"Before switching to {name}",
        profile=current_name,
    )

    with tempfile.TemporaryDirectory(
        prefix=".profile-switch-",
        dir=save_dir.parent,
    ) as temporary:
        rollback = Path(temporary) / "previous-save"
        _copy_verified(save_dir, rollback)
        if current_name:
            update_profile_from_current(save_dir, data_dir, current_name)
        _replace_directory(destination_profile / "save", save_dir)
        settings["active_profile"] = name
        try:
            save_settings(data_dir, settings)
        except OSError:
            _replace_directory(rollback, save_dir)
            raise
    return name


def game_cloud_warning() -> str:
    return (
        "Steam Cloud can replace a restored save with its cloud copy. Turn off Steam "
        "Cloud for Sekiro or restore while Steam is closed, then verify the save "
        "before reopening Steam."
    )
