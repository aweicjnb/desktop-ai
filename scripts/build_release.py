import argparse
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = (3, 13, 15)
PYTHON_SHA256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
RELEASE_NAME = "desktop-ai-v0.3.5-windows-x64.zip"

LAUNCHER = r"""@echo off
setlocal
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%runtime\pythonw.exe" (
    echo Extract the full Desktop AI ZIP before starting the application.
    pause
    exit /b 1
)
set "TCL_LIBRARY=%APP_DIR%runtime\tcl\tcl8.6"
set "TK_LIBRARY=%APP_DIR%runtime\tcl\tk8.6"
if /I "%~1"=="--smoke" goto smoke
start "" /D "%APP_DIR%." "%APP_DIR%runtime\pythonw.exe" "%APP_DIR%DesktopAI.pyw" %*
exit /b %errorlevel%

:smoke
"%APP_DIR%runtime\python.exe" "%APP_DIR%DesktopAI.pyw" %*
exit /b %errorlevel%
"""


def build(runtime_zip, output):
    if sys.version_info[:3] != PYTHON_VERSION or sys.maxsize < 2**32:
        raise RuntimeError("Build with the matching x64 Python 3.13.15 installation, including Tcl/Tk.")
    if sha256(runtime_zip.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise RuntimeError("The embedded Python ZIP does not match the pinned Python.org checksum.")
    output.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.log")
    base = Path(sys.base_prefix)
    with TemporaryDirectory(prefix="desktop-ai-", dir=output) as temporary:
        app = Path(temporary) / "DesktopAI"
        app.mkdir()
        for name in ("DesktopAI.pyw", "README.md"):
            shutil.copy2(PROJECT / name, app / name)
        for name in ("src", "assets", "docs"):
            shutil.copytree(PROJECT / name, app / name, ignore=ignore)
        chatgpt = PROJECT / '.runtime/chatgpt'
        required = ('node.exe', 'client-module.js', 'node_modules/jsdom/lib/api.js')
        if not all((chatgpt / name).is_file() for name in required):
            raise RuntimeError('Run scripts/setup_chatgpt_runtime.py before building the ChatGPT portable package.')
        shutil.copytree(chatgpt, app / '.runtime/chatgpt', ignore=ignore)
        images = PROJECT / '.runtime/images'
        if not (images / 'version.txt').is_file() or not (images / 'PIL/Image.py').is_file():
            raise RuntimeError('Run scripts/setup_image_runtime.py before building the image-capable portable package.')
        shutil.copytree(images, app / '.runtime/images', ignore=ignore)
        (app / 'scripts').mkdir()
        shutil.copy2(PROJECT / 'scripts/setup_chatgpt_runtime.py', app / 'scripts/setup_chatgpt_runtime.py')
        shutil.copy2(PROJECT / 'scripts/setup_image_runtime.py', app / 'scripts/setup_image_runtime.py')
        runtime = app / "runtime"
        with ZipFile(runtime_zip) as archive:
            archive.extractall(runtime)
        for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll"):
            shutil.copy2(base / "DLLs" / name, runtime / name)
        shutil.copytree(base / "Lib/tkinter", runtime / "Lib/tkinter",
                        ignore=shutil.ignore_patterns("__pycache__", "test"))
        for name in ("tcl8.6", "tk8.6"):
            shutil.copytree(base / "tcl" / name, runtime / "tcl" / name,
                            ignore=shutil.ignore_patterns("demos"))
        for source, destination in ((PROJECT / "scripts/licenses/Tcl-LICENSE.txt", "Tcl-LICENSE.txt"),
                                    (PROJECT / "scripts/licenses/Zlib-LICENSE.txt", "Zlib-LICENSE.txt"),
                                    (base / "LICENSE.txt", "Python-install-LICENSE.txt")):
            shutil.copy2(source, runtime / destination)
        (runtime / "python313._pth").write_text("python313.zip\n.\nLib\n", encoding="ascii")
        (app / "Start.cmd").write_text(LAUNCHER, encoding="ascii", newline="\r\n")
        target = output / RELEASE_NAME
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=7) as archive:
            for file in sorted(app.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(app.parent).as_posix())
    return target


def main():
    parser = argparse.ArgumentParser(description="Build the Desktop AI Windows x64 portable package.")
    parser.add_argument("--python-zip", type=Path, required=True,
                        help="Official Python 3.13.15 embeddable x64 ZIP.")
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    options = parser.parse_args()
    print(build(options.python_zip, options.output).resolve())


if __name__ == "__main__":
    main()
