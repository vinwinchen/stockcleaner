# -*- coding: utf-8 -*-
"""
数据清洗核心逻辑 (StockCleaner v2.0)

设计原则 (从第一性原理推导):

本工具的本质只有一句话: 在不丢失信息的前提下,
把"看起来是数值"的单元格变成真正的数值,
把"看起来是日期"的单元格变成统一的日期文本。

由此推导出五条规则:
1. 逐值解析, 解析成功才写回; 解析失败一律保留原值 —— 永不静默改写或丢弃数据。
2. 前导零数字串 (如股票代码 000001 / 00700, 含 Excel 导出的 "x.0" 形式 000001.0)
   本质是标识符而非数量: 列中一旦出现, 整列跳过数值化, 避免代码列被破坏。
3. 日期靠内容识别 (2023-01-01 / 2023/1/1 / 2023.01.01 / 2023年1月1日),
   不靠列名猜测 —— "交易日期"这类列名在 v1.x 会被整列漏掉。
4. 编码靠字节探测 (BOM + 严格解码顺序), 分隔符靠内容计数, 不靠 try/except
   碰运气 (v1.x 会把 UTF-8 文件用 GBK 兜底成乱码)。
5. CSV/TXT 统一按文本读入 (dtype=object), 一切类型转换交给显式清洗管道,
   避免 pandas 在读取阶段就把 000001 吃成 1。

每一步的转换量都计入 report, 让用户知道数据到底发生了什么。
"""

import os
import csv
import re
import warnings
from datetime import date, datetime
from io import StringIO

import pandas as pd

__all__ = [
    'parse_numeric', 'read_table', 'save_table', 'clean_table',
    'process_file', 'format_report', 'normalize_fullwidth', 'cell_repr',
]

# ==========================================
# 数值解析
# ==========================================

# 全角 -> 半角
_FW_TRANS = str.maketrans('０１２３４５６７８９，．＋－（）％：￥',
                          '0123456789,.+-()%:$')

