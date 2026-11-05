#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日自动增量同步：网关进程内的一个 asyncio 调度协程。

为什么不用 crontab / 任务计划
-----------------------------
因为**同步器本身就在网关进程里**（`runtime.SyncJob`，一把进程内锁），
外部再起一个 `mihealth-sync incremental` 会拿到第二把锁、和网页按钮抢库。
网关常开时，进程内调度就是最短的一条路：到点直接 `job.start(hours=72)`。

网关**不常开**（关机 / 笔记本合盖）时进程内调度自然不跑，这时才用
`schtasks` 兜底（见 README「同步与存储」），但**两者不要同时开**——
两把锁、白跑请求，还会让同步状态互相覆盖。

设计要点
--------
* 纯决策逻辑（`parse_at` / `next_run_at` / `should_catch_up`）与 IO 分开，
  单测不用起 FastAPI、不用等真时间。
* 状态存 `sync_state` 表的 `auto_daily` 行（`last_ts`，**秒**），不新建表。
* **每次读写都开一个短命 `Store`，绝不缓存句柄**：`SyncJob._finish()` 会
  `runtime.reset_store()` 关掉缓存连接，缓存句柄下一次就用不了了；
  而且 `Store(path)` 会把不存在的库文件建出来，那会让
  `runtime.get_store()` 从 live 模式翻成「0 行 db 模式」，看板静默变全 0。
* `MIH_DAILY_SYNC` 读不到 / 写错都只降级成「关闭 + 一行警告」，
  绝不让网关起不来。
