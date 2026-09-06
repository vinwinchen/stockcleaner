# -*- coding: utf-8 -*-
"""端到端性能核对: 通过真实 HTTP 链路跑大文件, 而不是把分段数字相加。

用法:
  python run.py --port 8720 --no-shell      # 另开一个终端
  python bench_e2e.py [base_url] [行数]
"""

import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bench_xlsx import COLS, gen_rows    # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8720').rstrip('/')
ROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 200000
QUICK = len(sys.argv) > 3 and sys.argv[3] == 'quick'


def post(path, body, timeout=900):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def wait_job(job_id, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = get('/api/jobs/' + job_id)
        if snap.get('status') != 'running':
            return snap
        time.sleep(0.25)
    raise SystemExit('任务超时未结束')


def run_one(src, cfg, tag):
    out = os.path.join(tempfile.gettempdir(), f'e2e_{tag}')
    os.makedirs(out, exist_ok=True)
    for name in os.listdir(out):
        os.remove(os.path.join(out, name))
    t0 = time.perf_counter()
    job = post('/api/run', {'paths': [src], 'output_dir': out, 'config': cfg})
    wait_job(job['job_id'])
    return time.perf_counter() - t0


def main():
    src = os.path.join(tempfile.gettempdir(), f'e2e_big_{ROWS}.xlsx')
    if not os.path.isfile(src):
        import pandas as pd
        t0 = time.perf_counter()
        pd.DataFrame(list(gen_rows(ROWS)), columns=COLS).to_excel(src, index=False)
        print(f'生成样本 {ROWS} 行: {time.perf_counter() - t0:.1f}s')
    print(f'样本 {os.path.getsize(src) / 1048576:.1f} MB  服务 {BASE}\n')

    cfg = {'numericize': True, 'convert_units': True, 'normalize_dates': True,
           'drop_empty_rows': True, 'fullwidth': True}

    if QUICK:
        # 只跑一组固定配置, 用来在两个服务实例之间做引擎 A/B
        base = {'numericize': True, 'convert_units': True, 'normalize_dates': True,
                'drop_empty_rows': True, 'fullwidth': False}
        for fmt in ('keep', 'csv'):
            cost = run_one(src, dict(base, output_format=fmt), 'quick')
            print(f'quick {fmt:<5} {cost:6.1f}s   ({BASE})')
        return

    print(f'{"输出格式":<10}{"端到端":>9}{"服务端":>9}  产出')
    print('-' * 68)
    for fmt in ('keep', 'csv', 'both'):
        out = os.path.join(tempfile.gettempdir(), f'e2e_out_{fmt}')
        os.makedirs(out, exist_ok=True)
        t0 = time.perf_counter()
        job = post('/api/run', {'paths': [src], 'output_dir': out,
                                'config': dict(cfg, output_format=fmt)})
        snap = wait_job(job['job_id'])
        wall = time.perf_counter() - t0
        files = sorted(f for f in os.listdir(out) if not f.startswith('_stock'))
        mb = sum(os.path.getsize(os.path.join(out, f)) for f in files) / 1048576
        print(f'{fmt:<10}{wall:>8.1f}s{snap["elapsed"]:>8.1f}s  {", ".join(files)} ({mb:.1f} MB)')
        assert snap['ok'] == 1, snap

    prev = get('/api/meta')
    print(f'\n内核版本 {prev["version"]}')

    # 归因隔离: 全角通道本身有成本, 不隔离就没法说清"读加速"到底省了多少
    print('\n归因隔离 (同一文件、同一服务, 只切两个开关):')
    print(f'{"配置":<30}{"耗时":>9}')
    print('-' * 40)
    for label, cfg in [
        ('keep + 全角关 (= 改造前配置)', dict(cfg, output_format='keep', fullwidth=False)),
        ('keep + 全角开', dict(cfg, output_format='keep', fullwidth=True)),
        ('csv + 全角关', dict(cfg, output_format='csv', fullwidth=False)),
        ('csv + 全角开', dict(cfg, output_format='csv', fullwidth=True)),
    ]:
        out = os.path.join(tempfile.gettempdir(), 'e2e_iso')
        os.makedirs(out, exist_ok=True)
        for name in os.listdir(out):
            os.remove(os.path.join(out, name))
        t0 = time.perf_counter()
        job = post('/api/run', {'paths': [src], 'output_dir': out, 'config': cfg})
        wait_job(job['job_id'])
        print(f'{label:<30}{time.perf_counter() - t0:>8.1f}s')

    print('\n对照参考: 改造前同一量级分段实测为 读 9.2s + 洗 7.5s + 写 xlsx 16.7s = 约 33s')


if __name__ == '__main__':
    main()