# 全角字符全集 (U+FF01..U+FF5E 连同字母一起映射到 ASCII, 另加全角空格 U+3000)。
# 只服务于显式开启的"全角转半角"通道; parse_numeric 仍用上面的窄表, 语义不变。
_FW_TRANS_FULL = str.maketrans({code: chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
                               | {0x3000: ' '})

# 前导零数字串视为标识符。含小数形式 (000001.0 / 00700.5): Excel 导出常把代码
# 存成 "x.0", 它们同样是标识符; 真·小数 (0.5) 的 0 后面不是数字, 不受影响。
_LEADING_ZERO_RE = re.compile(r'[+-]?0\d+(?:\.\d+)?')
_PAREN_NUM_RE = re.compile(r'\(([^()]+)\)')
# 中文单位后缀, 长词在前保证 "万亿/千万" 优先于 "万/千"
_UNIT_NUM_RE = re.compile(r'([+-]?[\d,]+(?:\.\d+)?)\s*(万亿|千亿|百万|千万|十万|亿|万|千)\s*(元)?')
_CURRENCY_RE = re.compile(r'^[$¥€]?\s*([+-]?[\d,]+(?:\.\d+)?)\s*元?$')
# 千分位分组必须是"1-3 位 + 若干组 3 位"。不合规的分组 (1,23 / 1,234,5 / 1.234,5)
# 一律按解析失败保留原值: 逗号既可能是千分位也可能是欧式小数点/小数分隔符,
# 旧实现无条件 replace(',','') 会把欧式 1.234,5 (即 1234.5) 洗成 1.2345 —— 差一千倍。
_GROUPED_RE = re.compile(r'[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?$')
# 十进制字面量, 拆成 整数系数 / 10^位数 做纯整数运算
_DECIMAL_RE = re.compile(r'([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$')

# 因子用 int, 整数路径全程精确 (不经过 float, 超过 2^53 也不丢精度)
_UNIT_FACTORS = {'万亿': 10 ** 12, '千亿': 10 ** 11, '千万': 10 ** 7, '百万': 10 ** 6,
                 '十万': 10 ** 5, '亿': 10 ** 8, '万': 10 ** 4, '千': 10 ** 3}
_UNIT_TOKENS = set(_UNIT_FACTORS) | {'元'}


def _numeric_core(value, convert_units=True):
    """把各种写法收敛成 (负号, 数字串, 单位因子); 不像数返回 None。

    parse_numeric 与"写法碰撞"检测共用这一份规整 —— 两边各写一遍迟早分叉,
    变成"一边认为能洗、另一边认为写法不同"。
    返回的是**去掉正负号后**能被 _DECIMAL_RE 匹配的数字串, 下游不再重复匹配。
    """
    if not isinstance(value, str):
        return None
    s = value.translate(_FW_TRANS).strip()
    if not s:
        return None

    negative = False
    m = _PAREN_NUM_RE.fullmatch(s)
    if m:
        negative = True
        s = m.group(1).strip()

    factor = 1
    if convert_units:
        m = _UNIT_NUM_RE.fullmatch(s)
        if m:
            factor = _UNIT_FACTORS[m.group(2)]
            s = m.group(1)
    m = _CURRENCY_RE.match(s)
    if m:
        s = m.group(1)

    if not s:
        return None
    if ',' in s and not _GROUPED_RE.fullmatch(s):
        # 分组不合规 (1,23 / 1.234,5): 逗号既可能是千分位也可能是欧式小数点, 不猜
        return None
    digits = s.replace(',', '')
    if _LEADING_ZERO_RE.fullmatch(digits):
        return None                      # 前导零是标识符
    if digits[:1] == '-':
        negative = not negative
        digits = digits[1:]
    elif digits[:1] == '+':
        digits = digits[1:]
    if not _DECIMAL_RE.fullmatch(digits):
        return None
    return negative, digits, factor


def _decimal_from(digits, factor=1):
    """digits x factor 的精确值; 能整除就是 int (任意精度), 除不尽落 float。

    全程整数运算: 尾数拆成 系数 / 10^小数位, 单位因子乘在分子上。
    旧实现是 float(digits) * factor, 于是 1.005万 -> 10049.999999999998、
    1.1亿 -> 110000000.00000001 —— 正是原则 9 要禁止的表示漂移。
    纯整数 (没有小数也没有指数) 占真实数据的绝大多数, 单独走一条快路。

    唯一的例外: |整数部分| >= 2^53 且带除不尽的小数时返回 None (按解析失败保留
    原值)。这个量级 float64 连整数部分都放不下 (相邻间隔 > 1), 强转必然舍入整数位、
    str() 还会写成科学计数法 —— 宁可不数值化, 也不静默改写 (原则 1)。
    """
    if '.' not in digits and 'e' not in digits and 'E' not in digits:
        return int(digits) * factor
    m = _DECIMAL_RE.fullmatch(digits)
    if not m:
        return None
    sign, int_part, frac_part, exp = m.groups()
    coef = int(int_part + (frac_part or ''))
    scale = 10 ** len(frac_part or '')
    e = int(exp or 0)
    if e >= 0:
        coef *= 10 ** e
    else:
        scale *= 10 ** -e
    coef *= factor
    whole, rest = divmod(coef, scale)
    if rest and abs(whole) >= 2 ** 53:
        return None
    value = whole if rest == 0 else whole + rest / scale
    return -value if sign == '-' else value


def _scaled_from(digits, factor=1):
    """digits x factor 的十进制文本, 保留书写的尾零。

    '1.5' x 万 -> '15000'、'1,234' -> '1234': 分组、货币、单位、指数这些差异在这一步
    折平, 于是"两种写法同一个数值"只剩下真正的**精度**差异 (2023.1 与 2023.10) 才算碰撞。
    绝大多数格子 (无单位、无指数、小数部分不带尾零) 的书写形已经是标准形,
    直接原样返回 —— 这是 20 万行 x 12 列的热路径, 每条快路都要算。
    """
    if factor == 1 and 'e' not in digits and 'E' not in digits:
        dot = digits.find('.')
        if dot < 0 or digits[-1] != '0':
            return digits
    m = _DECIMAL_RE.fullmatch(digits)
    if not m:
        return None
    _sign, int_part, frac_part, exp = m.groups()
    coef = int(int_part + (frac_part or ''))
    scale = 10 ** len(frac_part or '')
    e = int(exp or 0)
    if e >= 0:
        coef *= 10 ** e
    else:
        scale *= 10 ** -e
    coef *= factor
    whole, rest = divmod(coef, scale)
    if rest == 0:
        return str(whole)
    return f'{whole}.{str(rest).zfill(len(str(scale)) - 1)}'


def parse_numeric(value, convert_units=True):
    """把字符串解析为 int/float; 不像数就返回 None。

    支持: 全角数字/符号, 千分位逗号, 货币符号前后缀 (¥ $ 元),
    中文单位后缀 (万/亿/千万/万亿...), 会计括号负数 "(1,234)",
    科学计数法 "1.5e9"。纯整数返回 int (任意精度), 含小数点返回 float。
    前导零数字串 (000001) 视为标识符, 返回 None。
    千分位分组不合规 (1,23 / 1.234,5) 返回 None —— 逗号语义有歧义时保留原值。
    """
    core = _numeric_core(value, convert_units)
    if core is None:
        return None
    negative, digits, factor = core
    num = _decimal_from(digits, factor)
    if num is None:
        return None
    if isinstance(num, float) and num.is_integer() and abs(num) < 2 ** 53:
        num = int(num)
    return -num if negative else num


def _is_leading_zero_token(value):
    s = value.translate(_FW_TRANS).strip()
    return bool(_LEADING_ZERO_RE.fullmatch(s))


def numericize_dataframe(df, convert_units=True, skip_columns=None):
    """逐列数值化: 解析成功的写回数值, 失败保留原值。

    skip_columns (用户保护的列) 整列不进这一步: 保护列的净改动必须是 0,
    先洗再由 _revert_protected 退回去的话, numeric_cells 会计入随后被还原的格子,
    "报告说改了 N 格"就和"净改动 0 格"对不上 (与全角/日期通道同一口径)。

    标识符列保护: 列中任一前导零数字串 => 整列跳过。
    写法碰撞留痕: 同一列里两种**精度写法**洗成同一个数值 (2023.1 与 2023.10) 时不静默
    —— 数值是对的, 但"这一格原来写的是哪个"再也分不出来, 所以记入 collision_columns
    并写警告。像报告期这类列, 收到警告就该保护整列。
    """
    report = {'numeric_cells': 0, 'unit_cells': 0, 'id_columns': [],
              'collision_columns': [], 'warnings': []}
    skip = set() if skip_columns is None else set(skip_columns)
    for col in df.columns:
        if col in skip:
            continue
        series = df[col]
        if not (pd.api.types.is_object_dtype(series)
                or isinstance(series.dtype, pd.StringDtype)):
            continue
        is_str = series.map(lambda v: isinstance(v, str))
        if not is_str.any():
            continue
        sample = series[is_str]
        if sample.map(_is_leading_zero_token).any():
            report['id_columns'].append(str(col))
            continue

        # 一次遍历同时做完: 解析、单位计数、写法碰撞留痕。
        # 分成三遍写起来更"清楚", 但这是 20 万行 x 12 列的热路径, 而且旧写法
        # 实际是 parse_numeric 一遍、单位正则再一遍、碰撞再一遍。
        # 用显式 list 而非 Series.map: map 的类型推断会把 int 统一转成 float64,
        # 破坏"整数以 int 写回"的约定 (输出会漂移成 1234.0)。
        parsed_list = []
        forms = {}
        clashes = set()
        n_num = n_unit = 0
        for v in sample:
            core = _numeric_core(v, convert_units)
            if core is None:
                parsed_list.append(None)
                continue
            negative, digits, factor = core
            num = _decimal_from(digits, factor)
            if num is None:
                parsed_list.append(None)
                continue
            if isinstance(num, float) and num.is_integer() and abs(num) < 2 ** 53:
                num = int(num)
            if negative:
                num = -num
            parsed_list.append(num)
            n_num += 1
            if factor != 1:
                n_unit += 1
            if factor == 1 and '.' not in digits and 'e' not in digits and 'E' not in digits:
                # 纯整数写法不可能是精度碰撞的一方: 1500.0 会被折平成 1500,
                # 碰撞只发生在"同一个值的两种小数写法"之间。省掉多数格子的字典操作
                # (这是 20 万行 x 12 列的热路径)。
                continue
            scaled = _scaled_from(digits, factor)
            if scaled is None:
                continue
            form = ('-' if negative else '') + scaled
            prev = forms.get(num)
            if prev is None:
                forms[num] = form
            elif prev != form and len(clashes) < 3:
                # 只有"同一个数值的两种精度写法"才算碰撞。分组符、货币符、单位后缀、
                # 会计括号这些差异在 _scaled_from 里已经折平, 不会误报。
                clashes.add(f'{prev} 与 {form} 同为 {num}')

        if not n_num:
            continue
        parsed = pd.Series(parsed_list, index=sample.index, dtype=object)
        ok = parsed.notna()

        values = series.astype(object).copy()
        values.loc[ok.index[ok]] = parsed[ok].to_numpy()
        df[col] = values
        report['numeric_cells'] += n_num
        report['unit_cells'] += n_unit
        if clashes:
            report['collision_columns'].append(str(col))
            report['warnings'].append(
                f'列 "{col}" 内 {"; ".join(sorted(clashes))} 是两种写法同一个数值; '
                '数值本身没错, 但洗完全已分不出原写法 (报告期/编号这类列请点锁保护整列)')
    return report


# ==========================================
# 字符去除 / 日期统一
# ==========================================

def normalize_fullwidth(df, columns=None):
    """全角 -> 半角 (显式开启的清洗通道)。返回 (转换格数, 触及列)。

    放在去字符与数值化之前, 顺序是有意的:
    ２０２３．１．８ 先变成 2023.01.08 才谈得上被日期识别接住;
    全角代码 ０００００１ 先变成 000001, 前导零还在, 仍会被标识符保护接住。

    默认关闭: 开启后名称/备注这类文本列里的全角括号与字母同样被改写,
    所以逐格计数并写进报告, 不静默。
    """
    total = 0
    touched = []
    cols = list(df.columns) if columns is None else list(columns)
    for col in cols:
        if col not in df.columns:
            continue
        series = df[col]
        if not (pd.api.types.is_object_dtype(series)
                or isinstance(series.dtype, pd.StringDtype)):
            continue
        converted = series.map(lambda v: v.translate(_FW_TRANS_FULL) if isinstance(v, str) else v)
        changed = int(sum(1 for a, b in zip(series, converted)
                          if isinstance(a, str) and a != b))
        if changed:
            df[col] = converted
            total += changed
            touched.append(str(col))
    return total, touched


def strip_tokens_pass(df, tokens, column=None, skip_columns=None):
    """把用户给定 token 从字符串单元格中字面移除。返回 (移除单元格数, 触及列)。

    skip_columns (用户保护的列) 不参与: 保护列的净改动必须是 0, 计数不虚高
    (与全角/数值化通道同一口径)。
    """
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0, []
    if column is not None:
        # 按字符串口径找列: 表头是年份 (2023) 时列标签是 int, 服务层传来的可是字符串
        target = next((c for c in df.columns if str(c) == str(column)), None)
        if target is None:
            return 0, []
        cols = [target]
    else:
        cols = list(df.columns)
    if skip_columns:
        cols = [c for c in cols if c not in skip_columns]

    n = 0
    touched = []
    for col in cols:
        series = df[col]
        if not (pd.api.types.is_object_dtype(series)
                or isinstance(series.dtype, pd.StringDtype)):
            continue

        def _clean(v):
            for t in tokens:
                if t in v:
                    v = v.replace(t, '')
            return v

        cleaned = series.map(lambda v: _clean(v) if isinstance(v, str) else v)
        changed = int(sum(1 for a, b in zip(series, cleaned)
                          if isinstance(a, str) and a != b))
        if changed:
            df[col] = cleaned
            n += changed
            touched.append(str(col))
    return n, touched


_DATE_TEXT_RE = re.compile(
    r'^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?'
    r'\s*(?:[T\s](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d+))?)?\s*$')
TIME_COL_SUFFIX = '_时间'


def _split_temporal(v):
    """把"看起来是日期(时间)"的值拆成 (日期串, 时间串或 None); 认不出返回 (None, None)。

    时间分量单独交出去而不是就地 strftime('%Y-%m-%d'): 旧实现把 Excel 里的
    2023-01-05 14:30 与 09:15 都写成 2023-01-05 —— 时分秒被静默抹掉, 两行变成
    同一个值不可还原, 而预览里原值也按同一个格式渲染, 界面上完全看不出改了什么。
    """
    if isinstance(v, (pd.Timestamp, datetime)):
        if v.hour or v.minute or v.second or getattr(v, 'microsecond', 0):
            return v.strftime('%Y-%m-%d'), v.strftime('%H:%M:%S')
        return v.strftime('%Y-%m-%d'), None
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d'), None
    if not isinstance(v, str):
        return None, None
    m = _DATE_TEXT_RE.match(v)
    if not m:
        return None, None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None, None
    time_part = None
    if m.group(4) is not None:
        hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
        if hh > 23 or mm > 59 or ss > 59:
            return None, None                    # 时间部分不合法 => 整格按解析失败保留
        time_part = f'{hh:02d}:{mm:02d}:{ss:02d}'
        if m.group(7):
            time_part += '.' + m.group(7)
    return day.strftime('%Y-%m-%d'), time_part


def _parse_date_value(v):
    """只要日期部分; 认不出返回 None (调用方保留原值)。"""
    return _split_temporal(v)[0]


def _free_time_name(df, col):
    """给拆出来的时间列找一个不占用已有列名的名字。"""
    base = f'{col}{TIME_COL_SUFFIX}'
    if base not in df.columns:
        return base
    n = 1
    while f'{base}_{n}' in df.columns and n < 50:
        n += 1
    return f'{base}_{n}'


def normalize_dates(df, skip_columns=()):
    """把所有列中可识别为日期的值统一为 YYYY-MM-DD; 无法解析的保留原值。

    带时间分量的列拆成两列 (原列只留日期, 右侧新增 "列名_时间" 放 HH:MM:SS),
    返回 (改写格数, 触及列, 新增的时间列, 跳过的列)。拆列而不是截断, 因为截断不可逆;
    skip_columns 是用户标记保护的列, 整列不进这一步。
    """
    total = 0
    columns = []
    created = []
    for col in list(df.columns):
        if col in skip_columns:
            continue
        pairs = [_split_temporal(v) for v in df[col].tolist()]
        hit = [i for i, (d, _t) in enumerate(pairs) if d is not None]
        if not hit:
            continue
        values = df[col].astype(object).copy()
        values.iloc[hit] = [pairs[i][0] for i in hit]
        df[col] = values
        total += len(hit)
        columns.append(str(col))
        if any(t for _d, t in pairs):
            name = _free_time_name(df, col)
            pos = list(df.columns).index(col) + 1
            df.insert(pos, name, pd.Series([t for _d, t in pairs], index=df.index, dtype=object))
            created.append({'from': str(col), 'to': str(name),
                            'cells': sum(1 for _d, t in pairs if t)})
    return total, columns, created


# ==========================================
# 文件读取 (字节探测) 与保存
# ==========================================

def _decode_bytes(raw):
    """BOM 优先, 严格解码顺序 utf-8 -> gb18030 -> big5, 永不失败。

    gb18030 的字节域几乎覆盖 big5, 所以 big5 分支实际很难到达: Big5 源文件多半
    会被 gb18030 "成功" 解成乱码。这在没有编码探测器的前提下无法可靠区分,
    所以兜底解码一律发警告, 不静默 (原则 7: 全程留痕)。
    """
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'):
        return 'utf-16', raw.decode('utf-16'), None
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    for enc in ('utf-8', 'gb18030', 'big5'):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == 'gb18030':
            return enc, text, '非 utf-8 内容已按 gb18030 兜底解码; 若源文件是 Big5 繁体中文, 内容可能乱码, 请核对'
        if enc == 'big5':
            return enc, text, 'utf-8/gb18030 均解码失败, 已按 big5 解码, 请核对结果'
        return enc, text, None
    return 'latin-1', raw.decode('latin-1'), \
        '无法识别文件编码, 已按 latin-1 兜底读取 (中文可能乱码)'


def _sniff_delimiter(text, default=','):
    """按"能不能把每行切成同样多列"选分隔符, 而不是数原始字符。

    旧实现对前 20 行数原始字符, 引号里的逗号也被算进票数, 于是
    "分号分隔 + 引号内含逗号" 的文件会被误判成逗号 —— 整表塌成一列。
    现在对每个候选真的用 csv 解析一遍, 先比一致行的占比、再比列宽;
    只能切出 1 列的候选不参与竞争 (那说明它根本不是分隔符)。
    样本窗口 20 -> 200 行: 文件开头常见一段无分隔符的前言 (说明行、空行),
    只有 20 行时真分隔符可能凑不满"众数列数 >= 2"被排除, 误判回默认逗号;
    200 行的解析成本仍然可忽略 (text 已在内存, 每个候选只扫样本)。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:200]
    if not lines:
        return default
    sample = '\n'.join(lines)
    best, best_key = None, None
    for d in ('\t', ',', ';', '|'):
        try:
            counts = [len(r) for r in csv.reader(StringIO(sample), delimiter=d) if r]
        except csv.Error:
            continue
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        key = (counts.count(modal) / len(counts), modal)
        if best_key is None or key > best_key:
            best, best_key = d, key
    return best or default


def _read_excel(path):
    """Excel 读取。引擎按"快且保真"排序, 失败逐个回退, 最后才报错。

    calamine (Rust 实现) 实测比 openpyxl 快 4-5 倍 (20 万行 9.2s -> 2.2s),
    但它会做类型推断: 裸用会把股票代码 000001 读成 1。dtype=object 是这条路径
    能用的前提, 实测加 dtype=object 后 000001 原样保住。
    未安装 python-calamine 时第一次尝试抛 ImportError, 自动落到 openpyxl, 不影响功能。

    keep_default_na=False: pandas 默认会把 "N/A"/"NA"/"null"/"None"/"#N/A" 这些
    **文本**换成 NaN, 于是原则 4 的"读取阶段零破坏"在第一步就破了 —— 而且原值和
    结果在界面上都渲染成空, 差异页根本看不出来值被吃过, 输出文件里内容已经没了。
    空单元格本身由引擎给 None, 不依赖这套 NA 名单。

    环境变量 SC_EXCEL_ENGINE 可钉死引擎 (openpyxl / xlrd / calamine):
    既是"新引擎读某类怪文件出问题"时的兜底开关, 也是做 A/B 计时时的对照组。
    """
    ext = os.path.splitext(path)[1].lower()
    primary = 'xlrd' if ext == '.xls' else 'openpyxl'
    forced = (os.environ.get('SC_EXCEL_ENGINE') or '').strip().lower()
    order = (forced,) if forced else ('calamine', primary, None, 'openpyxl', 'xlrd')
    last_error = None
    for engine in order:
        try:
            # dtype=object: 阻止 pandas 读取阶段把 000001 推断成 1
            return (pd.read_excel(path, engine=engine, dtype=object,
                                  keep_default_na=False), engine or 'auto')
        except Exception as exc:                            # noqa: BLE001
            last_error = exc
            if forced:
                break
            continue
    raise ValueError(f"所有 Excel 引擎均无法读取该文件, 请检查文件是否损坏 ({last_error})")


INDEX_COL_NAME = '(索引列)'


def _rebuild_rows(rows, delim):
    """把 csv.reader 读出的原始行还原成 DataFrame, 返回 (df, warnings)。

    这是"字段数与表头不一致"的唯一一份处理 (预览与正式运行、结构预检与
    ParserError 兜底都走这里), 只认三种结构, 每一种都留痕:
    1. 每行都比表头多一列且首列是纯数字 => 像 to_csv(index=True) 的产物。
       旧实现交给 pandas index_col=0, 而写出时 index=False, 于是首列整列消失、
       零警告 —— 正好是原则 8 禁止的那种静默移位。现在给首列补一个列名保住它,
       并显式提示用户"要不要删这列由你决定"。猜错的代价只是多一列, 不是丢数据。
    2. 字段数不齐 => 超出的并回最后一个值 / 不足的补空, 每行都留。
    3. 齐 => 正常建表。
    """
    header = rows[0]
    exp = len(header)
    data = rows[1:]
    if exp >= 2 and data and all(len(r) == exp + 1 for r in data) \
            and all(_row_number_like(r[0]) for r in data):
        cols = _mangle_header([INDEX_COL_NAME] + list(header))
        return pd.DataFrame([[r[0]] + list(r[1:]) for r in data],
                            columns=cols, dtype=object), \
            [f'每行都比表头多一个字段且首列是数字，已按 "{cols[0]}" 保住整列 '
             '(多半是 to_csv(index=True) 留下的行号列；确认无用可在源端删掉)']
    if any(len(r) != exp for r in data):
        return _build_repaired(rows, exp, delim), \
            ['部分行字段数与表头不一致, 已按保留原值的方式修复 (请核对结果)']
    return pd.DataFrame([list(r) for r in data],
                        columns=_mangle_header(header), dtype=object), []


def _recover_ragged(text, delim):
    """pandas 抛 ParserError/ParserWarning 之后的兜底: 按 csv 语义自己重建。"""
    try:
        rows = [r for r in csv.reader(StringIO(text), delimiter=delim) if r]
    except csv.Error:
        rows = None
    if rows and len(rows) >= 2:
        return _rebuild_rows(rows, delim)
    # 连 csv 模块都读不动 (损坏的引号), 最后的宽松模式, 但一定发警告
    df = pd.read_csv(StringIO(text), sep=delim, dtype=object, engine='python',
                     on_bad_lines='skip', quoting=3, keep_default_na=False)
    return df, ['文件存在损坏的引号/行结构, 已按宽松模式读取, 结果可能不完整']


def _row_number_like(v):
    return v.strip().lstrip('-').isdigit()


def _build_repaired(rows, expected, delim):
    """超出的字段并回最后一个值 / 不足的字段补空, 保住每一行。"""
    return pd.DataFrame(_pad_rows(rows[1:], expected, delim),
                        columns=_mangle_header(rows[0]), dtype=object)


def _pad_rows(data, expected, delim):
    """把数据行补齐到 expected 列 (超出并回最后一列, 不足补空)。"""
    out = []
    for r in data:
        if len(r) > expected:
            r = list(r[:expected - 1]) + [delim.join(r[expected - 1:])]
        elif len(r) < expected:
            r = list(r) + [None] * (expected - len(r))
        out.append(list(r))
    return out


def _mangle_header(header):
    """表头按 pandas read_csv 的口径去重 (代码, 代码.1) 并给空名补 Unnamed。

    DataFrame(columns=[...]) 构造函数**不会**像 read_csv 那样自动去重, 而重复标签
    会让后面所有 df[col] 返回 DataFrame, 一路炸到写文件。修复路径必须自己补上这一步。
    """
    out, seen = [], {}
    for i, raw_name in enumerate(header):
        name = raw_name if str(raw_name) != '' else f'Unnamed: {i}'
        n = seen.get(name, 0)
        seen[name] = n + 1
        out.append(name if n == 0 else f'{name}.{n}')
    return out


def _read_delimited(path, ext):
    with open(path, 'rb') as f:
        raw = f.read()
    # 扩展名可以撒谎, NUL 字节不会: 真二进制文件 (改过名的 .doc / .xls / 压缩包)
    # 一旦被当文本读, 会洗出一整表乱码列还照样落盘, 所以在这里挡住。
    if b'\x00' in raw[:65536]:
        raise ValueError('内容是二进制 (含 NUL 字节), 未当文本读取; 请先在源端导出成 csv/xlsx')
    encoding, text, warning = _decode_bytes(raw)
    delim = _sniff_delimiter(text, default='\t' if ext in ('.txt', '.tsv') else ',')
    warns = [warning] if warning else []
    df = None

    # 结构预检: 引号感知地核对我到的每条记录的字段数, 不一致时走那一份共用修复,
    # 绝不让 pandas 静默移位/截断。一致时仍然交给 C 解析器 (20 万行差一个数量级)。
    try:
        rows = [r for r in csv.reader(StringIO(text), delimiter=delim) if r]
    except csv.Error:
        rows = None
    if rows and len(rows) >= 2 and any(len(r) != len(rows[0]) for r in rows[1:]):
        df, ragged_warnings = _rebuild_rows(rows, delim)
        warns.extend(ragged_warnings)

    if df is None:
        try:
            # index_col=False: 杜绝 pandas 把多出的首列静默当索引;
            # 该模式下 pandas 对不匹配只发 ParserWarning 并静默丢列, 故升级为异常。
            # keep_default_na=False: 不让 N/A / NA / null / None 这些文本在读取阶段变空。
            with warnings.catch_warnings():
                warnings.simplefilter('error', pd.errors.ParserWarning)
                df = pd.read_csv(StringIO(text), sep=delim, dtype=object, index_col=False,
                                 keep_default_na=False)
        except (pd.errors.ParserError, pd.errors.ParserWarning):
            df, ragged_warnings = _recover_ragged(text, delim)
            warns.extend(ragged_warnings)
    meta = {'encoding': encoding, 'delimiter': repr(delim), 'warnings': warns}
    return df, meta


# 明确知道怎么读的扩展名; 以及"肯定不是文本"的扩展名 (拒读, 不做乱码列);
# 其余扩展名走文本探测 + 警告。
# 扩展名清单只留这一份 (服务层以前自己抄了一份, 结果 .xlsm 能读却扫不到、
# 输出侧又不认它是 Excel)。
EXCEL_EXTS = ('.xlsx', '.xlsm', '.xls')
TEXT_EXTS = ('.csv', '.txt', '.tsv')
SUPPORTED_EXTS = TEXT_EXTS + EXCEL_EXTS
# 明确"肯定不是文本"的后缀: 不能当文本猜 (会洗出一整表乱码列还照样落盘)。
BINARY_EXTS = ('.doc', '.docx', '.ppt', '.pptx', '.zip', '.rar', '.7z', '.pdf', '.exe',
               '.dll', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.mp3', '.mp4', '.gz',
               '.tar', '.accdb', '.mdb', '.db', '.sqlite', '.parquet', '.feather', '.bin')


def read_table(path):
    """读入表格, 返回 (df, meta)。文本文件按文本保真读入, 不做类型推断。

    扩展名只用来选"读法", 不用来判生死: 券商/银行导出里 content 是逗号分隔、
    后缀却是 .dat/.log/无后缀的情况很常见, 一律拒读等于让用户先去改文件名。
    所以: Excel 后缀走 Excel, 文本后缀走文本, 二进制后缀拒读,
    其余后缀按文本猜一次并留下警告 (猜错的代价是一条提示, 不是静默乱码)。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in EXCEL_EXTS:
        df, engine = _read_excel(path)
        return df, {'warnings': [], 'engine': engine}
    if ext in TEXT_EXTS:
        return _read_delimited(path, ext)
    if ext in BINARY_EXTS:
        raise ValueError(f"不支持的文件类型: {ext} (支持 {' '.join(SUPPORTED_EXTS)})")
    df, meta = _read_delimited(path, ext)
    meta['warnings'] = list(meta.get('warnings') or []) + [
        f'扩展名 {ext or "(无)"} 不是表格后缀，已按文本猜测读取（分隔符 {meta.get("delimiter")}）；'
        '若列对不上，请在源端改成 .csv/.txt']
    return df, meta


def _xlsx_safe_ints(df):
    """xlsx 数值单元格按 float64 解释: |int| >= 2^53 的格子改按文本写出, 否则被舍入。

    只扫描 object 列 (内核数值化写回的值都在 object 列), 扫描成本只落在写 xlsx 路径。
    空串不算数值格: 它是"读取阶段保真"留下的真实空, 交给 Excel 自己显示成空格。
    """
    hits = {}
    for col in df.columns:
        series = df[col]
        if not pd.api.types.is_object_dtype(series):
            continue
        mask = series.map(lambda v: isinstance(v, int) and not isinstance(v, bool)
                          and abs(v) >= 2 ** 53)
        if mask.any():
            hits[col] = series.where(~mask, series.map(str))
    if not hits:
        return df
    out = df.copy(deep=False)
    for col, s in hits.items():
        out[col] = s
    return out


def save_table(df, original_path, output_dir, stem=None, output_format='keep'):
    """写出结果, 返回实际写出的路径列表 (第一个是主输出)。

    output_format:
      keep  跟随输入 (Excel -> .xlsx, 文本 -> utf-8-sig 的 .csv) —— 默认
      xlsx  一律 .xlsx
      csv   一律 .csv (utf-8-sig)。实测 20 万行写 xlsx 16.7s, 写 csv 0.6s
      both  两种都写, 便于"给人看用 xlsx, 给下游脚本用 csv"
    扩展名始终与真实内容一致, 不会把 .xls 改名成 .xlsx 却仍是旧格式。

    stem: 显式指定输出主文件名 (不含 _cleaned 与扩展名), 用于批量模式下
    不同子目录同名文件的防冲突。
    """
    name = stem or os.path.splitext(os.path.basename(original_path))[0]
    ext = os.path.splitext(original_path)[1].lower()
    os.makedirs(output_dir, exist_ok=True)
    is_excel_src = ext in ('.xls', '.xlsx', '.xlsm')
    fmt = (output_format or 'keep').lower()
    if fmt == 'keep':
        targets = ['xlsx'] if is_excel_src else ['csv']
    elif fmt == 'both':
        targets = ['xlsx', 'csv']
    else:
        targets = [fmt if fmt in ('xlsx', 'csv') else ('xlsx' if is_excel_src else 'csv')]

    written = []
    for target in targets:
        if target == 'xlsx':
            out_path = os.path.join(output_dir, f"{name}_cleaned.xlsx")
            _xlsx_safe_ints(df).to_excel(out_path, index=False, engine='openpyxl')
        else:
            out_path = os.path.join(output_dir, f"{name}_cleaned.csv")
            df.to_csv(out_path, index=False, encoding='utf-8-sig')
        written.append(out_path)
    return written


# ==========================================
# 清洗管道
# ==========================================

def _is_blank(v):
    """空值判定: None/NaN/空串/纯空白 都算"空"。

    dropna(how='all') 只认 NaN, 会漏掉 dtype=object 读入时的空字符串格,
    于是"删除全空行"对 CSV 数据失效。
    """
    if v is None:
        return True
    if isinstance(v, float):
        return v != v
    if isinstance(v, str):
        return not v.strip()
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def cell_repr(v):
    """单元格字符串口径 (webapp 的 json_safe/cell_text 必须与这里一致)。

    datetime 保留时分秒: 早先这里和 json_safe 都把 Timestamp 渲染成 %Y-%m-%d,
    于是"时间被截掉"这件事在差异页上完全隐形 —— 原值也被同一个格式渲染了。
    """
    if v is None:
        return ''
    if isinstance(v, float):
        if v != v:
            return ''
        if v.is_integer() and abs(v) < 2 ** 53:
            return str(int(v))
        return repr(v)
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, pd.Timestamp):
        return v.strftime('%Y-%m-%d %H:%M:%S') if (v.hour or v.minute or v.second
                                                   or v.microsecond) else v.strftime('%Y-%m-%d')
    if hasattr(v, 'strftime') and not isinstance(v, str):
        try:
            if isinstance(v, datetime) and (v.hour or v.minute or v.second
                                            or getattr(v, 'microsecond', 0)):
                return v.strftime('%Y-%m-%d %H:%M:%S')
            return v.strftime('%Y-%m-%d')
        except Exception:                                # noqa: BLE001
            pass
    return str(v)


