# -*- coding: utf-8 -*-
"""生成演示/自测用的样例数据 (含金融表格里的典型脏值)。

用法: python make_sample.py            -> 写到 samples/
覆盖的用例: 股票代码前导零、千分位、中文单位、全角、会计负数、
多种日期写法、GBK 编码、Tab 分隔、表头说明行、全空行、坏行字段数不一致,
以及三条 v2.2 的保守立场: N/A 这类文本不是缺失值标记 (读取阶段不许换成空)、
分组不合规的 1.234,5 不猜、首列像行号时保住整列而不是丢掉。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'samples')

A_SHARE = """代码,名称,交易日期,收盘价,涨跌幅,成交额,总市值,备注
000001,平安银行,2023/1/5,12.30,-1.20,"1,234.56万","1,980.5亿",
600519,贵州茅台,2023年1月6日,1800.00,0.85,3.2亿,"22,600亿",机构净买入
000002,万科A,2023.01.07,18.75,(2.50),"12,345,678",2300亿,
300750,宁德时代,２０２３．１．８,398.00,2.10,5.6亿元,1.75万亿,待复核
00700,腾讯控股,2023-01-09 09:30:00,340.20,-0.40,1.2亿,3.2万亿,港股通
688981,中芯国际,2023/1/10,55.80,0.00,待定,4500亿,N/A
000001,平安银行,2023-01-11,,,-1234.5,"1.234,5",空值行

002594,比亚迪,2023-1-12,245.60,1.35,8,900万,2800亿,坏行字段多一列
"""

REPORT_TXT = "证券代码\t证券名称\t期初市值\t期末市值\t区间收益\n600036\t招商银行\t１２３４．５６万\t2,345.67万\t-12.34%\n000651\t格力电器\t5.6亿\t6.2亿\t1,000万\n\t\t\t\t\n601318\t中国平安\t3.45万亿\t3.50万亿\t450亿\n"


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'A股样例_202301.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        f.write(A_SHARE)
    # GBK + Tab 的 txt (通达信等老系统导出常见形态)
    with open(os.path.join(OUT, '持仓明细_老导出.txt'), 'w', encoding='gbk', newline='') as f:
        f.write(REPORT_TXT)
    # 带说明行的 Excel 式表头 (前 2 行是标题/单位, 需要行裁剪)
    try:
        import pandas as pd
        rows = [
            ['XX证券公司 2023 年 1 月成交统计', None, None, None],
            ['单位: 股 / 元', None, None, None],
            ['代码', '成交量', '成交金额', '首次成交日'],
            ['000001', '123,456', '1,234.5万', '2023/1/5'],
            ['600519', '2,345', '3.2亿', '2023年1月6日'],
            ['000651', '456,789', '(5,678.90万)', '2023.01.07'],
        ]
        pd.DataFrame(rows[2:], columns=rows[0]).to_excel(
            os.path.join(OUT, '月度成交统计.xlsx'), index=False)
        with pd.ExcelWriter(os.path.join(OUT, '月度成交统计.xlsx'), engine='openpyxl',
                            mode='a', if_sheet_exists='replace') as w:
            pd.DataFrame(rows).iloc[:, :4].to_excel(w, index=False, header=False)
    except ImportError as exc:
        print(f'[skip] Excel 样例未生成: {exc}', file=sys.stderr)
    print('样例已写入', OUT)
    for name in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, name)
        print(f'  {name}  {os.path.getsize(p)} B')


if __name__ == '__main__':
    main()
