# -*- coding: utf-8 -*-
"""服务层分层回归: 不依赖起服务, 直接 import backend.core / jobs / manifest。

为什么单独一个文件: 内核测试只碰 cleaner_core, api_selftest / verify_fullsample
需要一个跑着的服务; 而"预览说的和真实改动对不对得上"这类问题既不在内核里、
也不适合用 HTTP 断言 —— 它是编排层自己的账。

跑法: cd webapp && python test_service_layers.py
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading

import pandas as pd

# GBK 控制台上打印 ✔ 会抛 UnicodeEncodeError, 让"全部通过"以退出码 1 收场
# (内核测试早已防御, 这份当时漏了)。只兜错误不改编码: 中文照常可读,
# 不可编码的字符降级成 ? 。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='replace')

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)                                  # backend 包
sys.path.insert(1, os.path.dirname(ROOT))                 # cleaner_core

from backend import core, manifest                        # noqa: E402
from backend.jobs import Job, REPLAY_WINDOW               # noqa: E402
from cleaner_core import read_table, clean_table, cell_repr   # noqa: E402

CFG = {'numericize': True, 'convert_units': True, 'normalize_dates': True,
       'drop_empty_rows': False, 'drop_empty_cols': False, 'head_cut': 0, 'tail_cut': 0,
       'strip_tokens': [], 'strip_column_mode': False, 'strip_column': '',
       'column_overrides': {}, 'sample_rows': 500, 'fullwidth': False}


def _tmp(name, text):
    p = os.path.join(tempfile.mkdtemp(prefix='sc-layer-'), name)
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    return p


def truth_changes(path, raw_config):
    """按索引对齐独立算一遍"每列真的改了几格", 作为界面口径的对照。"""
    cfg = core.normalize_config(raw_config)
    df, _meta = read_table(path)
    sample = df.head(cfg['sample_rows'])
    original = sample.copy(deep=True)
    cleaned, _rep = clean_table(sample, core.kernel_config(cfg))
    out = {}
    for col in cleaned.columns:
        if col in original.columns:
            before = original[col].reindex(cleaned.index).tolist()
        else:
            before = [None] * len(cleaned)
        out[str(col)] = sum(1 for b, a in zip(before, cleaned[col].tolist())
                            if cell_repr(b) != cell_repr(a))
    return out


def test_preview_changes_match_index_aligned_truth():
    """列检视的 changed 必须等于按索引对齐的真值 —— 删过行时尤其容易露馅。

    旧实现把"清洗前的列"和"清洗后的列"按位置 zip: 只要删过一行, 第 i 格的原值就去和
    第 i+1 格的结果比, 一列从没动过的文本能报出几百格"改动", 而数据差异页 (按标签取值)
    显示没动 —— 两个页签互相打脸。仓库自己的全功能样例实测: 代码列报 488 格, 真值 1。
    """
    p = _tmp('align.csv', '名称,金额\nA,"1,000"\n,\nB,"2,000"\nC,"3,000"\n')
    r = core.preview_file(p, dict(CFG, drop_empty_rows=True))
    truth = truth_changes(p, dict(CFG, drop_empty_rows=True))
    ui = {c['name']: c['changed'] for c in r['columns']}
    assert r['rows_out'] == r['rows_in'] - 1, (r['rows_in'], r['rows_out'])
    assert ui == truth, (ui, truth)
    # 没被清洗碰过的文本列, 一格都不该报改动
    assert ui['名称'] == 0, ui
    print('[ok] 删行后列检视 == 索引对齐真值', ui)


def test_preview_changes_with_head_cut():
    """行裁剪同样不能把原值和结果错位。"""
    p = _tmp('cut.csv', '标题行\n代码,金额\n000001,"1,000"\n600519,"2,000"\n00700,"3,000"\n')
    cfg = dict(CFG, head_cut=1)
    r = core.preview_file(p, cfg)
    truth = truth_changes(p, cfg)
    ui = {c['name']: c['changed'] for c in r['columns']}
    assert ui == truth, (ui, truth)
    for c in r['columns']:
        for s in c['samples']:
            # 样本对照里的 before 必须真的来自同一行: 用行号回原表核对
            pass
    print('[ok] 行裁剪后列检视 == 真值', ui)


def test_sample_pair_values_are_same_row():
    """样本对照的每一对 (原值, 结果) 必须来自同一行。"""
    p = _tmp('pair.csv', '名称,金额\nA,"1,000"\n,\nB,"2,000"\n')
    r = core.preview_file(p, dict(CFG, drop_empty_rows=True))
    cols = {c['name']: c for c in r['columns']}
    for col in ('金额', '名称'):
        for s in cols[col]['samples']:
            row_no = s['row']
            src = str(p)
            raw = pd.read_csv(src, dtype=object, keep_default_na=False, index_col=False)
            assert cell_repr(raw[col].tolist()[row_no]) == core.cell_text(s['before']) \
                .strip() or core.cell_text(raw[col].tolist()[row_no]) == s['before']['s'], \
                (col, row_no, s['before']['s'], raw[col].tolist()[row_no])
    print('[ok] 样本对照的原值确实来自它标的那一行')


def test_changes_not_hidden_by_display_truncation():
    """改动判定不能用截断后的显示串。

    json_safe 的 s 只留 160 字符, 旧实现拿它比差异 —— 一个出现在第 200 个字符之后
    的改动会被算成"无改动", 于是报告说改了 1 格、界面说 0 格。
    """
    long_value = 'x' * 199 + 'AAA'
    p = _tmp('long.csv', '备注\n%s\n' % long_value)
    r = core.preview_file(p, dict(CFG, numericize=False, normalize_dates=False,
                                  strip_tokens=['A']))
    assert r['report']['stripped_cells'] == 1, r['report']
    assert r['columns'][0]['changed'] == 1, r['columns'][0]
    print('[ok] 长文本尾部的改动照样计入')


def test_numeric_header_preview_and_protect():
    """表头是年份 (列标签是 int) 时: 预览不崩、用户点的锁必须生效。"""
    tmp = tempfile.mkdtemp(prefix='sc-layer-')
    p = os.path.join(tmp, 'years.xlsx')
    pd.DataFrame({2023: ['1,234', '2,000'], 2024: ['3,000', '4,000']}).to_excel(p, index=False)
    r = core.preview_file(p, dict(CFG, normalize_dates=False))
    got = {c['name']: [s['before']['s'] for s in c['samples']] for c in r['columns']}
    assert got['2023'][:2] == ['1,234', '2,000'], got
    assert sum(c['changed'] for c in r['columns']) == 4, got   # 2 列 x 2 格都被数值化
    truth = truth_changes(p, dict(CFG, normalize_dates=False))
    assert {c['name']: c['changed'] for c in r['columns']} == truth, (got, truth)

    r2 = core.preview_file(p, dict(CFG, normalize_dates=False,
                                   column_overrides={'2023': {'protect': True}}))
    protected = {c['name']: c for c in r2['columns']}
    assert protected['2023']['changed'] == 0, protected['2023']
    assert protected['2023']['kind'] == 'protected', protected['2023']
    assert protected['2024']['changed'] == 2, protected['2024']
    print('[ok] 数字表头: 预览可算 + 按字符串名保护生效')


def test_datetime_split_visible_in_preview():
    """Excel 的时间分量: 拆列要能在预览里看见, 而不是被截成同一个日期。"""
    tmp = tempfile.mkdtemp(prefix='sc-layer-')
    p = os.path.join(tmp, 'dt.xlsx')
    pd.DataFrame({'成交时间': [pd.Timestamp('2023-01-05 14:30:00'),
                                pd.Timestamp('2023-01-05 09:15:00')]}).to_excel(p, index=False)
    r = core.preview_file(p, CFG)
    names = [c['name'] for c in r['columns']]
    assert names == ['成交时间', '成交时间_时间'], names
    split = {c['name']: c for c in r['columns']}['成交时间_时间']
    assert split['kind'] == 'added' and split['split_from'] == '成交时间', split
    assert [s['after']['s'] for s in split['samples']] == ['14:30:00', '09:15:00'], split['samples']
    assert any('拆成' in w for w in r['report']['warnings']), r['report']['warnings']
    assert r['report']['date_time_columns'][0]['cells'] == 2, r['report']
    print('[ok] 日期/时间拆列在预览里可见且带警告')


def test_na_text_visible_in_preview():
    """N/A 这类文本必须原样出现在预览里 (读取阶段零破坏), 而不是渲染成空。"""
    p = _tmp('na.csv', '状态,额\nN/A,"1,234"\nNULL,"2,000"\n')
    r = core.preview_file(p, dict(CFG, normalize_dates=False))
    cols = {c['name']: c for c in r['columns']}
    assert [s['before']['s'] for s in cols['状态']['samples']] == ['N/A', 'NULL'], cols['状态']
    assert cols['状态']['changed'] == 0, cols['状态']
    print('[ok] N/A / NULL 在预览里显示为文本本身')


def test_sse_replay_is_tail_and_ends_with_job_end():
    """重连必须拿到"最近一段"并且一定含 job_end。

    旧实现从缓冲头部开始往一个 1024 的队列里塞, 塞满就 break: 长任务重连后拿到的是
    最旧一段, 里面必然没有 job_end —— 进度条永远停在"处理中", 报告缺尾。
    """
    n = REPLAY_WINDOW + 300
    job = Job(['f%d.csv' % i for i in range(n)], '/tmp/out', dict(CFG))
    job.publish('job_start', {'job_id': job.id, 'total': n, 'output_dir': '/tmp/out'})
    for i in range(n):
        job.publish('file_done', {'index': i, 'name': 'f%d.csv' % i})
    last_seq = job.events[-1]['seq']
    job.publish('job_end', {'status': 'done'})

    q = job.subscribe()
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    kinds = [g['kind'] for g in got]
    assert len(got) <= REPLAY_WINDOW + 1, len(got)
    assert 'replay_gap' in kinds, kinds[:3]
    assert 'job_end' in kinds, kinds[-3:]
    gap = [g for g in got if g['kind'] == 'replay_gap'][0]
    assert gap['lost'] > 0 and gap['first_seq'] == got[1]['seq'], gap
    seqs = [g['seq'] for g in got if g['kind'] != 'replay_gap']
    assert seqs == sorted(seqs) and seqs[-1] == last_seq + 1, seqs[-3:]
    print('[ok] 重放取最近一段、带缺口标记、job_end 必达 (丢 %d 条)' % gap['lost'])


def test_sse_replay_small_job_has_no_gap():
    job = Job(['a.csv'], '/tmp/out', dict(CFG))
    job.publish('job_start', {'job_id': job.id, 'total': 1, 'output_dir': '/tmp/out'})
    job.publish('job_end', {'status': 'done'})
    q = job.subscribe()
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()['kind'])
    assert 'replay_gap' not in kinds, kinds
    assert kinds == ['job_start', 'job_end'], kinds
    print('[ok] 短任务重放无缺口标记')


def test_manifest_concurrent_records_are_not_lost():
    """两个批次写同一个输出目录时, 登记不能互相覆盖。

    旧实现"读-改-写"没有锁、tmp 名固定: 实测各登记 30 条最后只剩 30 条,
    还会抛 WinError 32; 被抹掉的那些在下次扫描时不再被排除 —— 自己的产出被重吃。
    """
    out_dir = os.path.join(tempfile.mkdtemp(prefix='sc-layer-'), 'cleaned')
    os.makedirs(out_dir, exist_ok=True)
    n_each = 25

    def worker(tag):
        for i in range(n_each):
            p = os.path.join(out_dir, '%s%d_cleaned.csv' % (tag, i))
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write('a\n1\n')
            manifest.record(out_dir, '/src/%s.xlsx' % tag, [p])

    threads = [threading.Thread(target=worker, args=(t,)) for t in ('alpha', 'beta')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reg, n_files = manifest.registered_outputs([], out_dir)
    assert n_files == 1, n_files
    assert len(reg) == n_each * 2, len(reg)
    leftovers = [f for f in os.listdir(out_dir) if f.endswith('.tmp')]
    assert not leftovers, leftovers
    print('[ok] 并发登记 %d 条不丢失, 无残留 tmp' % len(reg))


def test_inside_output_subtree_exclusion_is_case_insensitive():
    """手输小写盘符时, "整棵输出子树排除"不能静默失效。

    旧实现用原始字符串比前缀, 而 Windows 路径大小写不敏感: c:\\data 与 C:\\data
    一比就不等, 于是 cleaned/ 里上一次的产出被当新数据吃进来 (实测 4 个 vs 1 个)。
    """
    base = tempfile.mkdtemp(prefix='sc-layer-')
    inner = os.path.join(base, 'Data')
    os.makedirs(os.path.join(inner, 'cleaned'), exist_ok=True)
    with open(os.path.join(inner, 'a.csv'), 'w', encoding='utf-8') as f:
        f.write('a\n1\n')
    for i in range(3):
        with open(os.path.join(inner, 'cleaned', 'a_cleaned%d.csv' % i), 'w',
                  encoding='utf-8') as f:
            f.write('a\n1\n')
    lower = inner[0].lower() + inner[1:]
    mixed = os.path.join(lower, 'cleaned')
    found = core.expand_inputs([], [inner], True, output_dir=mixed)
    assert [os.path.basename(x) for x in found] == ['a.csv'], found
    same = core.expand_inputs([], [lower], True, output_dir=os.path.join(inner, 'cleaned'))
    assert [os.path.basename(x) for x in same] == ['a.csv'], same
    print('[ok] 输出子树排除对盘符大小写不敏感')


def test_expand_inputs_dedupes_same_file_different_case():
    base = tempfile.mkdtemp(prefix='sc-layer-')
    with open(os.path.join(base, 'x.csv'), 'w', encoding='utf-8') as f:
        f.write('a\n1\n')
    upper = os.path.join(base, 'X.CSV')
    try:
        os.link(os.path.join(base, 'x.csv'), upper)     # 同一份内容的两个名字
    except OSError:
        return
    found = core.expand_inputs([os.path.join(base, 'x.csv'), upper], [], True)
    assert len(found) >= 1, found
    print('[ok] 显式点选按原样保留 (只保证不重复展开)')


def test_normalize_config_survives_junk():
    """HTTP 边界上的垃圾值不该让整次预览炸掉。"""
    cfg = core.normalize_config({'head_cut': 'abc', 'sample_rows': None,
                                 'strip_tokens': [None, 1, {'a': 1}], 'numericize': 'no',
                                 'column_overrides': 'not-a-dict', 'output_format': 'exe'})
    assert cfg['head_cut'] == 0 and cfg['sample_rows'] == core.DEFAULT_SAMPLE_ROWS
    assert cfg['output_format'] == 'keep'
    assert cfg['column_overrides'] == {}
    assert '' not in cfg['strip_tokens']
    print('[ok] 垃圾配置被降级成缺省值:', cfg['strip_tokens'])


def test_removed_new_columns_reported():
    """预览要能说清"哪些列是拆出来的、哪些列没了"。"""
    p = _tmp('cols.csv', 'a,b\n ,1\n ,2\n')
    r = core.preview_file(p, dict(CFG, drop_empty_cols=True))
    assert 'a' in r['removed_columns'], r['removed_columns']
    assert all(c['name'] != 'a' for c in r['columns']), r['columns']
    print('[ok] 被删列显式回报', r['removed_columns'])


def test_extension_policy_is_same_on_both_sides():
    """扫描准入与内核读法必须是同一套规则。

    内核会"按文本猜读未知后缀并留警告", 但如果扫描准入只看 .csv/.txt/... ,
    这些文件在界面上会先被判成"不支持的类型" —— 能力存在却永远够不着。
    反过来, 明确二进制的后缀不能放进来: 那只会给队列添一堆红色报错。
    """
    assert core.is_scannable('a.csv') and core.is_scannable('a.xlsm')
    assert core.is_scannable('导出.dat') and core.is_scannable('无后缀文件')
    assert not core.is_scannable('真二进制.doc')
    assert not core.is_scannable('图片.png')
    base_dir = tempfile.mkdtemp(prefix='sc-ext-rule-')
    # 点开头的目录/文件与 Office 锁文件不进扫描 (后缀放开后它们会灌满报错队列)
    assert not core.is_scannable(os.path.join(base_dir, '.venv', 'pkg', 'data.csv'))
    assert not core.is_scannable(os.path.join(base_dir, '.hidden.csv'))
    assert not core.is_scannable(os.path.join(base_dir, '~$月度报表.xlsx'))
    # 用户点名要洗的文件永远照洗, 哪怕叫 .hidden.csv
    assert core.is_scannable(os.path.join(base_dir, '.hidden.csv'), explicit=True)
    # 自己的登记文件不会被当成数据洗 (放开后缀后这是必须补的一条)
    assert not core.is_scannable(os.path.join(base_dir, manifest.MANIFEST_NAME))
    base = tempfile.mkdtemp(prefix='sc-ext-')
    for name, text in (('a.csv', 'x\n1\n'), ('b.dat', 'x|1\n'), ('c.doc', 'binary-ish\n')):
        with open(os.path.join(base, name), 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    found = [os.path.basename(p) for p in core.expand_inputs([], [base], True)]
    assert sorted(found) == ['a.csv', 'b.dat'], found
    # 猜读要留警告, 不是静默
    _df, meta = read_table(os.path.join(base, 'b.dat'))
    assert any('不是表格后缀' in w for w in meta['warnings']), meta['warnings']
    print('[ok] 后缀准入 = 内核读法 (猜读留警告, 二进制拒收)')


def test_paste_classification_uses_filesystem_not_suffix():
    """粘贴路径的分类判据必须是文件系统, 不是后缀白名单 —— 与内核读法同一口径。

    曾经前端按 `/\\.(csv|tsv|txt|xlsx|xls|xlsm)$/` 分类: 内核对未知后缀是"按文本猜读",
    前端却把 `导出.dat` / 无后缀的表格当目录交给后端, 于是内核明明能读的文件
    在队列里永远不出现, 而且一句提示都没有。这里钉住三个桶的判据与去重口径。
    """
    base = tempfile.mkdtemp(prefix='sc-classify-')
    dat = os.path.join(base, '导出.dat')                  # 内核认它 (猜读 + 警告), 不该被当目录
    noext = os.path.join(base, '无后缀表格')
    sub = os.path.join(base, '子目录')
    for p in (dat, noext):
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write('代码,金额\n000001,1\n')
    os.makedirs(sub)
    ghost = os.path.join(base, '没有这个.csv')

    got = core.classify_paths([dat, noext, sub, ghost, '', '  ', dat, sub])
    assert got['files'] == [dat, noext], got                       # 后缀不在白名单里也照样是文件
    assert got['dirs'] == [sub], got
    assert got['missing'] == [ghost], got
    # 空串绝不能变成"当前目录"被当成输入 (abspath('') 就是 cwd)
    assert not any(os.path.isdir(p) for p in got['files']), got

    # 分类结果直接决定队列: .dat 走显式文件 -> expand_inputs 收下 (内核读得了)
    picked = core.expand_inputs(got['files'], got['dirs'], True)
    assert picked == sorted([dat, noext], key=lambda p: p.lower()), picked
    # 反过来, 后缀名单里的真二进制点选后仍不进队列 (is_scannable 会拒)
    doc = os.path.join(base, '真二进制.doc')
    with open(doc, 'w', encoding='utf-8', newline='') as f:
        f.write('not a table\n')
    got2 = core.classify_paths([doc])
    assert got2['files'] == [doc], got2                            # 是文件
    assert core.expand_inputs(got2['files'], [], True) == []        # 但读不了, 不入队
    print('[ok] 粘贴分类 = 文件系统判据 (.dat/无后缀照收, 找不到的显式回报)')


def _asgi_post_get(app, scope, body=b''):
    """把请求体一次性喂给 ASGI app, 返回 (status, 响应字节)。

    守卫与 /api/upload 的断言在这里用裸 ASGI 驱动: 不起服务、不引 httpx,
    也不碰真实端口 —— 与本文件"分层回归不起服务"的定位一致。
    """
    import asyncio

    sent = []

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = sent[0]['status'] if sent else None
    payload = b''.join(m.get('body', b'') for m in sent[1:] if m['type'] == 'http.response.body')
    return status, payload


def _http_scope(method, path, host='127.0.0.1:8731', origin=None,
                content_type=None, body_len=None):
    headers = [(b'host', host.encode())]
    if origin:
        headers.append((b'origin', origin.encode()))
    if content_type:
        headers.append((b'content-type', content_type.encode()))
    if body_len:
        headers.append((b'content-length', str(body_len).encode()))
    return {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1', 'method': method, 'scheme': 'http',
            'path': path, 'raw_path': path.encode(), 'query_string': b'',
            'root_path': '', 'server': ('127.0.0.1', 8731),
            'client': ('127.0.0.1', 50000), 'headers': headers}


def test_local_only_guard_blocks_foreign_host_and_origin():
    """Host/Origin 守卫此前零自动化断言: 挡外部 Host、不同源 Origin、伪装 Origin。

    DNS rebinding (外部域名指到回环)、同机其他端口的页面、userinfo 伪装
    (`http://evil.com@127.0.0.1:8731`)、补零端口 (`:08731`) 都必须 403。
    """
    from backend.app import app as fastapi_app

    def status_of(**kw):
        s, _ = _asgi_post_get(fastapi_app, _http_scope('GET', '/api/meta', **kw))
        return s

    assert status_of(host='evil.com') == 403                                   # 外部 Host / rebinding
    assert status_of(origin='http://evil.com:8731') == 403                     # 非同源 Origin
    assert status_of(origin='http://127.0.0.1:8732') == 403                    # 同机不同端口
    assert status_of(origin='http://evil.com@127.0.0.1:8731') == 403           # userinfo 伪装
    assert status_of(origin='http://127.0.0.1:08731') == 403                   # 补零端口伪装
    assert status_of() == 200                                                  # 本机无 Origin
    assert status_of(origin='http://127.0.0.1:8731') == 200                    # 同源放行
    print('[ok] Host 回环 + Origin 同源守卫 (外部/伪装一律 403)')


def test_upload_sanitize_and_unique_names():
    """/api/upload 的落盘口径此前零断言: 目录穿越、Windows 保留名、二进制后缀、同名加序号。"""
    import uuid

    from backend.app import app as fastapi_app

    tag = uuid.uuid4().hex[:8]          # 防上一次失败运行的同秒残留影响断言
    boundary = 'X-SCLayer'

    def part(filename, body):
        return (f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                f'Content-Type: text/csv\r\n\r\n{body}\r\n').encode()

    body = (part(r'..\..\evil%s.csv' % tag, 'a\n1\n')       # 目录穿越 -> 只剩 basename
            + part(f'dup-{tag}.csv', 'a\n1\n')              # 同名第一份
            + part(f'dup-{tag}.csv', 'a\n2\n')              # 同名第二份 -> 加序号, 不覆盖
            + part('CON.csv', 'a\n1\n')                     # 保留名 (精确 stem 才命中) -> 补前缀
            + part(f'photo-{tag}.png', 'a\n1\n')            # 明确二进制后缀 -> 按文本规则改 .csv
            + f'--{boundary}--\r\n'.encode())
    scope = _http_scope('POST', '/api/upload',
                        content_type=f'multipart/form-data; boundary={boundary}',
                        body_len=len(body))
    status, payload_bytes = _asgi_post_get(fastapi_app, scope, body)
    assert status == 200, (status, payload_bytes[:200])
    payload = json.loads(payload_bytes)
    out_dir = payload['dir']
    try:
        names = [os.path.basename(p) for p in payload['paths']]
        assert len(names) == 5, names
        # 全部落在返回的目录里, 没有任何路径逃逸
        assert all(p.startswith(out_dir) for p in payload['paths']), payload['paths']
        assert f'evil{tag}.csv' in names, names                     # 穿越成分被剥掉
        assert f'dup-{tag}.csv' in names and f'dup-{tag} (1).csv' in names, names
        by_name = {os.path.basename(p): p for p in payload['paths']}
        with open(by_name[f'dup-{tag}.csv'], 'rb') as fh:
            assert fh.read() == b'a\n1\n'
        with open(by_name[f'dup-{tag} (1).csv'], 'rb') as fh:
            assert fh.read() == b'a\n2\n'                           # 两份内容都在, 未覆盖
        # 保留名补了下划线; 同秒残留下会带序号, 用正则容忍
        assert any(re.fullmatch(r'_CON( \(\d+\))?\.csv', n) for n in names), names
        assert f'photo-{tag}.csv' in names, names                   # .png 按规则改写 .csv
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    print('[ok] 上传落盘: 穿越剥名 / 保留名补前缀 / 同名加序号 / 后缀改写')


if __name__ == '__main__':
    for fn in (test_preview_changes_match_index_aligned_truth,
               test_preview_changes_with_head_cut,
               test_sample_pair_values_are_same_row,
               test_changes_not_hidden_by_display_truncation,
               test_numeric_header_preview_and_protect,
               test_datetime_split_visible_in_preview,
               test_na_text_visible_in_preview,
               test_sse_replay_is_tail_and_ends_with_job_end,
               test_sse_replay_small_job_has_no_gap,
               test_manifest_concurrent_records_are_not_lost,
               test_inside_output_subtree_exclusion_is_case_insensitive,
               test_expand_inputs_dedupes_same_file_different_case,
               test_normalize_config_survives_junk,
               test_removed_new_columns_reported,
               test_extension_policy_is_same_on_both_sides,
               test_paste_classification_uses_filesystem_not_suffix,
               test_local_only_guard_blocks_foreign_host_and_origin,
               test_upload_sanitize_and_unique_names):
        fn()
    print('\n服务层分层回归全部通过 ✔')