def _revert_protected(df, orig_protected, report):
    """把被保护的列整列回退为原值 (按索引对齐, 只覆盖幸存行, 不把裁掉的行塞回来)。

    回退发生在清洗管道的最末端, 因此行裁剪/去空/去字符/数值化/日期对同一行的
    其他列做了什么, 与保护列互不干扰; 保护逻辑只有这一份, 预览与正式运行共用。
    """
    reverted = 0
    reverted_cols = []
    lost_cols = []
    for col, before in orig_protected.items():
        if col not in df.columns:
            # 保护列被 drop_empty_cols 删掉了: 用户点过锁的列不该无声消失
            lost_cols.append(str(col))
            continue
        try:
            aligned = before.loc[df.index]
        except Exception:                                # noqa: BLE001
            aligned = None
        if aligned is None or len(aligned) != len(df):
            # 索引对不齐 (重复索引等) 时宁可不保护也绝不写错数据
            report['warnings'].append(f'列 "{col}" 索引无法对齐, 本次未做保护回退')
            continue
        n = sum(1 for a, b in zip(aligned, df[col]) if cell_repr(a) != cell_repr(b))
        df[col] = pd.Series(aligned.to_numpy(), index=df.index, dtype=object)
        reverted += n
        if n:
            reverted_cols.append(str(col))
    report['protected_cells'] = reverted
    report['protected_columns'] = reverted_cols
    if reverted:
        report['warnings'].append(f'{reverted} 格因列保护回退为原值')
    if lost_cols:
        report['warnings'].append(
            '已保护的列被"删除全空列"删掉了: ' + ', '.join(lost_cols) + ' (请关掉该选项或检查该列)')


