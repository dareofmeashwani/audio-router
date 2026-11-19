# -*- mode: python ; coding: utf-8 -*-
# Maintained PyInstaller spec for the macOS .app build.
#
# The key reason this spec exists (rather than building purely from CLI flags):
# capturing audio from the loopback device (BlackHole) counts as microphone
# access under macOS TCC, so the bundle MUST declare NSMicrophoneUsageDescription
# and a stable bundle identifier. Without it a freshly built .app is denied
# microphone access and routes no audio.

a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    # PyObjC CoreAudio switches the system output to BlackHole; AVFoundation
    # requests microphone permission up front. Keep both explicit so they are
    # always collected into the frozen app.
    hiddenimports=['objc', 'Foundation', 'CoreAudio', 'AVFoundation'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioRouter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icons/audio-router.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AudioRouter',
)
app = BUNDLE(
    coll,
    name='AudioRouter.app',
    icon='assets/icons/audio-router.icns',
    bundle_identifier='com.audiorouter.app',
    info_plist={
        'NSMicrophoneUsageDescription':
            'AudioRouter captures the audio playing on this Mac (via the '
            'BlackHole loopback device) and routes it to your selected output '
            'devices.',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.15',
        'CFBundleShortVersionString': '1.0.0',
    },
)
