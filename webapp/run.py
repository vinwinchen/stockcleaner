# -*- coding: utf-8 -*-
"""StockCleaner 桌面壳入口。

启动顺序: 起 uvicorn (守护线程) -> pywebview 隐藏窗口加载 http://127.0.0.1:PORT -> 页面 loaded 后显示。
不用 file:// 加载前端: fetch / EventSource(SSE) 在 file:// 下同源不可靠。

用法:
  python run.py                 # 生产: 加载 frontend/dist (需先 npm run build)
  python run.py --dev URL       # 开发: 加载 vite dev server, 默认 http://localhost:5173
  python run.py --no-shell      # 不起窗口, 仅本地服务并用系统浏览器打开
"""

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
# cleaner_core.py 在上一级 (stockcleaner/), 不把它放进 sys.path 就 import 不到
PARENT = os.path.dirname(ROOT)
# 窗口/任务栏图标。不给的话 pywebview 会去 ExtractIconW(sys.executable),
# 于是标题栏上显示的是 pythonw.exe 的图标。
ICON = os.path.join(ROOT, 'assets', 'stockcleaner.ico')
for _p in (ROOT, PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# pythonw.exe (无控制台窗口) 或从计划任务/服务里起时, sys.stdout / sys.stderr 是 None。
# uvicorn 的 ColoredFormatter 构造时第一件事就是 sys.stdout.isatty() -> AttributeError,
# 服务还没监听就退码 1 (实测报 "Unable to configure formatter 'default'")。
# 给它一个 devnull 兜住, 让"带终端启动"和"不带终端启动"走同一条代码路径。
for _name in ('stdout', 'stderr'):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, 'w', encoding='utf-8'))


def serve(port):
    """先绑定 socket 再交给 uvicorn: 消除"先挑随机端口再释放"的 TOCTOU 竞态。"""
    import uvicorn
    from backend.app import app, mount_static
    mount_static(app)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', port or 0))
    sock.set_inheritable(True)
    actual = sock.getsockname()[1]
    config = uvicorn.Config(app, host='127.0.0.1', port=actual, log_level='warning',
                            access_log=False, loop='asyncio')
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, args=([sock],), name='uvicorn', daemon=True).start()
    return server, actual


def reachable(url, timeout=1.0):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:                                       # noqa: BLE001
        return False


def wait_for_api(base, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if reachable(base + '/api/meta', 0.8):
            return True
        time.sleep(0.15)
    return False


# 页面首帧之前, 屏幕上只有壳刷的那块纯色 (WinForms BackColor + WebView2
# DefaultBackgroundColor)。所以这块颜色必须等于页面即将显示的 --bg:
# 不一致就是启动时"黑一下才亮"的闪屏。
#
# 解析顺序照抄 frontend/src/styles.css, 不要在这里发明第二套真值:
# SC_THEME 显式覆盖 -> 系统偏好 -> 亮色兜底 (CSS 的 :root 默认就是亮色)。
THEME_BG = {'light': '#f4f6f8', 'dark': '#0b0d10'}


def resolved_theme():
    pref = (os.environ.get('SC_THEME') or '').strip().lower()
    if pref in THEME_BG:
        return pref
    if sys.platform == 'win32':
        try:
            import winreg
            key = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as h:
                light, _ = winreg.QueryValueEx(h, 'AppsUseLightTheme')
            return 'light' if light else 'dark'
        except OSError:                                   # 键/值读不到 -> 跟 CSS 默认一致
            return 'light'
    return 'light'                                        # 非 Windows 暂不探测


def shell_background():
    return THEME_BG[resolved_theme()]


def pick_screen(webview):
    """把主屏交给 create_window(screen=...), 让它每次启动都落在屏幕正中。

    不写这行时 WinForms 走默认 StartPosition, 实测并不居中
    (1440x900 在 2560x1440 上落在 260,260, 而正中应是 560,270)。
    居中公式由 pywebview 自己算: 它定位和定尺寸用的是同一个 scale,
    在壳里另算一套只会在缩放屏上错位。

    `webview.screens` 是 module_property (Proxy), 按属性取值, 不能加括号调用。
    """
    try:
        screens = webview.screens
    except Exception as e:                              # noqa: BLE001  拿不到就不指定, 但要说一声
        print(f'[StockCleaner] 取屏幕信息失败, 窗口位置退回默认: {type(e).__name__}: {e}',
              file=sys.stderr)
        return None
    return screens[0] if screens else None


# 任务栏身份。试过把 AppUserModelID 直接写成 Start Menu 那条 .lnk 的路径 (想让 shell
# 照链上的图标/标签认这个 app), 实测不成立: API 接受 (S_OK), 但任务栏按钮的可访问名
# 仍是 "Python - 1 个运行窗口" —— Explorer 认的是**进程镜像路径** (venv 跳板背后是
# C:\Python314\pythonw.exe), 所以从"运行中的按钮"固定必然生成一条指向 Python 的坏链。
# 那条链的图标/参数由 make_shortcut.ps1 事后修, 这里只保留普通串。
FALLBACK_AUMID = 'StockCleaner.Desktop'


def set_taskbar_identity():
    """设定进程的 AppUserModelID, 必须在建窗之前调用。

    不设的话这个进程就是"pythonw.exe 的一个实例", 任务栏按钮沿用 pythonw 的图标 ——
    于是标题栏是数据库、任务栏还是 Python (实测如此)。
    """
    if sys.platform != 'win32':
        return
    import ctypes
    hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(FALLBACK_AUMID)
    if hr != 0:
        print(f'[StockCleaner] AppUserModelID 设置失败 (0x{hr & 0xFFFFFFFF:08X}), '
              f'任务栏图标可能仍是 Python', file=sys.stderr)


class DialogBridge:
    """系统原生文件/文件夹对话框。

    有 pywebview 窗口时用窗口的对话框 (对话框归属该窗口, 不会被别的程序盖住);
    无窗口 (--no-shell / --dev) 时退回 tkinter 原生对话框。两条路都是真原生对话框,
    不是"让用户手打路径"。
    """

    FILE_TYPES = ('数据文件 (*.csv *.tsv *.txt *.xlsx *.xls)', '所有文件 (*.*)')

    def __init__(self, window=None):
        self.window = window

    def pick(self, kind):
        if self.window is not None:
            return self._pick_webview(kind)
        return self._pick_tk(kind)

    def _pick_webview(self, kind):
        import webview
        if kind == 'folder':
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=self.FILE_TYPES)
        if not result:
            return []
        if isinstance(result, (list, tuple)):
            return [os.path.abspath(str(p)) for p in result]
        return [os.path.abspath(str(result))]

    def _pick_tk(self, kind):
        import tkinter.filedialog as fd
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            if kind == 'folder':
                path = fd.askdirectory(parent=root)
                return [os.path.abspath(path)] if path else []
            files = fd.askopenfilenames(
                parent=root,
                filetypes=[('数据文件', '*.csv *.tsv *.txt *.xlsx *.xls'), ('所有文件', '*.*')])
            return [os.path.abspath(f) for f in files] if files else []
        finally:
            root.destroy()