def clean_table(df, config):
    """按配置清洗 df, 返回 (df, report)。管道顺序: 裁剪 -> 去空 -> 去字符 -> 数值化 -> 日期 -> 列保护回退。"""
    report = {'rows_in': len(df), 'rows_out': len(df), 'cols_out': df.shape[1],
              'dropped_rows': 0,
              'fullwidth_cells': 0, 'fullwidth_columns': [],
              'stripped_cells': 0, 'numeric_cells': 0, 'unit_cells': 0,
              'date_cells': 0, 'date_columns': [], 'date_time_columns': [],
              'id_columns': [], 'collision_columns': [], 'warnings': []}

    # 逐列保护 (服务层把 column_overrides 翻译成 protected_columns 传进来):
    # 开头存原值, 结尾整列回退。必须在这里做而不是服务层清洗后回填,
    # 否则预览有保护、正式运行没有 —— 两层会分叉。
    # 列标签按字符串口径匹配: 表头是年份 (2023) 时标签是 int, 而前端只会送字符串,
    # 早先直接 `c in df.columns` 会把用户点的保护静默丢掉 (列照洗, 零警告)。
    protected = []
    for want in (config.get('protected_columns') or []):
        hit = next((c for c in df.columns if str(c) == str(want)), None)
        if hit is not None:
            protected.append(hit)
        else:
            report['warnings'].append(f'要保护的列 "{want}" 在当前表里不存在, 未生效')
    orig_protected = {c: df[c].copy(deep=True) for c in protected}

    head_cut = int(config.get('head_cut') or 0)
    tail_cut = int(config.get('tail_cut') or 0)
    if head_cut > 0:
        df = df.iloc[head_cut:]
    if tail_cut > 0:
        df = df.iloc[:-tail_cut]
    if df.empty:
        raise ValueError(
            f"裁剪后文件为空 (head_cut={head_cut}, tail_cut={tail_cut}), 已跳过")

    if config.get('drop_empty_rows'):
        df = df[~df.map(_is_blank).all(axis=1)]
    if config.get('drop_empty_cols'):
        blank_cols = df.columns[df.map(_is_blank).all(axis=0)]
        if len(blank_cols):
            df = df.drop(columns=blank_cols)
    if df.shape[1] == 0:
        raise ValueError("清洗后所有列均为空 (全表空值?), 已跳过该文件")
    report['dropped_rows'] = report['rows_in'] - len(df)

    # --- 全角转半角 (显式开启, 默认关); 必须在去字符与数值化之前 ---
    # 受保护的列不参与: 这些列最终整列回退为原值, 算进 fullwidth_cells 会让
    # "报告说改了 2 格"和"净改动 0 格"互相对不上。
    if config.get('fullwidth'):
        fw_cols = [c for c in df.columns if c not in orig_protected]
        n_fw, touched = normalize_fullwidth(df, fw_cols)
        report['fullwidth_cells'] = n_fw
        report['fullwidth_columns'] = touched

    # --- 去字符 (用户显式意图); 与单位换算互斥感知, 防止先把 "万" 删掉 ---
    tokens = list(config.get('strip_tokens') or [])
    convert_units = config.get('convert_units', True)
    blocked = []
    if convert_units:
        units = [t for t in tokens if t in _UNIT_TOKENS]
        if units:
            tokens = [t for t in tokens if t not in _UNIT_TOKENS]
            blocked = units
    if tokens:
        want_col = str(config.get('strip_column') or '').strip()
        column = want_col if config.get('strip_column_mode') else None
        if config.get('strip_column_mode') and not want_col:
            # 选了"只处理某列"却没给出列名: 不能静默什么都不做, 也不能擅自扩到全表
            report['warnings'].append('去字符: 已选"仅指定列"但没有列名, 本次未去任何字符')
            column = '__none__'            # 不存在的列名 => strip_tokens_pass 直接返回 0
        elif config.get('strip_column_mode') and want_col not in [str(c) for c in df.columns]:
            report['warnings'].append(
                f'未找到列 "{want_col}", 去字符已按全表处理')
            column = None
        elif config.get('strip_column_mode') and any(str(p) == want_col for p in orig_protected):
            # 锁优先于显式指定: 回退反正会把它原样还原, 与其洗了再退回去、计数虚高,
            # 不如一开始就不动, 并把原因说清楚
            report['warnings'].append(f'列 "{want_col}" 已被保护, 去字符未生效')
            column = '__none__'
        n, _ = strip_tokens_pass(df, tokens, column, skip_columns=set(orig_protected))
        report['stripped_cells'] = n
    for u in blocked:
        report['warnings'].append(
            f"字符 \"{u}\" 已由单位换算处理, 未作为去字符目标 (避免先删后换算失效)")

    # --- 数值化 (本质操作, 含千分位/全角/货币/单位) ---
    if config.get('numericize', True):
        rep = numericize_dataframe(df, convert_units, skip_columns=set(orig_protected))
        report['numeric_cells'] += rep['numeric_cells']
        report['unit_cells'] += rep['unit_cells']
        report['id_columns'] = rep['id_columns']
        report['collision_columns'] = rep['collision_columns']
        report['warnings'].extend(rep['warnings'])

    # --- 日期统一 (内容驱动) ---
    if config.get('normalize_dates'):
        n, cols, created = normalize_dates(df, skip_columns=set(orig_protected))
        report['date_cells'] = n
        report['date_columns'] = cols
        report['date_time_columns'] = created
        for c in created:
            report['warnings'].append(
                f'列 "{c["from"]}" 含时间分量, 已拆成 "{c["from"]}" (日期) + '
                f'"{c["to"]}" (HH:MM:SS) 两列, 共 {c["cells"]} 格')

    # --- 列保护回退 (管道最后一步, 只覆盖幸存行) ---
    if orig_protected:
        _revert_protected(df, orig_protected, report)

    report['rows_out'] = len(df)
    report['cols_out'] = df.shape[1]        # 日期/时间拆列会加列, 这里才是终值
    return df, report


