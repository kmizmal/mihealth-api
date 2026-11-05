# -*- coding: utf-8 -*-
"""每日自动同步调度器。

纯函数用例不碰时钟、不碰文件；存储层用例用 `tmp_path` 的临时库；
集成用例一律用假 `SyncJob`，绝不发请求。
"""
import asyncio
import contextlib
import os
import time

import pytest

from mihealth_api import paths, runtime, scheduler
from mihealth_api.store import Store


# ------------------------------------------------------------------ 纯函数

def _local_ts(hour, minute, day_offset=0):
    """今天（或 +N 天）本地时区 HH:MM 的时间戳。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset,
                        hour, minute, 0, 0, 0, -1))


@pytest.mark.parametrize("spec,expected", [
    ("04:00", (4, 0)),
    ("00:00", (0, 0)),
    ("23:59", (23, 59)),
    (" 04:00 ", (4, 0)),
])
def test_parse_at_accepts_strict_hh_mm(spec, expected):
    assert scheduler.parse_at(spec) == expected


@pytest.mark.parametrize("spec", [
    "4:00", "4:5", "04:0", "04:00:00", "25:00", "04:60", "abc", "2400", "",
])
def test_parse_at_rejects_malformed(spec):
    if spec == "":
        assert scheduler.parse_at(spec) is None      # 空串是「关闭」，不是「非法」
        return
    with pytest.raises(ValueError):
        scheduler.parse_at(spec)


@pytest.mark.parametrize("spec", ["off", "none", "", " OFF ", "Off", " NONE "])
def test_parse_at_disabled(spec):
    assert scheduler.parse_at(spec) is None


def test_next_run_at_same_day_when_before_the_hour():
    now = _local_ts(3, 0)
    assert scheduler.next_run_at(now, 4, 0) == _local_ts(4, 0)


def test_next_run_at_tomorrow_when_past_the_hour():
    now = _local_ts(5, 0)
    assert scheduler.next_run_at(now, 4, 0) == _local_ts(4, 0, day_offset=1)


def test_next_run_at_exactly_on_the_hour_returns_now():
    """「刚到点就跑」：now 正好是 HH:MM 时不去等明天。"""
    now = _local_ts(4, 0)
    assert scheduler.next_run_at(now, 4, 0) == now


def test_next_run_at_never_returns_the_past():
    now = _local_ts(4, 0) + 1        # 刚过一秒
    assert scheduler.next_run_at(now, 4, 0) > now


def test_should_catch_up_without_history():
    now = time.time()
    assert scheduler.should_catch_up(None, now) is True


def test_should_catch_up_when_over_the_gap():
    now = time.time()
    assert scheduler.should_catch_up(now - 21 * 3600, now) is True


def test_should_catch_up_when_within_the_gap():
    now = time.time()
    assert scheduler.should_catch_up(now - 19 * 3600, now) is False


def test_should_catch_up_after_multi_day_downtime():
    now = time.time()
    assert scheduler.should_catch_up(now - 3 * 86400, now) is True


def test_should_catch_up_honours_custom_gap():
    now = time.time()
    assert scheduler.should_catch_up(now - 60, now, gap_s=30) is True
    assert scheduler.should_catch_up(now - 60, now, gap_s=120) is False


# ------------------------------------------------------------------ 构造

def test_syncer_from_text_keeps_both_at_and_at_text():
    s = scheduler.DailySyncer("04:30")
    assert s.at == (4, 30)          # run() 里 *self.at 解包用
    assert s.at_text == "04:30"     # 日志里打印用，不能是 '(4, 30)'


def test_syncer_from_tuple_formats_at_text():
    s = scheduler.DailySyncer((7, 5))
    assert s.at == (7, 5)
    assert s.at_text == "07:05"


def test_syncer_rejects_disabled_spec():
    with pytest.raises(ValueError):
        scheduler.DailySyncer("off")


def test_describe_next_is_readable_and_local():
    s = scheduler.DailySyncer("04:00")
    text = s.describe_next()
    assert text.startswith("下次 ")
    assert "04:00" in text


def test_describe_next_never_raises(monkeypatch):
    """它嵌在 lifespan 的 print 里，抛出去就是网关起不来。"""
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(scheduler, "next_run_at", boom)
    assert scheduler.DailySyncer("04:00").describe_next() == "下次时间未知"


# ------------------------------------------------------------------ from_env

def test_from_env_defaults_to_0400():
    s = scheduler.from_env({})
    assert s is not None
    assert s.at == (4, 0)
    assert s.at_text == "04:00"


def test_from_env_reads_value():
    s = scheduler.from_env({"MIH_DAILY_SYNC": "07:15"})
    assert s is not None and s.at == (7, 15)


@pytest.mark.parametrize("value", ["off", "none", ""])
def test_from_env_disabled(value):
    assert scheduler.from_env({"MIH_DAILY_SYNC": value}) is None


def test_from_env_bad_value_returns_none_and_warns(capsys):
    """手误必须降级成「关闭 + 一行警告」，绝不能抛。"""
    assert scheduler.from_env({"MIH_DAILY_SYNC": "25:00"}) is None
    out = capsys.readouterr().out
    assert "MIH_DAILY_SYNC" in out
    assert "已关闭" in out


def test_from_env_reads_os_environ_when_env_is_none(monkeypatch):
    monkeypatch.setenv("MIH_DAILY_SYNC", "09:30")
    s = scheduler.from_env()
    assert s is not None and s.at == (9, 30)


# ------------------------------------------------------------------ 存储层

def _make_db(path):
    """建一个带 schema 的空库，然后关掉（模拟 `mihealth-sync` 建好的库）。"""
    st = Store(path)
    st.close()


def test_due_now_is_true_without_a_database(tmp_path):
    """没有库 = 从未同步过；而且**绝不能**顺手把库建出来。"""
    db_path = str(tmp_path / "mihealth.db")
    s = scheduler.DailySyncer("04:00", db_path=db_path)
    assert s.due_now() is True
    assert not os.path.exists(db_path)


def test_due_now_does_not_raise_when_database_exists(tmp_path):
    db_path = str(tmp_path / "mihealth.db")
    _make_db(db_path)
    s = scheduler.DailySyncer("04:00", db_path=db_path)
    assert s.due_now() is True          # 有库但没有 auto_daily 行 → 仍是「没跑过」


def test_mark_ran_then_due_now_is_false_immediately(tmp_path):
    """这条抓「get_state 用错默认字段（last_wm）」的 bug。"""
    db_path = str(tmp_path / "mihealth.db")
    _make_db(db_path)
    s = scheduler.DailySyncer("04:00", db_path=db_path)
    s.mark_ran(time.time())
    assert s.due_now() is False


def test_mark_ran_writes_seconds_into_last_ts(tmp_path):
    db_path = str(tmp_path / "mihealth.db")
    _make_db(db_path)
    s = scheduler.DailySyncer("04:00", db_path=db_path)
    now = time.time()
    s.mark_ran(now)

    st = Store(db_path)
    try:
        # 必须显式给字段名：默认字段是 last_wm
        last = st.get_state(scheduler.STATE_NAME, "last_ts")
        assert st.get_state(scheduler.STATE_NAME) is None      # 默认字段确实没写
    finally:
        st.close()
    assert last == int(now)
    assert 1e9 < last < 2e9                                    # 秒，不是毫秒


def test_mark_ran_gives_up_without_creating_the_database(tmp_path):
    """库始终不存在时最多等一小会儿就放弃，且绝不自己建库。"""
    db_path = str(tmp_path / "mihealth.db")
    s = scheduler.DailySyncer("04:00", db_path=db_path,
                              mark_retries=3, mark_retry_s=0.01)
    s.mark_ran(time.time())
    assert not os.path.exists(db_path)


def test_due_now_survives_reset_store(tmp_path):
    """模拟同步线程刚跑完：`SyncJob._finish()` 会关掉缓存的连接。

    若实现缓存了长句柄，这里会炸 sqlite3.ProgrammingError。
    """
    db_path = str(tmp_path / "mihealth.db")
    _make_db(db_path)
    s = scheduler.DailySyncer("04:00", db_path=db_path)
    s.mark_ran(time.time())
    runtime.reset_store()
    assert s.due_now() is False


def test_syncer_uses_paths_db_path_by_default(monkeypatch, tmp_path):
    """不传 db_path 时按 `paths.db_path()`（即 $MIH_DB / cwd）走。"""
    monkeypatch.setenv("MIH_DB", str(tmp_path / "cfg.db"))
    assert paths.db_path() == str(tmp_path / "cfg.db")
    assert scheduler.DailySyncer("04:00").due_now() is True


# ------------------------------------------------------------------ 集成（假 job）

class _FakeJob:
    def __init__(self, accepts=True):
        self.accepts = accepts
        self.calls = []

    def start(self, hours=72, families=None):
        self.calls.append({"hours": hours, "families": families})
        return self.accepts


def _wire(monkeypatch, job):
    monkeypatch.setattr(scheduler.runtime, "get_config",
                        lambda: {"db": None, "config_path": None})
    monkeypatch.setattr(scheduler.runtime, "get_sync_job",
                        lambda db=None, cfg=None: job)


def test_kick_records_run_when_job_starts(monkeypatch, tmp_path):
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    marks = []
    s = scheduler.DailySyncer("04:00", db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", marks.append)

    assert s._kick("catch-up") is True
    assert job.calls == [{"hours": 72, "families": None}]
    assert len(marks) == 1
    assert s._last is not None


def test_kick_skips_when_a_sync_is_already_running(monkeypatch, tmp_path):
    job = _FakeJob(accepts=False)
    _wire(monkeypatch, job)
    marks = []
    s = scheduler.DailySyncer("04:00", db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", marks.append)

    assert s._kick("scheduled") is False
    assert marks == []          # 没真的跑起来，今天这额度不能算用掉
    assert s._last is None      # 也不能触发去重窗口


def test_run_catches_up_once_and_then_waits(monkeypatch, tmp_path):
    """空库（catchup_gap_s=0）→ 启动即补跑一次，且只一次。"""
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    s = scheduler.DailySyncer("04:00", catchup_gap_s=0,
                              db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", lambda ts: None)

    async def drive():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert [c["hours"] for c in job.calls] == [72]


def test_run_ignores_failures_of_the_catch_up_probe(monkeypatch, tmp_path, capsys):
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    s = scheduler.DailySyncer("04:00", db_path=str(tmp_path / "mihealth.db"))

    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(s, "due_now", boom)

    async def drive():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert "[auto] 补跑判断失败，跳过" in capsys.readouterr().out
    assert job.calls == []


def test_run_survives_a_kick_error_and_retries(monkeypatch, tmp_path, capsys):
    """调度循环里的意外异常不能让协程死掉，而是打日志 + 退避后重来。"""
    monkeypatch.setattr(scheduler, "RETRY_S", 0.01)
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    s = scheduler.DailySyncer("04:00", db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", lambda ts: None)
    monkeypatch.setattr(s, "due_now", lambda: False)

    def boom(why):
        raise RuntimeError("kick exploded")
    monkeypatch.setattr(s, "_kick", boom)
    # 把「下一个定点」压到最短：run() 的 max(1.0, ...) 下限是 1 秒
    monkeypatch.setattr(scheduler, "next_run_at", lambda now, *at: now)

    async def drive():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(1.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert "[auto] 调度循环异常，60 秒后重试" in capsys.readouterr().out
    assert job.calls == []


def test_scheduled_kick_is_deduped_within_half_a_day(monkeypatch, tmp_path):
    """补跑刚跑完，到点时不再拉第二次（§5.4.1 ③）。"""
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    s = scheduler.DailySyncer("04:00", db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", lambda ts: None)

    assert s._recently_ran() is False        # 还没跑过
    assert s._kick("catch-up") is True
    assert s._recently_ran() is True         # 刚跑过，定点触发要让路

    s._last = time.time() - scheduler.DEDUPE_S - 1
    assert s._recently_ran() is False        # 超过半天，正常触发


def test_run_skips_the_scheduled_kick_after_a_catch_up(monkeypatch, tmp_path):
    job = _FakeJob(accepts=True)
    _wire(monkeypatch, job)
    s = scheduler.DailySyncer("04:00", catchup_gap_s=0,
                              db_path=str(tmp_path / "mihealth.db"))
    monkeypatch.setattr(s, "mark_ran", lambda ts: None)
    # 定点压到最短 —— run() 里 max(1.0, ...) 的下限就是 1 秒
    monkeypatch.setattr(scheduler, "next_run_at", lambda now, *at: now)

    async def drive():
        task = asyncio.create_task(s.run())
        await asyncio.sleep(1.2)      # 够跨过一次定点触发
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    # 补跑那一次 + 定点被 12 小时去重拦掉 → 只启动过一次同步
    assert len(job.calls) == 1
