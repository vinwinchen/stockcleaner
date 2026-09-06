# -*- coding: utf-8 -*-
"""向量化清洗原型 + 差分测试。

目的不是替换内核, 而是回答两个可验证的问题:
1. 能省多少 (按数据形态分别量, 不给单一数字);
2. 结果是否与内核逐格完全一致 (差分, 不是"看起来对")。

原型相对 numericize_dataframe 的三处改动, 按风险从低到高:
  D 消除: 单位计数不再对已解析格子重跑一遍正则, 在解析时顺带数出来 (纯冗余)
  B 收窄: 前导零扫描从"整列"收窄到"候选格" (前导零串只由数字组成, 必在候选集内)
  A+C 预筛: 用 Arrow 字符串内核先筛掉"含禁止字符 => 必然解析失败"的格子
        禁止字符集是 parse_numeric 接受字符集的超集, 所以筛掉的是注定 None 的格子

不做的事: 不把解析结果放进 float64 数组。内核第 9 条要求纯整数以 int 精确运算
(超过 2^53 也不丢), 一旦向量化成 float 数组, 600519 会变成 600519.0、
9007199254740993 会丢精度 —— 那是拿正确性换速度。

用法: python bench_vectorize_proto.py [行数]
"""

import os
import re
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from bench_xlsx import COLS, gen_rows                       # noqa: E402
from cleaner_core import (_FW_TRANS, _UNIT_NUM_RE, _is_leading_zero_token,  # noqa: E402
                          numericize_dataframe, parse_numeric)

# parse_numeric 可能接受的字符全集的超集。出现集合外字符 => 一定返回 None。
_FORBIDDEN = r'[^0-9,.\-+()eE¥$€元万亿千万百十\s０-９，．（）％：￥]'
_FORBIDDEN_RE = re.compile(_FORBIDDEN)


def numericize_fast(df, convert_units=True):
    """与 numericize_dataframe 语义等价的加速实现 (原型)。"""
    report = {'numeric_cells': 0, 'unit_cells': 0, 'id_columns': []}
    for col in df.columns:
        series = df[col]
        if not (pd.api.types.is_object_dtype(series)
                or isinstance(series.dtype, pd.StringDtype)):
            continue
        ss = series.astype('string')
        # 候选 = 非空 且 不含禁止字符。含禁止字符的格子注定解析失败。
        cand = series.notna() & ~ss.str.contains(_FORBIDDEN, regex=True, na=True)
        if not cand.any():
            continue
        sub = series[cand]
        # 前导零判定只看候选格: 前导零串由数字组成, 一定落在候选集内
        if any(_is_leading_zero_token(v) for v in sub):
            report['id_columns'].append(str(col))
            continue

        parsed = pd.Series(index=sub.index, dtype=object)
        values_list = []
        n_unit = 0
        for v in sub:
            r = parse_numeric(v, convert_units)
            values_list.append(r)
            if r is not None and _UNIT_NUM_RE.fullmatch(v.translate(_FW_TRANS).strip()):
                n_unit += 1                                     # D: 解析时顺带计数
        parsed = pd.Series(values_list, index=sub.index, dtype=object)
        ok = parsed.notna()
        n_num = int(ok.sum())
        if not n_num:
            continue
        values = series.astype(object).copy()
        values.loc[ok.index[ok]] = parsed[ok].to_numpy()
        df[col] = values
        report['numeric_cells'] += n_num
        report['unit_cells'] += n_unit
    return report


