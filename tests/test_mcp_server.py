# -*- coding: utf-8 -*-
"""MCP 工具层：7 个工具直调（同步函数，不经协议）。

覆盖意图
--------
* 无库时给一条能照着做的错误（不是 traceback）
* 有时间窗口的参数真的按**毫秒**入参、**秒**出参
* `limit` 夹到 [1, 5000]
* `sync_now` 是唯一写工具，重复调用不能起两个任务
* `build_app()` 的 Host 白名单随 `--host` 变化（`0.0.0.0` 只能关防护）
"""
import json
import time

import pytest

from mihealth_api import mcp_server as M
from mihealth_api import runtime
from mihealth_api.store import Store

NOW = int(time.time())


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    runtime.reset_store()
    runtime.reset_client()
    runtime._jobs.clear()
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "mcp.db"))
    runtime.reload_config()
    yield tmp_path
    runtime.reset_store()
    runtime.reset_client()
    runtime._jobs.clear()


@pytest.fixture
def db(clean):
    """一个带三种 fitness 键 + 两条 sports 的库。"""
    st = Store(str(clean / "mcp.db"))
    st.upsert("fitness", "steps", [
        {"time": NOW - 3600, "sid": "w", "value": json.dumps({"steps": 100})},
        {"time": NOW - 60, "sid": "w", "value": json.dumps({"steps": 250})},
        {"time": NOW - 60, "sid": "p", "value": json.dumps({"steps": 90})},
    ])
    st.upsert("fitness", "heart_rate", [
        {"time": NOW - 120, "sid": "w", "value": json.dumps({"bpm": 72})},
    ])
    st.upsert("sport", "running", [
        {"time": NOW - 7200, "sid": "w", "_t": NOW - 7200,
         "value": json.dumps({"distance": 5000, "duration": 1800})},
    ])
    runtime.set_store(st)
    return st


# ---------------------------------------------------------------- 无库

def test_tools_report_no_db_with_actionable_hint(clean):
    for out in (M.list_families(), M.list_keys("fitness"),
                M.query_records("fitness")):
        assert out["ok"] is False
        assert "mihealth-sync backfill" in out["err"]


def test_aggregate_and_latest_also_report_no_db(clean):
    assert M.aggregate("fitness", "steps", "steps")["ok"] is False
    assert M.latest(["steps"])["ok"] is False


def test_status_works_without_db_and_says_live(clean):
    out = M.status()
    assert out["ok"] is True and out["mode"] == "live"
    assert out["db_path"] is None
    assert out["sync"]["running"] is False


# ---------------------------------------------------------------- 读工具

def test_status_with_db_reports_counts_and_cursors(db):
    out = M.status()
    assert out["ok"] is True and out["mode"] == "db"
    assert out["db_path"].endswith("mcp.db")
    assert out["rows"] >= 3
    assert "fitness" in out["families"]
    assert isinstance(out["cursors"], dict)


def test_list_families_aggregates_per_family(db):
    out = M.list_families()
    assert out["ok"] is True
    fams = {f["family"]: f for f in out["families"]}
    assert set(fams) == {"fitness", "sport"}
    steps = [k for k in fams["fitness"]["keys"] if k["key"] == "steps"][0]
    assert steps["count"] == 2          # 去重后 w/p 各一条
    assert fams["fitness"]["first"] <= fams["fitness"]["last"]
    assert out["total_rows"] == sum(f["rows"] for f in out["families"])


def test_list_keys_returns_keys_and_sources(db):
    out = M.list_keys("fitness")
    assert out["ok"] is True and out["family"] == "fitness"
    assert {k["key"] for k in out["keys"]} == {"steps", "heart_rate"}
    assert {s["sid"] for s in out["sources"]} == {"w", "p"}


def test_query_records_dedups_by_default(db):
    out = M.query_records("fitness", "steps")
    assert out["ok"] is True and out["dedup"] is True
    assert out["count"] == 2            # NOW-60 的两源只留一行
    assert out["items"][0]["_t"] <= out["items"][-1]["_t"]


def test_query_records_can_keep_duplicates(db):
    out = M.query_records("fitness", "steps", dedup=False)
    assert out["count"] == 3


def test_query_records_accepts_millisecond_window(db):
    lo = (NOW - 120) * 1000
    hi = (NOW - 30) * 1000
    out = M.query_records("fitness", "steps", start_ms=lo, end_ms=hi)
    assert out["count"] == 1
    assert out["items"][0]["_t"] == NOW - 60


def test_query_records_order_desc(db):
    out = M.query_records("fitness", "steps", order="desc")
    assert out["items"][0]["_t"] > out["items"][-1]["_t"]


def test_query_records_limit_is_clamped(db):
    assert M.query_records("fitness", "steps", limit=1)["count"] == 1
    assert M.query_records("fitness", "steps", limit=0)["limit"] == 1
    assert M.query_records("fitness", "steps", limit=99999)["limit"] == M.MAX_LIMIT
    assert M.query_records("fitness", "steps", limit=None)["limit"] == M.DEFAULT_LIMIT


