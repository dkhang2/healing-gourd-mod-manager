import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Dict, List, Optional, TypedDict


BACKUP_RETENTION_MIN = 1
BACKUP_RETENTION_MAX = 100
SEKIRO_SLOT_COUNT = 10
SEKIRO_SETTINGS_ENTRY_INDEX = 10
SEKIRO_BND4_HEADER_SIZE = 64
SEKIRO_BND4_ENTRY_SIZE = 32
SEKIRO_MAX_SAVE_FILE_SIZE = 128 * 1024 * 1024
SEKIRO_SLOT_MIN_SIZE = 0x34563
SEKIRO_STEAM_ID_OFFSET = 0x33E54
SEKIRO_EXPORT_STEAM_ID_PLACEHOLDER = 1
SEKIRO_SETTINGS_STEAM_ID_OFFSET = 0x24
SEKIRO_SETTINGS_SLOT_OCCUPANCY_OFFSET = 0xD4
SEKIRO_SETTINGS_PAYLOAD_MIN_SIZE = 0x60000
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


class SekiroEntry(TypedDict):
    offset: int
    size: int


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


def _parse_sekiro_save_data(
    data: bytes,
) -> tuple[List[SekiroEntry], List[SekiroSaveSlot]]:
    file_size = len(data)
    if not SEKIRO_BND4_HEADER_SIZE <= file_size <= SEKIRO_MAX_SAVE_FILE_SIZE:
        raise ValueError(f"Unexpected Sekiro save file size: {file_size} bytes.")
    if data[:4] != b"BND4":
        raise ValueError("This is not a supported Sekiro BND4 save file.")

    entry_count = int.from_bytes(data[12:16], "little")
    table_end = SEKIRO_BND4_HEADER_SIZE + SEKIRO_BND4_ENTRY_SIZE * entry_count
    if not SEKIRO_SLOT_COUNT <= entry_count <= 64 or table_end > len(data):
        raise ValueError("The Sekiro save has an invalid BND4 entry table.")

    entries: List[SekiroEntry] = []
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
        entries.append({"offset": payload_offset, "size": entry_size})

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

    return entries, slots


def _read_sekiro_save_file(save_file: Path) -> tuple[bytes, List[SekiroEntry], List[SekiroSaveSlot]]:
    if save_file.is_symlink() or not save_file.is_file():
        raise ValueError(f"Sekiro save file is unavailable: {save_file}")
    data = save_file.read_bytes()
    entries, slots = _parse_sekiro_save_data(data)
    return data, entries, slots


def _read_sekiro_save_archive(
    archive_path: Path,
) -> tuple[bytes, List[SekiroEntry], List[SekiroSaveSlot]]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            save_entries = [
                entry for entry in archive.infolist()
                if not entry.is_dir()
                and PurePosixPath(entry.filename).name.casefold() == "s0000.sl2"
            ]
            if len(save_entries) != 1:
                raise ValueError(
                    "The ZIP must contain exactly one S0000.sl2 save file."
                )
            save_entry = save_entries[0]
            if stat.S_ISLNK(save_entry.external_attr >> 16):
                raise ValueError("The ZIP save file cannot be a symbolic link.")
            if not 0 < save_entry.file_size <= SEKIRO_MAX_SAVE_FILE_SIZE:
                raise ValueError("The ZIP save file has an invalid size.")
            data = archive.read(save_entry)
    except zipfile.BadZipFile as error:
        raise ValueError(f"This is not a valid save ZIP archive: {archive_path}") from error
    entries, slots = _parse_sekiro_save_data(data)
    return data, entries, slots


def _read_sekiro_save_source(
    source: Path,
) -> tuple[bytes, List[SekiroEntry], List[SekiroSaveSlot]]:
    if source.is_symlink():
        raise ValueError(f"Sekiro save file is unavailable: {source}")
    if source.is_dir():
        save_files = [
            path
            for path in source.rglob("*")
            if path.name.casefold() == "s0000.sl2"
        ]
        if len(save_files) != 1:
            raise ValueError(
                "Choose a folder containing exactly one S0000.sl2 save file."
            )
        return _read_sekiro_save_file(save_files[0])
    if not source.is_file():
        raise ValueError(f"Sekiro save file is unavailable: {source}")
    if zipfile.is_zipfile(source):
        return _read_sekiro_save_archive(source)
    return _read_sekiro_save_file(source)


