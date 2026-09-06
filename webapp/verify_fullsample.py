# -*- coding: utf-8 -*-
"""全功能样例逐项实测: 对着一个已启动的本地服务, 用 全功能验证* 三件套
把每条清洗路径的"输入 -> 期望输出"钉成断言。

用法:
  python run.py --port 8720 --no-shell      # 另开一个终端
  python make_fullsample.py                 # 先生成样例 (幂等)
  python verify_fullsample.py [base_url]    # 默认 http://127.0.0.1:8720

覆盖: 读取层 (编码/分隔符/引擎/兜底警告) -> 预览 (数值化全形态/日期全形态/
标识符全形态/全角开关两侧/删空行空列/行裁剪/去字符与单位互斥/样本口径警告)
-> 正式运行 (输出内容逐项核对/列保护贯通 run/大整数往返/both 双格式/同名防覆盖)。
"""

import json
import os
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8720').rstrip('/')
HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, 'samples')
CSV = os.path.join(SAMPLES, '全功能验证.csv')
TXT = os.path.join(SAMPLES, '全功能验证_GBK.txt')
XLSX = os.path.join(SAMPLES, '全功能验证.xlsx')

BIG = '9007199254740993'                      # 2^53 + 1, float64 恰好舍掉的那格

CONFIG = {
    'numericize': True, 'convert_units': True, 'normalize_dates': True,
    'drop_empty_rows': True, 'drop_empty_cols': False, 'head_cut': 0, 'tail_cut': 0,
    'strip_tokens': [], 'strip_column_mode': False, 'strip_column': '',
    'column_overrides': {}, 'sample_rows': 500,
    'fullwidth': True,                        # 产品默认: 全角开
}


def call(path, payload=None, timeout=120):
    status, data = raw_call(path, payload, timeout)
    if status >= 400:
        raise SystemExit(f'[FAIL] {path} -> {status} {json.dumps(data, ensure_ascii=False)}')
    return data


def raw_call(path, payload=None, timeout=120):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, method='POST' if data else 'GET')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode('utf-8'))
        except Exception:                                   # noqa: BLE001
            body = {}
        return exc.code, body


def check(label, cond, detail=''):
    print(f'{"[ok]  " if cond else "[FAIL]"} {label}{"" if cond else " :: " + str(detail)}')
    if not cond:
        raise SystemExit(1)


def sse_collect(job_id, timeout=120):
    req = urllib.request.Request(f'{BASE}/api/jobs/{job_id}/events')
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        deadline = time.time() + timeout
        for raw in resp:
            line = raw.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue
            event = json.loads(line[5:].strip())
            events.append(event)
            if event.get('kind') == 'job_end':
                break
            if time.time() > deadline:
                break
    return events


def run_and_wait(paths, output_dir, config):
    run = call('/api/run', {'paths': paths, 'output_dir': output_dir, 'config': config})
    events = sse_collect(run['job_id'])
    errors = [e for e in events if e.get('kind') == 'file_error']
    check(f'run 完成 ({len(paths)} 个文件, 无失败)', not errors, errors)
    return run


def grid_pair(pv, col, before_s):
    """在预览网格里按"原值"找一格, 返回 [before, after, changed]。"""
    gi = pv['grid']['columns'].index(col)
    for r in pv['grid']['rows']:
        cell = r['cells'][gi]
        if cell[0]['s'] == before_s:
            return cell
    return None


def sample_pair(cols, col, before_s):
    for s in cols[col]['samples']:
        if s['before']['s'] == before_s:
            return s
    return None


