# -*- coding: utf-8 -*-
"""产出登记 (manifest): 让"不重复吃进自己的输出"从猜名字变成查登记。

为什么不用文件名后缀: `*_cleaned` 猜测有假阳性。用户自己命名的 report_cleaned.csv
会被静默跳过, 于是"少洗了一个文件"却没有任何提示。漏文件比多洗一次严重得多。

登记里只有本工具真实写过的绝对路径, 所以排除是精确的:
- 没登记过的文件一律照常处理;
- 显式点选的文件永远处理 (那是明确意图, 哪怕它出现在登记里);
- 登记丢失或损坏时退化成"不排除任何东西", 宁可多跑一次也不静默漏文件。
"""

import json
import os
import threading
import time
import uuid

MANIFEST_NAME = '_stockcleaner_manifest.json'
# 读-改-写必须在同一把锁里: 两个批次同时写同一个输出目录时, 没有锁就是
# "后写的把先写的整份覆盖掉" —— 实测各登记 30 条最后只剩 30 条, 被抹掉的那些
# 在下次扫描时不再被排除, 于是自己的产出被当新数据重吃 (原则 11 的立身之本)。
# tmp 名也必须唯一: 固定名会让一个线程 os.replace 时撞上另一个线程还开着的句柄
# (Windows 直接 WinError 32)。
_WRITE_LOCK = threading.RLock()


def _atomic_write(path, payload):
    tmp = f'{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def manifest_path(output_dir):
    return os.path.join(os.path.abspath(output_dir), MANIFEST_NAME)


def load(path):
    """读一个登记文件。损坏/读不到一律当空, 不抛异常也不静默排除任何东西。"""
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    outputs = data.get('outputs') if isinstance(data, dict) else None
    if not isinstance(outputs, dict):
        return {}
    return {k: v for k, v in outputs.items() if isinstance(v, dict)}


def registered_outputs(roots, output_dir=None):
    """返回 (已登记的绝对输出路径集合, 读到的登记文件数)。

    **只信任当前输出目录里那一份登记** (output_dir 下的 _stockcleaner_manifest.json)。
    早先实现会去输入树里任意目录找登记文件来排除产出, 但这等于把一个可能被伪造、
    损坏或上一轮残留的清单当成权威来源: 数据目录里一旦被人放了内容指向真实文件的
    _stockcleaner_manifest.json, 扫描器就会把那些文件当成"自己的产出"静默跳过
    (漏文件远比多跑一次严重)。换成只认 output_dir, 宁可多洗一次, 也不对来路不明的
    登记买账。roots 参数保留仅为兼容调用方签名, 不再参与登记读取。
    """
    paths = set()
    files = 0
    if output_dir and os.path.isdir(output_dir):
        entries = load(manifest_path(output_dir))
        if entries:
            files += 1
            paths.update(_key(p) for p in entries)
    return paths, files


def record(output_dir, source_path, out_paths, config=None):
    """写出成功后登记。合并写、原子替换, 不破坏已有记录。"""
    path = manifest_path(output_dir)
    with _WRITE_LOCK:
        data = load(path)
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        for p in out_paths:
            try:
                stat = os.stat(p)
                size, mtime = stat.st_size, round(stat.st_mtime, 3)
            except OSError:
                size, mtime = None, None
            data[_key(p)] = {'source': os.path.abspath(source_path), 'size': size,
                             'mtime': mtime, 'written': now,
                             'format': os.path.splitext(p)[1].lstrip('.').lower()}
        payload = {'tool': 'StockCleaner', 'version': 1, 'updated': now,
                   'output_dir': os.path.abspath(output_dir), 'outputs': data}
        return _atomic_write(path, payload)


def prune_missing(output_dir):
    """清掉登记里已经不存在的文件 (用户手动删过输出时不让登记无限膨胀)。"""
    path = manifest_path(output_dir)
    with _WRITE_LOCK:
        data = load(path)
        kept = {k: v for k, v in data.items() if os.path.exists(k)}
        if len(kept) == len(data):
            return 0
        if kept:
            _atomic_write(path, {'tool': 'StockCleaner', 'version': 1,
                                 'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                                 'output_dir': os.path.abspath(output_dir), 'outputs': kept})
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        return len(data) - len(kept)
