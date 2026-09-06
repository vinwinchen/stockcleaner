# -*- coding: utf-8 -*-
"""cleaner_core 的冒烟测试: python test_cleaner_core.py"""

import os
import sys
import tempfile

from cleaner_core import (parse_numeric, numericize_dataframe, normalize_dates,
                          read_table, clean_table, process_file, strip_tokens_pass)

import pandas as pd

# GBK 控制台上打印 ✔ 会抛 UnicodeEncodeError, 让"全部通过"以退出码 1 收场。
# 不改编码只兜错误: 终端里中文照常可读, 不可编码的字符降级成 ? 。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='replace')


def test_parse_numeric():
    assert parse_numeric('1,234.5') == 1234.5
    assert parse_numeric(' 1,234 ') == 1234.0
    assert parse_numeric('1.5亿') == 150000000
    assert parse_numeric('1,234.5万') == 12345000
    assert parse_numeric('1.5万亿') == 1.5e12
    assert parse_numeric('3千万') == 3e7
    assert parse_numeric('２３４') == 234.0
    assert parse_numeric('¥1,234') == 1234.0
    assert parse_numeric('1,234元') == 1234.0
    assert parse_numeric('(1,234)') == -1234.0
    assert parse_numeric('-45.6') == -45.6
    assert parse_numeric('0.5') == 0.5
    # 标识符 / 非数值
    assert parse_numeric('000001') is None
    assert parse_numeric('00700') is None
    assert parse_numeric('待定') is None
    assert parse_numeric('2023-01-01') is None
    assert parse_numeric('12:10') is None
    assert parse_numeric('') is None
    assert parse_numeric(123) is None  # 非字符串不处理
    assert parse_numeric('万') is None
    print('[ok] parse_numeric')


def test_numericize_id_column():
    df = pd.DataFrame({'code': ['000001', '600519', '00700'],
                       'amt': ['1,234', '1.5万', '待定']})
    rep = numericize_dataframe(df)
    assert rep['id_columns'] == ['code']
    assert list(df['code']) == ['000001', '600519', '00700']  # 整列保留文本
    assert df['amt'].tolist()[0] == 1234.0
    assert df['amt'].tolist()[1] == 15000.0
    assert df['amt'].tolist()[2] == '待定'  # 失败保留原值
    assert rep['numeric_cells'] == 2 and rep['unit_cells'] == 1
    print('[ok] numericize_dataframe (id column guard)')


def test_dates():
    df = pd.DataFrame({'交易日期': ['2023年1月1日', '2023/2/3', '待定', 12345],
                       'val': [1, 2, 3, 4]})
    n, cols, created = normalize_dates(df)
    assert cols == ['交易日期'] and n == 2 and created == []
    assert df['交易日期'].tolist()[0] == '2023-01-01'
    assert df['交易日期'].tolist()[1] == '2023-02-03'
    assert df['交易日期'].tolist()[2] == '待定'      # 保留
    assert df['交易日期'].tolist()[3] == 12345       # 数字不动
    # 非法日期保留
    assert _parse('2023.2.30') is None
    assert _parse('2023-13-01') is None
    print('[ok] normalize_dates (content-driven)')


def _parse(v):
    from cleaner_core import _parse_date_value
    return _parse_date_value(v)


def test_strip_units_conflict():
    df = pd.DataFrame({'a': ['1.5亿', '2万', 'x亿']})
    config = {'strip_tokens': ['亿', '*'], 'convert_units': True,
              'numericize': True, 'drop_empty_rows': False,
              'drop_empty_cols': False, 'head_cut': 0, 'tail_cut': 0}
    out, rep = clean_table(df, config)
    assert out['a'].tolist()[:2] == [150000000.0, 20000.0]  # 单位换算生效
    assert out['a'].tolist()[2] == 'x亿'  # 换算失败, "亿" 未被去字符误删
    assert any('单位换算' in w for w in rep['warnings'])
    print('[ok] strip/unit conflict guard')


def test_read_and_pipeline():
    tmp = tempfile.mkdtemp()
    # UTF-8 CSV, 逗号分隔, 含股票代码与千分位
    p1 = os.path.join(tmp, 'a.csv')
    with open(p1, 'w', encoding='utf-8') as f:
        f.write('代码,日期,成交额,备注\n')
        f.write('000001,2023/1/5,"1,234.5万",ok\n')
        f.write('600519,2023年2月6日,3亿,\n')
    df, meta = read_table(p1)
    assert meta['encoding'] == 'utf-8'
    assert meta['delimiter'] == "','"
    assert list(df['代码']) == ['000001', '600519']  # 读取阶段保真

    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': True,
              'drop_empty_cols': True, 'strip_tokens': [], 'convert_units': True,
              'numericize': True, 'normalize_dates': True,
              'strip_column_mode': False, 'strip_column': ''}
    df2, rep = clean_table(df, config)
    assert df2['代码'].tolist() == ['000001', '600519']  # 标识符列未被破坏
    assert df2['成交额'].tolist() == [12345000.0, 300000000.0]
    assert df2['日期'].tolist() == ['2023-01-05', '2023-02-06']
    assert df2['备注'].tolist()[1] == ''   # 空字段保真读入就是空串, 不再被换成 NaN

    # GBK + Tab 分隔的 txt
    p2 = os.path.join(tmp, 'b.txt')
    with open(p2, 'w', encoding='gbk') as f:
        f.write('名称\t数值\n贵州茅台\t1,900.00\n平安银行\t12.5\n')
    df3, meta3 = read_table(p2)
    assert meta3['encoding'] == 'gb18030'
    assert meta3['delimiter'] == "'\\t'"
    assert list(df3['名称']) == ['贵州茅台', '平安银行']

    # 端到端
    report, out_path = process_file(p1, tmp, config)
    assert os.path.exists(out_path)
    assert report['rows_out'] == 2
    print('[ok] read_table + clean_table + process_file')


