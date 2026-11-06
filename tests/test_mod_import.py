import tempfile
import subprocess
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

    def make_7z_archive(self, name: str, files: dict[str, bytes]) -> Path:
        source = self.root / f"{Path(name).stem}-source"
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        archive = self.root / name
        helper = main.bundled_asset_path("7za.exe")
        subprocess.run(
            [str(helper), "a", "-t7z", str(archive), *files],
            cwd=source,
            check=True,
            capture_output=True,
        )
        return archive

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

    def test_imports_7z_and_uses_its_enclosing_folder_as_the_mod_name(self) -> None:
        archive = self.make_7z_archive(
            "download.7z",
            {"Phantom Blade Zero/parts/chr/model.bin": b"model"},
        )

        imported_name = main.import_mod(str(archive))

        self.assertEqual(imported_name, "Phantom Blade Zero")
        self.assertEqual(
            (self.library / imported_name / "parts/chr/model.bin").read_bytes(),
            b"model",
        )

    def test_7z_import_keeps_root_game_content_folder(self) -> None:
        archive = self.make_7z_archive(
            "parts-mod.7z",
            {"parts/chr/model.bin": b"model"},
        )

        imported_name = main.import_mod(str(archive))

        self.assertEqual(imported_name, "parts-mod")
        self.assertEqual(
            (self.library / imported_name / "parts/chr/model.bin").read_bytes(),
            b"model",
        )

    def test_7z_archive_path_validation_rejects_traversal_and_reserved_names(self) -> None:
        for path in ("../outside.txt", "C:/outside.txt", "parts/CON.txt"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "Unsafe path"):
                    main._validate_archive_member_path(path, "7z")

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

    def test_launching_does_not_implicitly_enable_disabled_mods(self) -> None:
        game_dir = Path("C:/Sekiro")
        calls: list[str] = []
        state = {"mods": {"Other Mod": {"parts/chr/other.bin": "hash"}}}

        with (
            mock.patch.object(main, "load_state", side_effect=lambda: state),
            mock.patch.object(main, "enable_mod") as enable_mod,
            mock.patch.object(main, "launch_game", side_effect=lambda _game_dir: calls.append("launch")),
        ):
            enabled_count = main.launch_modded_game(game_dir)

        self.assertEqual(enabled_count, 1)
        self.assertEqual(calls, ["launch"])
        self.assertNotIn("Test Mod", state["mods"])
        enable_mod.assert_not_called()

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

    def test_conflicting_enabled_mods_stack_and_restore_previous_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "Sekiro"
            library = root / "Library"
            first_mod = library / "Overhaul A"
            second_mod = library / "Overhaul B"
            relative = Path("parts") / "chr" / "shared.bin"
            for folder in (first_mod, second_mod):
                (folder / relative).parent.mkdir(parents=True)
            (first_mod / relative).write_bytes(b"first")
            (second_mod / relative).write_bytes(b"second")
            first_only = first_mod / "parts" / "chr" / "first-only.bin"
            second_only = second_mod / "parts" / "chr" / "second-only.bin"
            first_only.write_bytes(b"first only")
            second_only.write_bytes(b"second only")
            state = {
                "version": 1,
                "game_dir": str(game_dir),
                "mods": {},
                "display_names": {},
                "appearance": {"background_image": None, "app_icon": None},
            }
            destination = game_dir / "mods" / relative

            with (
                mock.patch.object(main, "LIBRARY", library),
                mock.patch.object(main, "load_state", side_effect=lambda: state),
                mock.patch.object(main, "save_state"),
                mock.patch.object(main, "get_game_dir", return_value=game_dir),
            ):
                main.enable_mod("Overhaul A")

                self.assertEqual(
                    main.find_mod_conflicts("Overhaul B", state),
                    [("parts/chr/shared.bin", ["Overhaul A"])],
                )
                with self.assertRaisesRegex(ValueError, "Mod file conflicts detected"):
                    main.enable_mod("Overhaul B")
                self.assertEqual(destination.read_bytes(), b"first")
                self.assertNotIn("Overhaul B", state["mods"])

                main.enable_mod("Overhaul B", allow_conflicts=True)
                self.assertEqual(destination.read_bytes(), b"second")
                self.assertTrue((game_dir / "mods" / second_only.relative_to(second_mod)).is_file())
                main.disable_mod("Overhaul B")
                self.assertEqual(destination.read_bytes(), b"first")
                self.assertFalse((game_dir / "mods" / second_only.relative_to(second_mod)).exists())
                self.assertTrue((game_dir / "mods" / first_only.relative_to(first_mod)).is_file())
                self.assertIn("Overhaul A", state["mods"])

                main.enable_mod("Overhaul B", allow_conflicts=True)
                main.disable_mod("Overhaul A")
                self.assertEqual(destination.read_bytes(), b"second")
                self.assertIn("Overhaul B", state["mods"])
                main.disable_mod("Overhaul B")

            self.assertFalse(destination.exists())
            self.assertEqual(state["mods"], {})

    def test_enable_never_overwrites_an_unmanaged_game_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "Sekiro"
            library = root / "Library"
            mod = library / "Overhaul"
            relative = Path("parts") / "chr" / "existing.bin"
            (mod / relative).parent.mkdir(parents=True)
            (mod / relative).write_bytes(b"mod")
            destination = game_dir / "mods" / relative
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"external")
            state = {
                "version": 1,
                "game_dir": str(game_dir),
                "mods": {},
                "display_names": {},
                "appearance": {"background_image": None, "app_icon": None},
            }

            with (
                mock.patch.object(main, "LIBRARY", library),
                mock.patch.object(main, "load_state", return_value=state),
                mock.patch.object(main, "get_game_dir", return_value=game_dir),
            ):
                with self.assertRaisesRegex(ValueError, "Deployment conflict"):
                    main.enable_mod("Overhaul", allow_conflicts=True)

            self.assertEqual(destination.read_bytes(), b"external")
            self.assertEqual(state["mods"], {})

    def test_enable_rolls_back_files_and_state_when_saving_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_dir = root / "Sekiro"
            library = root / "Library"
            mod = library / "Test Mod"
            relative = Path("parts") / "chr" / "test.bin"
            (mod / relative).parent.mkdir(parents=True)
            (mod / relative).write_bytes(b"mod")
            state = {
                "version": 1,
                "game_dir": str(game_dir),
                "mods": {},
                "display_names": {},
                "appearance": {"background_image": None, "app_icon": None},
            }

            with (
                mock.patch.object(main, "LIBRARY", library),
                mock.patch.object(main, "load_state", return_value=state),
                mock.patch.object(main, "save_state", side_effect=OSError("disk full")),
                mock.patch.object(main, "get_game_dir", return_value=game_dir),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    main.enable_mod("Test Mod")

            self.assertFalse((game_dir / "mods" / relative).exists())
            self.assertEqual(state["mods"], {})

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


class BackgroundImageTests(unittest.TestCase):
    def test_uses_bundled_background_when_no_custom_image_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle_dir = Path(temporary)
            default_image = bundle_dir / "Assets" / main.DEFAULT_BACKGROUND_NAME
            default_image.parent.mkdir()
            default_image.write_bytes(b"background")

            with mock.patch.object(main.sys, "_MEIPASS", str(bundle_dir), create=True):
                self.assertEqual(main.resolve_background_image_path(None), default_image)

    def test_custom_background_overrides_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom_image = root / "Assets" / "background.png"
            custom_image.parent.mkdir()
            custom_image.write_bytes(b"custom")
            default_image = root / "bundle" / "Assets" / main.DEFAULT_BACKGROUND_NAME
            default_image.parent.mkdir(parents=True)
            default_image.write_bytes(b"default")

            with (
                mock.patch.object(main, "ASSETS", custom_image.parent),
                mock.patch.object(main.sys, "_MEIPASS", str(root / "bundle"), create=True),
            ):
                self.assertEqual(main.resolve_background_image_path("background.png"), custom_image)

    def test_missing_custom_background_does_not_replace_it_with_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_image = root / "Assets" / main.DEFAULT_BACKGROUND_NAME
            default_image.parent.mkdir()
            default_image.write_bytes(b"default")

            with (
                mock.patch.object(main, "ASSETS", root / "custom-assets"),
                mock.patch.object(main.sys, "_MEIPASS", str(root), create=True),
            ):
                self.assertIsNone(main.resolve_background_image_path("missing.png"))


if __name__ == "__main__":
    unittest.main()
