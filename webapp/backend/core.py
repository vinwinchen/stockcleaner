# -*- coding: utf-8 -*-
"""编排层: 把 cleaner_core 的产物翻译成前端可渲染的 JSON。

四条约束 (为什么这样写):
1. 一切清洗只调用 cleaner_core, 本模块不复制任何解析逻辑。
   预览与实际结果必须同源, 否则"预览说改 12 格, 实际改了 40 格"这种分叉迟早出现。
2. 列级覆写 = 清洗后把该列整列回滚为原值。语义是"该列不参与清洗",
   不需要第二套清洗实现, 也就不可能与内核漂移。
3. 预览只扫描样本行 (默认前 500 行), 响应里显式标注 sampled; 正式运行处理全量。
   单元格级 Python 循环是 O(行x列), 大文件全量预览会卡住界面。
4. 所有数值以字符串输出 (json_safe)。JS Number 是 float64,
   9007199254740993 这样的精确整数会变成 ...900; 内核对大整数是 int 精确运算的,
   不能在传给前端的最后一步丢掉。
"""

import logging
import os
import glob as globlib
import tempfile
from datetime import datetime

import pandas as pd

from cleaner_core import (read_table, clean_table, process_file, cell_repr,
                          SUPPORTED_EXTS, EXCEL_EXTS, BINARY_EXTS)
from .manifest import MANIFEST_NAME

# 扩展名清单来自内核 (以前这里抄了一份, 结果 .xlsm 能读、能出 xlsx, 却扫不到);
# 保留旧名字是为了不改动本模块其余引用点。
SUPPORTED_EXT = SUPPORTED_EXTS
DEFAULT_SAMPLE_ROWS = 500
MAX_PREVIEW_SAMPLES = 12          # 每列展示几组 原值 -> 结果
DEFAULT_CONFIG = {
    'strip_tokens': [],
    'strip_column_mode': False,
    'strip_column': '',
    'numericize': True,
    'convert_units': True,
    'head_cut': 0,
    'tail_cut': 0,
    'drop_empty_rows': True,
    'drop_empty_cols': False,
    'normalize_dates': True,
    # 产品默认关: 开启后名称/备注这类文本列里的全角括号与字母同样被改写,
    # 影响面大, 交给用户在界面上显式开启 (内核缺省同样是不传就不做)。
    'fullwidth': False,
    'output_format': 'keep',      # keep | xlsx | csv | both
    'column_overrides': {},       # {列名: {'protect': True}}
    'sample_rows': DEFAULT_SAMPLE_ROWS,
}


# ---------------------------------------------------------------- 配置