def test_query_records_sid_filter(db):
    out = M.query_records("fitness", "steps", sid="p", dedup=False)
    assert out["count"] == 1 and out["items"][0]["_sid"] == "p"


def test_aggregate_sums_per_day(db):
    out = M.aggregate("fitness", "steps", "steps", agg="sum", bucket_s=86400)
    assert out["ok"] is True and out["agg"] == "sum" and out["bucket_s"] == 86400
    assert sum(p["v"] for p in out["points"]) == 350     # 去重后 100 + 250


def test_aggregate_unknown_field_yields_no_points(db):
    """json_extract 拿不到字段时 SQL 给 NULL，聚合结果为空而不是报错。"""
    out = M.aggregate("fitness", "steps", "nope", agg="sum")
    assert out["ok"] is True and out["points"] == []


def test_latest_picks_newest_per_key(db):
    out = M.latest(["steps", "heart_rate", "spo2"])
    assert out["ok"] is True
    assert out["items"]["steps"]["parsed"]["steps"] == 250
    assert out["items"]["steps"]["time"] == NOW - 60
    assert out["items"]["heart_rate"]["parsed"]["bpm"] == 72
    assert out["items"]["spo2"] is None
    assert out["count"] == 2


def test_latest_defaults_to_scalar_keys(db):
    out = M.latest()
    assert out["ok"] is True
    assert "steps" in out["items"] and "heart_rate" in out["items"]


def test_latest_respects_span(db):
    out = M.latest(["steps"], span_ms=1000)
    assert out["items"]["steps"] is None


# ---------------------------------------------------------------- 写工具

class _SlowSyncer:
    """慢同步器：让任务在第二次调用时仍处于 running。"""

    def __init__(self, *a, **k):
        self.db = type("D", (), {"close": lambda self: None})()

    def incremental(self, hours=72, families=("fitness", "sport")):
        time.sleep(0.3)
        return 7


def _drain(job):
    for _ in range(500):
        if not job.state["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("同步任务没在 5 秒内结束")


def test_sync_now_starts_once_and_refuses_second(monkeypatch, clean):
    import mihealth_api.sync as sync_mod
    from mihealth_api import runtime as rt

    monkeypatch.setattr(sync_mod, "Syncer", _SlowSyncer)
    first = M.sync_now(hours=1)
    assert first["ok"] is True and first["started"] is True
    second = M.sync_now(hours=1)
    assert second["ok"] is False and second["err"] == "already running"
    assert second["state"]["running"] is True
    job = rt.get_sync_job(runtime.get_config().get("db"),
                          runtime.get_config().get("config_path"))
    _drain(job)
    assert job.state["result"]["rows"] == 7


def test_sync_now_passes_hours_through(monkeypatch, clean):
    import mihealth_api.sync as sync_mod

    seen = {}

    class Spy(_SlowSyncer):
        def incremental(self, hours=72, families=("fitness", "sport")):
            seen["hours"], seen["families"] = hours, families
            return 3

    monkeypatch.setattr(sync_mod, "Syncer", Spy)
    assert M.sync_now(hours=6, families=["fitness"])["ok"] is True
    job = runtime.get_sync_job(runtime.get_config().get("db"),
                               runtime.get_config().get("config_path"))
    _drain(job)
    assert seen["hours"] == 6 and seen["families"] == ("fitness",)


# ---------------------------------------------------------------- app 装配

def test_build_app_mounts_at_root_path():
    app = M.build_app("127.0.0.1")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in paths            # streamable_http_path="/" → 挂到 /mcp 下就是 /mcp/


def test_allowed_hosts_lists_localhost_variants():
    sec = M._allowed_hosts("127.0.0.1")
    assert sec is not None
    assert sec.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in sec.allowed_hosts
    assert "localhost:*" in sec.allowed_hosts
    assert "testserver" in sec.allowed_hosts      # TestClient 的 Host


def test_allowed_hosts_includes_bind_host():
    sec = M._allowed_hosts("192.168.1.5")
    assert "192.168.1.5" in sec.allowed_hosts
    assert "192.168.1.5:*" in sec.allowed_hosts


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_allowed_hosts_disabled_for_wildcard_bind(host):
    """绑全网卡时无法枚举 Host，只能关防护（SDK 也无法用 * 通配）。"""
    assert M._allowed_hosts(host) is None


def test_build_app_with_wildcard_bind_omits_transport_security():
    app = M.build_app("0.0.0.0")
    assert app is not None


def test_tool_catalog_is_the_documented_seven():
    names = {t.name for t in M.mcp._tool_manager.list_tools()}
    assert names == {"status", "list_families", "list_keys", "query_records",
                     "aggregate", "latest", "sync_now"}


def test_only_sync_now_is_write():
    tools = {t.name: t for t in M.mcp._tool_manager.list_tools()}
    for name, t in tools.items():
        hint = getattr(t.annotations, "read_only_hint", None)
        if name == "sync_now":
            assert hint is False
        else:
            assert hint is True, name