def main():
    parser = argparse.ArgumentParser(description='StockCleaner 本地服务 + 桌面壳')
    parser.add_argument('--dev', nargs='?', const='http://localhost:5173', default=None,
                        metavar='URL', help='加载 vite dev server')
    parser.add_argument('--no-shell', action='store_true', help='不开窗口, 用系统浏览器')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()

    try:
        _server, port = serve(args.port)
    except OSError as exc:
        print(f'[StockCleaner] 端口无法绑定 ({args.port or "自动"}): {exc}', file=sys.stderr)
        sys.exit(1)
    base = f'http://127.0.0.1:{port}'
    if not wait_for_api(base):
        print('[StockCleaner] 本地服务未能就绪, 请看上方报错。', file=sys.stderr)
        sys.exit(1)

    from backend.app import bind_native_bridge, allow_local_origin

    url = base
    if args.dev:
        # vite dev server 的请求经代理打到后端, Origin 是 dev 端口而非服务端口,
        # 必须登记为明确放行源, 否则会 403 (见 app.py 的 LocalOnlyGuard)
        allow_local_origin(args.dev)
        if not reachable(args.dev, 1.5):
            print(f'[StockCleaner] 开发服务器没在跑: {args.dev}\n'
                  f'              先在 frontend/ 执行 npm install && npm run dev',
                  file=sys.stderr)
        else:
            url = args.dev

    if args.no_shell:
        bind_native_bridge(DialogBridge(None))
        webbrowser.open(url)
        print(f'[StockCleaner] 服务: {base}\n[StockCleaner] 前端: {url}\n'
              f'[StockCleaner] Ctrl+C 退出')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
        return

    import webview
    set_taskbar_identity()
    # background_color 用 shell_background() 而不是写死的深色: 首帧之前屏幕上就是这块颜色,
    # 它和页面 --bg 不一致就会"黑一下才亮"。
    # hidden=True + loaded 再 show: 连"只有一块纯色、还没有内容"的那段时间都不露出来。
    window = webview.create_window('StockCleaner · 金融数据清洗', url=url,
                                   width=1440, height=900, min_size=(1100, 700),
                                   resizable=True, background_color=shell_background(),
                                   text_select=True, hidden=True,
                                   screen=pick_screen(webview))
    bridge = DialogBridge(window)
    bind_native_bridge(bridge)

    revealed = threading.Event()
    reveal_lock = threading.Lock()

    def reveal(*_args):
        with reveal_lock:
            if revealed.is_set():
                return                  # loaded 可能重复触发 (刷新/重定向), 只显示一次
            revealed.set()
        window.show()

    window.events.loaded += reveal

    def reveal_watchdog():
        # loaded 不来 (前端挂了 / 导航被吞) 也不能让窗口永远看不见
        if not revealed.wait(8.0):
            print('[StockCleaner] 页面 loaded 未触发, 兜底显示窗口。', file=sys.stderr)
            reveal()

    threading.Thread(target=reveal_watchdog, name='reveal-watchdog', daemon=True).start()

    webview.start(icon=ICON, debug=bool(os.environ.get('SC_DEBUG')))


if __name__ == '__main__':
    main()
