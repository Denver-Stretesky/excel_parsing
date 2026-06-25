# PyInstaller spec for SupplierPipeline desktop app.
#
# Build:
#   uv run pyinstaller app.spec --noconfirm
#
# Output:
#   dist/SupplierPipeline.app   (macOS .app bundle — distribute this)
#   dist/SupplierPipeline/      (unbundled onedir layout — ignore for distribution)
#
# Notes:
# - onedir + BUNDLE: standard PyInstaller pattern for macOS .app, future-proof
#   (onefile + .app is deprecated and slated for removal in v7).
# - --windowed (console=False): no terminal pops up.
# - Bundled data files (ExtractSpec.txt, spec_schema.json) are loaded via
#   sys._MEIPASS — see _bundle_dir() in supplier_pipeline.py.
# - User-writeable output goes to ~/Documents/SupplierPipeline/.

import sys
sys.path.insert(0, SPECPATH)  # so we can import sibling modules like _version

from PyInstaller.utils.hooks import collect_submodules

from _version import __version__

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ExtractSpec.txt', '.'),
        ('spec_schema.json', '.'),
    ],
    hiddenimports=[
        'supplier_pipeline',
        'gemini_csv_to_specs',
        'spec_cleanup',
        *collect_submodules('google.genai'),
        *collect_submodules('customtkinter'),
        *collect_submodules('keyring'),         # backends are loaded lazily
        *collect_submodules('keyring.backends'),
        *collect_submodules('jsonschema'),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't use Anthropic in the GUI app; drop it to save bundle size.
        'anthropic',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # onedir: binaries are collected separately
    name='SupplierPipeline',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # --windowed
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SupplierPipeline',
)

app = BUNDLE(
    coll,
    name='SupplierPipeline.app',
    icon=None,
    bundle_identifier='com.cerve.supplierpipeline',
    info_plist={
        'NSHighResolutionCapable': 'True',
        'CFBundleShortVersionString': __version__,
        'CFBundleVersion': __version__,
        'LSMinimumSystemVersion': '11.0',
    },
)
