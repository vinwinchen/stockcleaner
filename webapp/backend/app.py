# -*- coding: utf-8 -*-
"""FastAPI 层: 前端与 cleaner_core 之间唯一的通道。

只绑 127.0.0.1, 不对外提供服务。所有耗时操作 (pandas/文件 IO) 走线程池,
不阻塞事件循环; SSE 事件带 seq + 缓冲, 断线重连可补齐进度。
"""

import asyncio
import json
import os
import queue
import re
import sys
import threading
import tempfile
import time
import urllib.parse
from collections import OrderedDict

from fastapi import FastAPI, Request
from starlette.datastructures import UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import core
from .jobs import registry

APP_VERSION = '2.2.5'
# 单次拖拽上传的总量上限。没有上限时, 一次拖进来的东西可以无限写满系统临时目录
# (落盘发生在任何一个字节被解析之前), 而本机进程面本来就无鉴权。
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
_WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} \
                | {f'LPT{i}' for i in range(1, 10)}


class _OverQuota(Exception):
    """上传超出单次配额 (只用于跳出分块写循环, 不外泄给调用方)。"""
DIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'frontend', 'dist')

app = FastAPI(title='StockCleaner', version=APP_VERSION)

# 前端全走相对路径 (生产同源, --dev 走 vite proxy), CORS 从来不需要;
# 曾经的 allow_origins=['*'] 是纯多余暴露面: 任意网页都能跨站读 /api/preview
# (本地文件内容外泄)、POST /api/run (任意目录写)。这里换成本机守卫:
# Host 必须是回环地址 (同时挡掉 DNS rebinding); 带 Origin 时要求**同源**
# (Origin 端口 == 服务监听端口) 或已登记的明确放行源 (dev, 见 allow_local_origin)。
_LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
# 需要精确放行的额外本机来源 (只用于 `--dev` 的 vite dev server)。
# 生产同源请求由下面的"端口一致"规则放行, 不需要进这份名单。
_EXTRA_ALLOWED_ORIGINS: 'set[str]' = set()


def allow_local_origin(origin):
    """放行一个本机来源 (如 `--dev` 的 vite dev server), 并补上它的兄弟回环主机名。

    只登记 host:port, 不登记路径; 与生产同源规则互不干扰。
    """
    origin = (origin or '').strip()
    if not origin:
        return
    if '://' not in origin:
        origin = 'http://' + origin
    scheme, _, rest = origin.rpartition('://')
    hostport, _, _path = rest.partition('/')
    hostport = hostport.rstrip('/')
    _EXTRA_ALLOWED_ORIGINS.add(f'{scheme}://{hostport}'.lower())
    host, _, port = hostport.rpartition(':')
    bare = host.strip('[]').lower()
    if bare in ('localhost', '127.0.0.1') and port:
        alt = '127.0.0.1' if bare == 'localhost' else 'localhost'
        _EXTRA_ALLOWED_ORIGINS.add(f'{scheme}://{alt}:{port}'.lower())


def _origin_parts(origin):
    """把 Origin 拆成 (scheme, 主机名, 端口); 形状不对就一律 None (不放行)。

    整个解析都在 try 里: 畸形 Origin 不能让 parts.port 的 ValueError 冒出守卫 (会变 500)。
    这里坚持"整串 Origin 形状"而不只是取 host —— 只比 host+port 的话,
    `http://evil.com@127.0.0.1:8731` 会被 urlsplit 解析成 host=127.0.0.1 而放行,
    `http://127.0.0.1:08731` 这种补零端口也会被判成同端口。浏览器送不出这种头,
    但守卫不该依赖"客户端诚实"。
    """
    try:
        parts = urllib.parse.urlsplit(origin)
        host = (parts.hostname or '').strip('[]').lower()
        port = parts.port
    except ValueError:
        return None
    netloc = parts.netloc or ''
    if not host or '@' in netloc or parts.path not in ('', '/') \
            or parts.query or parts.fragment:
        return None
    # 端口写成 08750 这类非规范形式时, urlsplit 的 .port 会把它归一成 8750,
    # "补零端口"就这样伪装成了同端口。浏览器送不出这种头, 但守卫不该依赖客户端诚实。
    tail = netloc.rsplit(':', 1)
    if len(tail) == 2 and ']' not in tail[0]:
        raw_port = tail[1]
        if not (raw_port.isdigit() and (raw_port == '0' or not raw_port.startswith('0'))):
            return None
    return parts.scheme.lower(), host, port


