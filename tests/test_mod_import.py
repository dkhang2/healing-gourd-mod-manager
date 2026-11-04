import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import main


class ModImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "Library"
        self.library.mkdir()
        self.library_patch = mock.patch.object(main, "LIBRARY", self.library)
        self.library_patch.start()

    def tearDown(self) -> None:
        self.library_patch.stop()
        self.temporary.cleanup()

    def make_archive(self, name: str, entries: dict[str, bytes]) -> Path:
        archive_path = self.root / name
        with zipfile.ZipFile(archive_path, "w") as archive:
            for entry_name, content in entries.items():
                archive.writestr(entry_name, content)
        return archive_path

    def test_imports_zip_and_removes_matching_wrapper_folder(self) -> None:
        archive = self.make_archive(
            "Test Mod.zip",
            {
                "Test Mod/parts/chr/test-part.bin": b"part",
                "Test Mod/readme.txt": b"readme",
            },
        )

        main.import_mod(str(archive))

        imported = self.library / "Test Mod"
        self.assertEqual((imported / "parts/chr/test-part.bin").read_bytes(), b"part")
        self.assertEqual((imported / "readme.txt").read_bytes(), b"readme")
        self.assertFalse((imported / "Test Mod").exists())

    def test_zip_wrapper_name_becomes_the_library_name(self) -> None:
        archive = self.make_archive(
            "download.zip",
            {"Phantom Blade Zero/parts/chr/model.bin": b"model"},
        )

        imported_name = main.import_mod(str(archive))

        self.assertEqual(imported_name, "Phantom Blade Zero")
        self.assertTrue((self.library / imported_name / "parts/chr/model.bin").is_file())

    def test_import_preserves_game_mod_folder_layout(self) -> None:
        archive = self.make_archive("Another Mod.zip", {"parts/chr/test-part.bin": b"part"})

        main.import_mod(str(archive))

        self.assertEqual(
            (self.library / "Another Mod/parts/chr/test-part.bin").read_bytes(),
            b"part",
        )

    def test_folder_import_still_works(self) -> None:
        source = self.root / "Extracted Mod"
        source.mkdir()
        (source / "mod.bin").write_bytes(b"mod")

        main.import_mod(str(source))

        self.assertEqual((self.library / "Extracted Mod/mod.bin").read_bytes(), b"mod")

    def test_rejects_path_traversal_without_writing_outside_library(self) -> None:
        archive = self.make_archive("Unsafe.zip", {"../outside.txt": b"bad"})

        with self.assertRaisesRegex(ValueError, "Unsafe path"):
            main.import_mod(str(archive))

        self.assertFalse((self.root / "outside.txt").exists())
        self.assertFalse((self.library / "Unsafe").exists())

    def test_rejects_symbolic_links(self) -> None:
        archive_path = self.root / "Linked.zip"
        info = zipfile.ZipInfo("Linked/link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(info, "target")

        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            main.import_mod(str(archive_path))

        self.assertFalse((self.library / "Linked").exists())


class LaunchGameTests(unittest.TestCase):
    def test_launches_sekiro_from_the_game_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game_dir = Path(temporary)
            executable = game_dir / "sekiro.exe"
            executable.write_bytes(b"test")

            with mock.patch.object(main.subprocess, "Popen") as popen:
                main.launch_game(game_dir)

            popen.assert_called_once_with([str(executable)], cwd=str(game_dir))

    def test_launching_selected_mod_enables_it_before_game(self) -> None:
        game_dir = Path("C:/Sekiro")
        calls: list[str] = []
        state = {"mods": {}}

        def enable_mod(name: str) -> None:
            calls.append(f"enable:{name}")
            state["mods"][name] = {}

        with (
            mock.patch.object(main, "load_state", side_effect=lambda: state),
            mock.patch.object(main, "enable_mod", side_effect=enable_mod),
            mock.patch.object(main, "launch_game", side_effect=lambda _game_dir: calls.append("launch")),
        ):
            enabled_count = main.launch_modded_game(game_dir, "Test Mod")

        self.assertEqual(enabled_count, 1)
        self.assertEqual(calls, ["enable:Test Mod", "launch"])

    def test_launching_current_setup_does_not_change_enabled_mods(self) -> None:
        game_dir = Path("C:/Sekiro")
        state = {"mods": {"Existing Mod": {}}}

        with (
            mock.patch.object(main, "load_state", return_value=state),
            mock.patch.object(main, "enable_mod") as enable_mod,
            mock.patch.object(main, "launch_game") as launch_game,
        ):
            enabled_count = main.launch_modded_game(game_dir)

        self.assertEqual(enabled_count, 1)
        enable_mod.assert_not_called()
        launch_game.assert_called_once_with(game_dir)

    def test_rejects_invalid_game_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(main.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(ValueError, "sekiro.exe"):
                    main.launch_game(Path(temporary))

            popen.assert_not_called()

    def test_nested_mod_install_flattens_content_into_mod_engine_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "Sekiro"
            nested_mod = game_dir / "mods" / "Named Mod"
            (nested_mod / "parts" / "chr").mkdir(parents=True)
            (game_dir / "sekiro.exe").write_bytes(b"test")
            (nested_mod / "parts" / "chr" / "model.bin").write_bytes(b"model")
            state = {
                "version": 1,
                "game_dir": str(game_dir),
                "mods": {},
                "display_names": {},
                "appearance": {"background_image": None, "app_icon": None},
            }

            with (
                mock.patch.object(main, "LIBRARY", root / "Library"),
                mock.patch.object(main, "load_state", side_effect=lambda: state),
                mock.patch.object(main, "save_state"),
                mock.patch.object(main, "launch_game") as launch_game,
            ):
                enabled_count = main.install_external_mod(game_dir, "Named Mod")

            self.assertEqual(enabled_count, 1)
            self.assertTrue((game_dir / "mods" / "parts" / "chr" / "model.bin").is_file())
            self.assertTrue((nested_mod / "parts" / "chr" / "model.bin").is_file())
            self.assertIn("Named Mod", state["mods"])
            launch_game.assert_called_once_with(game_dir)

    def test_only_named_folders_wrapping_game_content_are_detected_as_nested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            named_mod = root / "Named Mod"
            (named_mod / "parts").mkdir(parents=True)
            game_parts = root / "parts"
            (game_parts / "chr").mkdir(parents=True)

            self.assertTrue(main.is_nested_mod_folder(named_mod))
            self.assertFalse(main.is_nested_mod_folder(game_parts))

    def test_nested_mod_listing_shows_folder_name_and_inactive_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            mods_dir = Path(temporary) / "mods"
            nested_mod = mods_dir / "Phantom Blade Zero"
            (nested_mod / "parts" / "chr").mkdir(parents=True)
            (nested_mod / "parts" / "chr" / "model.bin").write_bytes(b"model")
            game_parts = mods_dir / "parts"
            (game_parts / "chr").mkdir(parents=True)
            (game_parts / "chr" / "other.bin").write_bytes(b"other")

            installed = main.list_unmanaged_installed_mods(mods_dir, set())

            self.assertIn(("Phantom Blade Zero", 1, "Nested - inactive"), installed)
            self.assertIn(("parts", 1, "External"), installed)


if __name__ == "__main__":
    unittest.main()