def process_file(file_path, output_dir, config):
    """单文件完整流程: 读取 -> 清洗 -> 保存。返回 (report, 主输出路径)。

    output_format='both' 时第二个输出放在 report['extra_outputs'],
    返回值固定是 (report, 主输出路径) 两元组: 调用方只关心"这个文件写到哪了",
    附加产物属于报告细节, 不该改变函数形状。
    """
    df, meta = read_table(file_path)
    if df is None or df.empty:
        raise ValueError("文件为空或无法读取")
    df, report = clean_table(df, config)
    # 批量模式防冲突: config['stem_map'] 为重名文件提供唯一输出名
    stem = (config.get('stem_map') or {}).get(os.path.abspath(file_path))
    out_paths = save_table(df, file_path, output_dir, stem,
                           config.get('output_format', 'keep'))
    report['warnings'].extend(meta.get('warnings') or [])
    if len(out_paths) > 1:
        report['extra_outputs'] = out_paths[1:]
    if 'encoding' in meta:
        report['encoding'] = meta['encoding']
        report['delimiter'] = meta['delimiter']
    if 'engine' in meta:
        report['engine'] = meta['engine']
    return report, out_paths[0]


def format_report(filename, report, out_path):
    """把单文件报告格式化为日志文本。"""
    lines = [f"[成功] {filename}: {report['rows_out']} 行 x {report['cols_out']} 列"]
    parts = []
    if report['dropped_rows']:
        parts.append(f"裁剪/删除 {report['dropped_rows']} 行")
    if report.get('fullwidth_cells'):
        parts.append("全角转半角 {0} 格 ({1})".format(
            report['fullwidth_cells'], ', '.join(report.get('fullwidth_columns') or [])))
    if report['stripped_cells']:
        parts.append(f"去除字符 {report['stripped_cells']} 格")
    if report['numeric_cells']:
        parts.append(f"数值化 {report['numeric_cells']} 格")
    if report['unit_cells']:
        parts.append(f"单位换算 {report['unit_cells']} 格")
    if report['date_cells']:
        parts.append(f"日期统一 {report['date_cells']} 格 ({', '.join(report['date_columns'])})")
    if report.get('protected_cells'):
        parts.append(f"列保护回退 {report['protected_cells']} 格 "
                     f"({', '.join(report.get('protected_columns') or [])})")
    if parts:
        lines.append("    " + "; ".join(parts))
    if report['id_columns']:
        lines.append("    识别为标识符列 (保留文本, 未数值化): "
                     + ", ".join(report['id_columns']))
    for w in report['warnings']:
        lines.append(f"    [警告] {w}")
    lines.append(f"    输出: {out_path}")
    return "\n".join(lines)
