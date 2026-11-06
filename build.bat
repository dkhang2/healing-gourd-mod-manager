@echo off
setlocal
pushd "%~dp0"

set "PYTHON=%~1"
if not defined PYTHON set "PYTHON=python"
"%PYTHON%" --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Pass its path as an argument or add it to PATH.
    popd
    exit /b 1
)

set "BUILD_ENV=%TEMP%\healing-gourd-build-env"
if not exist "%BUILD_ENV%\Scripts\python.exe" (
    "%PYTHON%" -m venv "%BUILD_ENV%"
    if errorlevel 1 (
        echo Could not create the isolated build environment.
        popd
        exit /b 1
    )
)

set "BUILD_PYTHON=%BUILD_ENV%\Scripts\python.exe"
"%BUILD_PYTHON%" -m pip install --requirement "%~dp0requirements-build.txt"
if errorlevel 1 (
    echo Could not install the pinned build dependencies.
    popd
    exit /b 1
)

set "BUILD_ROOT=%~dp0build"
set "PYINSTALLER_DIST=%BUILD_ROOT%\pyinstaller-dist"
set "PYINSTALLER_WORK=%BUILD_ROOT%\pyinstaller-work"
set "PYINSTALLER_SPEC=%BUILD_ROOT%\pyinstaller-spec"
set "PACKAGE_SOURCE=%PYINSTALLER_DIST%\Healing Gourd Mod Manager"
set "APP_OUTPUT=%~dp0dist\Healing Gourd Mod Manager"
set "RELEASE_DIR=%~dp0release"
set "RELEASE_ZIP=%RELEASE_DIR%\Healing Gourd Mod Manager.zip"
set "RELEASE_TEMP=%RELEASE_DIR%\Healing Gourd Mod Manager.tmp.zip"
set "ICON=%~dp0Assets\healing-gourd.ico"
set "BACKGROUND=%~dp0Assets\sekiro-background.png"
set "SEVEN_ZIP=%~dp0Assets\7za.exe"
set "SEVEN_ZIP_LICENSE=%~dp0Assets\7zip-license.txt"

if not exist "%ICON%" (
    echo Could not find the default app icon: "%ICON%"
    popd
    exit /b 1
)
if not exist "%BACKGROUND%" (
    echo Could not find the default background image: "%BACKGROUND%"
    popd
    exit /b 1
)
if not exist "%SEVEN_ZIP%" (
    echo Could not find the bundled 7-Zip helper: "%SEVEN_ZIP%"
    popd
    exit /b 1
)
if not exist "%SEVEN_ZIP_LICENSE%" (
    echo Could not find the 7-Zip license: "%SEVEN_ZIP_LICENSE%"
    popd
    exit /b 1
)

if exist "%PYINSTALLER_DIST%" rmdir /s /q "%PYINSTALLER_DIST%"
if errorlevel 1 (
    echo Could not clean the previous PyInstaller output.
    popd
    exit /b 1
)
if exist "%PYINSTALLER_WORK%" rmdir /s /q "%PYINSTALLER_WORK%"
if errorlevel 1 (
    echo Could not clean the previous PyInstaller work files.
    popd
    exit /b 1
)
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean --onedir --windowed --optimize 2 ^
    --icon "%ICON%" ^
    --add-data "%~dp0Assets\healing-gourd.ico;Assets" ^
    --add-data "%~dp0Assets\healing-gourd.png;Assets" ^
    --add-data "%BACKGROUND%;Assets" ^
    --add-data "%SEVEN_ZIP%;Assets" ^
    --add-data "%SEVEN_ZIP_LICENSE%;Assets" ^
    --name "Healing Gourd Mod Manager" ^
    --distpath "%PYINSTALLER_DIST%" ^
    --workpath "%PYINSTALLER_WORK%" ^
    --specpath "%PYINSTALLER_SPEC%" ^
    main.py
if errorlevel 1 (
    echo PyInstaller build failed.
    popd
    exit /b 1
)

copy /Y "%~dp0README.md" "%PACKAGE_SOURCE%\README.md" >nul
if errorlevel 1 (
    echo Could not include the installation guide.
    popd
    exit /b 1
)

if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
if errorlevel 1 (
    echo Could not create the release folder.
    popd
    exit /b 1
)
"%BUILD_PYTHON%" -c "import pathlib, zipfile; source = pathlib.Path(r'%PACKAGE_SOURCE%'); destination = pathlib.Path(r'%RELEASE_TEMP%'); archive = zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6); [archive.write(path, path.relative_to(source).as_posix()) for path in source.rglob('*') if path.is_file()]; archive.close()"
if errorlevel 1 (
    echo Could not create the portable ZIP with Python.
    popd
    exit /b 1
)
if not exist "%RELEASE_TEMP%" (
    echo Portable ZIP was not created.
    popd
    exit /b 1
)
move /Y "%RELEASE_TEMP%" "%RELEASE_ZIP%" >nul
if errorlevel 1 (
    echo Could not replace the previous portable ZIP.
    popd
    exit /b 1
)

if not exist "%~dp0dist" mkdir "%~dp0dist"
if errorlevel 1 (
    echo Could not create the local app output folder.
    popd
    exit /b 1
)
if exist "%APP_OUTPUT%" rmdir /s /q "%APP_OUTPUT%"
if errorlevel 1 (
    echo Could not clean the previous packaged app output.
    popd
    exit /b 1
)
xcopy "%PACKAGE_SOURCE%\*" "%APP_OUTPUT%\" /E /I /Y >nul
if errorlevel 1 (
    echo Could not stage the local app.
    popd
    exit /b 1
)

if exist "%~dp0dist\Healing Gourd Mod Manager.exe" del /q "%~dp0dist\Healing Gourd Mod Manager.exe"
if errorlevel 1 (
    echo Could not remove the obsolete single-file executable.
    popd
    exit /b 1
)

echo Built local app: dist\Healing Gourd Mod Manager\Healing Gourd Mod Manager.exe
echo Built portable package: release\Healing Gourd Mod Manager.zip
popd
