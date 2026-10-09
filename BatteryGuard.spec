# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['battery_guard_panel.py'],
    pathex=[],
    binaries=[],
    datas=[('app.png', '.'), ('app.ico', '.')],
    # encodings.idna/stringprep 会被 socket.getfqdn() 间接用到，
    # 不显式声明时打包产物可能缺失，导致 HTTP 服务启动即崩
    hiddenimports=['encodings.idna', 'stringprep'],
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
    a.binaries,
    a.datas,
    [],
    name='BatteryGuard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
