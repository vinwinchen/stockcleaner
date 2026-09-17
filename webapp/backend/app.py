# -*- coding: utf-8 -*-
"""FastAPI 层: 前端与 cleaner_core 之间唯一的通道。

只绑 127.0.0.1, 不对外提供服务。所有耗时操作 (pandas/文件 IO) 走线程池,
不阻塞事件循环; SSE 事件带 seq + 缓冲, 断线重连可补齐进度。
"""

import asyncio
import hmac
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

APP_VERSION = '2.2.8'
# 单次拖拽上传的总量上限。没有上限时, 一次拖进来的东西可以无限写满系统临时目录
# (落盘发生在任何一个字节被解析之前), 而本机进程面本来就无鉴权。
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
# 除上传外所有接口的请求体上限: 这些接口只收 JSON 配置, 8 MiB 远超任何真实请求。
# request.json() 会把整个请求体收进内存, 没有上限就是一个内存放大器。
MAX_JSON_BYTES = 8 * 1024 ** 2
# 给 multipart 分隔头/多文件留的余量。上传体在这条线以内仍然走到应用层的逐文件
# 配额 ("xxx.csv: 超过单次上限 N MB, 已丢弃"), 中间件只拦真正离谱的体量 ——
# 那条逐文件提示是给用户看的, 不能让中间件把它的机会抢掉。
MAX_MULTIPART_SLACK = 32 * 1024 ** 2
# 访问 token: 桌面壳启动时生成 (run.py), 随窗口 URL 的 fragment 交给前端, 前端在每个
# /api 请求上带回来。打包产物/CI 可以直接用 SC_TOKEN 环境变量钉一个, 免得生成后没人知道。
#
# 为什么需要: Host/Origin 守卫只回答"你是不是本机、同源", 不回答"你是不是我开的那个窗口"。
# 端口是本机任意进程都能扫到的, 于是**权限比用户低**的本机程序 (受限令牌/沙箱进程: 连得上
# 回环却读不到用户文件) 可以借这套 API 读任意文本文件、往任意可写目录落文件 —— 拿本工具
# 当"内应"(confused deputy)。token 让它猜不出来。
# 边界必须说清: 这挡不住同权限的进程 (能读本进程内存/浏览器存储, token 一样拿得到),
# 只是把门槛从"知道端口"抬到"得是那个窗口"。空值 = 未配置, 一律拒绝 (失败要响)。
_TOKEN = (os.environ.get('SC_TOKEN') or '').strip()
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