def _same_service_origin(origin, scope, host):
    """同源判定: Origin 的主机名是本机, 且端口等于服务实际监听的端口。

    端口从 scope['server'] 取 (uvicorn 的真实绑定地址), 不信任 Host 头 ——
    旧实现只校验 Origin 的主机名是本机, 结果任一同机 localhost 端口的页面都能
    打到本 API (配合 request.json() 无视 Content-Type, 用 text/plain 的 CORS
    简单请求即可绕过预检, 触发写副作用)。这里把"本机"收紧成"同源 + 明确放行"。
    比对用的是重新拼出来的 scheme://host:port, 不是原串: 补零端口、大小写、
    尾斜杠这类差异都不该换来一个放行。
    """
    parts = _origin_parts(origin)
    if parts is None:
        return False
    scheme, ohost, oport = parts
    if ohost not in _LOCAL_HOSTS:
        return False
    rebuilt = (f'{scheme}://{ohost}:{oport}' if oport is not None
               else f'{scheme}://{ohost}').lower()
    if rebuilt in _EXTRA_ALLOWED_ORIGINS:
        return True
    if scheme != 'http':
        return False                       # 本服务只走 http, 不做跨方案"同源"
    sport = None
    server = scope.get('server') or ()
    if len(server) > 1:
        sport = server[1]
    if sport is None:
        # 兜底: 从 Host 头推断 (uvicorn 直连时 scope.server 一般都有)
        _h = host.rsplit(':', 1)
        if len(_h) == 2 and _h[1].isdigit():
            sport = int(_h[1])
    if oport is not None and sport is not None and oport == sport:
        return True
    # 无端口 Origin (默认端口) 仅在服务也跑在默认端口时放行; 本应用恒定随机端口, 走不到
    if oport is None and sport in (80, 443, None):
        return True
    return False


