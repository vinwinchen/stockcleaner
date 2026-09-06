# -*- coding: utf-8 -*-
"""发布冒烟测试: 对打包产物 (或任意 --base 指向的服务) 走一条真实链路。

步骤:
  1. (可选) 启动 exe, 固定端口, 轮询 /api/meta 就绪;
  2. /api/meta 断言 version / static_ready / native;
  3. /api/preview 对样例文件干跑, 断言拿到了列检视数据;
  4. /api/run 提交真实清洗, 轮询 job 到终态, 断言 job_end 且产出文件存在;
  5. 结束时杀掉自己启动的 exe (--base 模式不动外部服务)。

用法:
  .venv/Scripts/python.exe pack/smoke_test.py --exe dist/StockCleaner/StockCleaner.exe \
      --sample samples/A股样例_202301.csv --out %TEMP%/sc_smoke_out

任何一步失败都会以非零码退出, 可直接当发布门槛用。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


def http_json(method, url, payload=None, timeout=10):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def wait_meta(base, deadline_s=40):
    deadline = time.time() + deadline_s
    last = None
    while time.time() < deadline:
        try:
            return http_json('GET', base + '/api/meta', timeout=1.5)
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            time.sleep(0.4)
    raise SystemExit(f'[FAIL] 服务未就绪: {last}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', help='打包产物 exe 路径; 与 --base 二选一')
    ap.add_argument('--base', help='已在运行的服务地址, 如 http://127.0.0.1:8720')
    ap.add_argument('--port', type=int, default=8731, help='启动 exe 用的固定端口')
    ap.add_argument('--sample', required=True, help='用于冒烟的数据文件')
    ap.add_argument('--out', required=True, help='清洗输出目录 (会先清空)')
    args = ap.parse_args()

    proc = None
    if args.base:
        base = args.base.rstrip('/')
    else:
        if not args.exe or not os.path.isfile(args.exe):
            raise SystemExit('[FAIL] 找不到 exe, 或缺 --base')
        port = args.port
        proc = subprocess.Popen([os.path.abspath(args.exe), '--port', str(port)],
                                cwd=os.path.dirname(os.path.abspath(args.exe)))
        base = f'http://127.0.0.1:{port}'
        print(f'[1/5] 已启动 {args.exe} (port {port}), 等待 /api/meta ...')

    try:
        meta = wait_meta(base)
        print(f'[2/5] meta = {json.dumps(meta, ensure_ascii=False)}')
        assert meta.get('static_ready') is True, '静态资源未挂载 (frontend/dist 没进包?)'

        sample = os.path.abspath(args.sample)
        assert os.path.isfile(sample), f'样例不存在: {sample}'

        preview = http_json('POST', base + '/api/preview',
                            {'path': sample, 'config': {}})
        assert preview.get('ok') is not False, f'preview 失败: {preview}'
        cols = preview.get('columns') or preview.get('report', {}).get('columns') or []
        print(f"[3/5] preview ok: keys={sorted(preview.keys())[:8]}, 列数={len(cols)}")

        if os.path.isdir(args.out):
            shutil.rmtree(args.out)
        os.makedirs(args.out, exist_ok=True)
        run = http_json('POST', base + '/api/run',
                        {'paths': [sample], 'output_dir': os.path.abspath(args.out),
                         'config': {}})
        job_id = run['job_id']
        snapshot = {}
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = http_json('GET', f'{base}/api/jobs/{job_id}')
            if snapshot.get('status') in ('done', 'error', 'cancelled'):
                break
            time.sleep(0.4)
        assert snapshot.get('status') == 'done', f'job 终态异常: {snapshot}'
        assert snapshot.get('ok') == snapshot.get('total') == 1 and not snapshot.get('failed'), \
            f'job 结果异常: {snapshot}'
        print(f"[4/5] job done: ok={snapshot.get('ok')} failed={snapshot.get('failed')} "
              f"elapsed={snapshot.get('elapsed')}s")

        produced = []
        for root, _dirs, names in os.walk(args.out):
            produced += [os.path.join(root, n) for n in names]
        assert produced, '输出目录为空, 清洗没有落盘'
        for p in produced:
            print(f'      产出: {p} ({os.path.getsize(p)} bytes)')
        print('[5/5] 冒烟测试通过')
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == '__main__':
    main()
