# -*- mode: python ; coding: utf-8 -*-
"""StockCleaner 桌面版打包配置 (PyInstaller, onedir + 无控制台窗口)。

用法 (在 webapp/ 下):
  .venv/Scripts/pyinstaller.exe StockCleaner.spec --noconfirm
产物: dist/StockCleaner/StockCleaner.exe (整体目录随包分发)

打包要点 (为什么不是一条 pyinstaller 命令行):
- cleaner_core.py 在 webapp 的上一级, 运行期靠 run.py 的 sys.path 注入;
  分析期必须用 pathex 提前登记, 否则 modulefinder 找不到它。
- frontend/dist 与 assets/stockcleaner.ico 是运行期数据:
  app.py 的 DIST_DIR 与 run.py 的 ICON 都以 __file__ 推导,
  冻结后落在 _internal/ 下, datas 的目标路径必须与之吻合
  (frontend/dist -> frontend/dist, assets/stockcleaner.ico -> assets)。
- uvicorn 对 loop/protocol/lifespan 是按字符串动态导入的,
  hooks-contrib 的 uvicorn hook 覆盖了主要路径, 这里再显式钉一份,
  避免 hooks-contrib 未来某版漏收时静默起不来服务。
- 无控制台 (console=False) 时 sys.stdout/stderr 是 None,
  run.py 顶部已有 devnull 兜底, 与 README 的 pythonw 行为同一条代码路径。
"""

import os

SPEC_DIR = os.path.abspath(SPECPATH)          # webapp/
PROJECT_ROOT = os.path.dirname(SPEC_DIR)      # stockcleaner/ (cleaner_core.py 所在)

a = Analysis(
    [os.path.join(SPEC_DIR, 'run.py')],
    pathex=[SPEC_DIR, PROJECT_ROOT],
    binaries=[],
    datas=[
        # 前端产物: DIST_DIR 冻结后解析为 _internal/frontend/dist
        (os.path.join(SPEC_DIR, 'frontend', 'dist'), 'frontend/dist'),
        # 桌面壳窗口/任务栏图标: ICON 冻结后解析为 _internal/assets/stockcleaner.ico
        (os.path.join(SPEC_DIR, 'assets', 'stockcleaner.ico'), 'assets'),
    ],
    hiddenimports=[
        'cleaner_core',
        # uvicorn 按字符串动态导入的部分
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # pywebview Windows 后端 (WinForms 经 pythonnet 走 WebView2)
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
    ],
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
    name='StockCleaner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(SPEC_DIR, 'assets', 'stockcleaner.ico'),
    version=os.path.join(SPEC_DIR, 'pack', 'version_info.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='StockCleaner',
)