def test_head_tail_cut():
    df = pd.DataFrame({'a': range(10)})
    config = {'head_cut': 2, 'tail_cut': 3, 'drop_empty_rows': False,
              'drop_empty_cols': False, 'strip_tokens': [], 'numericize': False,
              'normalize_dates': False, 'convert_units': False}
    out, rep = clean_table(df, config)
    assert len(out) == 5 and rep['dropped_rows'] == 5
    try:
        clean_table(pd.DataFrame({'a': range(3)}), {**config, 'head_cut': 5})
        assert False, '应当抛出裁剪后为空'
    except ValueError:
        pass
    print('[ok] head/tail cut + empty guard')


def test_int_representation():
    # 对抗性回归: 整数不得漂移成 x.0
    assert parse_numeric('600519') == 600519
    assert isinstance(parse_numeric('600519'), int)
    assert isinstance(parse_numeric('1.5万'), int) and parse_numeric('1.5万') == 15000
    assert isinstance(parse_numeric('1.5万亿'), int)
    assert isinstance(parse_numeric('1234.5'), float)
    assert isinstance(parse_numeric('0.5'), float)
    print('[ok] integer representation (no x.0 drift)')


def test_duplicate_stem_no_overwrite():
    tmp = tempfile.mkdtemp()
    d1, d2 = os.path.join(tmp, 'd1'), os.path.join(tmp, 'd2')
    os.makedirs(d1), os.makedirs(d2)
    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False,
              'drop_empty_cols': False, 'strip_tokens': [], 'convert_units': False,
              'numericize': False, 'normalize_dates': False,
              'strip_column_mode': False, 'strip_column': ''}
    stem_map = {}
    outs = []
    for d, v in ((d1, '1'), (d2, '2')):
        p = os.path.join(d, 'x.csv')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('a\n%s\n' % v)
        stem_map[os.path.abspath(p)] = os.path.basename(d) + '_x'
        _, out = process_file(p, tmp, {**config, 'stem_map': stem_map})
        outs.append(out)
    assert outs[0] != outs[1], '重名文件输出被覆盖!'
    assert os.path.exists(outs[0]) and os.path.exists(outs[1])
    print('[ok] duplicate stems produce distinct outputs')


def test_all_empty_columns_guard():
    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False,
              'drop_empty_cols': True, 'strip_tokens': [], 'numericize': False,
              'convert_units': False, 'normalize_dates': False,
              'strip_column_mode': False, 'strip_column': ''}
    try:
        clean_table(pd.DataFrame({'a': [None, None]}), config)
        assert False, '全空列应被拦截'
    except ValueError:
        pass
    print('[ok] all-empty columns guarded')


def test_ragged_and_index_shift():
    # 对抗性回归: 单列 CSV 带千分位 -> pandas 曾把首列静默当索引, 数据损坏
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'mix.csv')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('v\n1,234\n\n待定\n-2,000\n')
    df, meta = read_table(p)
    vals = [str(v) for v in df['v'].tolist()]
    assert '1,234' in vals and '-2,000' in vals and '待定' in vals, vals
    # 行号索引的正常双列表不受影响
    p2 = os.path.join(tmp, 'idx.csv')
    with open(p2, 'w', encoding='utf-8') as f:
        f.write('a,b\n0,10\n1,20\n2,30\n')
    df2, _ = read_table(p2)
    assert list(df2['a']) == ['0', '1', '2'] or list(df2.index) == [0, 1, 2]
    print('[ok] ragged rows preserved, no silent index shift')


def test_numeric_precision():
    # 超过 2^53 的纯整数不得丢精度; 科学计数法/括号带单位
    assert parse_numeric('9,007,199,254,740,993') == 9007199254740993
    assert isinstance(parse_numeric('9,007,199,254,740,993'), int)
    assert parse_numeric('1.5e9') == 1500000000
    assert parse_numeric('1.5E+9') == 1500000000
    assert parse_numeric('(1,234万)') == -12340000
    assert parse_numeric('-1.5万') == -15000
    assert parse_numeric('12.5%') is None   # 百分比歧义, 保留原值
    print('[ok] numeric precision (int64+, sci notation, paren+unit)')