def read_sekiro_slots(save_file: Path) -> List[SekiroSaveSlot]:
    _, _, slots = _read_sekiro_save_source(save_file)
    if not slots:
        raise ValueError("No populated Sekiro save slots were found in this file.")
    return slots


def read_sekiro_slot_table(save_file: Path) -> List[Optional[SekiroSaveSlot]]:
    _, _, slots = _read_sekiro_save_source(save_file)
    populated = {slot["slot"]: slot for slot in slots}
    return [populated.get(slot_number) for slot_number in range(1, SEKIRO_SLOT_COUNT + 1)]


def _sekiro_slot_occupancy(data: bytes, settings: SekiroEntry) -> List[bool]:
    occupancy_start = (
        settings["offset"]
        + 16
        + SEKIRO_SETTINGS_SLOT_OCCUPANCY_OFFSET
    )
    occupancy_end = occupancy_start + SEKIRO_SLOT_COUNT
    settings_end = settings["offset"] + settings["size"]
    if occupancy_end > settings_end:
        raise ValueError("Sekiro save slot occupancy table has invalid bounds.")
    return [
        value != 0
        for value in data[occupancy_start:occupancy_end]
    ]


def read_sekiro_slot_occupancy(save_file: Path) -> List[bool]:
    data, _, _ = _read_sekiro_save_file(save_file)
    return _sekiro_slot_occupancy(data, _settings_entry(data))


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


def _settings_entry(data: bytes) -> SekiroEntry:
    entry_count = int.from_bytes(data[12:16], "little")
    if entry_count <= SEKIRO_SETTINGS_ENTRY_INDEX:
        raise ValueError("This Sekiro save does not contain its settings entry.")
    entry_header = (
        SEKIRO_BND4_HEADER_SIZE
        + SEKIRO_BND4_ENTRY_SIZE * SEKIRO_SETTINGS_ENTRY_INDEX
    )
    size = int.from_bytes(data[entry_header + 8 : entry_header + 16], "little")
    offset = int.from_bytes(data[entry_header + 16 : entry_header + 20], "little")
    end = offset + size
    minimum_size = 16 + SEKIRO_SETTINGS_PAYLOAD_MIN_SIZE
    if (
        size < minimum_size
        or offset < SEKIRO_BND4_HEADER_SIZE + SEKIRO_BND4_ENTRY_SIZE * entry_count
        or end > len(data)
    ):
        raise ValueError("Sekiro save settings entry has invalid bounds.")
    if hashlib.md5(data[offset + 16 : end], usedforsecurity=False).digest() != data[offset : offset + 16]:
        raise ValueError("Sekiro save settings entry failed its checksum.")
    return {"offset": offset, "size": size}


def _set_sekiro_slot_occupancy(
    data: bytearray,
    settings: SekiroEntry,
    slot_number: int,
    occupied: bool,
) -> None:
    if not 1 <= slot_number <= SEKIRO_SLOT_COUNT:
        raise ValueError(f"Slot number must be between 1 and {SEKIRO_SLOT_COUNT}.")
    payload_start = settings["offset"] + 16
    occupancy_offset = payload_start + SEKIRO_SETTINGS_SLOT_OCCUPANCY_OFFSET + slot_number - 1
    settings_end = settings["offset"] + settings["size"]
    if occupancy_offset >= settings_end:
        raise ValueError("Sekiro save slot occupancy table has invalid bounds.")
    data[occupancy_offset] = int(occupied)
    data[settings["offset"] : settings["offset"] + 16] = hashlib.md5(
        data[payload_start:settings_end],
        usedforsecurity=False,
    ).digest()