def numericize_light(df, convert_units=True):
    """只消除冗余, 不引入 Arrow 字符串内核: 对应 D 段 + B 段收窄。

    D 是纯重复劳动 (对 C 已经解析过的格子再跑一遍 translate+正则), 删掉它
    不改变任何判定路径; B 从整列扫描收窄到候选格。
    这一档的风险等级明显低于 A+C 预筛。
    """
    report = {'numeric_cells': 0, 'unit_cells': 0, 'id_columns': []}
    for col in df.columns:
        series = df[col]
        if not (pd.api.types.is_object_dtype(series)
                or isinstance(series.dtype, pd.StringDtype)):
            continue
        is_str = series.map(lambda v: isinstance(v, str))
        if not is_str.any():
            continue
        sample = series[is_str]
        if any(_is_leading_zero_token(v) for v in sample):
            report['id_columns'].append(str(col))
            continue
        values_list = []
        n_unit = 0
        for v in sample:
            r = parse_numeric(v, convert_units)
            values_list.append(r)
            if r is not None and _UNIT_NUM_RE.fullmatch(v.translate(_FW_TRANS).strip()):
                n_unit += 1
        parsed = pd.Series(values_list, index=sample.index, dtype=object)
        ok = parsed.notna()
        n_num = int(ok.sum())
        if not n_num:
            continue
        values = series.astype(object).copy()
        values.loc[ok.index[ok]] = parsed[ok].to_numpy()
        df[col] = values
        report['numeric_cells'] += n_num
        report['unit_cells'] += n_unit
    return report


def snapshot(df):
    """逐格快照: 值 + Python 类型。int 1234 与 float 1234.0 必须能区分开。"""
    return [(v, type(v).__name__) for v in df._values.ravel()]


def diff_report(a, b):
    if a == b:
        return None
    bad = []
    for i, ((va, ta), (vb, tb)) in enumerate(zip(a, b)):
        if va != vb or ta != tb:
            bad.append((i, f'{va!r}:{ta}', f'{vb!r}:{tb}'))
            if len(bad) >= 5:
                break
    return bad


CASES = {}


def build_cases(n):
    # 形态 1: 数值密集 (金融导出的典型行情表)
    CASES['数值密集 (行情表)'] = pd.DataFrame(list(gen_rows(n)), columns=COLS, dtype=object)
    # 形态 2: 文本为主 (成分股清单/行业标签, 数值列少)
    rows = [(f'{(i % 5000) + 1:06d}', f'样本主键{i}', f'行业分类{i % 30}',
             f'备注文本较长的一段内容{i % 7}', f'{1000 + i:,}', '2023-01-05')
            for i in range(n)]
    CASES['文本为主 (成分股清单)'] = pd.DataFrame(
        rows, columns=['代码', '名称', '行业', '备注', '市值', '日期'], dtype=object)
    # 形态 3: 几乎全是脏文本 (需要大量"解析失败保留原值"的路径)
    rows = [(f'待定{i%3}', f'—', f'{i}%', f'N/A', f'({i}万)') for i in range(n)]
    CASES['脏文本混杂 (待定/百分比/占位)'] = pd.DataFrame(
        rows, columns=['a', 'b', 'c', 'd', 'e'], dtype=object)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
    build_cases(n)
    print(f'每组 {n:,} 行\n')
    print(f'{"数据形态":<22}{"内核":>7}{"去冗余":>8}{"再预筛":>8}   差分 (两档都要过)')
    print('-' * 70)
    for label, df in CASES.items():
        base = df.copy(deep=True)
        t0 = time.perf_counter(); ra = numericize_dataframe(base); ca = time.perf_counter() - t0
        snap_a = snapshot(base)

        d1 = df.copy(deep=True)
        t0 = time.perf_counter(); r1 = numericize_light(d1); c1 = time.perf_counter() - t0
        ok1 = diff_report(snap_a, snapshot(d1)) is None and r1 == ra

        d2 = df.copy(deep=True)
        t0 = time.perf_counter(); r2 = numericize_fast(d2); c2 = time.perf_counter() - t0
        ok2 = diff_report(snap_a, snapshot(d2)) is None and r2 == ra

        verdict = ('两档逐格一致' if (ok1 and ok2)
                   else f'{"去冗余" if not ok1 else ""}{" 预筛" if not ok2 else ""} 不一致')
        print(f'{label:<22}{ca:>6.2f}s{c1:>7.2f}s{c2:>7.2f}s   {verdict}'
              f'   省 {(1 - c1 / ca) * 100:>4.0f}% / {(1 - c2 / ca) * 100:>4.0f}%')


if __name__ == '__main__':
    main()