def test_fullwidth_channel():
    """全角转半角是显式通道: 默认关, 开着才动文本; 且不能绕过标识符保护。"""
    from cleaner_core import normalize_fullwidth

    # 默认关: 与历史行为完全一致 (日期路径不做全角翻译)
    df = pd.DataFrame({'d': ['２０２３．１．８'], 'v': ['１２３４．５']})
    out, rep = clean_table(df.copy(deep=True), {'fullwidth': False, 'numericize': True,
                                                'normalize_dates': True, 'convert_units': True,
                                                'head_cut': 0, 'tail_cut': 0,
                                                'drop_empty_rows': False, 'drop_empty_cols': False,
                                                'strip_tokens': []})
    assert out['d'].tolist() == ['２０２３．１．８'], '关掉时不得改写文本'
    assert rep['fullwidth_cells'] == 0

    # 开着: 先转半角, 于是日期识别与数值化都能接住
    df2 = pd.DataFrame({'d': ['２０２３．１．８'], 'v': ['１２３４．５']})
    out2, rep2 = clean_table(df2, {'fullwidth': True, 'numericize': True,
                                   'normalize_dates': True, 'convert_units': True,
                                   'head_cut': 0, 'tail_cut': 0,
                                   'drop_empty_rows': False, 'drop_empty_cols': False,
                                   'strip_tokens': []})
    assert out2['d'].tolist() == ['2023-01-08'], out2['d'].tolist()
    assert out2['v'].tolist() == [1234.5], out2['v'].tolist()
    assert rep2['fullwidth_cells'] == 2 and rep2['fullwidth_columns'] == ['d', 'v']

    # 全角代码转完仍是前导零, 标识符保护必须继续接住 (顺序: 全角 -> 数值化)
    df3 = pd.DataFrame({'code': ['０００００１', '６００５１９']})
    out3, rep3 = clean_table(df3, {'fullwidth': True, 'numericize': True,
                                   'normalize_dates': False, 'convert_units': True,
                                   'head_cut': 0, 'tail_cut': 0,
                                   'drop_empty_rows': False, 'drop_empty_cols': False,
                                   'strip_tokens': []})
    assert out3['code'].tolist() == ['000001', '600519'], out3['code'].tolist()
    assert rep3['id_columns'] == ['code'], '全角转换不得把代码列变成可数值化'

    # 字母与标点: 选了"全集含字母", Ａ-Ｚ/ａ-ｚ/／/全角空格都要落进来
    # 注意全角空格是"一个字符 -> 一个半角空格", 不会变成两个
    probe = pd.DataFrame({'t': ['ＨＫＣ／ＡＢＢ　（２０２４）']})
    n, cols = normalize_fullwidth(probe)
    assert n == 1 and cols == ['t']
    assert probe['t'].tolist() == ['HKC/ABB (2024)'], probe['t'].tolist()
    print('[ok] fullwidth channel (kernel default off, id guard intact, letters+punct)')


def test_output_format():
    """输出格式可选, 且扩展名必须与真实内容一致。"""
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, 'in.xlsx')
    pd.DataFrame({'a': ['1,234'], 'b': ['000001']}).to_excel(src, index=False)
    base = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False, 'drop_empty_cols': False,
            'strip_tokens': [], 'numericize': False, 'convert_units': False,
            'normalize_dates': False}

    _, p1 = process_file(src, tmp, dict(base, output_format='keep'))
    assert p1.endswith('in_cleaned.xlsx'), p1
    _, p2 = process_file(src, tmp, dict(base, output_format='csv'))
    assert p2.endswith('in_cleaned.csv'), p2
    rep3, p3 = process_file(src, tmp, dict(base, output_format='both'))
    assert p3.endswith('.xlsx') and rep3['extra_outputs'][0].endswith('.csv'), (p3, rep3)
    # csv 内容真的是 csv (不是改了扩展名的 xlsx)
    with open(p2, encoding='utf-8-sig') as fh:
        assert fh.readline().strip() == 'a,b'
    # 文本源在 keep 下仍出 csv
    txt = os.path.join(tmp, 't.csv')
    with open(txt, 'w', encoding='utf-8') as fh:
        fh.write('a,b\n1,2\n')
    _, p4 = process_file(txt, tmp, dict(base, output_format='keep'))
    assert p4.endswith('t_cleaned.csv'), p4
    print('[ok] output format keep/csv/xlsx+both, extension matches content')


def test_leading_zero_decimal():
    """对抗性回归: 000001.0 / 00700.5 这类带小数点的前导零也是标识符, 不得数值化。

    Excel 导出常把代码存成 "x.0"; 旧正则 [+-]?0\\d+ 对小数形式 fullmatch 失败,
    parse_numeric 会把 000001.0 洗成 1 —— 前导零被静默吃掉。
    """
    assert parse_numeric('000001.0') is None
    assert parse_numeric('00700.5') is None
    assert parse_numeric('-0123.45') is None
    assert parse_numeric('0.5') == 0.5            # 真·小数不受影响
    assert parse_numeric('0,123.5') is None       # 逗号剥掉后是 0123.5, 同样按标识符保留
    df = pd.DataFrame({'code': ['600519', '000001.0', '300750.0']})
    rep = numericize_dataframe(df)
    assert rep['id_columns'] == ['code'], rep
    assert list(df['code']) == ['600519', '000001.0', '300750.0']
    print('[ok] leading-zero decimals guarded (000001.0 stays text)')