"""
import asyncio
import os
import time

from . import paths, runtime
from .store import Store

# `sync_state` 里的行名；`mihealth-sync status` 会把它当一条 cursor 打出来
STATE_NAME = "auto_daily"

# 距上次自动同步超过这么久（秒），启动时就立刻补跑一次
CATCHUP_GAP_S = 20 * 3600

# 定点触发与补跑之间至少隔半天，避免「02:00 补跑 + 04:00 定点」一天拉两次
DEDUPE_S = 12 * 3600

# 调度循环里意外异常的退避时间（秒）
RETRY_S = 60.0

# 这些取值表示「关掉」
DISABLED = ("off", "none", "")


# ------------------------------------------------------------------ 纯函数

def parse_at(spec):
    """``'04:00'`` -> ``(4, 0)``；``'off'`` / ``'none'`` / ``''`` -> ``None``。

    格式非法一律抛 `ValueError`（纯函数好测），由 `from_env()` 负责降级。
    严格两位纯数字：``'4:00'`` / ``'04:0'`` / ``'25:00'`` / ``'04:60'`` 都算错。
    """
    text = (spec or "").strip().lower()
    if text in DISABLED:
        return None
    parts = text.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2 \
            or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"不是 HH:MM 形式: {spec!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError(f"时分超出范围: {spec!r}")
    return (hour, minute)


def next_run_at(now, hour, minute):
    """``now`` 之后（含）最近的一个本地时区 ``HH:MM``，返回秒级时间戳。

    用 `time.localtime` / `time.mktime` 而不是 `datetime`，避免本地时区
    与 UTC 混搭；`now` 正好落在 ``HH:MM`` 上时返回 `now` 本身。
    """
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if target < now:
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                              hour, minute, 0, 0, 0, -1))
    return target


def should_catch_up(last_ts, now, gap_s=CATCHUP_GAP_S):
    """启动时要不要立刻补跑：没跑过、或距今已超过 `gap_s`。"""
    if last_ts is None:
        return True
    return (now - last_ts) >= gap_s


# ------------------------------------------------------------------ 调度器

def _hhmm(pair):
    return f"{int(pair[0]):02d}:{int(pair[1]):02d}"


class DailySyncer:
    """每天定点跑一次增量同步；`run()` 由 lifespan 包在 task 里。

    `.at` 是 `(hour, minute)`（`run()` 里 `*self.at` 解包），
    `.at_text` 是给人看的 `'HH:MM'`（直接打印 tuple 会变成 `(4, 0)`）。
    """

    at = (4, 0)
    at_text = "04:00"

    def __init__(self, at="04:00", catchup_gap_s=CATCHUP_GAP_S, db_path=None,
                 mark_retries=30, mark_retry_s=0.1):
        if isinstance(at, str):
            parsed = parse_at(at)
            if parsed is None:
                # 'off' 不该走到这里 —— 由 from_env() 拦掉
                raise ValueError(f"自动同步已禁用，不该构造 DailySyncer: {at!r}")
            self.at = parsed
        else:
            self.at = (int(at[0]), int(at[1]))
        self.at_text = _hhmm(self.at)
        self.catchup_gap_s = catchup_gap_s
        self.db_path = db_path
        # `mark_ran` 等库文件落盘的窗口（见 mark_ran 注释）
        self.mark_retries = mark_retries
        self.mark_retry_s = mark_retry_s
        # 上次真正 kick 成功的时刻，用于定点触发的去重
        self._last = None

    # ---------------------------------------------------------- 展示
    def describe_next(self):
        """给启动日志用的「下次 ……」。**出错也不能抛**：它嵌在 lifespan 的
        print 里，抛出去就是网关起不来。"""
        try:
            ts = next_run_at(time.time(), *self.at)
            return "下次 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        except Exception:                                        # pragma: no cover
            return "下次时间未知"

    # ---------------------------------------------------------- 状态读写
    def _db_file(self):
        return self.db_path or paths.db_path()

    def _open_db(self):
        """只读写一行 state 用的短命连接；库不存在就返回 None，**绝不建库**。"""
        path = self._db_file()
        if not os.path.exists(path):
            return None
        return Store(path)

    def due_now(self):
        """要不要立刻补跑。没有库 = 从未同步过。"""
        db = self._open_db()
        if db is None:
            return True
        try:
            # 必须显式给字段名：get_state 的默认字段是 last_wm，不是 last_ts
            last = db.get_state(STATE_NAME, "last_ts")
        finally:
            db.close()
        return should_catch_up(last, time.time(), self.catchup_gap_s)

    def mark_ran(self, ts):
        """记下「这次自动同步已启动」。

        建库的是 `SyncJob` 的线程（`Store(db_path)`），`job.start()` 只是起
        线程就返回，所以紧接着这里库文件**可能还没落盘**。最多等
        `mark_retries * mark_retry_s` 秒，等不到就放弃 —— 下次启动
        `due_now()` 仍返 True，最多重跑一次，无害。
        **不要改成「那就自己建库吧」**，那正好踩中 §5.2.1 的 live→db 坑。
        """
        for attempt in range(self.mark_retries):
            db = self._open_db()
            if db is not None:
                try:
                    db.set_state(STATE_NAME, last_ts=int(ts))
                finally:
                    db.close()
                return
            if attempt + 1 < self.mark_retries:
                time.sleep(self.mark_retry_s)

    def _recently_ran(self):
        """半天内已经 kick 过就不再按点触发（§5.4.1 ③ 的去重）。"""
        return self._last is not None and (time.time() - self._last) < DEDUPE_S

    # ---------------------------------------------------------- 触发
    def _kick(self, why):
        """让 `SyncJob` 跑一次增量；返回是否真的启动了。"""
        cfg = runtime.get_config()
        job = runtime.get_sync_job(cfg.get("db"), cfg.get("config_path"))
        if not job.start(hours=72):
            print(f"[auto] {why}: 已有同步在跑，跳过", flush=True)
            return False
        now = time.time()          # 两处共用同一个时刻，便于对齐断言
        self.mark_ran(now)
        self._last = now
        print(f"[auto] {why}: 已启动增量同步（hours=72）", flush=True)
        return True

    async def run(self):
        """无限循环；被 lifespan 的 finally `task.cancel()` 结束。"""
        # 1) 启动时先判断补跑（关机几天后重开 → 立刻补一次）
        try:
            if self.due_now():
                print("[auto] 距上次自动同步超过 20 小时，立即补跑一次", flush=True)
                self._kick("catch-up")
        except Exception as e:
            print(f"[auto] 补跑判断失败，跳过: {e}", flush=True)

        # 2) 之后每天定点
        while True:
            try:
                now = time.time()
                # 下限 1.0 而不是 60：`next_run_at` 已保证 >= now，这里只防 0 和负数
                delay = max(1.0, next_run_at(now, *self.at) - now)
                await asyncio.sleep(delay)
                if self._recently_ran():
                    continue
                self._kick("scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[auto] 调度循环异常，60 秒后重试: {e}", flush=True)
                await asyncio.sleep(RETRY_S)


def from_env(env=None):
    """按 `MIH_DAILY_SYNC` 造一个调度器；关闭或非法都返回 None。

    **绝不能抛异常**：lifespan 在 `yield` 之前、uvicorn 已接管之后执行，
    这里抛 `ValueError` 会让进程起不来、端口不监听，用户只看到一堆栈。
    `04:0` / `25:00` / `4点` 这类手误必须降级成「关闭 + 一行警告」。
    """
    source = os.environ if env is None else env
    raw = source.get("MIH_DAILY_SYNC", "04:00")
    try:
        parsed = parse_at(raw)
    except ValueError as e:
        print(f"[auto] MIH_DAILY_SYNC 无法解析（{e}），每日自动同步已关闭", flush=True)
        return None
    if parsed is None:
        return None
    return DailySyncer(parsed)
