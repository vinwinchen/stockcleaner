# -*- coding: utf-8 -*-
"""服务层契约自测: 对着一个已启动的本地服务跑完整链路。

用法:
  python run.py --port 8720 --no-shell      # 另开一个终端
  python api_selftest.py [base_url]         # 默认 http://127.0.0.1:8720

覆盖: meta -> collect -> inspect -> preview(列保护/规则变更) -> run -> SSE -> 输出内容核对,
另含两组关键回归: 列保护贯通正式运行路径 (曾只在预览生效)、>2^53 大整数全程零精度丢失。
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

CONFIG = {
    'numericize': True, 'convert_units': True, 'normalize_dates': True,
    'drop_empty_rows': True, 'drop_empty_cols': False, 'head_cut': 0, 'tail_cut': 0,
    'strip_tokens': [], 'strip_column_mode': False, 'strip_column': '',
    'column_overrides': {}, 'sample_rows': 500,
    # 基线显式钉关: 让"全角日期保留原值"这类断言继续描述内核原始路径。
    # 产品默认是开的, 由下面第 6 组用"不传该键"的请求单独钉住。
    'fullwidth': False,
}

FALLBACK_CONFIG = dict(CONFIG, numericize=True, normalize_dates=True)


def call(path, payload=None, timeout=60):
    status, data = raw_call(path, payload, timeout)
    if status >= 400:
        raise SystemExit(f'[FAIL] {path} -> {status} {json.dumps(data, ensure_ascii=False)}')
    return data


def raw_call(path, payload=None, timeout=60):
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


def sse_collect(job_id, want_done, timeout=60):
    """读 SSE 直到 done 数达标; 顺带验证事件可重放 (断开再连一次)。"""
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


def main():
    meta = call('/api/meta')
    check('meta 服务在线', meta.get('version'), meta)

    files = [os.path.join(SAMPLES, n) for n in
             ('A股样例_202301.csv', '持仓明细_老导出.txt', '月度成交统计.xlsx')]
    files = [f for f in files if os.path.isfile(f)]
    check('样例文件齐备', len(files) == 3, files)

    sandbox = os.path.join(tempfile.gettempdir(), f'sc-selftest-{uuid.uuid4().hex[:8]}')
    out_dir = os.path.join(sandbox, 'cleaned')
    os.makedirs(out_dir, exist_ok=True)

    collected = call('/api/collect', {'files': files, 'dirs': [], 'recursive': True,
                                      'output_dir': out_dir})
    check('collect 展开 3 个文件', collected['count'] == 3, collected)
    plan = {p['name']: p for p in collected['plan']}
    check('输出名带 _cleaned 后缀',
          plan['A股样例_202301.csv']['out_name'].endswith('_cleaned.csv'), plan)
    check('Excel 输出转 xlsx',
          plan['月度成交统计.xlsx']['out_name'].endswith('_cleaned.xlsx'), plan)

    info = call('/api/inspect', {'paths': files, 'config': CONFIG})
    by_name = {i['name']: i for i in info['items']}
    check('txt 走 GBK 探测', by_name['持仓明细_老导出.txt'].get('encoding') == 'gb18030',
          by_name['持仓明细_老导出.txt'])
    check('txt 走 Tab 探测', by_name['持仓明细_老导出.txt'].get('delimiter') == "'\\t'",
          by_name['持仓明细_老导出.txt'])
    check('csv 列数正确', by_name['A股样例_202301.csv']['cols'] == 8,
          by_name['A股样例_202301.csv'])

    pv = call('/api/preview', {'path': files[0], 'config': CONFIG})
    cols = {c['name']: c for c in pv['columns']}
    check('预览不落盘', not any(f.endswith('_cleaned.csv') for f in os.listdir(out_dir)),
          os.listdir(out_dir))
    check('代码列被识别为标识符', cols['代码']['id_protected'] and cols['代码']['changed'] == 0,
          cols.get('代码'))
    check('成交额发生数值化', cols['成交额']['changed'] > 0, cols.get('成交额'))
    amt = {s['row']: s for s in cols['成交额']['samples'] if s['changed']}
    first = [s for r, s in sorted(amt.items())][0]
    check('千分位+万 -> 整数放大', first['after']['s'] == '12345600', first)
    dates = {s['row']: s for s in cols['交易日期']['samples'] if s['changed']}
    check('2023/1/5 -> 2023-01-05',
          any(s['after']['s'] == '2023-01-05' for s in dates.values()), dates)
    check('2023年1月6日 -> 2023-01-06',
          any(s['after']['s'] == '2023-01-06' for s in dates.values()), dates)
    check('2023.01.07 -> 2023-01-07',
          any(s['after']['s'] == '2023-01-07' for s in dates.values()), dates)

    # 既有行为, 不是本次要改的东西: 内核只在数值化路径做全角翻译, 日期路径不做,
    # 所以全角日期"解析失败 -> 保留原值"。安全, 但与数字列的表现不一致 (已汇报)。
    gcols = pv['grid']['columns']
    di = gcols.index('交易日期')
    fw = [r['cells'][di] for r in pv['grid']['rows'] if '２' in r['cells'][di][0]['s']]
    check('全角日期保留原值 (日期路径不做全角翻译)',
          len(fw) == 1 and fw[0][2] is False and fw[0][0]['s'] == fw[0][1]['s'], fw)
    check('报告含标识符列', '代码' in pv['report']['id_columns'], pv['report'])
    check('百分比不被改写', all('%' not in s['after']['s'] or s['after']['s'] == s['before']['s']
                          for s in cols['涨跌幅']['samples']), cols['涨跌幅']['samples'])
    check('读取阶段留痕进了预览 (坏行修复警告)',
          any('字段数' in w for w in pv['report']['warnings']), pv['report']['warnings'])
    check('坏行内容未被丢弃',
          '坏行字段多一列' in json.dumps(pv, ensure_ascii=False), '损坏行的内容找不到')

    protected = call('/api/preview', {
        'path': files[0],
        'config': dict(CONFIG, column_overrides={'成交额': {'protect': True}})})
    pc = {c['name']: c for c in protected['columns']}
    check('列保护后该列零改动', pc['成交额']['changed'] == 0, pc['成交额'])
    check('列保护不影响其他列', pc['总市值']['changed'] > 0, pc['总市值'])

    cut = call('/api/preview', {
        'path': files[2],
        'config': dict(CONFIG, head_cut=2, drop_empty_cols=False)})
    check('行裁剪生效', cut['rows_out'] == cut['rows_in'] - 2, (cut['rows_in'], cut['rows_out']))

    status, bad = raw_call('/api/preview', {'path': files[0], 'config': dict(CONFIG, head_cut=9999)})
    check('裁空被拦成 422 而不是静默空文件', status == 422 and '为空' in str(bad.get('error')),
          (status, bad))

    run = call('/api/run', {'paths': files, 'output_dir': out_dir, 'config': CONFIG})
    check('run 返回 job_id', bool(run.get('job_id')), run)
    events = sse_collect(run['job_id'], run['total'])
    kinds = [e['kind'] for e in events]
    check('SSE 覆盖全部文件', kinds.count('file_done') + kinds.count('file_error') == run['total'],
          kinds)
    check('事件带 seq 可重放 (hello=0, 之后连续)',
          [e['seq'] for e in events] == list(range(len(events))), [e['seq'] for e in events])
    done = [e for e in events if e['kind'] == 'file_done']
    check('无文件失败', not [e for e in events if e['kind'] == 'file_error'],
          [e for e in events if e['kind'] == 'file_error'])

    time.sleep(0.2)
    written = sorted(f for f in os.listdir(out_dir) if not f.startswith('_stockcleaner'))
    check('输出 3 个文件', len(written) == 3, os.listdir(out_dir))
    mpath = os.path.join(out_dir, '_stockcleaner_manifest.json')
    check('产出登记已写出', os.path.isfile(mpath), mpath)
    with open(mpath, encoding='utf-8') as fh:
        man = json.load(fh)
    check('登记了 3 条产出且带源文件与格式', len(man['outputs']) == 3
          and all(v.get('source') and v.get('format') for v in man['outputs'].values()),
          list(man['outputs'].items())[:1])
    check('登记不污染输入 (输入目录里没有登记文件)',
          not any(f.startswith('_stockcleaner') for f in os.listdir(SAMPLES)),
          os.listdir(SAMPLES))

    csv_out = os.path.join(out_dir, plan['A股样例_202301.csv']['out_name'])
    with open(csv_out, encoding='utf-8-sig') as fh:
        body = fh.read()
    check('输出保留了股票代码前导零', '000001' in body, body[:400])
    check('输出无 pandas 式的 .0 漂移', '12345600.0' not in body and '198050000000.0' not in body,
          body[:400])
    check('输出成交值为数值', '12345600' in body, body[:400])
    check('输出日期已统一', '2023-01-05' in body, body[:400])

    txt_out = os.path.join(out_dir, plan['持仓明细_老导出.txt']['out_name'])
    with open(txt_out, encoding='utf-8-sig') as fh:
        check('GBK 源文件输出为可读 UTF-8', '招商银行' in fh.read())

    # --- 防自吞噬: 从"猜文件名"换成"结构隔离 + 产出登记" ---
    # 1) 输出目录在输入树内时, 按结构排除, 原因是 output_dir
    parent = os.path.dirname(out_dir)
    again = call('/api/collect', {'files': [], 'dirs': [parent], 'recursive': True,
                                  'output_dir': out_dir, 'config': CONFIG})
    reasons = {s['reason'] for s in again['skipped']}
    check('输出目录在输入树内被结构排除', 'output_dir' in reasons, again['skipped'])
    check('排除数量等于已写出的文件数', len(again['skipped']) == 3, again['skipped'])
    # 登记文件自己不算"待处理数据": 它既不该被洗, 也不该占用一条跳过提示
    assert not any('manifest' in s['name'] for s in again['skipped']), again['skipped']

    # 2) 直接扫输出目录本身 (output_dir 就是它自己) 时, 只信任本目录的登记来排除
    scan_out = call('/api/collect', {'files': [], 'dirs': [out_dir], 'recursive': True,
                                     'output_dir': out_dir, 'config': CONFIG})
    check('按登记排除自己的产出', scan_out['count'] == 0
          and {s['reason'] for s in scan_out['skipped']} == {'manifest'}, scan_out['skipped'])
    # 覆盖回归: 用别的输出目录扫同一文件夹时, 不同目录下的登记不再被当作权威,
    # 宁可重洗也不因一份来路不明的清单静默漏文件
    elsewhere = os.path.join(sandbox, 'out2')
    foreign = call('/api/collect', {'files': [], 'dirs': [out_dir], 'recursive': True,
                                    'output_dir': elsewhere, 'config': CONFIG})
    check('不同输出目录的登记不生效, 宁可重洗 (防伪造清单静默跳过)',
          foreign['count'] > 0, foreign['skipped'])

    # 3) 关键回归: 用户自己命名的 *_cleaned 文件不能被误跳过 (旧实现会静默漏文件)
    decoy_dir = os.path.join(sandbox, 'decoy')
    os.makedirs(decoy_dir, exist_ok=True)
    decoy = os.path.join(decoy_dir, 'report_cleaned.csv')
    with open(decoy, 'w', encoding='utf-8') as fh:
        fh.write('a,b\n1,2\n')
    probe = call('/api/collect', {'files': [], 'dirs': [decoy_dir], 'recursive': True,
                                  'output_dir': decoy_dir, 'config': CONFIG})
    check('平铺模式 (输出目录==输入目录) 不会把自己排空, 用户命名的 _cleaned 照洗',
          probe['count'] == 1 and not probe['skipped'], probe)

    # 4) 显式点选登记过的产出, 仍然照洗 (那是明确意图)
    csv_out_explicit = os.path.join(out_dir, 'A股样例_202301_cleaned.csv')
    if os.path.isfile(csv_out_explicit):
        explicit = call('/api/collect', {'files': [csv_out_explicit], 'dirs': [],
                                         'recursive': True, 'output_dir': elsewhere,
                                         'config': CONFIG})
        check('显式点选 _cleaned 文件不被拦', explicit['count'] == 1, explicit)

    # 5) 默认输出建议落在输入父目录下的 cleaned/
    sug = call('/api/collect', {'files': [], 'dirs': [SAMPLES], 'recursive': True,
                                'output_dir': '', 'config': CONFIG})
    check('默认输出建议为 cleaned/ 子目录',
          sug['suggested_output'].endswith('cleaned'), sug['suggested_output'])

    # 6) 全角通道: 产品默认开 (不传该键就该改写), 显式关则保留原值
    no_key = {k: v for k, v in CONFIG.items() if k != 'fullwidth'}
    dflt = call('/api/preview', {'path': files[0], 'config': no_key})
    d_cols = {c['name']: c for c in dflt['columns']}
    check('服务层默认开着全角 (配置里不传该键)',
          dflt['report']['fullwidth_cells'] > 0
          and any(s['after']['s'] == '2023-01-08' for s in d_cols['交易日期']['samples']
                  if s['changed']), dflt['report'])
    fw = call('/api/preview', {'path': files[0], 'config': dict(CONFIG, fullwidth=True)})
    fw_cols = {c['name']: c for c in fw['columns']}
    check('全角通道有计数', fw['report']['fullwidth_cells'] > 0, fw['report'])
    check('默认开 与 显式开 结果一致', fw['report']['fullwidth_cells']
          == dflt['report']['fullwidth_cells'], (fw['report'], dflt['report']))
    check('全角日期在开启后可识别',
          any(s['after']['s'] == '2023-01-08' for s in fw_cols['交易日期']['samples']
              if s['changed']), fw_cols['交易日期']['samples'])
    check('全角代码转完仍受标识符保护', fw_cols['代码']['changed'] == 0
          or all(s['after']['s'].startswith('0') for s in fw_cols['代码']['samples']),
          fw_cols['代码']['samples'])

    # 7) 输出格式可选
    csv_plan = call('/api/collect', {'files': [files[2]], 'dirs': [], 'recursive': True,
                                     'output_dir': elsewhere,
                                     'config': dict(CONFIG, output_format='csv')})
    check('xlsx 输入选 csv 输出时扩展名跟着变',
          csv_plan['plan'][0]['out_name'].endswith('_cleaned.csv'), csv_plan['plan'])
    both_plan = call('/api/collect', {'files': [files[2]], 'dirs': [], 'recursive': True,
                                      'output_dir': elsewhere,
                                      'config': dict(CONFIG, output_format='both')})
    check('both 模式预告第二个输出', both_plan['plan'][0]['extra_names'] == ['月度成交统计_cleaned.csv'],
          both_plan['plan'])

    # 8) 关键回归: 列保护必须贯通正式运行路径 (旧实现只在预览回填, run 照洗)
    #    与 >2^53 大整数端到端零精度丢失 (预览 JSON 与 csv 输出都要逐字符保真)。
    prot_dir = os.path.join(sandbox, 'protect')
    os.makedirs(prot_dir, exist_ok=True)
    prot_src = os.path.join(prot_dir, 'prot.csv')
    with open(prot_src, 'w', encoding='utf-8') as fh:
        fh.write('代码,金额,日期\n000001,"1,234",2023/1/1\n000002,"5,678",2023/1/2\n')
    big = 2 ** 53 + 1                       # 9007199254740993, float64 恰好会舍掉的那格
    big_src = os.path.join(prot_dir, 'big.csv')
    with open(big_src, 'w', encoding='utf-8') as fh:
        fh.write(f'v\n{big}\n9007199254740992\n')
    prot_out = os.path.join(prot_dir, 'cleaned')

    pv_big = call('/api/preview', {'path': big_src, 'config': CONFIG})
    gcols_b = pv_big['grid']['columns']
    vi = gcols_b.index('v')
    got = [r['cells'][vi][1]['s'] for r in pv_big['grid']['rows']]
    check('预览大整数逐字符保真 (>2^53)', str(big) in got, got)

    run_p = call('/api/run', {'paths': [prot_src], 'output_dir': prot_out,
                              'config': dict(CONFIG, column_overrides={'金额': {'protect': True}})})
    sse_collect(run_p['job_id'], run_p['total'])
    with open(os.path.join(prot_out, 'prot_cleaned.csv'), encoding='utf-8-sig') as fh:
        pbody = fh.read()
    check('列保护贯通正式运行 (run 路径不再忽略保护)',
          '"1,234"' in pbody and '"5,678"' in pbody, pbody)

    run_b = call('/api/run', {'paths': [big_src], 'output_dir': prot_out,
                              'config': CONFIG})
    sse_collect(run_b['job_id'], run_b['total'])
    with open(os.path.join(prot_out, 'big_cleaned.csv'), encoding='utf-8-sig') as fh:
        bbody = fh.read()
    check('大整数经正式运行零精度丢失', str(big) in bbody and '9007199254740992' in bbody, bbody)

    # 8) 同名文件防覆盖: /api/run 生成的 stem_map 必须真的拼出不同名
    #    (回归: 旧 build_stem_map 拿"文件自己的目录"当归属根, rel 永远是裸文件名,
    #     两个同名文件输出同一个名字, 后写的覆盖先写的)
    dup_dir = os.path.join(sandbox, 'dup')
    for sub in ('一', '二'):
        d = os.path.join(dup_dir, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'same.csv'), 'w', encoding='utf-8') as fh:
            fh.write('a\n%s\n' % sub)
    dup_out = os.path.join(dup_dir, 'out')
    run2 = call('/api/run', {'paths': [os.path.join(dup_dir, '一', 'same.csv'),
                                       os.path.join(dup_dir, '二', 'same.csv')],
                             'output_dir': dup_out, 'config': CONFIG})
    ev2 = sse_collect(run2['job_id'], run2['total'])
    check('同名文件 run 无失败', not [e for e in ev2 if e['kind'] == 'file_error'], ev2)
    outs2 = sorted(f for f in os.listdir(dup_out) if f.endswith('_cleaned.csv'))
    check('同名文件输出互不覆盖 (不同子目录)', len(outs2) == 2 and outs2[0] != outs2[1], outs2)

    print(f'\n全部通过。输出目录: {out_dir}')
    print('  ' + '\n  '.join(written))


if __name__ == '__main__':
    main()
