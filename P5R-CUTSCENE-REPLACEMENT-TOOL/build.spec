# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None
ROOT = Path.cwd()

a = Analysis(
    ['p5r_gui.py'],
    pathex=[str(ROOT)],
    binaries=[
        (r'C:\Users\凯梦\AppData\Local\Programs\Python\Python310\lib\site-packages\cricodecs\__init__.cp310-win_amd64.pyd', 'cricodecs'),
    ],
    datas=[
        ('assets', 'assets'),
        ('bin', 'bin'),
        ('使用说明.md', '.'),
    ],
    hiddenimports=[
        'cricodecs',
        'cricodecs.cpk',
        'cricodecs.usm',
        'cricodecs.hca',
        'cricodecs.adx',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    exclude_binaries=True,
    name='P5R过场动画替换工具',
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
    icon='assets/app_icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='P5R_Cutscene_Tool',
)
