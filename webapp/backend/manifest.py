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
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        # 替换失败 (WinError 32 等) 不留孤儿 tmp: 它会被 is_scannable 的 *.tmp 规则
        # 挡在扫描之外, 但会一直躺在输出目录里, 越攒越多
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def manifest_path(output_dir):
    return os.path.join(os.path.abspath(output_dir), MANIFEST_NAME)


def _is_trusted_key(key, output_dir):
    """登记里的键是否可信: 必须是本输出目录内部的本机绝对路径。

    登记文件是磁盘上的普通 JSON, 而"待清洗的数据包"整个拷来拷去时, 它同样可能出自
    别人之手。旧实现把每个键都当权威, 于是两个后果:
      1) 伪造的键指向真实文件 => 该文件被当成"自己的产出"静默跳过 —— 漏文件,
         而界面只报聚合条数, 用户看不出少的是哪一个;
      2) prune_missing 对每个键做 os.path.exists() => 键写成 \\\\host\\share\\x 就会让
         Windows 去连 SMB/DNS, 把用户的 NTLM 响应递给攻击者 (换成 os.stat 一样出网)。
    所以形状 (绝对路径 / 不用 UNC 与设备命名空间 / 无驱动器相对形式) 与归属
    (必须在本输出目录之内) 都要校验。核心原则: 不对不可信字符串做文件系统访问。
    """
    if not isinstance(key, str) or not key:
        return False
    if key.startswith(('\\\\', '//')):   # UNC 与 //host/share: 一次 exists 就是一次出网
        return False
    if not os.path.isabs(key):           # 相对路径, 含 C:xxx 这类驱动器相对形式
        return False
    if not output_dir:
        return False
    from . import core                  # 延迟导入: core 在模块级 import 了本模块
    return core._inside(key, output_dir)


def load(path, output_dir=None):
    """读一个登记文件。损坏/读不到一律当空, 不抛异常也不静默排除任何东西。

    只收下"本输出目录内部"的条目 (见 _is_trusted_key), 其余一律当不存在: 效果是
    多洗一次自己上一轮的产出, 而不是漏文件、也不是对不可信字符串发出网络请求。
    传了 output_dir 才做归属校验, 不传等于"不信任任何条目"。

    读侧也持 _WRITE_LOCK。写侧是 os.replace 换文件, 而 Windows 上目标文件正被
    别人打开时 replace 会以 WinError 32 失败 (上面的注释已为"写-写"踩过一次)。
    以前只串了写者, 读-写仍会撞: 失败的那次登记被 run_file 降级成一句 warning,
    该产出从此不在登记里, 下次扫描就重吃自己的输出。RLock 允许 record/prune_missing
    内部重入, 不会自锁。
    """
    with _WRITE_LOCK:
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        outputs = data.get('outputs') if isinstance(data, dict) else None
        if not isinstance(outputs, dict):
            return {}
        return {k: v for k, v in outputs.items()
                if isinstance(v, dict) and _is_trusted_key(k, output_dir)}


def registered_outputs(roots, output_dir=None):
    """返回 (已登记的绝对输出路径集合, 读到的登记文件数)。

    **只信任当前输出目录里那一份登记** (output_dir 下的 _stockcleaner_manifest.json)。
    早先实现会去输入树里任意目录找登记文件来排除产出, 但这等于把一个可能被伪造、
    损坏或上一轮残留的清单当成权威来源: 数据目录里一旦被人放了内容指向真实文件的
    _stockcleaner_manifest.json, 扫描器就会把那些文件当成"自己的产出"静默跳过
    (漏文件远比多跑一次严重)。换成只认 output_dir, 宁可多洗一次, 也不对来路不明的
    登记买账。roots 参数保留仅为兼容调用方签名, 不再参与登记读取。

    条目本身也要落在 output_dir 之内才作数 (见 _is_trusted_key): "只认这一份登记"
    挡的是"登记文件本身可伪造", 挡不住"这一份里的内容可伪造" —— 后者才是把真实文件
    静默标成产出的那条路。
    """
    paths = set()
    files = 0
    if output_dir and os.path.isdir(output_dir):
        entries = load(manifest_path(output_dir), output_dir)
        if entries:
            files += 1
            paths.update(_key(p) for p in entries)
    return paths, files


def record(output_dir, source_path, out_paths, config=None):
    """写出成功后登记。合并写、原子替换, 不破坏已有记录。

    合并读进来的既有条目也过一遍归属校验 (load 的 output_dir): 混在登记里的越界条目
    会在这一次写出时被顺手清掉, 不会一直留在文件里当下一轮的隐患。
    """
    path = manifest_path(output_dir)
    with _WRITE_LOCK:
        data = load(path, output_dir)
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
    """清掉登记里已经不存在的文件 (用户手动删过输出时不让登记无限膨胀)。

    这里的 os.path.exists() 是本模块唯一一处"拿登记里的字符串碰文件系统"的地方,
    所以它必须只看到过了归属校验的键: 一个 \\\\host\\share\\x 形态的条目在这里
    就是一次 SMB 外连 (用户 NTLM 响应外泄)。校验在 load 里, 不在这个循环里 ——
    循环里再判一次就等于给了"以后有人往循环里加一行"的机会。
    """
    path = manifest_path(output_dir)
    with _WRITE_LOCK:
        data = load(path, output_dir)
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
