# -*- coding: utf-8 -*-
"""生成"全功能验证"样例 (samples/): 一份数据踩遍所有清洗路径。

与 make_sample.py 的分工: 那份模拟真实业务导出, 这份是刻意的对抗样例 ——
每个特性列都集齐代表性值, 供 verify_fullsample.py 逐项断言, 也适合在界面里
人工核对每列"原值 -> 结果"。

数据设计:
- 前 15 行为特性行: 数值化全形态 (千分位/中文单位/会计负数/全角货币￥/科学计数法/
  全角数字/大整数/失败保留)、日期全形态 (四种写法/全角日期/非法日期/待定/带时间)、
  标识符全形态 (前导零/小数形式 x.0/全角代码)、全角字母标点、百分比保留,
  以及三条"保守优先"的钉: 分组不合规的欧式小数不猜 (1.234,5)、同一数值的两种
  精度写法要留痕 (2023.1 / 2023.10)、N/A 与 NULL 这类文本不能在读取阶段被换成空;
- 第 16 行是"纯空格行", 第 7 列是"空白列" (空串与纯空格混合):
  纯空格才能钉住"空字符串也算空"这条回归;
- 之后确定性填充, 把总行数顶过预览样本线 (500), 使 sampled=True,
  让"样本口径警告"这条修复也进断言。

三个容器各测一条读取路径:
  全功能验证.csv       utf-8 + 逗号
  全功能验证_GBK.txt   gbk + Tab (编码兜底警告 + Tab 探测)
  全功能验证.xlsx      Excel 引擎 (calamine/openpyxl + dtype=object)
"""

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'samples')

HEADERS = ['代码', '名称', '成交额', '涨跌幅', '交易日期', '备注', '空白列']

FEATURE_ROWS = [
    # 代码       名称              成交额            涨跌幅     交易日期          备注        空白列
    ['000001', '平安银行', '1,234.5万', '12.5%', '2023/1/5', '*重点*', ''],
    ['00700', 'ＴＥＣＨ（港）', '3亿', '-3.2%', '2023年1月6日', '正常备注', '   '],
    ['300750', '宁德时代', '1.5万亿', '0.5%', '2023.01.07', '', ''],
    ['600519', 'ＨＫＣ／ＡＢＢ　（２０２４）', '3千万', '10%', '2023-01-08', '含,逗号', '   '],
    ['000001.0', '贵州茅台', '(1,234)', '—', '２０２３．１．８', '', ''],
    ['６００５１９', '五粮液', '￥9,876', '2.1%', '2023.2.30', '*待复核*', '   '],
    ['601398', '工商银行', '1,234元', '0.0%', '2023/2/1', '', ''],
    ['000002.0', '招商银行', '9007199254740993', '-1.5%', '2023/2/2', '大整数行', '   '],
    ['00700.5', '中国平安', '1.5e9', '0.8%', '2023/2/3', '', ''],
    ['688981', '中芯国际', '２３４', '—', '待定', '全角数字', '   '],
    ['300059', '东方财富', '待定', '5.5%', '2023/2/6', '', ''],
    ['600036', '兴业银行', '-2,000', '-0.7%', '2023/2/7', '尾行', ''],
    # 三条"保守优先"的钉: 逗号语义有歧义 / 同一数值的两种精度写法 / 文本不是缺失值标记
    ['600000', '浦发银行', '1.234,5', '0.0%', '2023-01-09 09:30:00', 'N/A', ''],
    ['600001', '白云机场', '2023.1', '-0.1%', '2023-01-10', 'NULL', '   '],
    ['600002', '万科企业', '2023.10', '0.2%', '2023-01-11', '', ''],
]
BLANK_ROW = ['   '] * 7          # 全列纯空格: 旧 dropna 删不掉, 新逻辑必须删
PAD_ROWS = 585                   # 15 + 1 + 585 = 601 行 > 预览样本线 500


def build_rows():
    rows = list(FEATURE_ROWS)
    rows.append(BLANK_ROW)
    for i in range(PAD_ROWS):
        rows.append([
            str(630001 + i),                    # 6 位无前导零, 不干扰标识符判定
            f'样例股份{1 + i}',
            f'{1_000 + i:,}',
            f'{i % 10}.0%',
            f'2023/3/{1 + i % 28}',
            f'批量{i}',
            '   ' if i % 2 else '',
        ])
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = build_rows()

    with open(os.path.join(OUT, '全功能验证.csv'), 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(rows)

    # GBK + Tab 变体 (前 6 个特性行): 钉住编码兜底警告、Tab 探测与行裁剪测试
    with open(os.path.join(OUT, '全功能验证_GBK.txt'), 'w', encoding='gbk', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(HEADERS[:5])
        w.writerows(r[:5] for r in FEATURE_ROWS[:6])

    # Excel 变体 (前 5 列特性行)
    try:
        import pandas as pd
        df = pd.DataFrame(FEATURE_ROWS, columns=HEADERS)
        df.to_excel(os.path.join(OUT, '全功能验证.xlsx'), index=False)
    except ImportError as exc:
        print(f'[skip] Excel 样例未生成: {exc}', file=sys.stderr)

    print('全功能样例已写入', OUT)
    for name in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, name)
        print(f'  {name}  {os.path.getsize(p)} B')


if __name__ == '__main__':
    main()