class TokenGuard:
    """/api/* 必须带访问 token: 请求头 X-SC-Token, 或 SSE 用的 ?t=。

    静态资源**不设防**, 这是有意的: 窗口首次导航拿不到任何自定义请求头 (token 在 URL 的
    fragment 里, 按 URL 规范 fragment 不会发给服务端), 得先把页面发出去, 页面才有机会把
    token 带上。静态资源里没有任何用户数据, 真正的能力全在 /api/* 上。

    EventSource 不能设请求头, 所以 SSE 那条额外认 ?t= (访问日志本来就关着)。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not (scope.get('path') or '').startswith('/api/'):
            await self.app(scope, receive, send)
            return
        supplied = self._supplied(scope)
        if _TOKEN and supplied and hmac.compare_digest(supplied, _TOKEN):
            await self.app(scope, receive, send)
            return
        message = ('未配置访问 token (SC_TOKEN), 拒绝所有 /api 请求'
                   if not _TOKEN else '未授权的本机访问')
        body = json.dumps({'error': message}, ensure_ascii=False).encode('utf-8')
        await send({'type': 'http.response.start', 'status': 403,
                    'headers': [(b'content-type', b'application/json; charset=utf-8'),
                                (b'content-length', str(len(body)).encode('ascii'))]})
        await send({'type': 'http.response.body', 'body': body})

    @staticmethod
    def _supplied(scope):
        for k, v in scope.get('headers') or []:
            if k.lower() == b'x-sc-token':
                return v.decode('latin-1').strip()
        query = scope.get('query_string') or b''
        if query:
            values = urllib.parse.parse_qs(query.decode('latin-1')).get('t') or []
            if values:
                return values[0].strip()
        return ''


class _BodyTooLarge(Exception):
    """请求体超过上限 (只在中间件内部用, 不冒到应用层)。"""


def _body_limit(path):
    if path == '/api/upload':
        return MAX_UPLOAD_BYTES + MAX_MULTIPART_SLACK
    return MAX_JSON_BYTES


class BodyLimitGuard:
    """请求体上限 (纯 ASGI 中间件, 与 LocalOnlyGuard 同层)。

    为什么必须在中间件里做: starlette 的 request.form() / request.json() 是**先**把整个
    请求体收完再交给应用 —— 文件部分超过 1 MiB 就溢写到真实临时文件, 之后应用里那套
    分块计数才开始跑。也就是说应用内那道上限只约束了"第二次拷贝", 解析阶段该写满盘
    照样写满 (单个 file part 发 100 GiB, 解析器会一直往里写)。

    两条路径用两套做法, 因为体量差三个数量级, 而且应用侧对异常的态度不同:
      - 非上传接口只收 JSON, 上限 8 MiB: 自己读完再交给应用, 超限根本不进应用。
        不能靠"从 receive 里抛异常" —— _body 的 `except Exception: return {}` 会把任何
        异常吞掉 (那是它在边界上该做的), 上限就白设了。
      - /api/upload 的体可达 GiB 级, 绝不能缓冲: 边收边数, 超限从 receive 里抛出。
        upload 在读完整个体之前不会回响应, 且 request.form() 外没有 except, 异常能冒到这里。
    能读到 Content-Length 时先直接 413, 省掉整个接收过程。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path') or ''
        limit = _body_limit(path)
        for k, v in scope.get('headers') or []:
            if k.lower() == b'content-length':
                try:
                    declared = int(v)
                except ValueError:
                    break
                if declared > limit:
                    await self._reject(send, limit)
                    return
                break

        if path == '/api/upload':
            await self._pass_stream_capped(scope, receive, send, limit)
        else:
            await self._pass_buffered(scope, receive, send, limit)

    async def _pass_buffered(self, scope, receive, send, limit):
        chunks, total = [], 0
        while True:
            message = await receive()
            if message['type'] != 'http.request':
                break
            body = message.get('body') or b''
            total += len(body)
            if total > limit:
                await self._reject(send, limit)
                return
            chunks.append(body)
            if not message.get('more_body'):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks)}
            # 之后的调用交还给真实流: SSE 的 request.is_disconnected() 靠它判断开,
            # 一直回 disconnect 会把长连接立刻掐掉。
            return await receive()

        await self.app(scope, receive=replay, send=send)

    async def _pass_stream_capped(self, scope, receive, send, limit):
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body') or b'')
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, receive=limited_receive, send=tracking_send)
        except _BodyTooLarge:
            # 响应已经开出去就不能再补一个 (SSE 这类长连接会撞上 ASGI 协议错误);
            # upload 在读体阶段不会先回响应, 走不到这个分支。
            if not started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit):
        body = json.dumps(
            {'error': f'请求体超过上限 {limit // (1 << 20)} MB'}, ensure_ascii=False
        ).encode('utf-8')
        await send({'type': 'http.response.start', 'status': 413,
                    'headers': [(b'content-type', b'application/json; charset=utf-8'),
                                (b'content-length', str(len(body)).encode('ascii'))]})
        await send({'type': 'http.response.body', 'body': body})


# 注册顺序: Starlette 的 add_middleware 是前插, 后加的在外层。所以从内到外加 ——
#   外层 LocalOnlyGuard (来源不对就 403, 不陪它收请求体)
#   → TokenGuard     (token 不对就 403, 同样不必收体)
#   → 内层 BodyLimitGuard (到这里才值得读/限制请求体)
app.add_middleware(BodyLimitGuard)
app.add_middleware(TokenGuard)
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

    def _scan():
        # 递归 glob + 对每个候选文件读文件头 (sniff_container): 把一个大目录
        # (或一个 UNC 路径) 加进队列时, 这些同步 I/O 不能占着事件循环 —— 否则
        # 一次 collect 期间 SSE 心跳、任务快照、取消请求全部停摆, 界面卡死又取消不了。
        # 同文件的 fs_list / classify / preview 早就走了 to_thread, 这里是遗漏。
        skipped: list = []
        paths = core.expand_inputs(files, dirs, recursive,
                                   output_dir=output_dir, skipped=skipped)
        roots = list(dirs) + [os.path.dirname(p) for p in paths]
        plan = core.plan_outputs(paths, output_dir, roots, fmt) if paths else []
        return paths, plan, skipped

    paths, plan, skipped = await asyncio.to_thread(_scan)
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