def test_protected_columns_kernel():
    """列保护在内核层生效: 预览与正式运行走同一个 clean_table, 不可能分叉。

    回归: 服务层曾在清洗后自行回填保护列, 但正式运行路径 (process_file) 拿到的
    config 已被剥掉 column_overrides, 用户点"保护"的列实际照洗。
    """
    df = pd.DataFrame({'code': ['000001', '600519'],
                       'amt': ['1,234', '5,678'],
                       'd': ['2023/1/1', '2023/1/2']})
    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False,
              'drop_empty_cols': False, 'strip_tokens': [], 'numericize': True,
              'convert_units': True, 'normalize_dates': True,
              'strip_column_mode': False, 'strip_column': '',
              'protected_columns': ['amt']}
    out, rep = clean_table(df.copy(deep=True), config)
    assert out['amt'].tolist() == ['1,234', '5,678'], out['amt'].tolist()
    assert out['code'].tolist() == ['000001', '600519']      # 未保护的代码列走标识符保护
    assert out['d'].tolist() == ['2023-01-01', '2023-01-02']  # 其他列照常清洗
    # 保护列不再被洗: 无改动就无须回退, 计数保持 0 (报告口径 == 净改动)
    assert rep['protected_cells'] == 0, rep['protected_cells']
    assert rep['protected_columns'] == [], rep['protected_columns']
    assert not any('列保护回退' in w for w in rep['warnings']), rep['warnings']

    # 行裁剪与保护叠加: 幸存行按原值回退, 不把裁掉的行塞回来
    config2 = dict(config, head_cut=1, protected_columns=['amt', 'code'])
    df2 = pd.DataFrame({'code': ['000001', '600519'], 'amt': ['1,234', '5,678']})
    out2, _rep2 = clean_table(df2, config2)
    assert len(out2) == 1
    assert out2['amt'].tolist() == ['5,678'] and out2['code'].tolist() == ['600519']

    # 保护列全为空串的行仍会被"删全空行"逻辑删除 (保护不改变行级决策的输入)
    df3 = pd.DataFrame({'amt': ['', 'x']})
    out3, _ = clean_table(df3, dict(config, protected_columns=['amt'], drop_empty_rows=True,
                                    normalize_dates=False, numericize=False,
                                    convert_units=False))
    assert len(out3) == 1 and out3['amt'].tolist() == ['x']
    print('[ok] protected columns revert inside kernel (preview==run path)')


def test_drop_blank_rows_and_cols():
    """删全空行/列必须把空字符串与纯空白也当空 (dtype=object 读入时 CSV 空格就是 '')。"""
    df = pd.DataFrame({'a': ['x', '', '  ', None], 'b': [1, '', '  ', None]})
    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': True, 'drop_empty_cols': False,
              'strip_tokens': [], 'numericize': False, 'convert_units': False,
              'normalize_dates': False, 'strip_column_mode': False, 'strip_column': ''}
    out, rep = clean_table(df, config)
    assert len(out) == 1 and out['a'].tolist() == ['x'], out
    assert rep['dropped_rows'] == 3
    # 全空列同样被删 (旧行为只认 NaN)
    df2 = pd.DataFrame({'a': ['x', 'y'], 'c': ['', '   ']})
    out2, _ = clean_table(df2, dict(config, drop_empty_cols=True))
    assert list(out2.columns) == ['a'], out2.columns
    print('[ok] blank-string rows/cols dropped as empty')


def test_xlsx_big_int_survives_roundtrip():
    """>2^53 的整数写 xlsx 必须按文本: 数值单元格是 float64, 会被舍入丢精度。"""
    big = 2 ** 53 + 1                      # 9007199254740993
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, 'big.xlsx')
    pd.DataFrame({'v': [str(big), '3.14']}).to_excel(src, index=False)
    config = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False, 'drop_empty_cols': False,
              'strip_tokens': [], 'numericize': True, 'convert_units': False,
              'normalize_dates': False, 'strip_column_mode': False, 'strip_column': ''}
    _, out_path = process_file(src, tmp, dict(config, output_format='xlsx'))
    df_out, _meta = read_table(out_path)
    got = [str(v).strip() for v in df_out['v'].tolist()]
    assert str(big) in got, (got, '大整数经 xlsx 往返丢了精度')
    print('[ok] xlsx big-int written as text (2^53+1 survives roundtrip)')


