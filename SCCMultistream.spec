# PyInstaller build for the desktop app.
#
#   pip install -r requirements-windows.txt pyinstaller
#   python tools/fetch_binaries.py
#   pyinstaller --noconfirm SCCMultistream.spec
#
# Produces dist/SCCMultistream/SCCMultistream.exe (plus its _internal folder,
# which carries the page and bin/: MediaMTX and FFmpeg). The GitHub Actions
# workflow in .github/workflows/build-windows.yml runs this.
import os
import sys

root = SPECPATH
icon = os.path.join(root, "packaging", "icon.ico")

hidden, datas = [], [(os.path.join(root, "web"), "web"), (os.path.join(root, "bin"), "bin")]
if sys.platform == "win32":
    # pywebview's Windows window (Edge WebView2) is loaded at runtime.
    hidden += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]

a = Analysis(
    [os.path.join(root, "SCCMultistream.py")],
    pathex=[root],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SCCMultistream",
    console=False,
    icon=icon if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SCCMultistream")
