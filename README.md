# Healing Gourd Mod Manager

A Windows desktop app for organizing Sekiro mods and deploying them to the
game's `mods` folder.

## Install and run

1. Download `Sekiro Mod Engine from Nexus first`.
2. Download `Healing Gourd Mod Manager Portable.zip on the Release tab`.
3. Extract **all** files from the ZIP into a folder you can write to.
4. Run `Healing Gourd Mod Manager.exe` from that extracted folder.

Python is not required to run the packaged app. Keep the `_internal` folder
beside the executable; do not run the executable from inside the ZIP.

The app stores its settings, mod library, and custom UI assets in:

`%LOCALAPPDATA%\Healing Gourd Mod Manager`

This keeps user data separate from the app files, so replacing or moving the
portable app does not remove your settings or library. When upgrading an older
copy, the app copies existing settings, library files, and custom UI assets into
this location without deleting the originals. To remove the app, close it and
delete the extracted folder. Remove the Local AppData folder only if you also
want to delete the saved settings, library, and custom UI assets.

The Sekiro banner is the default background. Use **Customize UI** to choose a
different PNG or GIF; choose **Clear** to restore the default.

## Import and run mods

- Choose **Import ZIP...** to import a `.zip` mod archive, or **Import folder...**
  for an already-extracted mod. If the ZIP contains one enclosing folder, that
  folder's name is used as the mod name; game content folders such as `parts`
  are not treated as mod names.
- Select a library mod and choose **Enable** to deploy it into Sekiro's `mods`
  folder; choose **Disable** to remove that mod's unchanged deployed files while
  keeping the mod in the library. You can enable multiple mods, and disabled
  mods stay available until you enable them again. **Run Modded** launches
  `sekiro.exe` with only the mods currently marked enabled; selecting a disabled
  library entry does not enable it.
- Mod Engine loads content directly under its configured override folder (for
  example, `mods\parts\...`). A layout such as `mods\Phantom Blade Zero\parts\...`
  is one folder too deep and will be shown as **Nested - inactive**. Select that
  entry and choose **Run Modded** to copy it into the manager library and deploy
  it at the correct level. The original nested folder is kept as a backup;
  existing files are never overwritten, and conflicts are reported.
- A compatible Sekiro mod loader must already be installed in the game folder
  for the game to load mods. This manager deploys mod files and starts the game;
  it does not install a mod loader.

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
builds the app into `dist\Healing Gourd Mod Manager`, and creates the portable
ZIP in `release`. Build intermediates are kept in `build`.

Run the import and launch regression tests with:

```bat
python -m unittest discover -s tests
```