class LocalOnlyGuard:
    """纯 ASGI 中间件 (不用 BaseHTTPMiddleware, 避免干扰 SSE 流式响应)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        host = origin = ''
        for k, v in scope.get('headers') or []:
            name = k.decode('latin-1').lower()
            if name == 'host':
                host = v.decode('latin-1').strip().lower()
            elif name == 'origin':
                origin = v.decode('latin-1').strip()
        hostname = host.rsplit(':', 1)[0].strip('[]')
        ok = hostname in _LOCAL_HOSTS
        if ok and origin:
            ok = _same_service_origin(origin, scope, host)
        if not ok:
            body = json.dumps({'error': '仅限本机访问'}, ensure_ascii=False).encode('utf-8')
            await send({'type': 'http.response.start', 'status': 403,
                        'headers': [(b'content-type', b'application/json; charset=utf-8'),
                                    (b'content-length', str(len(body)).encode('ascii'))]})
            await send({'type': 'http.response.body', 'body': body})
            return
        await self.app(scope, receive, send)


app.add_middleware(LocalOnlyGuard)

# 预览结果缓存: (路径, mtime, 配置指纹) -> 结果。规则一改指纹就变, 不会读到旧结果。
_PREVIEW_CACHE: 'OrderedDict[str, dict]' = OrderedDict()
_CACHE_MAX = 60
# 预览由 asyncio.to_thread 并发调用, 共享写上面那个 OrderedDict, 需要用锁串起来
_CACHE_LOCK = threading.Lock()
_native_bridge = None


def bind_native_bridge(bridge):
    """桌面壳 (pywebview) 启动后注入自己, 以便复用系统原生文件对话框。"""
    global _native_bridge
    _native_bridge = bridge


# ---------------------------------------------------------------- 工具

def _config_fp(config):
    return json.dumps(config or {}, sort_keys=True, ensure_ascii=False, default=str)


def _cached_preview(path, config):
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise ValueError(f'无法读取文件: {exc}') from exc
    # mtime 取纳秒整型: 秒级粒度下同一秒内改文件且大小不变会命中陈旧预览
    key = f'{path}|{stat.st_mtime_ns}|{stat.st_size}|{_config_fp(config)}'
    with _CACHE_LOCK:
        hit = _PREVIEW_CACHE.get(key)
        if hit is not None:
            _PREVIEW_CACHE.move_to_end(key)
            return {**hit, 'cached': True}
    result = core.preview_file(path, config)
    with _CACHE_LOCK:
        _PREVIEW_CACHE[key] = result
        _PREVIEW_CACHE.move_to_end(key)
        while len(_PREVIEW_CACHE) > _CACHE_MAX:
            _PREVIEW_CACHE.popitem(last=False)
    return result


async def _body(request):
    """请求体 -> dict。

    只认 JSON 对象: `"NOTJSON"` 和 `[1,2]` 都是合法 JSON, 但 data.get(...) 会
    AttributeError 直接 500。边界上认不出形状就当空对象, 让路由自己报"缺参数"。
    """
    try:
        data = await request.json()
    except Exception:                                    # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 路由

@app.get('/api/meta')
async def meta():
    # SC_THEME=dark|light 可固定默认主题 (打包成 exe 或做外观皮肤时用);
    # 不设就交给前端按 prefers-color-scheme 决定
    theme = (os.environ.get('SC_THEME') or '').strip().lower()
    return {'version': APP_VERSION, 'native': _native_bridge is not None,
            'static_ready': os.path.isdir(DIST_DIR),
            'theme': theme if theme in ('dark', 'light') else 'system',
            'ts': time.time()}


@app.post('/api/collect')
async def collect(request: Request):
    """把"文件列表/文件夹(可递归)"展开成待处理清单, 并给出输出名计划。"""
    data = await _body(request)
    files = data.get('files') or []
    dirs = data.get('dirs') or []
    recursive = bool(data.get('recursive', True))
    output_dir = (data.get('output_dir') or '').strip()
    fmt = str((data.get('config') or {}).get('output_format') or 'keep')
    skipped: list = []
    paths = core.expand_inputs(files, dirs, recursive, output_dir=output_dir, skipped=skipped)
    roots = list(dirs) + [os.path.dirname(p) for p in paths]
    plan = core.plan_outputs(paths, output_dir, roots, fmt) if paths else []
    unsupported = [p for p in files if not core.is_scannable(p)]
    return {'count': len(paths), 'plan': plan,
            'unsupported': [os.path.basename(p) for p in unsupported],
            'skipped': skipped,
            'suggested_output': core.suggest_output_dir(files, dirs)}


@app.post('/api/fs/list')
async def fs_list(request: Request):
    data = await _body(request)
    include_files = bool(data.get('include_files'))
    return await asyncio.to_thread(core.list_dir, data.get('path'), include_files)


@app.post('/api/classify')
async def classify(request: Request):
    """粘贴进来的路径按文件系统分类: 是文件 / 是目录 / 不存在。

    分类必须在服务端做 —— 前端只有字符串, 按后缀猜会把 `.dat`、无后缀的表格
    当成目录 (内核本来能读它们), 与扫描文件夹那条路的口径分叉。
    """
    data = await _body(request)
    return await asyncio.to_thread(core.classify_paths, _str_list(data.get('paths')))


@app.post('/api/dialog')
async def pick_dialog(request: Request):
    """原生对话框: 桌面壳可用时走系统对话框, 否则前端退回内联目录浏览。"""
    if _native_bridge is None:
        return {'ok': False, 'reason': 'no-native-shell'}
    data = await _body(request)
    kind = data.get('kind', 'files')
    try:
        paths = await asyncio.to_thread(_native_bridge.pick, kind)
    except Exception as exc:                             # noqa: BLE001
        return {'ok': False, 'reason': core.error_text(exc)}
    return {'ok': bool(paths), 'paths': paths or []}


@app.post('/api/preview')
async def preview(request: Request):
    """干跑预览: 只读, 不写任何输出文件。"""
    data = await _body(request)
    path = str(data.get('path') or '').strip()
    if not path or not os.path.isfile(path):
        return JSONResponse({'ok': False, 'error': '文件不存在'}, status_code=400)
    try:
        config = core.normalize_config(data.get('config'))
    except Exception as exc:                             # noqa: BLE001
        return JSONResponse({'ok': False, 'error': core.error_text(exc)}, status_code=422)
    try:
        return await asyncio.to_thread(_cached_preview, path, config)
    except Exception as exc:                             # noqa: BLE001
        return JSONResponse({'ok': False, 'error': core.error_text(exc)}, status_code=422)


def _str_list(value):
    """JSON 里的 paths/files/dirs: 只收字符串元素。

    给个字符串就迭代出每个字符 ("x.csv" -> 5 个"文件"), 给个数字直接 TypeError;
    边界上先规整, 后面的分支才只处理真实路径。
    """
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


@app.post('/api/inspect')
async def inspect_files(request: Request):
    """文件队列: 每个文件只回行数/列数/编码等表头级信息 (不跑清洗), 响应快。

    与清洗同口径 (同一份 read_table、同一套 keep_default_na), 但这里不跑清洗,
    所以前端传的 config 在本接口不起作用 —— 响应里显式标出来, 不假装"按规则算过"。
    """
    data = await _body(request)
    paths = _str_list(data.get('paths'))
    try:
        limit = int(data.get('limit') or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(2000, limit))

    def _scan(p):
        try:
            df, meta = core.read_table(p)
            return {'path': p, 'name': os.path.basename(p), 'ok': True,
                    'size': os.path.getsize(p),
                    'rows': int(len(df)), 'cols': int(df.shape[1]),
                    'columns': [str(c) for c in list(df.columns)[:40]],
                    'encoding': meta.get('encoding'), 'delimiter': meta.get('delimiter'),
                    'engine': meta.get('engine'),
                    'warnings': list(meta.get('warnings') or [])}
        except Exception as exc:                         # noqa: BLE001
            return {'path': p, 'name': os.path.basename(p), 'ok': False,
                    'size': os.path.getsize(p) if os.path.exists(p) else 0,
                    'rows': None, 'cols': None, 'columns': [],
                    'error': core.error_text(exc)}

    items = await asyncio.gather(*[asyncio.to_thread(_scan, p) for p in paths[:limit]])
    return {'items': list(items), 'truncated': len(paths) > limit,
            'config_applied': False}


@app.post('/api/run')
async def run(request: Request):
    data = await _body(request)
    paths = _str_list(data.get('paths'))
    output_dir = str(data.get('output_dir') or '').strip()
    if not paths:
        return JSONResponse({'error': '没有待处理文件'}, status_code=400)
    if not output_dir:
        return JSONResponse({'error': '未指定输出目录'}, status_code=400)
    os.makedirs(output_dir, exist_ok=True)
    config = core.normalize_config(data.get('config'))
    config['stem_map'] = {}
    job = registry.create(paths, output_dir, config)
    return {'job_id': job.id, 'total': job.total}


@app.get('/api/jobs/{job_id}')
async def job_snapshot(job_id: str):
    job = registry.get(job_id)
    return job.snapshot() if job else JSONResponse({'error': 'job 不存在'}, status_code=404)


@app.post('/api/jobs/{job_id}/cancel')
async def job_cancel(job_id: str):
    job = registry.get(job_id)
    if not job:
        return JSONResponse({'error': 'job 不存在'}, status_code=404)
    job.cancel()
    return {'ok': True}


@app.get('/api/jobs/{job_id}/events')
async def job_events(job_id: str, request: Request):
    job = registry.get(job_id)
    if not job:
        return JSONResponse({'error': 'job 不存在'}, status_code=404)
    q = job.subscribe()

    async def stream():
        try:
            hello = {'kind': 'hello', 'job_id': job_id, 'seq': 0, 'at': round(time.time(), 3)}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                drained = False
                while True:
                    try:
                        event = q.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    yield f'data: {json.dumps(event, ensure_ascii=False)}\n\n'
                    if event.get('kind') == 'job_end':
                        return
                if not drained:
                    yield ': keep-alive\n\n'
                await asyncio.sleep(0.25)
        finally:
            job.unsubscribe(q)

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.post('/api/upload')
async def upload(request: Request):
    """拖拽进来的文件只有文件名没有绝对路径 (WebView 的安全边界)。

    这里把字节落到系统临时目录再交给同一套内核, 而不是在前端伪造路径。
    """
    form = await request.form()
    files = [v for v in form.getlist('files') if isinstance(v, UploadFile)]
    if not files:
        return JSONResponse({'error': '没有收到文件'}, status_code=400)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target_dir = os.path.join(tempfile.gettempdir(), 'StockCleaner', f'dropped-{stamp}')
    os.makedirs(target_dir, exist_ok=True)
    saved = []
    rejected = []
    total = 0
    for field in files:
        # 只取 basename, 丢掉任何目录成分: 防止 ../ 逃逸到别处
        safe = os.path.basename(str(field.filename or 'pasted').replace('\\', '/'))
        safe = re.sub(r'[\x00-\x1f<>:"|?*]', '_', safe).strip() or 'pasted'
        stem, ext = os.path.splitext(safe)
        if ext and not core.is_scannable(stem + ext):
            # 明确二进制的后缀 (拖进来也只可能是误拖) 一律按 .csv 落盘;
            # .dat/.log/无后缀这类保持原样, 交给内核猜读并留下警告
            ext = '.csv'
        # Windows 设备名即使带扩展名也是保留名 (CON.csv / nul.txt / COM1.csv),
        # 落盘会撞到设备而不是文件; 前面补一个下划线就只是普通文件名了
        if stem.upper().rstrip('.').split('.')[0] in _WIN_RESERVED:
            stem = '_' + stem
        # 同名不静默覆盖: 追加序号 (与输出的防覆盖改名同一立场)。
        # 占位必须原子: 旧实现 exists 检查与 open 之间有竞态窗口, 同一秒内并发
        # 拖入的同名文件会双双通过检查, 后写者静默覆盖先写者。O_CREAT|O_EXCL
        # 让内核保证只有一个赢家, 输的那一方拿到下一个序号。
        n = 0
        while True:
            dest = os.path.join(target_dir, stem + ext) if n == 0 \
                else os.path.join(target_dir, f'{stem} ({n}){ext}')
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, 'O_BINARY', 0), 0o666)
                break
            except FileExistsError:
                n += 1
        written = 0
        try:
            with os.fdopen(fd, 'wb') as fh:
                # 分块落盘: 大文件不再整体读进内存; 同时按字节数封顶, 不能让一次
                # 拖拽把临时目录写满
                while True:
                    chunk = await field.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    written += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise _OverQuota()
                    fh.write(chunk)
        except _OverQuota:
            try:
                os.remove(dest)
            except OSError:
                pass
            rejected.append(f'{safe}: 超过单次上限 {MAX_UPLOAD_BYTES // (1 << 20)} MB, 已丢弃')
            continue
        saved.append(os.path.abspath(dest))
    out = {'paths': saved, 'dir': os.path.abspath(target_dir)}
    if rejected:
        out['rejected'] = rejected
    return out


@app.post('/api/reveal')
async def reveal(request: Request):
    """在系统文件管理器里定位输出文件 (仅本机路径, 不做网络出口)。"""
    data = await _body(request)
    path = (data.get('path') or '').strip()
    if not path or not os.path.exists(path):
        return JSONResponse({'ok': False, 'error': '路径不存在'}, status_code=404)
    import subprocess
    if os.name == 'nt':
        subprocess.Popen(['explorer', '/select,', os.path.abspath(path)])
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', os.path.abspath(path)])
    else:
        subprocess.Popen(['xdg-open', os.path.dirname(os.path.abspath(path))])
    return {'ok': True}


# ---------------------------------------------------------------- 静态资源

_FALLBACK = """<!doctype html><html><head><meta charset="utf-8"><title>StockCleaner</title>
<style>body{font:14px/1.6 system-ui;background:#0C0D10;color:#E8EAF0;display:grid;place-items:center;
height:100vh;margin:0}code{background:#1A1D24;padding:2px 6px;border-radius:4px}</style></head>
<body><div>前端尚未构建。<br>请执行 <code>npm install &amp;&amp; npm run build</code>,
或以 <code>npm run dev</code> 访问 <a style="color:#78B6FF" href="http://localhost:5173">http://localhost:5173</a></div>
</body></html>"""


def mount_static(target=app):
    if os.path.isdir(DIST_DIR):
        target.mount('/', StaticFiles(directory=DIST_DIR, html=True), name='ui')
    else:
        @target.get('/', response_class=HTMLResponse)
        async def _fallback():
            return _FALLBACK