def _truth_changes(path, raw_config):
    """按索引对齐独立算一遍"每列真改了几格", 用来核对界面口径。

    列检视曾经把清洗前的列和清洗后的列按位置 zip: 只要删过一行, 第 i 格的原值就去和
    第 i+1 格的结果比, 从没动过的文本列能报出几百格改动。这里刻意不复用服务层的
    analyze_columns —— 复用了就变成"自己证明自己没错"。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for extra in (here, os.path.dirname(here)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import pandas as pd
    from cleaner_core import read_table, clean_table, cell_repr
    from backend import core

    cfg = core.normalize_config(raw_config)
    df, _meta = read_table(path)
    sample = df.head(cfg['sample_rows'])
    original = sample.copy(deep=True)
    cleaned, _rep = clean_table(sample, core.kernel_config(cfg))
    out = {}
    for col in cleaned.columns:
        before = (original[col].reindex(cleaned.index).tolist() if col in original.columns
                  else [None] * len(cleaned))
        out[str(col)] = sum(1 for b, a in zip(before, cleaned[col].tolist())
                            if cell_repr(b) != cell_repr(a))
    return out


def expect_cell(pv, col, before_s, after_s):
    cell = grid_pair(pv, col, before_s)
    check(f'{col}: {before_s!r} -> {after_s!r}',
          cell is not None and cell[1]['s'] == after_s,
          None if cell is None else [cell[0], cell[1]])


def expect_sample(cols, col, before_s, after_s, want_type=None):
    s = sample_pair(cols, col, before_s)
    ok = s is not None and s['after']['s'] == after_s
    if ok and want_type:
        ok = s['after']['t'] == want_type
    check(f'{col}: {before_s!r} -> {after_s!r}' + (f' ({want_type})' if want_type else ''), ok,
          None if s is None else s['after'])


def main():
    meta = call('/api/meta')
    check('meta 服务在线', meta.get('version'), meta)

    missing = [p for p in (CSV, TXT, XLSX) if not os.path.isfile(p)]
    check('全功能样例齐备 (先跑 make_fullsample.py)', not missing, missing)

    sandbox = os.path.join(tempfile.gettempdir(), f'sc-fullverify-{uuid.uuid4().hex[:8]}')
    out_dir = os.path.join(sandbox, 'cleaned')
    os.makedirs(out_dir, exist_ok=True)

    # ---------- 1. 读取层 ----------
    info = call('/api/inspect', {'paths': [CSV, TXT, XLSX], 'config': CONFIG})
    by_name = {i['name']: i for i in info['items']}
    check('csv: utf-8 + 逗号', by_name['全功能验证.csv']['encoding'] == 'utf-8'
          and by_name['全功能验证.csv']['delimiter'] == "','", by_name['全功能验证.csv'])
    check('txt: gb18030 + Tab', by_name['全功能验证_GBK.txt']['encoding'] == 'gb18030'
          and by_name['全功能验证_GBK.txt']['delimiter'] == "'\\t'",
          by_name['全功能验证_GBK.txt'])
    check('txt: 兜底解码带警告 (Big5 立场, 不静默)',
          any('gb18030' in w and '兜底' in w for w in by_name['全功能验证_GBK.txt']['warnings']),
          by_name['全功能验证_GBK.txt']['warnings'])
    check('xlsx: 引擎已回报', by_name['全功能验证.xlsx'].get('engine') in ('calamine', 'openpyxl'),
          by_name['全功能验证.xlsx'])
    check('csv: 601 行 7 列', by_name['全功能验证.csv']['rows'] == 601
          and by_name['全功能验证.csv']['cols'] == 7, by_name['全功能验证.csv'])

    # ---------- 2. 默认预览 (全角开) ----------
    pv = call('/api/preview', {'path': CSV, 'config': CONFIG})
    cols = {c['name']: c for c in pv['columns']}
    check('超过样本线: sampled=True 且带样本口径警告',
          pv['sampled'] and any('样本口径' in w for w in pv['report']['warnings']),
          pv['report']['warnings'])
    check('纯空格空行被删 (空字符串也算空的回归)', pv['report']['dropped_rows'] == 1,
          pv['report']['dropped_rows'])
    check('代码列: 标识符保护命中', cols['代码']['id_protected'], cols['代码'])
    expect_cell(pv, '代码', '000001.0', '000001.0')          # 小数形式不被吃成 1
    expect_cell(pv, '代码', '６００５１９', '600519')         # 全角代码转半角后仍是文本
    expect_sample(cols, '成交额', '1,234.5万', '12345000', 'int')
    expect_sample(cols, '成交额', '1.5万亿', '1500000000000', 'int')
    expect_sample(cols, '成交额', '3千万', '30000000', 'int')
    expect_sample(cols, '成交额', '(1,234)', '-1234', 'int')
    expect_sample(cols, '成交额', '￥9,876', '9876', 'int')
    expect_sample(cols, '成交额', '1,234元', '1234', 'int')
    expect_sample(cols, '成交额', '1.5e9', '1500000000', 'int')
    expect_sample(cols, '成交额', '２３４', '234', 'int')
    expect_sample(cols, '成交额', '-2,000', '-2000', 'int')
    expect_sample(cols, '成交额', BIG, BIG, 'int')            # >2^53 预览逐字符保真
    expect_cell(pv, '成交额', '待定', '待定')                  # 解析失败保留原值
    expect_cell(pv, '涨跌幅', '12.5%', '12.5%')               # 百分比歧义不动
    expect_cell(pv, '涨跌幅', '—', '—')
    expect_cell(pv, '交易日期', '２０２３．１．８', '2023-01-08')  # 全角日期被全角通道接住
    expect_cell(pv, '交易日期', '2023.2.30', '2023.2.30')      # 非法日期保留
    expect_cell(pv, '交易日期', '待定', '待定')
    expect_cell(pv, '名称', 'ＨＫＣ／ＡＢＢ　（２０２４）', 'HKC/ABB (2024)')
    check('全角通道有计数', pv['report']['fullwidth_cells'] > 0, pv['report'])
    check('空白列默认还在 (drop_empty_cols 关)', '空白列' in cols, list(cols))

    # ---------- 2b. 三条"保守优先"的钉 ----------
    # 逗号既可能是千分位也可能是欧式小数点, 语义有歧义时不猜, 保留原值
    expect_cell(pv, '成交额', '1.234,5', '1.234,5')
    check('欧式小数不被重解释 (旧实现会洗成 1.2345)',
          '1.2345' not in json.dumps([s['after']['s'] for s in cols['成交额']['samples']]),
          cols['成交额']['samples'])
    # 同一数值的两种精度写法 (2023.1 / 2023.10) 洗完全分不出来 —— 必须留痕
    check('两种精度写法同一数值 -> 显式警告',
          any('两种写法' in w for w in pv['report']['warnings']), pv['report']['warnings'])
    # N/A / NULL 是文本, 不是缺失值标记: 读取阶段不许把它换成空
    # (expect_cell 是拿"原值"去差异页里找格子, 所以原值一旦被渲染成空就找不到)
    expect_cell(pv, '备注', 'N/A', 'N/A')
    expect_cell(pv, '备注', 'NULL', 'NULL')
    # 带时间的日期拆成两列, 而不是把时分秒截掉 (截掉不可逆)
    split = [c for c in pv['columns'] if c['name'] == '交易日期_时间']
    check('日期含时间分量时拆出 "交易日期_时间" 列', len(split) == 1,
          [c['name'] for c in pv['columns']])
    if split:
        check('拆出的时间列标记为新增列且指向来源列',
              split[0]['kind'] == 'added' and split[0]['split_from'] == '交易日期', split[0])
        check('时间分量真的保住了',
              any(s['after']['s'] == '09:30:00' for s in split[0]['samples']), split[0]['samples'])
    check('拆列写进警告', any('拆成' in w for w in pv['report']['warnings']),
          pv['report']['warnings'])
    check('列检视的改动数 == 按索引对齐的真值 (删过行时最容易串位)',
          {c['name']: c['changed'] for c in pv['columns']} == _truth_changes(CSV, CONFIG),
          ({c['name']: c['changed'] for c in pv['columns']}, _truth_changes(CSV, CONFIG)))

    # ---------- 3. 全角关: 文本列不动, 但数值化路径的全角数字仍接得住 ----------
    pv_off = call('/api/preview', {'path': CSV, 'config': dict(CONFIG, fullwidth=False)})
    off_cols = {c['name']: c for c in pv_off['columns']}
    expect_cell(pv_off, '交易日期', '２０２３．１．８', '２０２３．１．８')
    expect_cell(pv_off, '名称', 'ＨＫＣ／ＡＢＢ　（２０２４）', 'ＨＫＣ／ＡＢＢ　（２０２４）')
    expect_sample(off_cols, '成交额', '２３４', '234', 'int')   # 内核窄表始终做全角数字翻译

    # ---------- 4. 删空列: 混有纯空格的列也要被删 ----------
    pv_dc = call('/api/preview', {'path': CSV, 'config': dict(CONFIG, drop_empty_cols=True)})
    check('空白列被删 (纯空格也算空的回归)', '空白列' not in {c['name'] for c in pv_dc['columns']},
          [c['name'] for c in pv_dc['columns']])

    # ---------- 5. 行裁剪 (小文件, 不受样本线影响) ----------
    cut = call('/api/preview', {'path': TXT, 'config': dict(CONFIG, head_cut=1, tail_cut=2)})
    check('行裁剪: 头 1 + 尾 2', cut['rows_out'] == cut['rows_in'] - 3 and not cut['sampled'],
          (cut['rows_in'], cut['rows_out'], cut['sampled']))

    # ---------- 6. 去字符 + 与单位换算互斥 ----------
    pv_st = call('/api/preview', {'path': CSV,
                                  'config': dict(CONFIG, strip_tokens=['*', '元'])})
    check('去字符: 与单位换算冲突的 "元" 被拦下并警告',
          any('元' in w and '单位换算' in w for w in pv_st['report']['warnings']),
          pv_st['report']['warnings'])
    check('去字符: 有移除计数', pv_st['report']['stripped_cells'] > 0, pv_st['report'])
    expect_cell(pv_st, '备注', '*重点*', '重点')
    expect_cell(pv_st, '备注', '*待复核*', '待复核')
    expect_sample({c['name']: c for c in pv_st['columns']}, '成交额', '1,234元', '1234', 'int')

    # ---------- 7. 正式运行三件套 + 输出逐项核对 ----------
    run_and_wait([CSV, TXT, XLSX], out_dir, CONFIG)
    time.sleep(0.2)
    written = sorted(f for f in os.listdir(out_dir) if not f.startswith('_stockcleaner'))
    check('输出 3 个文件', len(written) == 3, os.listdir(out_dir))
    with open(os.path.join(out_dir, '_stockcleaner_manifest.json'), encoding='utf-8') as fh:
        man = json.load(fh)
    check('产出登记 3 条', len(man['outputs']) == 3, list(man['outputs'])[:1])

    with open(os.path.join(out_dir, '全功能验证_cleaned.csv'), encoding='utf-8-sig') as fh:
        body = fh.read()
    for needle, label in [
        ('000001.0', '小数形式代码原样'),
        ('600519', '全角代码转半角'),
        ('12345000', '千分位+万'),
        ('1500000000000', '万亿整数'),
        ('-1234', '会计负数'),
        ('9876', '货币前缀'),
        ('1234', '元后缀'),
        (BIG, '大整数精确'),
        ('12.5%', '百分比保留'),
        ('2023.2.30', '非法日期保留'),
        ('2023-01-05', '日期统一'),
        ('HKC/ABB (2024)', '全角文本转半角'),
        ('待定', '失败值保留'),
    ]:
        check(f'csv 输出含 {label} ({needle})', needle in body, body[:300])
    check('csv 输出无 .0 漂移 (定点: 千分位万/货币)', '12345000.0' not in body
          and '9876.0' not in body, body[:200])
    check('csv 输出大整数未被舍入 (没有 ...992)', '9007199254740992' not in body, None)

    with open(os.path.join(out_dir, '全功能验证_GBK_cleaned.csv'), encoding='utf-8-sig') as fh:
        check('GBK 源输出为可读 UTF-8', '平安银行' in fh.read())

    # ---------- 8. 列保护贯通正式运行 (关键回归) ----------
    prot_dir = os.path.join(sandbox, 'protect')
    run_and_wait([CSV], prot_dir, dict(CONFIG, column_overrides={'成交额': {'protect': True}}))
    with open(os.path.join(prot_dir, '全功能验证_cleaned.csv'), encoding='utf-8-sig') as fh:
        pbody = fh.read()
    check('列保护 run: 原值逐字保留 ("1,234.5万")', '"1,234.5万"' in pbody, pbody[:300])
    check('列保护 run: 全角原值也回退 (２３４)', '２３４' in pbody, pbody[:300])
    check('列保护 run: 其他列照常清洗 (日期已统一)', '2023-01-05' in pbody, pbody[:300])

    # ---------- 9. both 双格式 ----------
    both_dir = os.path.join(sandbox, 'both')
    run_and_wait([XLSX], both_dir, dict(CONFIG, output_format='both'))
    check('both: xlsx + csv 双产出',
          os.path.isfile(os.path.join(both_dir, '全功能验证_cleaned.xlsx'))
          and os.path.isfile(os.path.join(both_dir, '全功能验证_cleaned.csv')),
          os.listdir(both_dir))

    # ---------- 10. 批量同名防覆盖 ----------
    batch_dir = os.path.join(sandbox, 'batch')
    for sub, v in (('子一', '1'), ('子二', '2')):
        d = os.path.join(batch_dir, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '同名.csv'), 'w', encoding='utf-8') as fh:
            fh.write('a\n%s\n' % v)
    batch_out = os.path.join(batch_dir, 'cleaned')
    run_and_wait([os.path.join(batch_dir, '子一', '同名.csv'),
                  os.path.join(batch_dir, '子二', '同名.csv')], batch_out, CONFIG)
    outs = sorted(f for f in os.listdir(batch_out) if f.endswith('_cleaned.csv'))
    check('同名文件输出互不覆盖', len(outs) == 2 and outs[0] != outs[1], outs)

    print(f'\n全功能实测通过。输出目录: {sandbox}')


if __name__ == '__main__':
    main()
