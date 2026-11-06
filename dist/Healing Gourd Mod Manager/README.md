# Healing Gourd Mod Manager

A Windows desktop app for organizing Sekiro mods and deploying them to the
game's `mods` folder.

## Install and run

1. Download `Sekiro Mod Engine from Nexus first`.
2. Download `Healing Gourd Mod Manager.zip` from the GitHub Releases page.
3. Extract **all** files from the ZIP into a folder you can write to.
4. Run `Healing Gourd Mod Manager.exe` from that extracted folder.

Python is not required to run the packaged app. Keep the `_internal` folder
beside the executable; do not run the executable from inside the ZIP.

The app stores its settings, mod library, and custom UI assets in:

`%LOCALAPPDATA%\Healing Gourd Mod Manager`

Save backups and save profiles are also kept under this app-data folder, in
`SaveBackups` and `SaveProfiles`. They are separate from the game saves.

This keeps user data separate from the app files, so replacing or moving the
portable app does not remove your settings or library. When upgrading an older
copy, the app copies existing settings, library files, and custom UI assets into
this location without deleting the originals. To remove the app, close it and
delete the extracted folder. Remove the Local AppData folder only if you also
want to delete the saved settings, library, and custom UI assets.

The Sekiro banner is the default background. Use **Customize UI** to choose a
different PNG or GIF; choose **Clear** to restore the default.

The app's left sidebar switches between the **Mod Manager** and **Save
Manager** workspaces. **Run Modded** and **Customize UI** remain available at
the bottom of the sidebar.

## Import and run mods

- Choose **Import ZIP/7z...** to import a `.zip` or `.7z` archive, or
  **Import folder...** for an already-extracted mod. If an archive contains one
  enclosing folder, that folder's name is used as the mod name; game content
  folders such as `parts` are not treated as mod names. Password-protected
  archives are not supported.
- Select a library mod and choose **Enable** to deploy it into Sekiro's `mods`
  folder; choose **Disable** to remove that mod's unchanged deployed files while
  keeping the mod in the library. You can enable multiple mods, and disabled
  mods stay available until you enable them again. **Run Modded** launches
  `sekiro.exe` with only the mods currently marked enabled; selecting a disabled
  library entry does not enable it.
- If an enabled mod and the mod being enabled contain the same relative file
  paths, the manager lists those conflicts and asks before proceeding. The newly
  enabled mod takes priority only for the overlapping paths; non-overlapping
  files from both mods remain active. Disabling the later mod restores the
  earlier enabled mod's version. This is file replacement, not a merge, so
  overhaul mods can still be incompatible even when the file conflicts are
  resolved.
- Mod Engine loads content directly under its configured override folder (for
  example, `mods\parts\...`). A layout such as `mods\Phantom Blade Zero\parts\...`
  is one folder too deep and will be shown as **Nested - inactive**. Select that
  entry and choose **Run Modded** to copy it into the manager library and deploy
  it at the correct level. The original nested folder is kept as a backup;
  conflicts with unmanaged game files are not overwritten.
- A compatible Sekiro mod loader must already be installed in the game folder
  for the game to load mods. This manager deploys mod files and starts the game;
  it does not install a mod loader.

## Save manager

- Open the **Save Manager** tab in the main window and confirm the save folder. Sekiro commonly stores
  saves in `%APPDATA%\Sekiro\<numeric Steam ID>\`, with `S0000.sl2` inside;
  auto-detection only selects folders matching that pattern. Verify the folder
  on your PC before relying on it.
- The current save, selected backups, and save profiles show each populated
  Sekiro slot's journey/NG+ level, playtime, Vitality, and Attack Power. This is
  a read-only summary; files with an unknown layout or invalid checksum are
  shown as unavailable rather than guessed. The field layout is cross-checked
  against the open-source [sl2-analyzer](https://github.com/darthdemono/sl2-analyzer)
  Sekiro format documentation.
- **Run Modded** creates a timestamped, verified backup of the entire selected
  save folder before launching. If the save folder is not configured, Run
  Modded asks you to configure it first.
- Use **Backup now** to create a labeled backup. Retention defaults to the
  newest 10 backups and can be set from 1 to 100 in Save Manager.
- **Restore selected...** asks for confirmation, refuses to run while
  `sekiro.exe` is open, backs up the current save first, verifies the chosen
  backup, then replaces the whole save folder. Steam Cloud may overwrite a
  restored save; disable Cloud for Sekiro or restore while Steam is closed.
- To keep playthroughs separate, create a profile from the current save (for
  example, `Vanilla`), then create another (for example, `Randomizer`). Switching
  profiles backs up the current folder and saves it to the active profile before
  copying in the selected profile. Close Sekiro before switching. Profiles
  contain independent copies of the complete save folder.

## Build from source

On Windows, install Python and run:

```bat
build.bat
```

To select a specific Python interpreter:

```bat
build.bat "C:\Path\To\python.exe"
```

The script installs the pinned build dependency in an isolated environment,
builds the app into `dist\Healing Gourd Mod Manager`, and creates
`release\Healing Gourd Mod Manager.zip`. Build intermediates are kept in
`build`.

Run the import and launch regression tests with:

```bat
python -m unittest discover -s tests
```

The portable app includes the 7-Zip Extra console helper for `.7z` extraction.
Its third-party license is included at `Assets\7zip-license.txt` inside the
application bundle. The helper is distributed under the GNU LGPL, with some
components under BSD licenses.