def test_excel_engine_preserves_leading_zero():
    """calamine 快但会做类型推断; dtype=object 是这条路径可用的前提。

    装了 calamine 时必须钉死引擎名 —— 这条测试描述的就是 calamine 路径,
    静默退回 openpyxl 会让断言变成空转。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'code.xlsx')
    pd.DataFrame({'代码': ['000001', '00700'], '额': ['1,234.5万', '2亿']}).to_excel(p, index=False)
    df, meta = read_table(p)
    engine = meta.get('engine')
    try:
        import python_calamine                            # noqa: F401
        has_calamine = True
    except ImportError:
        has_calamine = False
    if has_calamine:
        assert engine == 'calamine', f'已安装 calamine 却用了 {engine!r}'
    assert list(df['代码']) == ['000001', '00700'], (df['代码'].tolist(), engine)
    assert engine, '应回报实际使用的引擎'
    print(f"[ok] excel engine={engine} 保住了前导零 (calamine {'钉死' if has_calamine else '未安装'})")


BASE_CFG = {'head_cut': 0, 'tail_cut': 0, 'drop_empty_rows': False, 'drop_empty_cols': False,
            'strip_tokens': [], 'strip_column_mode': False, 'strip_column': '',
            'convert_units': True, 'numericize': True, 'normalize_dates': False,
            'fullwidth': False}


def test_unit_precision_no_drift():
    """对抗性回归: 单位换算不得经过 float 乘法。

    旧实现是 float(尾数) * 因子, 于是 1.005万 -> 10049.999999999998、
    1.1亿 -> 110000000.00000001 —— 正是原则 9 禁止的表示漂移, 而旧测试只钉了
    1.5万/3千万/1.5万亿 这些二进制能精确表示的尾数, 所以一直全绿。
    """
    assert parse_numeric('1.005万') == 10050
    assert isinstance(parse_numeric('1.005万'), int)
    assert parse_numeric('1.1亿') == 110000000
    assert isinstance(parse_numeric('1.1亿'), int)
    assert parse_numeric('0.29亿') == 29000000
    assert parse_numeric('1.005万亿') == 10 ** 12 + 5 * 10 ** 9
    assert isinstance(parse_numeric('1.005万亿'), int)
    assert parse_numeric('1.005千万') == 10050000
    assert parse_numeric('1.005e3') == 1005          # 科学计数法同一条路
    assert parse_numeric('1.005') == 1.005           # 除不尽才落 float
    assert repr(parse_numeric('1.005')) == '1.005'

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'u.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('募资额\n1.005万\n1.1亿\n0.29亿\n')
    _, out = process_file(p, os.path.join(tmp, 'o'), dict(BASE_CFG, numericize=True))
    body = open(out, encoding='utf-8-sig').read()
    assert '10050' in body and '110000000' in body and '29000000' in body, body
    assert '9999' not in body and '00000001' not in body, body
    print('[ok] 单位换算全程整数 (无 float 表示漂移)')


def test_float_no_double_rounding():
    """对抗性回归: 落 float 必须是字面量的正确舍入, 不许用加法拼浮点。

    旧实现 whole + rest/scale 有两次舍入 (除法一次、加法一次), 1.64 会拼成
    1.6400000000000001、2.97 -> 2.9699999999999998、1.61 -> 1.6099999999999999,
    全部比 float('1.64') 的正确舍入值偏出一个 ULP。
    """
    for s in ('1.64', '2.97', '1.61', '3.33', '0.1', '12.34'):
        got = parse_numeric(s)
        assert got == float(s) and repr(got) == repr(float(s)), (s, got, float(s))
    for i in range(500):                       # 大批量两位小数, 覆盖各种舍入边界
        s = f'{i}.{(i * 37) % 100:02d}'
        assert parse_numeric(s) == float(s), (s, parse_numeric(s), float(s))
    # 单位 / 指数路径同样不许漂
    assert parse_numeric('1.64万') == 16400 and isinstance(parse_numeric('1.64万'), int)
    assert repr(parse_numeric('1.23456千')) == '1234.56'
    assert parse_numeric('2.97e-2') == float('2.97e-2')
    # x.0 形式的大整数仍走精确整数; 整数部分超 2^53 且带非零小数仍保留原值
    assert parse_numeric('9007199254740993.0') == 9007199254740993
    assert isinstance(parse_numeric('9007199254740993.0'), int)
    assert parse_numeric('9007199254740993.5') is None

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'f.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('价\n1.64\n2.97\n1.61\n')
    _, out = process_file(p, os.path.join(tmp, 'o'), dict(BASE_CFG, numericize=True))
    body = open(out, encoding='utf-8-sig').read()
    assert '1.64' in body and '2.97' in body, body
    assert '6400000000000001' not in body and '9699999999999998' not in body, body
    print('[ok] 小数落 float 为正确舍入 (1.64/2.97 不再偏出一个 ULP)')


def test_thousands_grouping_rejected():
    """对抗性回归: 分组不合规时不猜逗号是什么。

    旧实现无条件 replace(',',''), 于是欧式小数 1.234,5 (即 1234.5) 被洗成 1.2345,
    差一千倍; 1,23 / 1,234,5 这类畸形分组也被照单全收。现在一律按解析失败保留原值。
    """
    assert parse_numeric('1.234,5') is None
    assert parse_numeric('1,23') is None
    assert parse_numeric('1,234,5') is None
    assert parse_numeric('12,345,678.90') == 12345678.9
    assert parse_numeric('1,234,567,890') == 1234567890
    assert parse_numeric('(1,23)') is None           # 会计负数同样要校验分组
    print('[ok] 千分位分组不合规 -> 保留原值 (含欧式小数)')


def test_na_text_survives_read():
    """对抗性回归: 读取阶段零破坏 (原则 4), pandas 的默认 NA 名单不算保真。

    read_csv/read_excel 默认把 N/A / NULL / nan / #N/A 这些**文本**换成 NaN,
    而 json_safe 把原值也渲染成空 —— 差异页看不出任何变化, 输出文件里内容已经没了。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'na.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('状态,备注\nN/A,正常\nNULL,已撤\nnan,无\n#N/A,停牌\n')
    d, meta = read_table(p)
    assert d['状态'].tolist() == ['N/A', 'NULL', 'nan', '#N/A'], d['状态'].tolist()
    assert int(d.isna().sum().sum()) == 0, d.isna().sum().to_dict()
    rep, out = process_file(p, os.path.join(tmp, 'o'), dict(BASE_CFG, drop_empty_rows=True))
    body = open(out, encoding='utf-8-sig').read()
    for needle in ('N/A', 'NULL', '#N/A'):
        assert needle in body, (needle, body)

    px = os.path.join(tmp, 'na.xlsx')
    pd.DataFrame({'状态': ['N/A', 'NULL', '-']}).to_excel(px, index=False)
    dx, mx = read_table(px)
    assert dx['状态'].tolist() == ['N/A', 'NULL', '-'], (dx['状态'].tolist(), mx.get('engine'))
    print('[ok] N/A / NULL / nan 在读取与输出全程保住 (CSV + Excel)')


