# -*- coding: utf-8 -*-
"""清洗热路径的分段成本剖析: 7.5s 到底花在哪四个循环上。

numericize_dataframe 里有四段逐格循环, 不拆开看就没法判断"向量化"值不值得做:
  A. isinstance 过滤 (哪些格子是字符串)
  B. 前导零扫描 (判定标识符列, 整列扫!)
  C. parse_numeric 主解析
  D. 中文单位计数 (对 C 已成功的格子再跑一次正则 = 重复劳动)

用法: python bench_clean_stages.py [行数]
"""

import os
import re
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from bench_xlsx import COLS, gen_rows          # noqa: E402
from cleaner_core import (_FW_TRANS, _LEADING_ZERO_RE, _UNIT_NUM_RE,  # noqa: E402
                          _is_leading_zero_token, parse_numeric)


def build(n):
    return pd.DataFrame(list(gen_rows(n)), columns=COLS, dtype=object)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
    df = build(n)
    cells = df.size
    print(f'{n} 行 x {len(COLS)} 列 = {cells:,} 格\n')

    t = {}
    marks = {}

    def tick(label, t0):
        t[label] = time.perf_counter() - t0
        return time.perf_counter()

    t0 = time.perf_counter()
    str_masks = {}
    for col in df.columns:
        s = df[col]
        str_masks[col] = s.map(lambda v: isinstance(v, str))
    t0 = tick('A  isinstance 过滤', t0)

    id_hits = {}
    for col in df.columns:
        sample = df[col][str_masks[col]]
        id_hits[col] = sample.map(_is_leading_zero_token).any()
    t0 = tick('B  前导零整列扫描', t0)

    parsed_all = {}
    for col in df.columns:
        sample = df[col][str_masks[col]]
        parsed_all[col] = pd.Series([parse_numeric(v, True) for v in sample],
                                    index=sample.index, dtype=object)
    t0 = tick('C  parse_numeric 主解析', t0)

    unit_total = 0
    for col in df.columns:
        p = parsed_all[col]
        ok = p.notna()
        sample = df[col][str_masks[col]]
        unit_total += int(sample[ok].map(
            lambda v: _UNIT_NUM_RE.fullmatch(v.translate(_FW_TRANS).strip()) is not None
        ).sum())
    t0 = tick('D  中文单位二次计数', t0)

    print(f'{"阶段":<26}{"耗时":>9}   占比')
    print('-' * 48)
    total = sum(t.values())
    for k, v in t.items():
        print(f'{k:<26}{v:>8.2f}s   {v / total * 100:>4.1f}%')
    print('-' * 48)
    print(f'{"合计":<26}{total:>8.2f}s')
    print(f'\n单位计数结果 unit_cells={unit_total:,}')

    # 关键判断: 有多少格子根本不需要进 parse_numeric, 以及能不能向量化筛掉它们。
    # 允许字符集是 parse_numeric 接受集合的超集 -> 不在集合内的格子必然解析失败,
    # 所以跳过它们不影响正确性 (保守预筛)。
    pat = r'[^0-9,.\-+()eE¥$€元万亿千万百十\s０-９，．（）％：￥]'
    probe = df['名称']
    t0 = time.perf_counter()
    py_hits = int(probe.map(lambda v: isinstance(v, str) and not re.search(pat, v)).sum())
    py_cost = time.perf_counter() - t0
    t0 = time.perf_counter()
    mask = ~probe.astype('string').str.contains(pat, regex=True, na=False)
    arrow_cost = time.perf_counter() - t0
    arrow_hits = int(mask.sum())
    print(f'\n保守预筛 (文本列 {len(probe):,} 格): 必然可跳过 {len(probe) - arrow_hits:,} 格')
    print(f'  Python 逐格 {py_cost:.2f}s (命中 {py_hits:,}) vs '
          f'Arrow 向量化 {arrow_cost:.2f}s (命中 {arrow_hits:,})')
    print(f'  两者命中一致: {py_hits == arrow_hits}')


if __name__ == '__main__':
    main()
