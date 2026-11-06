import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import save_manager


def write_test_sekiro_save(
    path: Path,
    populated: dict[int, tuple[int, int, int, int, int]],
) -> None:
    entry_count = save_manager.SEKIRO_SLOT_COUNT
    table_end = save_manager.SEKIRO_BND4_HEADER_SIZE + (
        save_manager.SEKIRO_BND4_ENTRY_SIZE * entry_count
    )
    payload_size = save_manager.SEKIRO_SLOT_MIN_SIZE
    file_data = bytearray(table_end)
    file_data[:4] = b"BND4"
    struct.pack_into("<I", file_data, 12, entry_count)
    next_offset = table_end

    for slot_index in range(entry_count):
        payload = bytearray(payload_size)
        if slot_index in populated:
            steam_id, journey, playtime, vitality, attack_power = populated[slot_index]
            start = save_manager.SEKIRO_STEAM_ID_OFFSET
            payload[start : start + 8] = steam_id.to_bytes(8, "little")
            payload[save_manager.SEKIRO_JOURNEY_OFFSET] = journey
            payload[
                save_manager.SEKIRO_PLAYTIME_OFFSET :
                save_manager.SEKIRO_PLAYTIME_OFFSET + 4
            ] = playtime.to_bytes(4, "little")
            for offset, value in (
                (save_manager.SEKIRO_VITALITY_OFFSET, vitality),
                (save_manager.SEKIRO_ATTACK_POWER_OFFSET, attack_power),
            ):
                payload[offset : offset + 4] = value.to_bytes(4, "little")

        entry = hashlib.md5(payload, usedforsecurity=False).digest() + payload
        entry_header = (
            save_manager.SEKIRO_BND4_HEADER_SIZE
            + save_manager.SEKIRO_BND4_ENTRY_SIZE * slot_index
        )
        struct.pack_into("<Q", file_data, entry_header + 8, len(entry))
        struct.pack_into("<I", file_data, entry_header + 16, next_offset)
        file_data.extend(entry)
        next_offset += len(entry)

    path.write_bytes(file_data)


class SaveManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "manager-data"
        self.save_dir = self.root / "Sekiro" / "123456789"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "S0000.sl2").write_bytes(b"save-v1")
        (self.save_dir / "steam_autocloud.vdf").write_bytes(b"cloud metadata")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovers_numeric_steam_save_folder(self) -> None:
        game_root = self.root / "Roaming" / "Sekiro"
        expected = game_root / "7654321"
        (expected).mkdir(parents=True)
        (expected / "S0000.sl2").write_bytes(b"save")
        invalid = game_root / "backup"
        invalid.mkdir()
        (invalid / "S0000.sl2").write_bytes(b"save")
        no_save = game_root / "123"
        no_save.mkdir()

        self.assertEqual(save_manager.discover_save_folders(game_root.parent), [expected.resolve()])

    def test_reads_and_formats_multiple_populated_sekiro_slots(self) -> None:
        save_file = self.root / "sample.sl2"
        write_test_sekiro_save(
            save_file,
            {
                0: (123456789, 0, 45240, 12, 4),
                3: (123456789, 2, 3600, 20, 99),
            },
        )

        self.assertEqual(
            save_manager.read_sekiro_slots(save_file),
            [
                {
                    "slot": 1,
                    "journey": 0,
                    "playtime_seconds": 45240,
                    "vitality": 12,
                    "attack_power": 4,
                },
                {
                    "slot": 4,
                    "journey": 2,
                    "playtime_seconds": 3600,
                    "vitality": 20,
                    "attack_power": 99,
                },
            ],
        )
        self.assertEqual(
            save_manager.format_sekiro_save_summary(save_file),
            "Slot 1: Journey 1 (NG) · 12h 34m · Vitality 12 · Attack Power 4\n"
            "Slot 4: Journey 3 (NG+2) · 1h 00m · Vitality 20 · Attack Power 99",
        )

    def test_rejects_sekiro_slot_with_invalid_checksum(self) -> None:
        save_file = self.root / "sample.sl2"
        write_test_sekiro_save(save_file, {0: (123456789, 0, 100, 1, 1)})
        with save_file.open("r+b") as output:
            output.seek(
                save_manager.SEKIRO_BND4_HEADER_SIZE
                + save_manager.SEKIRO_BND4_ENTRY_SIZE * save_manager.SEKIRO_SLOT_COUNT
                + 16
                + save_manager.SEKIRO_ATTACK_POWER_OFFSET
            )
            output.write(b"\xff")

        with self.assertRaisesRegex(ValueError, "checksum"):
            save_manager.read_sekiro_slots(save_file)

    def test_rejects_unrecognized_sekiro_save_container(self) -> None:
        save_file = self.root / "sample.sl2"
        save_file.write_bytes(b"X" * save_manager.SEKIRO_BND4_HEADER_SIZE)

        with self.assertRaisesRegex(ValueError, "not a supported Sekiro BND4"):
            save_manager.read_sekiro_slots(save_file)

    def test_does_not_report_empty_slots_as_characters(self) -> None:
        save_file = self.root / "sample.sl2"
        write_test_sekiro_save(save_file, {})

        with self.assertRaisesRegex(ValueError, "No populated Sekiro save slots"):
            save_manager.read_sekiro_slots(save_file)

    def test_backup_copies_and_verifies_entire_save_folder(self) -> None:
        backup = save_manager.create_backup(
            self.save_dir,
            self.data_dir,
            label="before boss rush",
            profile="Vanilla",
        )
        self.assertEqual((backup / "save" / "S0000.sl2").read_bytes(), b"save-v1")
        self.assertEqual(
            (backup / "save" / "steam_autocloud.vdf").read_bytes(),
            b"cloud metadata",
        )
        metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["label"], "before boss rush")
        self.assertEqual(metadata["profile"], "Vanilla")
        self.assertEqual(metadata["kind"], "backup")
        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"save-v1")

    def test_backup_retention_keeps_only_newest_configured_count(self) -> None:
        for value in (b"save-1", b"save-2", b"save-3"):
            (self.save_dir / "S0000.sl2").write_bytes(value)
            save_manager.create_backup(self.save_dir, self.data_dir, keep_backups=2)

        backups = save_manager.list_backups(self.data_dir)
        self.assertEqual(len(backups), 2)
        contents = {
            (Path(backup["path"]) / "save" / "S0000.sl2").read_bytes()
            for backup in backups
        }
        self.assertEqual(contents, {b"save-2", b"save-3"})

    def test_restore_backs_up_current_save_then_restores_whole_folder(self) -> None:
        original_backup = save_manager.create_backup(self.save_dir, self.data_dir)
        (self.save_dir / "S0000.sl2").write_bytes(b"current")
        (self.save_dir / "new-extra-file").write_bytes(b"temporary")

        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            restored = save_manager.restore_backup(original_backup, self.save_dir, self.data_dir)

        self.assertEqual(restored, original_backup.resolve())
        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"save-v1")
        self.assertFalse((self.save_dir / "new-extra-file").exists())
        backups = save_manager.list_backups(self.data_dir)
        self.assertEqual(len(backups), 2)
        self.assertTrue(any(backup["label"] == "Before restore" for backup in backups))

    def test_restore_refuses_while_game_is_running_without_changing_save(self) -> None:
        original_backup = save_manager.create_backup(self.save_dir, self.data_dir)
        (self.save_dir / "S0000.sl2").write_bytes(b"current")

        with mock.patch.object(save_manager, "is_game_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "Close Sekiro"):
                save_manager.restore_backup(original_backup, self.save_dir, self.data_dir)

        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"current")
        self.assertEqual(len(save_manager.list_backups(self.data_dir)), 1)

    def test_restore_with_one_backup_keeps_the_undo_backup(self) -> None:
        original_backup = save_manager.create_backup(self.save_dir, self.data_dir, keep_backups=1)
        (self.save_dir / "S0000.sl2").write_bytes(b"current")
        settings = save_manager.load_settings(self.data_dir)
        settings["keep_backups"] = 1
        save_manager.save_settings(self.data_dir, settings)

        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            save_manager.restore_backup(original_backup, self.save_dir, self.data_dir)

        backups = save_manager.list_backups(self.data_dir)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["label"], "Before restore")
        self.assertEqual(
            (Path(backups[0]["path"]) / "save" / "S0000.sl2").read_bytes(),
            b"current",
        )

    def test_restore_rolls_back_if_active_profile_cannot_be_updated(self) -> None:
        original_backup = save_manager.create_backup(self.save_dir, self.data_dir)
        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            save_manager.create_profile(self.save_dir, self.data_dir, "Vanilla")
        (self.save_dir / "S0000.sl2").write_bytes(b"current")

        with (
            mock.patch.object(save_manager, "is_game_running", return_value=False),
            mock.patch.object(
                save_manager,
                "update_profile_from_current",
                side_effect=OSError("profile storage unavailable"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "profile storage unavailable"):
                save_manager.restore_backup(original_backup, self.save_dir, self.data_dir)

        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"current")

    def test_restore_rejects_tampered_backup_before_backing_up_or_writing(self) -> None:
        backup = save_manager.create_backup(self.save_dir, self.data_dir)
        (backup / "save" / "S0000.sl2").write_bytes(b"tampered")
        (self.save_dir / "S0000.sl2").write_bytes(b"current")

        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            with self.assertRaisesRegex(ValueError, "integrity verification"):
                save_manager.restore_backup(backup, self.save_dir, self.data_dir)

        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"current")
        self.assertEqual(len(save_manager.list_backups(self.data_dir)), 1)

    def test_save_profiles_keep_independent_full_save_copies(self) -> None:
        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            save_manager.create_profile(self.save_dir, self.data_dir, "Vanilla")
            save_manager.create_profile(self.save_dir, self.data_dir, "Randomizer")
            save_manager.switch_profile(self.save_dir, self.data_dir, "Randomizer")
            (self.save_dir / "S0000.sl2").write_bytes(b"randomizer-save")
            (self.save_dir / "randomizer-extra").write_bytes(b"extra")

            save_manager.switch_profile(self.save_dir, self.data_dir, "Vanilla")
            self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"save-v1")
            self.assertFalse((self.save_dir / "randomizer-extra").exists())

            save_manager.switch_profile(self.save_dir, self.data_dir, "Randomizer")
            self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"randomizer-save")
            self.assertEqual((self.save_dir / "randomizer-extra").read_bytes(), b"extra")

        self.assertEqual(save_manager.list_profiles(self.data_dir), ["Randomizer", "Vanilla"])
        self.assertEqual(
            save_manager.load_settings(self.data_dir)["active_profile"],
            "Randomizer",
        )
        backups = save_manager.list_backups(self.data_dir)
        self.assertGreaterEqual(len(backups), 2)

    def test_switching_profile_refuses_when_game_running(self) -> None:
        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            save_manager.create_profile(self.save_dir, self.data_dir, "Vanilla")
            (self.save_dir / "S0000.sl2").write_bytes(b"changed")

        with mock.patch.object(save_manager, "is_game_running", return_value=True):
            with self.assertRaisesRegex(ValueError, "Close Sekiro"):
                save_manager.switch_profile(self.save_dir, self.data_dir, "Vanilla")

        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"changed")

    def test_failed_profile_switch_restores_the_original_save(self) -> None:
        with mock.patch.object(save_manager, "is_game_running", return_value=False):
            save_manager.create_profile(self.save_dir, self.data_dir, "Vanilla")
            (self.save_dir / "S0000.sl2").write_bytes(b"randomizer")
            save_manager.create_profile(self.save_dir, self.data_dir, "Randomizer")

            with mock.patch.object(save_manager, "save_settings", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    save_manager.switch_profile(self.save_dir, self.data_dir, "Vanilla")

        self.assertEqual((self.save_dir / "S0000.sl2").read_bytes(), b"randomizer")
        self.assertEqual(
            save_manager.load_settings(self.data_dir)["active_profile"],
            "Randomizer",
        )

    def test_manager_storage_cannot_overlap_save_folder(self) -> None:
        data_dir = self.save_dir / "manager"

        with self.assertRaisesRegex(ValueError, "cannot be inside"):
            save_manager.create_backup(self.save_dir, data_dir)

    def test_running_game_detection_checks_the_exact_process_name(self) -> None:
        running = subprocess.CompletedProcess(
            args=["tasklist"],
            returncode=0,
            stdout='"sekiro.exe","1234","Console","1","12,345 K"\r\n',
            stderr="",
        )
        not_running = subprocess.CompletedProcess(
            args=["tasklist"],
            returncode=0,
            stdout="INFO: No tasks are running which match the specified criteria.\r\n",
            stderr="",
        )
        with mock.patch.object(save_manager.subprocess, "run", return_value=running) as run:
            self.assertTrue(save_manager.is_game_running())
        run.assert_called_once()
        with mock.patch.object(save_manager.subprocess, "run", return_value=not_running):
            self.assertFalse(save_manager.is_game_running())

    def test_load_settings_rejects_invalid_retention_count(self) -> None:
        self.data_dir.mkdir()
        save_manager.settings_path(self.data_dir).write_text(
            '{"version": 1, "save_dir": null, "keep_backups": 0, "active_profile": null}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Invalid save manager settings"):
            save_manager.load_settings(self.data_dir)


if __name__ == "__main__":
    unittest.main()
