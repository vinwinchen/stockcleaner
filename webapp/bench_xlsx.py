# -*- coding: utf-8 -*-
"""XLSX 大文件耗时基准: 把"读"和"洗"两段分开计时。

结论要回答的问题是: 慢在读 (openpyxl 建 cell 对象) 还是慢在清洗 (逐格 Python 循环)。
不同答案的修法完全不同, 所以必须分段测, 不能只看总时长。

用法: python bench_xlsx.py [行数]   (默认 50000)
"""

import os
import statistics
import sys
import tempfile
import time

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(ROOT))

from cleaner_core import clean_table, read_table          # noqa: E402
from backend.core import normalize_config, kernel_config  # noqa: E402

COLS = ['代码', '名称', '交易日期', '收盘价', '涨跌幅', '成交额', '总市值',
        '成交量', '市盈率', '振幅', '换手率', '备注']


def gen_rows(n):
    """造典型脏值: 千分位、中文单位、全角、会计负数、混合日期、空值、前导零代码。"""
    units = ['万', '亿', '']
    for i in range(n):
        code = f'{(i % 6000) + 1:06d}'
        yield [
            code,
            f'样本股票{i % 997}',
            ['2023/1/5', '2023年2月6日', '2023.03.07', '2023-04-08'][i % 4],
            f'{10 + (i % 90)},{(i % 97):02d}',
            f'{(i % 2) and "-" or ""}{(i % 9)}.{i % 10}%',
            f'{(i % 5000) + 1:,}.{i % 89:02d}{units[i % 3]}',
            f'{(i % 300) + 1}亿' if i % 5 else f'{(i % 9000) + 1:,}',
            f'{(i % 100000) + 1:,}',
            f'{(i % 40)}.{i % 9:01d}',
            f'({(i % 30)}.{i % 7})' if i % 11 == 0 else f'{(i % 20)}.{i % 6}',
            f'{(i % 99) / 3:.2f}',
            '' if i % 7 else '待定',
        ]


def timed(fn, repeat=3):
    out = None
    costs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        costs.append(time.perf_counter() - t0)
    return min(costs), statistics.median(costs), out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
    rows = list(gen_rows(n))
    path = os.path.join(tempfile.gettempdir(), f'sc_bench_{n}.xlsx')

    t0 = time.perf_counter()
    pd.DataFrame(rows, columns=COLS).to_excel(path, index=False)
    write_cost = time.perf_counter() - t0
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f'样本: {n} 行 x {len(COLS)} 列, {size_mb:.1f} MB (生成 {write_cost:.1f}s)\n')

    cfg = kernel_config(normalize_config({}))
    results = {}

    # 1) 现状: pandas + openpyxl, dtype=object
    def read_openpyxl():
        return pd.read_excel(path, engine='openpyxl', dtype=object)
    results['read  openpyxl (现状)'] = timed(read_openpyxl)

    # 2) calamine (Rust 引擎, pandas>=2.2 官方支持)
    try:
        def read_calamine():
            return pd.read_excel(path, engine='calamine')
        results['read  calamine'] = timed(read_calamine)
    except Exception as exc:                                  # noqa: BLE001
        results['read  calamine'] = (float('nan'), float('nan'), f'失败: {exc}')

    # 3) openpyxl read_only 手工构造 (不建 cell 对象)
    def read_readonly():
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        header = next(it)
        data = [list(r) for r in it]
        wb.close()
        return pd.DataFrame(data, columns=header, dtype=object)
    results['read  openpyxl read_only'] = timed(read_readonly)

    # 4) 清洗阶段单独计时 (用 openpyxl 读到的 df)
    df_for_clean = read_openpyxl()
    t0 = time.perf_counter()
    clean_table(df_for_clean.copy(deep=True), cfg)
    clean_cost = time.perf_counter() - t0

    # 5) 输出阶段
    df_clean, _ = clean_table(df_for_clean.copy(deep=True), cfg)
    out_x = os.path.join(tempfile.gettempdir(), 'sc_bench_out.xlsx')
    out_c = os.path.join(tempfile.gettempdir(), 'sc_bench_out.csv')
    t0 = time.perf_counter(); df_clean.to_excel(out_x, index=False); x_out = time.perf_counter() - t0
    t0 = time.perf_counter(); df_clean.to_csv(out_c, index=False, encoding='utf-8-sig'); c_out = time.perf_counter() - t0

    print(f'{"阶段":<28}{"最快(s)":>10}{"中位(s)":>10}')
    print('-' * 48)
    for label, (lo, med, _out) in results.items():
        print(f'{label:<28}{lo:>10.2f}{med:>10.2f}')
    print(f'{"clean (逐格解析, 全表)":<28}{clean_cost:>10.2f}{clean_cost:>10.2f}')
    print(f'{"to_excel 输出":<28}{x_out:>10.2f}{x_out:>10.2f}')
    print(f'{"to_csv 输出":<28}{c_out:>10.2f}{c_out:>10.2f}')

    # 6) calamine 是否保住前导零与文本保真 (决定它能不能进主路径)
    if 'read  calamine' in results and not results['read  calamine'][0] != results['read  calamine'][0]:
        cal = pd.read_excel(path, engine='calamine')
        first = str(cal['代码'].iloc[0])
        amt = str(cal['成交额'].iloc[0])
        print(f'\ncalamine 保真核对: 代码首值={first!r} 成交额首值={amt!r}')
        print(f'  前导零保住: {first.startswith("00")} | 千分位仍是文本: {"," in amt}')

    for p in (path, out_x, out_c):
        try:
            os.remove(p)
        except OSError:
            pass


if __name__ == '__main__':
    main()