def test_index_shift_keeps_column():
    """对抗性回归: 首列像行号时不再整列消失 (原则 8 的字面要求)。

    旧实现交给 pandas index_col=0, 而写出时 index=False, 于是这一列被静默丢掉、
    零警告 —— 正是原则 8 说"不会做"的那件事。现在补列名保住它并发警告。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'idx.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('a,b\n0,10,20\n1,11,21\n2,12,22\n')
    d, meta = read_table(p)
    assert list(d.columns) == ['(索引列)', 'a', 'b'], list(d.columns)
    assert [str(v) for v in d['(索引列)'].tolist()] == ['0', '1', '2']
    assert any('索引' in w or '多一个字段' in w for w in meta['warnings']), meta['warnings']
    _, out = process_file(p, os.path.join(tmp, 'o'), BASE_CFG)
    back, _ = read_table(out)
    assert list(back.columns) == ['(索引列)', 'a', 'b'], back.columns.tolist()
    print('[ok] 索引移位不再丢列, 且显式留痕')


def test_datetime_splits_into_two_columns():
    """对抗性回归: 带时间的列拆两列, 不再把 14:30 和 09:15 截成同一个日期。"""
    df = pd.DataFrame({'成交时间': [pd.Timestamp('2023-01-05 14:30:00'),
                                    pd.Timestamp('2023-01-05 09:15:00')]})
    out, rep = clean_table(df, dict(BASE_CFG, normalize_dates=True))
    assert list(out.columns) == ['成交时间', '成交时间_时间'], list(out.columns)
    assert out['成交时间'].tolist() == ['2023-01-05', '2023-01-05']
    assert out['成交时间_时间'].tolist() == ['14:30:00', '09:15:00']
    assert rep['date_time_columns'] == [{'from': '成交时间', 'to': '成交时间_时间', 'cells': 2}]
    assert any('拆成' in w for w in rep['warnings']), rep['warnings']

    # 文本源同样拆 (同一份数据不该因为容器不同而表现不同)
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'dt.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('委托时间\n2023-01-05 14:30:00\n2023-01-05 09:15:00\n2023-01-06\n')
    d, _ = read_table(p)
    out2, rep2 = clean_table(d, dict(BASE_CFG, normalize_dates=True))
    assert out2['委托时间'].tolist() == ['2023-01-05', '2023-01-05', '2023-01-06']
    assert out2['委托时间_时间'].tolist() == ['14:30:00', '09:15:00', None]
    assert rep2['date_cells'] == 3

    # 纯日期列不拆
    df3 = pd.DataFrame({'d': [pd.Timestamp('2023-01-05'), pd.Timestamp('2023-01-06')]})
    out3, rep3 = clean_table(df3, dict(BASE_CFG, normalize_dates=True))
    assert list(out3.columns) == ['d'] and rep3['date_time_columns'] == []
    # 非法时间整格保留
    df4 = pd.DataFrame({'d': ['2023-01-05 25:61:00']})
    out4, _ = clean_table(df4, dict(BASE_CFG, normalize_dates=True))
    assert out4['d'].tolist() == ['2023-01-05 25:61:00']
    print('[ok] 日期/时间拆列 (Excel 与文本同口径, 纯日期不拆, 非法值保留)')


def test_delimiter_ignores_quoted_separators():
    """对抗性回归: 嗅探不能数引号里的字符。

    旧实现对前 20 行数原始字符, "分号分隔 + 引号内含多个逗号" 的文件会被误判成
    逗号, 整表塌成一列, 还给出一条误导性的"字段数不一致已修复"。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'semi.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('代码;名称;金额\n000001;"A,B,C,D";"1,234"\n600519;"E,F,G,H";"2,345"\n'
                '00700;"I,J,K,L";"3,456"\n')
        f.write('600000;"M,N,O,P";"4,567"\n')
    d, meta = read_table(p)
    assert meta['delimiter'] == "';'", (meta['delimiter'], list(d.columns))
    assert list(d.columns) == ['代码', '名称', '金额'], list(d.columns)
    assert d['名称'].tolist()[0] == 'A,B,C,D'
    # 表头含逗号、真正的分隔符是 Tab 时, 平票仍优先 Tab
    p2 = os.path.join(tmp, 'tb.txt')
    with open(p2, 'w', encoding='utf-8', newline='') as f:
        f.write('名称,别名\t代码\n贵州茅台,GZMT\t600519\n')
    d2, meta2 = read_table(p2)
    assert meta2['delimiter'] == "'\\t'", meta2['delimiter']
    assert list(d2.columns) == ['名称,别名', '代码']
    print('[ok] 分隔符按结构一致性判定 (引号内字符不计票)')


def test_protected_and_strip_by_string_name():
    """对抗性回归: 列标签不是字符串时, 用户点锁 / 指定列必须仍然生效。"""
    df = pd.DataFrame({2023: ['1,234', '2,000'], 2024: ['3,000', '4,000']})
    out, rep = clean_table(df.copy(deep=True), dict(BASE_CFG, protected_columns=['2023']))
    assert out[2023].tolist() == ['1,234', '2,000'], out[2023].tolist()
    assert out[2024].tolist() == [3000, 4000], out[2024].tolist()
    # 数字标签的锁让该列完全不进清洗通道, 净改动 0 => 无回退可数
    assert rep['protected_cells'] == 0, rep['protected_cells']
    assert rep['protected_columns'] == [], rep['protected_columns']

    # 保护了一个不存在的列 => 必须说一声, 不能静默
    out2, rep2 = clean_table(pd.DataFrame({2023: ['1,234']}),
                             dict(BASE_CFG, protected_columns=['不存在的列']))
    assert any('不存在' in w for w in rep2['warnings']), rep2['warnings']

    # 去字符: 选了"仅指定列"却没列名 -> 显式说明, 不静默空转
    df3 = pd.DataFrame({'a': ['X*1'], 'b': ['Y*2']})
    _o, rep3 = clean_table(df3, dict(BASE_CFG, strip_tokens=['*'], numericize=False,
                                     strip_column_mode=True, strip_column=''))
    assert rep3['stripped_cells'] == 0
    assert any('没有列名' in w for w in rep3['warnings']), rep3['warnings']

    # 按字符串列名指定去字符列, 数字标签也要能中
    df4 = pd.DataFrame({2023: ['X*1'], 2024: ['Y*2']})
    out4, rep4 = clean_table(df4, dict(BASE_CFG, strip_tokens=['*'], numericize=False,
                                       strip_column_mode=True, strip_column='2024'))
    assert out4[2023].tolist() == ['X*1'] and out4[2024].tolist() == ['Y2']
    assert rep4['stripped_cells'] == 1
    print('[ok] 非字符串列名下的列保护 / 去字符均生效且留痕')