def _prune_dropped(keep_days=7):
    """清掉过期的 dropped-* 落盘目录, 返回删掉几个。

    上传的字节落在 <工作目录>/dropped-*/ (随机名, 见 upload), 从前无人回收: 单次拖拽上限
    2 GiB, 拖几次就永久占着磁盘 (系统没有替应用清目录的义务)。只删比
    keep_days 更老的 —— 本轮任务引用的是刚落的目录, 永远扫不到, 所以不会删掉队列里
    正在等清洗的文件。删不掉 (被别的进程占着等) 就跳过, 不影响这次上传。
    """
    import shutil

    root = core.work_dir()
    cutoff = time.time() - keep_days * 86400
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if not entry.name.startswith('dropped-'):
            continue
        try:
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
    return removed


@app.post('/api/upload')
async def upload(request: Request):
    """拖拽进来的文件只有文件名没有绝对路径 (WebView 的安全边界)。

    这里把字节落到本工具的私有工作目录再交给同一套内核, 而不是在前端伪造路径。
    """
    await asyncio.to_thread(_prune_dropped)     # 扫描+递归删除, 不能占着事件循环
    form = await request.form()
    files = [v for v in form.getlist('files') if isinstance(v, UploadFile)]
    if not files:
        return JSONResponse({'error': '没有收到文件'}, status_code=400)
    # mkdtemp: 随机名 + 独占创建。原来用秒级时间戳命名, 名字可预测 —— 攻击者能在
    # 清理之后、上传之前, 在工作目录下摆一个同名 junction 把上传内容引到任意目录
    # (实测顺着 junction 写, 字节确实落在其目标目录; 且 os.path.islink 对 junction
    # 返回 False, 只有 lstat 的 REPARSE_POINT 位看得出来, 靠 islink 防不住)。
    # 随机名让"预先摆好"这件事不成立; 父目录那一层由 ensure_work_dir 复查 (见 core.py)。
    try:
        base = core.ensure_work_dir()
    except OSError as exc:
        return JSONResponse({'error': f'工作目录不可用: {exc}'}, status_code=500)
    target_dir = tempfile.mkdtemp(prefix='dropped-', dir=base)
    saved = []
    rejected = []
    total = 0
    for field in files:
        # 只取 basename, 丢掉任何目录成分: 防止 ../ 逃逸到别处
        safe = os.path.basename(str(field.filename or 'pasted').replace('\\', '/'))
        safe = re.sub(r'[\x00-\x1f<>:"|?*]', '_', safe).strip() or 'pasted'
        # basename('.') / ('..') 仍是目录本身, 不是文件名: 落盘会撞上目录, 而 Windows
        # 抛的是 PermissionError 而不是 FileExistsError, 旧实现直接 500, 并把**整批**
        # 上传一起丢掉 (实测 ['good.csv','..','good2.csv'] 三个都没落盘, 无任何提示)。
        if safe in ('.', '..'):
            safe = 'pasted'
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
        # 兜 OSError 而不只是 FileExistsError: 名字撞上目录/保留设备时 Windows 抛的是
        # PermissionError, 漏出去就是 500 + 整批上传作废。有上限, 免得在这里空转。
        n = 0
        fd = None
        while fd is None and n < 1000:
            dest = os.path.join(target_dir, stem + ext) if n == 0 \
                else os.path.join(target_dir, f'{stem} ({n}){ext}')
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, 'O_BINARY', 0), 0o666)
            except OSError:
                n += 1
        if fd is None:
            rejected.append(f'{safe}: 无法落盘, 已丢弃')
            continue
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


def _is_local_path(path):
    """本机绝对路径: 不是 UNC/设备命名空间, 也不是驱动器相对形式 (C:xxx)。

    判据存在的理由是 reveal 那句承诺 (见它的 docstring): `os.path.exists('\\\\host\\share')`
    在 Windows 上会真的去连 SMB —— DNS 查询 + 认证, 等于让"打开文件夹"这个动作出网。
    其余接受任意路径的接口 (preview/inspect/fs.list) 是用户点名要处理的文件, 指向网络
    共享是正常用法, 所以只在有明确承诺的这里拦。
    """
    p = str(path or '')
    if not p or p.startswith(('\\\\', '//')):
        return False
    return os.path.isabs(p)


@app.post('/api/reveal')
async def reveal(request: Request):
    """在系统文件管理器里定位输出文件 (仅本机路径: UNC/网络共享一律拒绝)。"""
    data = await _body(request)
    path = (data.get('path') or '').strip()
    if not _is_local_path(path):
        return JSONResponse({'ok': False, 'error': '只支持本机路径 (不接受 UNC/网络共享)'},
                            status_code=400)
    if not os.path.exists(path):
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
