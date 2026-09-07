# -*- coding: utf-8 -*-
"""批量任务: 后台线程 + 事件缓冲 (SSE 可重放)。

为什么用事件缓冲而不是简单回调: 桌面壳里的页面会刷新/重连,
重连后必须能补齐错过的进度, 否则进度条会永远停在断线那一刻。
"""

import os
import queue
import threading
import time
import uuid
from collections import OrderedDict

from .core import run_file, error_text, build_stem_map
from .manifest import prune_missing

MAX_EVENTS = 20000       # 单任务事件缓冲上限: 超出丢最旧
REPLAY_WINDOW = 1024     # 一次重连最多回放多少条 (订阅队列同宽, 决定重连能否接上)
DEFAULT_KEEP_JOBS = 8


class Job:
    def __init__(self, files, output_dir, config):
        self.id = uuid.uuid4().hex[:12]
        self.files = list(files)
        self.output_dir = output_dir
        self.config = dict(config)
        self.total = len(self.files)
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.status = 'running'          # running | done | cancelled | error
        self.events = []                 # 事件缓冲, 供重连重放; 超过 MAX_EVENTS 丢最旧
        self._seq = 0                    # seq 单调计数, 独立于缓冲长度
        self.subscribers = []            # [queue.Queue]
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.finished_at = None
        self.thread = None

    # ---- 事件 ----
    def publish(self, kind, payload):
        # 入缓冲与投递必须在同一把锁里完成: 分开做的时候, 一个线程取完快照、
        # 还没 put 之前另一个线程 publish 了新事件, 订阅者收到的 seq 就会倒着走
        # (实测会让前端"最后一条比第一条旧")。队列不设上限: 设了就得在
        # 这里抛异常, 宁可让一条流慢慢排干, 也不能丢正在看进度的用户的更新。
        with self.lock:
            self._seq += 1
            event = {'seq': self._seq, 'kind': kind, 'at': round(time.time(), 3)}
            event.update(payload)
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]
            for q in self.subscribers:
                q.put(event)
        return event

    def subscribe(self):
        """新订阅者拿到**最近** REPLAY_WINDOW 条, 而不是最旧的 1024 条。

        旧实现从缓冲头部开始塞、塞满就 break, 于是长任务断线重连后拿到的是
        最旧一段、且里面一定没有 job_end —— 进度条永远停在"处理中", 报告缺尾。
        真有缺口时显式发一条 replay_gap 事件, 让界面能说"前面丢了多少", 而不是静默。
        """
        q = queue.Queue()
        with self.lock:
            backlog = self.events[-REPLAY_WINDOW:]
            lost = len(self.events) - len(backlog)
            if lost:
                q.put({'seq': 0, 'kind': 'replay_gap', 'lost': lost,
                       'first_seq': backlog[0]['seq'] if backlog else None,
                       'at': round(time.time(), 3)})
            for event in backlog:
                q.put(event)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def snapshot(self):
        with self.lock:
            return {'job_id': self.id, 'status': self.status, 'total': self.total,
                    'done': self.done, 'ok': self.ok, 'failed': self.failed,
                    'output_dir': self.output_dir,
                    'elapsed': round((self.finished_at or time.time()) - self.started_at, 2),
                    'events': self.events[-40:]}

    def cancel(self):
        self.cancelled.set()

    def _note(self, **fields):
        """在锁内改状态再发事件, 保证 snapshot() 看到的和事件流里的一致。"""
        with self.lock:
            for key, value in fields.items():
                setattr(self, key, value)

    # ---- 执行 ----
    def start(self):
        self.thread = threading.Thread(target=self._run, name=f'cleaner-{self.id}', daemon=True)
        self.thread.start()

    def _finish(self, status, extra=None):
        self.finished_at = time.time()
        payload = {'status': status, 'ok': self.ok, 'failed': self.failed,
                   'elapsed': round(self.finished_at - self.started_at, 2)}
        payload.update(extra or {})
        self._note(status=status)
        self.publish('job_end', payload)

    def _progress_reporter(self, index):
        """把内核的阶段进度回调翻译成 file_progress 事件。

        阶段事件每文件只有固定的几条 (约 8 个), 不需要节流; 重放窗口截断最多
        丢掉中间某条 frac, file_done 必然跟着一条 progress 事件收口, 进度条不会悬空。
        """
        def report(frac, stage):
            self.publish('file_progress', {'index': index,
                                           'frac': round(min(1.0, max(0.0, frac)), 3),
                                           'stage': stage})
        return report

    def _run(self):
        # 批量模式不同子目录同名文件防覆盖 (带输出格式判定: 只拦真的会互覆的组合)
        stem_map, _renamed = build_stem_map(
            self.files, output_format=self.config.get('output_format', 'keep'))
        self.config['stem_map'] = stem_map

        self.publish('job_start', {'job_id': self.id, 'total': self.total,
                                   'output_dir': self.output_dir})
        for index, path in enumerate(self.files):
            if self.cancelled.is_set():
                # 计数只在这里读一次; 中途不再被 snapshot 覆盖
                self._finish('cancelled', {'cancelled': True})
                return
            self.publish('file_start', {'index': index, 'name': os.path.basename(path),
                                        'path': path})
            try:
                payload = run_file(path, self.output_dir, self.config,
                                   progress=self._progress_reporter(index))
                self._note(ok=self.ok + 1, done=self.done + 1)
                self.publish('file_done', {'index': index, **payload})
            except Exception as exc:                        # noqa: BLE001
                self._note(failed=self.failed + 1, done=self.done + 1)
                self.publish('file_error', {'index': index, 'name': os.path.basename(path),
                                            'error': error_text(exc)})
            self.publish('progress', {'done': self.done, 'total': self.total,
                                      'ok': self.ok, 'failed': self.failed})
        # 用户手动删过输出时, 登记会无限膨胀; 收尾清掉已经不存在的条目
        try:
            pruned = prune_missing(self.output_dir)
            if pruned:
                self.publish('note', {'message': f'产出登记清理了 {pruned} 条已不存在的记录'})
        except OSError:
            pass
        self._finish('done')


class JobRegistry:
    def __init__(self, keep=DEFAULT_KEEP_JOBS):
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        self.keep = keep

    def create(self, files, output_dir, config):
        job = Job(files, output_dir, config)
        with self._lock:
            self._jobs[job.id] = job
            # 只淘汰已经结束的旧任务: 按插入序硬砍会在第 keep+1 个批次进来时
            # 把一个还在写文件的任务从表里摘掉 —— 界面查不到它、也取消不了它,
            # 而它仍在往输出目录里写。
            finished = [jid for jid, j in self._jobs.items()
                        if jid != job.id and j.status != 'running']
            for jid in finished[:max(0, len(finished) - self.keep)]:
                self._jobs.pop(jid, None)
        job.start()
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)


registry = JobRegistry()