def test_fullwidth_and_protected_counts():
    """受保护的列不进全角通道: 否则报告说改了 2 格、净改动却是 0 格。"""
    df = pd.DataFrame({'名称': ['Ａ公司', 'Ｂ公司'], '备注': ['x', 'y']})
    out, rep = clean_table(df.copy(deep=True),
                           dict(BASE_CFG, fullwidth=True, protected_columns=['名称']))
    assert rep['fullwidth_cells'] == 0, rep['fullwidth_cells']
    assert rep['fullwidth_columns'] == [], rep['fullwidth_columns']
    assert out['名称'].tolist() == ['Ａ公司', 'Ｂ公司']

    df2 = pd.DataFrame({'名称': ['Ａ公司', 'Ｂ公司'], '备注': ['x', 'y']})
    out2, rep2 = clean_table(df2, dict(BASE_CFG, fullwidth=True))
    assert rep2['fullwidth_cells'] == 2 and rep2['fullwidth_columns'] == ['名称']
    print('[ok] 全角通道跳过保护列 (报告口径 == 净改动)')


def test_numeric_collision_warns():
    """同一列两种写法洗成同一个数值时留痕 (报告期 2023.1 / 2023.10 这类)。"""
    df = pd.DataFrame({'期间': ['2023.1', '2023.10']})
    out, rep = clean_table(df.copy(deep=True), dict(BASE_CFG, numericize=True))
    assert rep['collision_columns'] == ['期间'], rep['collision_columns']
    assert any('两种写法' in w for w in rep['warnings']), rep['warnings']
    # 纯分组差异不算碰撞 (1,234 与 1234 是同一写法的千分位形式)
    df2 = pd.DataFrame({'额': ['1,234', '1234']})
    _o2, rep2 = clean_table(df2.copy(deep=True), dict(BASE_CFG, numericize=True))
    assert rep2['collision_columns'] == [], rep2['collision_columns']
    print('[ok] 数值碰撞留痕, 分组差异不误报')


