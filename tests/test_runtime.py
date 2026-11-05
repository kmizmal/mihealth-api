# -*- coding: utf-8 -*-
"""进程内共享活对象：惰性 store、client 钩子、同步任务单例。"""
import json
import os
import threading
import time

import pytest

from mihealth_api import runtime
from mihealth_api.store import Store


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    """每条用例前后都把全局单例清干净，避免用例互相污染。"""
    runtime.reset_store()
    runtime.reset_client()
    runtime._jobs.clear()
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "mihealth.db"))
    runtime.reload_config()
    yield tmp_path
    runtime.reset_store()
    runtime.reset_client()
    runtime._jobs.clear()


def _make_db(path, rows=2):
    st = Store(str(path))
    now = int(time.time())
    st.upsert("fitness", "steps", [
        {"time": now - i, "sid": "w", "value": json.dumps({"steps": i})}
        for i in range(rows)])
    st.close()


def test_get_store_returns_none_without_file(clean):
    assert runtime.get_store() is None


def test_get_store_opens_later_created_db(clean):
    """先起网关、后 backfill 的场景：库文件出现后必须能自动打开。"""
    assert runtime.get_store() is None
    _make_db(clean / "mihealth.db")
    st = runtime.get_store()
    assert st is not None and st.count() == 2


def test_get_store_caches_same_path(clean):
    _make_db(clean / "mihealth.db")
    assert runtime.get_store() is runtime.get_store()


def test_get_store_reopens_on_path_change(clean, monkeypatch):
    _make_db(clean / "mihealth.db")
    first = runtime.get_store()
    other = clean / "other.db"
    _make_db(other, rows=3)
    monkeypatch.setenv("MIH_DB", str(other))
    runtime.reload_config()
    second = runtime.get_store()
    assert second is not first and second.count() == 3


def test_reset_store_forces_reopen(clean):
    _make_db(clean / "mihealth.db")
    first = runtime.get_store()
    runtime.reset_store()
    assert runtime.get_store() is not first


def test_set_store_injects_instance(clean):
    _make_db(clean / "mihealth.db")
    st = Store(str(clean / "mihealth.db"))
    runtime.set_store(st)
    assert runtime.get_store() is st


def test_get_store_survives_corrupt_file(clean, capsys):
    (clean / "mihealth.db").write_bytes(b"not a database at all")
    assert runtime.get_store() is None
    assert "cannot open" in capsys.readouterr().out


def test_config_cached_and_reloaded(clean):
    cfg = clean / "config.json"
    cfg.write_text(json.dumps({"cuser_id": "A"}), encoding="utf-8")
    monkeypatch_ok = runtime.reload_config()
    assert monkeypatch_ok["cuser_id"] == "A"


def test_set_client_hook_applies_to_future_clients(clean):
    """核心回归：reset_client() 之后新 client 也要带上 401 自愈回调。"""
    seen = {}

    def hook(cl, why):
        seen["hook"] = True
        return True

    runtime.set_client_hook(hook)
    cfg = clean / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "S", "service_token": "T",
                               "cuser_id": "C"}), encoding="utf-8")
    runtime.reload_config()
    first = runtime.get_client()
    assert first.on_auth_error is hook
    runtime.reset_client()
    second = runtime.get_client()
    assert second is not first and second.on_auth_error is hook


def test_set_client_overrides_singleton(clean):
    class Cl:
        on_auth_error = None

    c = Cl()
    runtime.set_client(c)
    assert runtime.get_client() is c


def test_sync_job_start_finish_and_state(clean, monkeypatch):
    class Ok:
        def __init__(self, *a, **k):
            self.db = type("D", (), {"close": lambda s: None})()

        def incremental(self, hours=72, families=None):
            return 11

    monkeypatch.setattr("mihealth_api.sync.Syncer", Ok)
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    assert job.start() is True
    for _ in range(200):
        if not job.state["running"]:
            break
        time.sleep(0.01)
    st = job.state
    assert st["running"] is False
    assert st["result"] == {"ok": True, "rows": 11}
    assert st["started"] and isinstance(st["started"], int)


def test_sync_job_rejects_second_start(clean):
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    job._state["running"] = True
    assert job.start() is False


def test_sync_job_state_is_copy(clean):
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    job.state["running"] = True
    assert job.state["running"] is False


def test_sync_job_captures_exception(clean, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("cannot create")

    monkeypatch.setattr("mihealth_api.sync.Syncer", Boom)
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    job.start()
    for _ in range(200):
        if not job.state["running"]:
            break
        time.sleep(0.01)
    assert job.state["result"] == {"ok": False, "err": "cannot create"}


def test_sync_job_maps_systemexit_to_auth_error(clean, monkeypatch):
    class Exit:
        def __init__(self, *a, **k):
            self.db = type("D", (), {"close": lambda s: None})()

        def incremental(self, hours=72, families=None):
            raise SystemExit(2)

    monkeypatch.setattr("mihealth_api.sync.Syncer", Exit)
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    job.start()
    for _ in range(200):
        if not job.state["running"]:
            break
        time.sleep(0.01)
    assert job.state["result"]["ok"] is False
    assert "auth/credentials" in job.state["result"]["err"]


def test_sync_job_passes_families_through(clean, monkeypatch):
    seen = {}

    class Ok:
        def __init__(self, *a, **k):
            self.db = type("D", (), {"close": lambda s: None})()

        def incremental(self, hours=72, families=None):
            seen["families"] = families
            return 1

    monkeypatch.setattr("mihealth_api.sync.Syncer", Ok)
    job = runtime.SyncJob(str(clean / "mihealth.db"))
    job.start(hours=24, families=["fitness"])
    for _ in range(200):
        if not job.state["running"]:
            break
        time.sleep(0.01)
    assert seen["families"] == ("fitness",)


def test_get_sync_job_is_singleton(clean):
    """HTTP 与 MCP 必须拿到同一个任务实例，否则各持一把锁 = 没有锁。"""
    a = runtime.get_sync_job(str(clean / "mihealth.db"), str(clean / "config.json"))
    b = runtime.get_sync_job(str(clean / "mihealth.db"), str(clean / "config.json"))
    assert a is b
    c = runtime.get_sync_job(str(clean / "other.db"), str(clean / "config.json"))
    assert c is not a