def _write_bytes_atomically(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
    except OSError:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise


def export_sekiro_save_folder(save_dir: Path, destination: Path) -> Path:
    _require_game_closed()
    save_dir = validate_save_dir(save_dir)
    source_save = save_dir / "S0000.sl2"
    _read_sekiro_save_file(source_save)
    if destination.resolve().is_relative_to(save_dir):
        raise ValueError("Choose an export path outside the Sekiro save folder.")
    manifest = _file_manifest(save_dir)
    if not manifest:
        raise ValueError("The Sekiro save folder contains no files to export.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for relative_name in sorted(manifest):
                archive.write(
                    save_dir / Path(relative_name),
                    arcname=relative_name,
                )

        with zipfile.ZipFile(temporary_path) as archive:
            if archive.testzip() is not None:
                raise OSError("The exported save ZIP failed its CRC check.")
            exported_names = {
                entry.filename: entry
                for entry in archive.infolist()
                if not entry.is_dir()
            }
            if set(exported_names) != set(manifest):
                raise OSError("The exported save ZIP does not match the save folder.")
            for relative_name, expected in manifest.items():
                archived_data = archive.read(exported_names[relative_name])
                expected_size = expected.get("size")
                expected_digest = expected.get("sha256")
                if (
                    type(expected_size) is not int
                    or not isinstance(expected_digest, str)
                    or len(archived_data) != expected_size
                    or hashlib.sha256(archived_data).hexdigest() != expected_digest
                ):
                    raise OSError(
                        f"Save ZIP verification failed for {relative_name}."
                    )
        _read_sekiro_save_archive(temporary_path)
        os.replace(temporary_path, destination)
    except (OSError, zipfile.BadZipFile):
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise
    return destination


def export_sekiro_slot(save_file: Path, slot_number: int, destination: Path) -> Path:
    if not 1 <= slot_number <= SEKIRO_SLOT_COUNT:
        raise ValueError(f"Slot number must be between 1 and {SEKIRO_SLOT_COUNT}.")
    if destination.resolve() == save_file.resolve():
        raise ValueError("Choose a different export path so the original save stays untouched.")

    data, entries, slots = _read_sekiro_save_file(save_file)
    selected_index = slot_number - 1
    if not any(slot["slot"] == slot_number for slot in slots):
        raise ValueError(f"Save slot {slot_number} is empty.")
    settings = _settings_entry(data)
    exported = bytearray(data)

    for index, entry_info in enumerate(entries):
        if index == selected_index:
            selected_start = entry_info["offset"] + 16 + SEKIRO_STEAM_ID_OFFSET
            exported[selected_start : selected_start + 8] = (
                SEKIRO_EXPORT_STEAM_ID_PLACEHOLDER.to_bytes(8, "little")
            )
            selected_end = entry_info["offset"] + entry_info["size"]
            exported[entry_info["offset"] : entry_info["offset"] + 16] = hashlib.md5(
                exported[entry_info["offset"] + 16 : selected_end],
                usedforsecurity=False,
            ).digest()
            continue
        start = entry_info["offset"]
        end = start + entry_info["size"]
        empty_payload = bytes(entry_info["size"] - 16)
        exported[start : start + 16] = hashlib.md5(
            empty_payload,
            usedforsecurity=False,
        ).digest()
        exported[start + 16 : end] = empty_payload

    for index in range(SEKIRO_SLOT_COUNT):
        occupancy_offset = (
            settings["offset"]
            + 16
            + SEKIRO_SETTINGS_SLOT_OCCUPANCY_OFFSET
            + index
        )
        exported[occupancy_offset] = int(index == selected_index)
    settings_payload_start = settings["offset"] + 16
    account_id_start = settings_payload_start + SEKIRO_SETTINGS_STEAM_ID_OFFSET
    exported[account_id_start : account_id_start + 8] = bytes(8)
    settings_end = settings["offset"] + settings["size"]
    exported[settings["offset"] : settings["offset"] + 16] = hashlib.md5(
        exported[settings_payload_start:settings_end],
        usedforsecurity=False,
    ).digest()

    _, exported_slots = _parse_sekiro_save_data(exported)
    if [slot["slot"] for slot in exported_slots] != [slot_number]:
        raise ValueError("The exported Sekiro save did not contain exactly the selected slot.")
    exported_settings = _settings_entry(exported)
    exported_occupancy_start = (
        exported_settings["offset"]
        + 16
        + SEKIRO_SETTINGS_SLOT_OCCUPANCY_OFFSET
    )
    if exported[exported_occupancy_start : exported_occupancy_start + SEKIRO_SLOT_COUNT] != bytes(
        int(index == selected_index) for index in range(SEKIRO_SLOT_COUNT)
    ):
        raise ValueError("The exported Sekiro save has an invalid slot roster.")
    _write_bytes_atomically(destination, exported)
    return destination


def import_sekiro_slot(
    source_file: Path,
    source_slot_number: int,
    save_dir: Path,
    target_slot_number: int,
    data_dir: Path,
) -> Path:
    _require_game_closed()
    if not 1 <= source_slot_number <= SEKIRO_SLOT_COUNT:
        raise ValueError(f"Source slot must be between 1 and {SEKIRO_SLOT_COUNT}.")
    if not 1 <= target_slot_number <= SEKIRO_SLOT_COUNT:
        raise ValueError(f"Target slot must be between 1 and {SEKIRO_SLOT_COUNT}.")

    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    destination_file = save_dir / "S0000.sl2"
    if source_file.resolve() == destination_file.resolve():
        raise ValueError("Choose a different source save file.")

    source_data, source_entries, source_slots = _read_sekiro_save_source(source_file)
    current_data, current_entries, current_slots = _read_sekiro_save_file(destination_file)
    settings = _settings_entry(current_data)
    current_occupancy = _sekiro_slot_occupancy(current_data, settings)
    source_index = source_slot_number - 1
    target_index = target_slot_number - 1
    source_slot = next(
        (slot for slot in source_slots if slot["slot"] == source_slot_number),
        None,
    )
    if source_slot is None:
        raise ValueError(f"Source save slot {source_slot_number} contains no character data.")
    if (
        any(slot["slot"] == target_slot_number for slot in current_slots)
        or current_occupancy[target_index]
    ):
        raise ValueError(
            f"Target save slot {target_slot_number} is already occupied. "
            "Choose an empty slot; importing will not overwrite existing characters."
        )

    source_entry = source_entries[source_index]
    target_entry = current_entries[target_index]
    if source_entry["size"] != target_entry["size"]:
        raise ValueError(
            "The selected save slot is a different size from this game's slot. "
            "The import was stopped to avoid corrupting the save."
        )

    settings_payload_start = settings["offset"] + 16
    local_steam_id = int.from_bytes(
        current_data[
            settings_payload_start + SEKIRO_SETTINGS_STEAM_ID_OFFSET :
            settings_payload_start + SEKIRO_SETTINGS_STEAM_ID_OFFSET + 8
        ],
        "little",
    )
    if local_steam_id == 0:
        raise ValueError(
            "Could not read the local Steam ID from Sekiro's settings. "
            "Launch Sekiro once and save before importing another player's slot."
        )

    source_start = source_entry["offset"]
    source_end = source_start + source_entry["size"]
    target_start = target_entry["offset"]
    target_end = target_start + target_entry["size"]
    imported_entry = bytearray(source_data[source_start:source_end])
    owner_start = SEKIRO_STEAM_ID_OFFSET
    imported_entry[
        16 + owner_start : 16 + owner_start + 8
    ] = local_steam_id.to_bytes(8, "little")
    imported_entry[:16] = hashlib.md5(
        imported_entry[16:],
        usedforsecurity=False,
    ).digest()

    updated_data = bytearray(current_data)
    updated_data[target_start:target_end] = imported_entry
    _set_sekiro_slot_occupancy(
        updated_data,
        settings,
        target_slot_number,
        True,
    )
    _, verified_slots = _parse_sekiro_save_data(updated_data)
    imported_result = next(
        (slot for slot in verified_slots if slot["slot"] == target_slot_number),
        None,
    )
    if imported_result is None or any(
        imported_result[key] != source_slot[key]
        for key in ("journey", "playtime_seconds", "vitality", "attack_power")
    ):
        raise ValueError("The imported save slot failed verification.")
    if not _sekiro_slot_occupancy(updated_data, settings)[target_index]:
        raise ValueError("The imported save slot is missing its game roster entry.")

    settings_record = load_settings(data_dir)
    before_import = create_backup(
        save_dir,
        data_dir,
        keep_backups=settings_record["keep_backups"],
        label=f"Before importing save slot {target_slot_number}",
        profile=settings_record["active_profile"],
        prune=False,
    )
    _write_bytes_atomically(destination_file, updated_data)
    try:
        if settings_record["active_profile"]:
            update_profile_from_current(
                save_dir,
                data_dir,
                settings_record["active_profile"],
            )
    except (OSError, ValueError, shutil.Error):
        try:
            backup_file = before_import / "save" / "S0000.sl2"
            _write_bytes_atomically(destination_file, backup_file.read_bytes())
        except OSError as rollback_error:
            raise OSError(
                "The profile could not be updated, and the original save could not be "
                f"restored from backup '{before_import}': {rollback_error}"
            ) from rollback_error
        raise
    _prune_backups(data_dir, settings_record["keep_backups"])
    return before_import


def remove_sekiro_slot(save_dir: Path, slot_number: int, data_dir: Path) -> Path:
    _require_game_closed()
    if not 1 <= slot_number <= SEKIRO_SLOT_COUNT:
        raise ValueError(f"Slot number must be between 1 and {SEKIRO_SLOT_COUNT}.")

    save_dir = validate_save_dir(save_dir)
    _validate_storage_separation(save_dir, data_dir)
    destination_file = save_dir / "S0000.sl2"
    current_data, entries, slots = _read_sekiro_save_file(destination_file)
    settings = _settings_entry(current_data)
    occupancy = _sekiro_slot_occupancy(current_data, settings)
    selected_slot = next((slot for slot in slots if slot["slot"] == slot_number), None)
    if selected_slot is None and not occupancy[slot_number - 1]:
        raise ValueError(f"Save slot {slot_number} is empty.")

    entry = entries[slot_number - 1]
    start = entry["offset"]
    end = start + entry["size"]
    empty_payload = bytes(entry["size"] - 16)
    updated_data = bytearray(current_data)
    updated_data[start : start + 16] = hashlib.md5(
        empty_payload,
        usedforsecurity=False,
    ).digest()
    updated_data[start + 16 : end] = empty_payload
    _set_sekiro_slot_occupancy(
        updated_data,
        settings,
        slot_number,
        False,
    )

    _, verified_slots = _parse_sekiro_save_data(updated_data)
    expected_slots = [slot for slot in slots if slot["slot"] != slot_number]
    if verified_slots != expected_slots:
        raise ValueError("The save slot removal failed verification.")
    if _sekiro_slot_occupancy(updated_data, settings)[slot_number - 1]:
        raise ValueError("The save slot roster entry was not cleared.")

    settings_record = load_settings(data_dir)
    before_removal = create_backup(
        save_dir,
        data_dir,
        keep_backups=settings_record["keep_backups"],
        label=f"Before removing save slot {slot_number}",
        profile=settings_record["active_profile"],
        prune=False,
    )
    _write_bytes_atomically(destination_file, updated_data)
    try:
        if settings_record["active_profile"]:
            update_profile_from_current(
                save_dir,
                data_dir,
                settings_record["active_profile"],
            )
    except (OSError, ValueError, shutil.Error):
        try:
            backup_file = before_removal / "save" / "S0000.sl2"
            _write_bytes_atomically(destination_file, backup_file.read_bytes())
        except OSError as rollback_error:
            raise OSError(
                "The profile could not be updated, and the original save could not be "
                f"restored from backup '{before_removal}': {rollback_error}"
            ) from rollback_error
        raise
    _prune_backups(data_dir, settings_record["keep_backups"])
    return before_removal


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
        raise ValueError("Close Sekiro before changing or restoring save data.")


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