def normalize_config(raw):
    """补齐缺省值并过滤空 token, 产出 clean_table/process_file 可直接吃的 config。

    数值/布尔字段一律容错: 这是 HTTP 边界, 一个 head_cut="abc" 不该让整次预览
    变成 500 (调用方的 try 只包住清洗, 包不住这里)。认不出来就用缺省值, 不猜。
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg['strip_tokens'] = []
    cfg['column_overrides'] = {}
    for key, value in (raw or {}).items():
        if key in cfg:
            cfg[key] = value

    tokens = cfg.get('strip_tokens') or []
    if isinstance(tokens, str):
        tokens = tokens.split(',')
    elif not isinstance(tokens, (list, tuple)):
        tokens = []
    cfg['strip_tokens'] = [str(t).strip() for t in tokens
                           if isinstance(t, (str, int, float)) and str(t).strip()]
    cfg['strip_column'] = str(cfg.get('strip_column') or '').strip()
    for key in ('numericize', 'convert_units', 'normalize_dates', 'fullwidth',
                'drop_empty_rows', 'drop_empty_cols', 'strip_column_mode'):
        cfg[key] = bool(cfg[key])
    fmt = str(cfg.get('output_format') or 'keep').lower()
    cfg['output_format'] = fmt if fmt in ('keep', 'xlsx', 'csv', 'both') else 'keep'
    cfg['head_cut'] = _as_int(cfg['head_cut'], 0)
    cfg['tail_cut'] = _as_int(cfg['tail_cut'], 0)
    cfg['sample_rows'] = min(20000, max(20, _as_int(cfg['sample_rows'], DEFAULT_SAMPLE_ROWS)))

    overrides = {}
    raw_overrides = cfg.get('column_overrides')
    if isinstance(raw_overrides, dict):
        for col, opt in raw_overrides.items():
            if isinstance(opt, dict) and opt.get('protect'):
                overrides[str(col)] = {'protect': True}
    cfg['column_overrides'] = overrides
    return cfg


def _as_int(value, fallback):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return n if n >= 0 else fallback


def kernel_config(cfg):
    """剥掉只属于服务层的字段, 交给 cleaner_core; 列保护翻译成内核的 protected_columns。

    保护必须经内核执行: 预览与正式运行走同一个 clean_table, 不可能分叉。
    """
    overrides = cfg.get('column_overrides') or {}
    out = {k: v for k, v in cfg.items() if k not in ('sample_rows', 'column_overrides')}
    out['protected_columns'] = [str(col) for col, opt in overrides.items() if opt.get('protect')]
    return out


# ---------------------------------------------------------------- 值的安全表达

def cell_text(v):
    """单元格的"可比文本": 与内核 cell_repr 同一口径, 不截断。

    比较必须用这个, 不能用 json_safe 的显示串 —— 后者被截到 160 字符,
    于是一个只出现在第 200 个字符之后的改动会算成"无改动", 报告与界面互相矛盾。
    日期带时分秒时保留时分秒, 否则"时间被截掉"这件事在界面上完全隐形。
    """
    return cell_repr(v)


def json_safe(v, max_len=160):
    """任意单元格 -> 可 JSON 序列化的字符串; 类型信息不丢, 精度不丢。

    's' 是**显示**用文本 (会截断), 不参与改动判定; 改动判定走 cell_text。
    """
    if v is None:
        return {'s': '', 't': 'empty', 'nan': True}
    if isinstance(v, bool):
        return {'s': 'TRUE' if v else 'FALSE', 't': 'bool'}
    if isinstance(v, float):
        if pd.isna(v):
            return {'s': '', 't': 'empty', 'nan': True}
        if v.is_integer() and abs(v) < 2 ** 53:
            return {'s': str(int(v)), 't': 'int'}
        s = repr(v)
        return {'s': s[:max_len], 't': 'float'}
    if isinstance(v, int):
        return {'s': str(v), 't': 'int'}
    if isinstance(v, (pd.Timestamp,)):
        has_time = bool(v.hour or v.minute or v.second or v.microsecond)
        fmt = '%Y-%m-%d %H:%M:%S' if has_time else '%Y-%m-%d'
        return {'s': v.strftime(fmt), 't': 'date'}
    if hasattr(v, 'strftime') and not isinstance(v, str):
        try:
            has_time = isinstance(v, datetime) and (v.hour or v.minute or v.second
                                                    or getattr(v, 'microsecond', 0))
            fmt = '%Y-%m-%d %H:%M:%S' if has_time else '%Y-%m-%d'
            return {'s': v.strftime(fmt), 't': 'date'}
        except Exception:
            pass
    s = str(v)
    if s != s.strip():
        return {'s': s.strip()[:max_len], 't': 'text', 'padded': True}
    return {'s': s[:max_len], 't': 'text'}


def _output_targets(path, fmt='keep'):
    """按输出格式算出实际会写出的扩展名序列 (第一个是主输出)。"""
    is_excel_src = str(path).lower().endswith(EXCEL_EXTS)
    if fmt == 'both':
        return ['xlsx', 'csv']
    if fmt in ('xlsx', 'csv'):
        return [fmt]
    return ['xlsx' if is_excel_src else 'csv']


def public_report(report, path=None, output_format='keep'):
    """内核报告 -> 前端报告。预览与正式运行共用这一份字段清单。

    两处各写一遍白名单迟早会漏字段 (protected_cells 之前就只在预览侧出现),
    所以字段清单只留一份。
    """
    out = {
        'numeric_cells': int(report.get('numeric_cells', 0)),
        'unit_cells': int(report.get('unit_cells', 0)),
        'date_cells': int(report.get('date_cells', 0)),
        'stripped_cells': int(report.get('stripped_cells', 0)),
        'fullwidth_cells': int(report.get('fullwidth_cells', 0)),
        'protected_cells': int(report.get('protected_cells', 0)),
        'dropped_rows': int(report.get('dropped_rows', 0)),
        'id_columns': [str(c) for c in (report.get('id_columns') or [])],
        'date_columns': [str(c) for c in (report.get('date_columns') or [])],
        'fullwidth_columns': [str(c) for c in (report.get('fullwidth_columns') or [])],
        'protected_columns': [str(c) for c in (report.get('protected_columns') or [])],
        'collision_columns': [str(c) for c in (report.get('collision_columns') or [])],
        'date_time_columns': [{'from': str(c['from']), 'to': str(c['to']),
                               'cells': int(c['cells'])}
                              for c in (report.get('date_time_columns') or [])],
        'warnings': list(dict.fromkeys(report.get('warnings') or [])),
    }
    if path is not None:
        targets = _output_targets(path, output_format)
        out['output_ext'] = targets[0]
        out['output_extras'] = targets[1:]
    return out


# ---------------------------------------------------------------- 列分析

def analyze_columns(original, cleaned, report, cfg):
    """逐列给出: 识别类型、变更格数、样本对照 (原值 -> 结果)。

    两条对齐规矩, 都是实测踩出来的:
    1. 改动判定按**索引**配对, 不能按位置 zip。删过行的话, 第 i 格的原值会去和第 i+1 格
       的结果比, 一列从没动过的文本能报出几百格"改动"; 而数据差异页按标签取值, 显示没动
       —— 两个页签互相打脸。全功能样例实测: 代码列报 488 格改动, 真值是 1。
    2. 取列一律用真实标签, 不能拿 str(col) 去查。表头是年份 (2023/2024) 时
       original['2023'] 直接 KeyError, 整列原值会被当成空。
    行号显示的是该格在**原始样本**里的位置, 不是清洗后的位置, 否则用户按行号回文件里找不到。
    """
    id_columns = set(str(c) for c in (report.get('id_columns') or []))
    date_columns = set(str(c) for c in (report.get('date_columns') or []))
    collisions = set(str(c) for c in (report.get('collision_columns') or []))
    created = {str(c['to']): c for c in (report.get('date_time_columns') or [])}
    overrides = cfg.get('column_overrides') or {}
    orig_by_label = {str(c): c for c in original.columns}
    orig_pos = {idx: pos for pos, idx in enumerate(original.index)}
    # 界面上的行号 = 这一格在原文件 (样本口径) 里的第几行, 不是清洗后重排的第几行,
    # 否则用户按行号回到文件里对不上号
    cleaned_pos = [orig_pos.get(idx, i) for i, idx in enumerate(cleaned.index)]
    unalignable = False
    columns = []

    for col in cleaned.columns:
        name = str(col)
        added_from = created.get(name)
        olabel = col if col in original.columns else orig_by_label.get(name)
        if olabel is not None:
            before_series = original[olabel]
            try:
                # 原值一律按索引取: 位置对齐在删过行的时候会整列串位
                aligned = before_series.reindex(cleaned.index)
            except ValueError:
                # 索引有重复, 对不上 —— 退回位置对齐, 至少不崩, 并由下面标注不可核对
                unalignable = True
                aligned = before_series.reset_index(drop=True).reindex(
                    pd.RangeIndex(len(before_series)))[:len(cleaned)].reset_index(drop=True)
        else:
            # 拆出来的时间列: 原表根本没有这一列, "原值"就是空
            aligned = pd.Series([None] * len(cleaned), dtype=object)
        pairs = list(zip(aligned.tolist(), cleaned[col].tolist()))
        changed = sum(1 for b, a in pairs if cell_text(b) != cell_text(a))

        # 样本对照: 变更格优先占额度, 未变更的只留 2 行作上下文, 让"没动的地方"也可核对
        samples, context_left, diff_shown = [], 2, 0
        for pos, (b, a) in enumerate(pairs):
            b_json, a_json = json_safe(b), json_safe(a)
            is_diff = cell_text(b) != cell_text(a)
            if is_diff and diff_shown < MAX_PREVIEW_SAMPLES:
                diff_shown += 1
                samples.append({'row': int(cleaned_pos[pos]), 'before': b_json,
                                'after': a_json, 'changed': True})
            elif not is_diff and context_left:
                context_left -= 1
                samples.append({'row': int(cleaned_pos[pos]), 'before': b_json,
                                'after': a_json, 'changed': False})
            if diff_shown >= MAX_PREVIEW_SAMPLES and not context_left:
                break

        protected = bool(overrides.get(name, {}).get('protect'))
        kind = 'text'
        if added_from:
            kind = 'added'
        elif protected:
            kind = 'protected'
        elif name in id_columns:
            kind = 'identifier'
        elif name in date_columns:
            kind = 'date'
        elif changed:
            kind = 'number' if any(s['changed'] and s['after']['t'] in ('int', 'float')
                                   for s in samples) else 'text'

        columns.append({
            'name': name,
            'kind': kind,
            'cells': int(len(pairs)),
            'changed': int(changed),
            'protected': protected,
            'id_protected': name in id_columns,
            'collision': name in collisions,
            'split_from': (added_from or {}).get('from'),
            'samples': samples,
        })

    removed = [str(c) for c in original.columns if c not in cleaned.columns]
    notes = []
    if unalignable:
        notes.append('本文件行索引有重复, 列检视的原值/结果只能按位置对齐, 行号仅供参考')
    return columns, removed, notes


def build_grid(original, cleaned, columns, max_rows=60):
    """行级 diff: 每格带 原值/结果/是否变更, 供"数据差异"页做单元格着色。

    与 analyze_columns 同一套对齐规矩: 按标签取原值、用真实列标签而不是字符串名、
    比较用不截断的 cell_text。
    """
    orig_by_label = {str(c): c for c in original.columns}
    picked = []
    for c in columns[:24]:
        name = c['name']
        label = next((cl for cl in cleaned.columns if str(cl) == name), None)
        if label is None:
            continue
        picked.append((name, label, orig_by_label.get(name), bool(c.get('split_from'))))
    rows = []
    for pos, idx in enumerate(list(cleaned.index)[:max_rows]):
        cells = []
        for _name, label, olabel, is_new in picked:
            after = json_safe(cleaned.at[idx, label])
            if olabel is not None and idx in original.index:
                before = json_safe(original.at[idx, olabel])
            elif is_new:
                before = {'s': '', 't': 'empty', 'nan': True}   # 拆出来的新列, 原表没有这一列
            else:
                before = after
            cells.append([before, after, cell_text(before) != cell_text(after)])
        rows.append({'row': pos, 'cells': cells})
    return {'columns': [p[0] for p in picked], 'rows': rows,
            'truncated_columns': max(0, len(columns) - len(picked)),
            'truncated_rows': max(0, int(cleaned.shape[0]) - max_rows)}


# ---------------------------------------------------------------- 预览

def preview_file(path, raw_config):
    """只读干跑: 读 -> 清洗样本 -> 逐列 diff -> 结构化报告。不写任何文件。"""
    cfg = normalize_config(raw_config)
    df, meta = read_table(path)
    if df is None or df.empty:
        raise ValueError('文件为空或无法读取')
    # 读取阶段的留痕 (编码兜底、坏行修复) 必须出现在预览里:
    # 用户应当在"决定跑"之前就看到这些数据已经被动过
    read_warnings = list(meta.get('warnings') or [])

    total_rows = int(len(df))
    total_cols = int(df.shape[1])
    sample = df.head(cfg['sample_rows'])
    original = sample.copy(deep=True)

    # 列保护在内核 clean_table 内部执行 (与正式运行同一条代码路径, 不可能分叉)
    cleaned, report = clean_table(sample, kernel_config(cfg))
    if total_rows > cfg['sample_rows']:
        read_warnings.insert(0, f'预览为样本口径 (前 {cfg["sample_rows"]} 行): '
                                f'列类型识别与改动计数只基于样本, 全量运行可能不同')

    columns, removed, notes = analyze_columns(original, cleaned, report, cfg)
    read_warnings.extend(notes)
    grid = build_grid(original, cleaned, columns)
    preview_cols = [c['name'] for c in columns[:24]]
    # 前端拿到的永远是字符串列名, 取数要用真实标签 (表头是年份时标签是 int)
    by_name = {str(c): c for c in cleaned.columns}
    rows = []
    for i, (idx, _) in enumerate(cleaned.iterrows()):
        if i >= 80:
            break
        rows.append({'index': int(idx) if isinstance(idx, (int, float)) else str(idx),
                     'cells': [json_safe(cleaned.at[idx, by_name[c]]) for c in preview_cols]})

    encoding = meta.get('encoding') or ('excel' if str(path).lower().endswith(
        EXCEL_EXTS) else 'auto')
    return {
        'ok': True,
        'path': os.path.abspath(path),
        'name': os.path.basename(path),
        'size': os.path.getsize(path),
        'encoding': encoding,
        'delimiter': meta.get('delimiter'),
        'engine': meta.get('engine'),
        'rows_in': total_rows,
        'cols_in': total_cols,
        'rows_out': int(len(cleaned)),
        'cols_out': int(cleaned.shape[1]),
        'sampled': total_rows > cfg['sample_rows'],
        'sample_rows': min(total_rows, cfg['sample_rows']),
        'columns': columns,
        'removed_columns': removed,
        'grid': grid,
        'preview': {'columns': preview_cols, 'rows': rows,
                    'truncated_columns': max(0, len(columns) - len(preview_cols))},
        'report': _preview_report(report, read_warnings, path, cfg['output_format']),
    }


def _preview_report(report, read_warnings, path, output_format):
    """预览报告 = 内核报告字段 + 读取阶段的留痕, 顺序: 先读取后清洗。"""
    out = public_report(report, path, output_format)
    out['warnings'] = list(dict.fromkeys(
        list(read_warnings) + list(report.get('warnings') or [])))
    return out


# ---------------------------------------------------------------- 正式运行

def run_file(path, output_dir, cfg, progress=None):
    """全量处理一个文件, 返回结构化报告并登记产出。

    走的就是内核那个 process_file: 预览与正式运行共用同一实现, 服务层不另开一条路。
    progress: 透传给内核的阶段进度回调 (frac, stage), 批量任务用它发布细粒度进度。
    """
    from . import manifest

    report, out_path = process_file(path, output_dir, kernel_config(cfg),
                                    progress=progress)
    all_outputs = [out_path] + list(report.get('extra_outputs') or [])
    try:
        manifest.record(output_dir, path, all_outputs)
    except OSError:
        # 登记写不进去 (只读目录等) 不该让清洗失败, 但必须说出来
        report.setdefault('warnings', []).append('产出登记写入失败, 下次扫描可能重复处理输出文件')
    return {
        'name': os.path.basename(path),
        'path': os.path.abspath(path),
        'output': out_path,
        'outputs': all_outputs,
        'rows_out': int(report['rows_out']),
        'cols_out': int(report['cols_out']),
        'report': public_report(report),
        'encoding': report.get('encoding'),
        'delimiter': report.get('delimiter'),
        'engine': report.get('engine'),
    }


# ---------------------------------------------------------------- 文件枚举

def _inside(child, parent):
    """child 是否严格位于 parent 之下。

    必须走 normcase: Windows 路径大小写不敏感, 而盘符大小写取决于用户怎么打
    (手输 c:\\data 与对话框返回的 C:\\data 是同一个目录)。用原始字符串比前缀时,
    这两种写法一比就不等, "整棵输出子树排除"会静默失效, 实测下一次扫描就会把
    cleaned/ 里自己的产出当新数据吃进来。manifest 那条查找本来就是 normcase 的,
    两边统一按同一口径比。
    """
    child, parent = _key_of(child), _key_of(parent)
    return child == parent or child.startswith(parent.rstrip('\\/') + os.sep)


def is_scannable(path, explicit=False):
    """这个路径能不能进待处理清单。

    不只是 `endswith(SUPPORTED_EXT)`: 那会让内核的"按文本猜读"永远够不着 ——
    用户在资源管理器里看到的 `导出.dat` / 无后缀文件会被静默判成"不支持的类型",
    而内核其实能读并会给出猜读警告。所以规则是: 认识的表格后缀进, 明确二进制的
    (.doc/.pdf/.png/...) 不进 (那是真读不了, 放进去只是给队列添一堆红色报错),
    其余不认识的后缀进。

    放开后缀就必须补上"自己的产出"这一条: _stockcleaner_manifest.json 的 .json 不在
    二进制名单里, 放开后它会被当数据扫进来、洗成一份 _stockcleaner_manifest_cleaned.csv,
    再污染下一轮的登记读取。这类排除只用于**文件夹扫描** —— 用户点名要洗的文件永远照洗
    (那是明确意图, 哪怕他点的是 .tmp)。
    """
    low = str(path).lower()
    if not explicit:
        parts = [p for p in os.path.normpath(low).split(os.sep) if p and ':' not in p]
        # 任何一层是点开头 (.venv / .git / .hidden.csv) 或 Office 锁文件 (~$x.xlsx) 都不扫:
        # 后缀放开后这些目录里全是能"猜读"的文件, 灌进队列只会添一堆红色报错。
        # 与 list_dir 跳过 . / ~$ 的口径一致。
        if any(p.startswith('.') or p.startswith('~$') for p in parts):
            return False
        base = parts[-1] if parts else low
        if base == MANIFEST_NAME or base.endswith('.tmp'):
            return False                   # 自己的登记文件 / 原子写残留
    if low.endswith(SUPPORTED_EXT):
        return True
    return not low.endswith(BINARY_EXTS)


def expand_inputs(files, folders, recursive=True, output_dir: str | None = None, skipped=None):
    """把显式文件 + 文件夹 glob 合并成去重、稳定排序的绝对路径列表。

    文件夹扫描按两类规则排除, 都不靠猜文件名:
    1. 输出目录严格位于被扫目录之下时, 排掉那整个子树 (默认输出就在输入目录的
       cleaned/ 子目录里, 里面的东西按构造就是本工具的产出)。
       输出目录 == 被扫目录时**不能**用这条: 否则平铺习惯下所有输入都被排掉,
       用户点"开始"后什么都没发生;
    2. manifest 登记过的产出 (只排本工具真实写过的路径, 精确)。
    显式点选的文件不排除: 用户点名要洗的文件, 哪怕是自己上一次的输出也照洗。
    """
    from . import manifest

    found = []
    skipped_map = {}

    def note(path, reason):
        skipped_map[_key_of(path)] = {'path': path, 'name': os.path.basename(path),
                                      'reason': reason}

    _out = (output_dir or '').strip()
    out_abs = os.path.abspath(_out) if _out else None
    registered, _n_files = manifest.registered_outputs(folders or [], out_abs)

    for f in files or []:
        p = os.path.abspath(str(f))
        if os.path.isfile(p) and is_scannable(p, explicit=True):
            found.append(p)

    for d in folders or []:
        d = os.path.abspath(str(d))
        if not os.path.isdir(d):
            continue
        # 只有输出目录在被扫目录"里面"时才能整棵子树排除; 相等或反向都不行
        subtree = out_abs if (out_abs and _inside(out_abs, d)
                              and _key_of(out_abs) != _key_of(d)) else None
        pattern = '**/*' if recursive else '*'
        for entry in globlib.glob(os.path.join(d, pattern), recursive=recursive):
            if not os.path.isfile(entry) or not is_scannable(entry):
                continue
            ab = os.path.abspath(entry)
            if subtree and _inside(ab, subtree):
                note(ab, 'output_dir')
                continue
            if _key_of(ab) in registered:
                note(ab, 'manifest')
                continue
            found.append(ab)

    if skipped is not None:
        skipped.extend(skipped_map.values())
    # 去重按 normcase 口径: Windows 上 c:\a\x.csv 与 C:\a\x.csv 是同一个文件,
    # 按字符串去重会留两份, 于是同一个文件被洗两遍、输出自相覆盖。
    uniq = {}
    for p in found:
        uniq.setdefault(_key_of(p), p)
    return sorted(uniq.values(), key=lambda p: p.lower())


def _key_of(path):
    return os.path.normcase(os.path.abspath(path))


def suggest_output_dir(files, dirs):
    """默认输出位置: 输入公共父目录下的 cleaned/ 子目录。

    为什么默认进子目录而不是平铺: 平铺时输出和输入混在一起, 下一次文件夹扫描就会
    把上一次的产出当新数据吃进来。结构上分开, 这件事从源头不会发生。
    """
    anchors = [os.path.abspath(d) for d in (dirs or []) if os.path.isdir(d)]
    anchors += [os.path.dirname(os.path.abspath(f)) for f in (files or [])
                if os.path.isfile(f)]
    if not anchors:
        return ''
    try:
        base = os.path.commonpath(anchors) if len(anchors) > 1 else anchors[0]
    except ValueError:
        base = anchors[0]
    if os.path.isfile(base):
        base = os.path.dirname(base)
    return os.path.join(base, 'cleaned')


def build_stem_map(paths, output_format='keep'):
    """同名文件输出防覆盖: 只对"真的会写出同一个文件"的组合生成唯一主文件名。

    判定必须带输出格式: keep 下 x.csv 与 x.xlsx 的输出扩展名不同, 本就不冲突,
    强行改名反而让输出名不可读; 选 xlsx/both 时两者都会写 x_cleaned.xlsx,
    这才是真冲突。真冲突的组内, 用"相对共同祖先的路径"拼唯一名 (d1_x);
    同目录同名不同扩展 (x.csv / x.xlsx 被强制同格式) 补源扩展名 (x_csv);
    跨盘 (commonpath 失败) 退化为全路径。收尾一致性检查兜底,
    "同组不同文件不同 stem" 由本函数无条件保证。

    旧实现拿调用方传入的 roots 找"所属根", 但 /api/run 把每个文件自己的目录
    也当 root 传进来, rel 永远是裸文件名, 两个同名文件拼出同一个 stem ——
    照样互相覆盖 (全功能样例实测抓到)。
    """
    groups = {}
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        targets = tuple(_output_targets(p, output_format))
        groups.setdefault((stem, targets), []).append(os.path.abspath(p))
    dup_groups = [g for g in groups.values() if len(g) > 1]
    if not dup_groups:
        return {}, []

    def unique_stem(p):
        drive, tail = os.path.splitdrive(p)
        s = ((drive.rstrip(':') + '_' + tail) if drive else tail)
        return s.lstrip(os.sep + '/').replace(os.sep, '_').replace('/', '_')

    stem_map, renamed = {}, []
    for group in dup_groups:
        parents = [os.path.dirname(p) for p in group]
        try:
            base = os.path.commonpath(parents)
        except ValueError:                               # 跨盘: 直接走全路径
            base = ''
        stems = {}
        for p in group:
            if base:
                rel = os.path.relpath(p, base)
                stems[p] = os.path.splitext(rel)[0].replace(os.sep, '_').replace('/', '_')
            else:
                stems[p] = unique_stem(p)
        # 组内仍撞名 = 同目录同名不同扩展 (keep 下到不了这里, 强制格式下会到):
        # 补源扩展名区分
        counts = {}
        for s in stems.values():
            counts[s] = counts.get(s, 0) + 1
        for p, s in stems.items():
            if counts[s] > 1:
                stems[p] = f"{s}_{os.path.splitext(p)[1].lstrip('.').lower()}"
        if len(set(stems.values())) != len(group):       # 理论到不了, 兜底保证唯一
            for p in group:
                stems[p] = unique_stem(p)
        for p, s in stems.items():
            stem_map[p] = s
            if s != os.path.splitext(os.path.basename(p))[0]:
                renamed.append({'from': os.path.basename(p), 'to': f'{s}_cleaned'})
    return stem_map, renamed


# ---------------------------------------------------------------- 文件系统浏览 (原生对话框不可用时的退路)

def _drive_list():
    if os.name != 'nt':
        return [{'name': '/', 'path': '/'}]
    import string
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        bitmask = 0
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            root = f'{letter}:\\'
            out.append({'name': root, 'path': os.path.abspath(root)})
    return out


def list_dir(path=None, include_files=False):
    """列目录 (仅目录), 供内联路径选择器使用。路径不存在时退回根列表。"""
    if not path:
        return {'drives': _drive_list(), 'parent': None, 'path': None, 'dirs': []}
    target = os.path.abspath(str(path))
    if os.path.isfile(target):
        target = os.path.dirname(target)
    if not os.path.isdir(target):
        parent = os.path.dirname(target.rstrip('\\/')) or None
        return {'drives': _drive_list(), 'path': target, 'parent': parent, 'dirs': [],
                'files': [], 'error': '目录不存在'}
    parent = os.path.dirname(target.rstrip('\\/')) or None
    dirs, files = [], []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith(('.', '~$')):
                continue
            if entry.is_dir():
                dirs.append({'name': entry.name, 'path': os.path.abspath(entry.path)})
            elif include_files and is_scannable(entry.name):
                files.append({'name': entry.name, 'path': os.path.abspath(entry.path)})
    except OSError as exc:
        return {'drives': _drive_list(), 'path': target, 'parent': parent, 'dirs': [],
                'files': [], 'error': str(exc)}
    return {'drives': _drive_list(), 'path': target, 'parent': parent,
            'dirs': dirs[:500], 'files': files[:500], 'truncated': len(dirs) > 500}


def plan_outputs(paths, output_dir, roots, output_format='keep'):
    """在跑之前就把输出名算出来 (含同名防覆盖), 让用户先看到会写在哪。"""
    stem_map, _ = build_stem_map(paths, output_format=output_format)
    fmt = (output_format or 'keep').lower()
    plan = []
    for p in paths:
        abs_p = os.path.abspath(p)
        stem = stem_map.get(abs_p) or os.path.splitext(os.path.basename(p))[0]
        targets = _output_targets(p, fmt)
        out_name = f'{stem}_cleaned.{targets[0]}'
        out_path = os.path.join(output_dir, out_name) if output_dir else out_name
        plan.append({'path': abs_p, 'name': os.path.basename(p), 'out_name': out_name,
                     'out_path': os.path.abspath(out_path) if output_dir else None,
                     'extra_names': [f'{stem}_cleaned.{t}' for t in targets[1:]],
                     'merged_stem': bool(stem_map.get(abs_p)),
                     'exists': bool(output_dir) and os.path.exists(out_path)})
    return plan


# 意外异常的完整堆栈落这里 (FileHandler 带 delay: 真发生异常才建文件)。
_ERROR_LOG_PATH = os.path.join(tempfile.gettempdir(), 'StockCleaner', 'backend.log')


def _setup_error_log():
    logger = logging.getLogger('stockcleaner')
    if logger.handlers:                    # 重复 import 不重复挂 handler
        return logger
    try:
        os.makedirs(os.path.dirname(_ERROR_LOG_PATH), exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            _ERROR_LOG_PATH, encoding='utf-8', delay=True)
    except OSError:                        # 临时目录不可写时退回 stderr, 不拦住服务
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


_ERROR_LOG = _setup_error_log()


def error_text(exc):
    """用户可读的错误文本, 同时给意外异常做日志分流。

    ValueError 是内核/编排层刻意抛的, 消息本来就是写给用户看的, 原样直出;
    其余异常属于"不该发生的内部错误": 页面只回一行泛化提示 + 异常类型名,
    完整堆栈写进日志文件, 不把内部路径/状态细节整段暴露给前端。
    """
    if isinstance(exc, ValueError):
        return str(exc).strip() or type(exc).__name__
    _ERROR_LOG.error('未预期的内部错误', exc_info=exc)
    return f'内部错误 ({type(exc).__name__})；完整信息见日志: {_ERROR_LOG_PATH}'
