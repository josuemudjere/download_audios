# Fichier de build PyInstaller commun Mac (.app → .dmg) et Windows (.exe)
#   Mac     : ./build_mac.sh
#   Windows : build_windows.bat  (ou automatiquement via GitHub Actions)
import sys

APP_NAME = "Kacou Audio"
VERSION = "2.1"

a = Analysis(
    ["app.py"],
    datas=[("assets/banner.png", "assets"), ("assets/icon.png", "assets")],
    hiddenimports=["certifi"],
    excludes=["PIL", "numpy", "pytest", "unittest", "pydoc"],
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME,
              console=False, icon="assets/icon.icns")
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon="assets/icon.icns",
        bundle_identifier="local.kacouaudio.app",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="KacouAudio",
              console=False, icon="assets/icon.ico", upx=False)