def test_repair_mangles_duplicate_header():
    """修复路径自己建表, 必须补上 pandas 读表时的表头去重。

    DataFrame(columns=[重复名]) 不会自动改名, 重复标签会让后面所有 df[col]
    返回 DataFrame, 一路炸到写文件。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'dup.csv')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write('代码,代码\n000001,600519,A\n')     # 数据行比表头多一列 -> 走修复
    d, meta = read_table(p)
    labels = [str(c) for c in d.columns]
    assert len(set(labels)) == len(labels), labels      # 没有重复标签
    assert labels == ['(索引列)', '代码', '代码.1'], labels
    assert any('多一个字段' in w for w in meta['warnings']), meta['warnings']
    out, rep = clean_table(d, dict(BASE_CFG, protected_columns=[str(d.columns[0])]))
    assert len(out) == 1
    print('[ok] 修复路径表头去重, 重复标签不再炸管道')


def test_protected_col_deleted_by_option_warns():
    df = pd.DataFrame({'a': ['', '  '], 'b': ['1', '2']})
    _o, rep = clean_table(df, dict(BASE_CFG, drop_empty_cols=True, protected_columns=['a']))
    assert any('被"删除全空列"删掉' in w for w in rep['warnings']), rep['warnings']
    print('[ok] 被保护列被删空列选项移除时会警告')


def test_protected_columns_never_enter_wash_channels():
    """保护列不进去字符/数值化通道: 计数必须等于净改动 (全角通道已有同款断言)。

    旧实现先洗再回退: 数据无损, 但 stripped_cells/numeric_cells 把随后被还原的
    格子也算进去, "报告说改了 N 格"和列检视的 0 格互相打脸。
    """
    df = pd.DataFrame({'名称': ['ＡＢＣ公司', 'ＹＺ集团'],
                       '金额': ['1,234', '5,678'],
                       '备注': ['x*1', 'y*2']})
    out, rep = clean_table(df.copy(deep=True),
                           dict(BASE_CFG, fullwidth=True, strip_tokens=['*'],
                                protected_columns=['名称']))
    assert out['名称'].tolist() == ['ＡＢＣ公司', 'ＹＺ集团']      # 原值未动 (全角也没转)
    assert rep['fullwidth_cells'] == 0, rep
    assert rep['stripped_cells'] == 2, rep     # 只有备注的两格
    assert rep['numeric_cells'] == 2, rep      # 只有金额的两格
    # 指定列去字符撞上保护: 说清楚为什么没生效, 不静默空转
    df2 = pd.DataFrame({'名称': ['a*b'], '备注': ['c*d']})
    out2, rep2 = clean_table(df2, dict(BASE_CFG, strip_tokens=['*'],
                                       strip_column_mode=True, strip_column='名称',
                                       protected_columns=['名称']))
    assert out2['名称'].tolist() == ['a*b'] and out2['备注'].tolist() == ['c*d']
    assert rep2['stripped_cells'] == 0, rep2
    assert any('已被保护' in w for w in rep2['warnings']), rep2['warnings']
    print('[ok] 保护列不进去字符/数值化通道 (报告口径 == 净改动)')


def test_huge_decimal_with_fraction_keeps_original():
    """|整数部分| >= 2^53 且带除不尽的小数: 落 float 必然舍入整数位还写科学计数法。

    按"解析失败"保留原值 —— 宁可不数值化, 也不静默改写 (原则 1)。
    纯整数不受影响: int 全程精确 (原则 9 的承诺只覆盖纯整数)。
    """
    huge = '9007199254740993.5'                                    # 2^53 + 1.5
    assert parse_numeric(huge) is None
    assert parse_numeric('9007199254740992') == 9007199254740992   # 纯整数照旧精确
    df = pd.DataFrame({'额': [huge, '1234.5']})
    out, rep = clean_table(df, dict(BASE_CFG))
    assert out['额'].tolist()[0] == huge, out['额'].tolist()
    assert out['额'].tolist()[1] == 1234.5
    assert rep['numeric_cells'] == 1, rep
    print('[ok] 超 2^53 的小数按解析失败保留原值 (纯整数不受影响)')


def test_utf16_bom_read_and_binary_reject():
    """对抗性回归: 带 BOM 的 UTF-16 是文本, NUL 守卫必须给它放行。

    UTF-16 的文本本身就含 NUL 字节, 旧实现一律被 NUL 守卫当成二进制拒读,
    _decode_bytes 的 utf-16 分支成了死代码。现在: BOM 在 -> 严格解码后照常读;
    无 BOM 的 UTF-16 (无法与二进制区分)、解码后仍含 NUL 的伪 UTF-16
    (UTF-32 顶着 BOM)、BOM 后不是合法 UTF-16 (截断/改名二进制) 仍按二进制拒读。
    """
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, 'u16.csv')
    with open(p, 'wb') as f:
        f.write('代码,名称\n000001,平安银行\n600519,贵州茅台\n'.encode('utf-16'))
    d, meta = read_table(p)
    assert meta['encoding'] == 'utf-16', meta
    assert list(d.columns) == ['代码', '名称'], list(d.columns)
    assert list(d['代码']) == ['000001', '600519'], d['代码'].tolist()
    assert list(d['名称']) == ['平安银行', '贵州茅台'], d['名称'].tolist()

    # 无 BOM 的 UTF-16LE: 仍被 NUL 守卫拦下 (行为不变)
    p2 = os.path.join(tmp, 'u16le.csv')
    with open(p2, 'wb') as f:
        f.write('代码,名称\n000001,平安银行\n'.encode('utf-16-le'))
    try:
        read_table(p2)
        assert False, '无 BOM 的 UTF-16 应被拒读'
    except ValueError:
        pass

    # UTF-32LE 顶着 UTF-16 BOM: 能"解码"但解码后仍含 NUL, 同样拒读
    p3 = os.path.join(tmp, 'u32.csv')
    with open(p3, 'wb') as f:
        f.write(b'\xff\xfe\x00\x00' + '代码\n000001\n'.encode('utf-32-le'))
    try:
        read_table(p3)
        assert False, '解码后仍含 NUL 应被拒读'
    except ValueError:
        pass

    # BOM 后不是合法 UTF-16 (截断): 明确报错, 不落乱码表
    p4 = os.path.join(tmp, 'truncated.csv')
    with open(p4, 'wb') as f:
        f.write(b'\xff\xfe\x61')               # BOM + 半个码元
    try:
        read_table(p4)
        assert False, 'BOM 后非法 UTF-16 应被拒读'
    except ValueError:
        pass
    print('[ok] 带 BOM 的 UTF-16 可读; 无 BOM/伪 BOM/截断仍按二进制拒读')


if __name__ == '__main__':
    test_parse_numeric()
    test_numericize_id_column()
    test_leading_zero_decimal()
    test_dates()
    test_strip_units_conflict()
    test_read_and_pipeline()
    test_head_tail_cut()
    test_drop_blank_rows_and_cols()
    test_int_representation()
    test_duplicate_stem_no_overwrite()
    test_all_empty_columns_guard()
    test_ragged_and_index_shift()
    test_numeric_precision()
    test_protected_columns_kernel()
    test_fullwidth_channel()
    test_output_format()
    test_xlsx_big_int_survives_roundtrip()
    test_excel_engine_preserves_leading_zero()
    test_unit_precision_no_drift()
    test_float_no_double_rounding()
    test_thousands_grouping_rejected()
    test_na_text_survives_read()
    test_index_shift_keeps_column()
    test_datetime_splits_into_two_columns()
    test_delimiter_ignores_quoted_separators()
    test_protected_and_strip_by_string_name()
    test_fullwidth_and_protected_counts()
    test_numeric_collision_warns()
    test_repair_mangles_duplicate_header()
    test_protected_col_deleted_by_option_warns()
    test_protected_columns_never_enter_wash_channels()
    test_huge_decimal_with_fraction_keeps_original()
    test_utf16_bom_read_and_binary_reject()
    print('\n全部测试通过 ✔')
