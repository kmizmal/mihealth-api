# -*- coding: utf-8 -*-
"""同步器：分页入库、水位游标、并发、`time=0` 不丢、鉴权失败给操作员指路。"""
import json

import pytest

from mihealth_api import sync as SY
from mihealth_api.client import AuthError
from mihealth_api.store import Store


class FakeClient:
    """把云端方法换成可编排的假实现，顺便记录调用。"""

    def __init__(self):
        self.calls = []
        self.fitness_pages = {}      # key -> [[item, ...], ...]
        self.sport_pages = []
        self.diet_rows = []
        self.goal_rows = []
        self.max_wm = 1000
        self.wm_pages = []           # [(items, watermark)]
        self.auth_error = None

    # -- fitness ---------------------------------------------------------
    def get_fitness_data_all(self, key, start, end, max_pages=1000,
                             full_history=False, on_page=None):
        self.calls.append(("fitness", key, start, end, max_pages, full_history))
        if self.auth_error:
            raise self.auth_error
        pages = self.fitness_pages.get(key, [])
        for i, batch in enumerate(pages):
            if i >= max_pages:
                break
            if on_page:
                on_page(batch, str(i + 1), i)
        return {"has_more": len(pages) > max_pages, "pages": len(pages)}

    # -- sport -----------------------------------------------------------
    def get_sport_records_all(self, start, end, max_pages=500,
                              full_history=False, on_page=None):
        self.calls.append(("sport", start, end, max_pages, full_history))
        if self.auth_error:
            raise self.auth_error
        for i, batch in enumerate(self.sport_pages):
            if on_page:
                on_page(batch, str(i + 1), i)
        return {"has_more": False}

    # -- diet / goals ----------------------------------------------------
    def get_diet_records_by_time(self, start_s, end_s):
        self.calls.append(("diet", start_s, end_s))
        return {"diet_records": self.diet_rows}

    def get_aggregated_all(self, _nil, start, end, name, limit):
        self.calls.append(("goal", start, end, name, limit))
        return {"data_list": self.goal_rows}

    # -- watermark -------------------------------------------------------
    def get_max_watermark(self, wtype):
        self.calls.append(("max_wm", wtype))
        return self.max_wm

    def sync_by_watermark(self, sub, field, wm, max_pages=400, on_page=None):
        self.calls.append(("wm", sub, field, wm, max_pages))
        last = wm
        for i, (items, watermark) in enumerate(self.wm_pages):
            if on_page:
                on_page(items, watermark, i)
            last = watermark or last
        return [], last, len(self.wm_pages) > max_pages


@pytest.fixture
def syncer(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(SY, "client_from_config", lambda *a, **k: fake)
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {"ssecurity": "S"})
    s = SY.Syncer(str(tmp_path / "s.db"), workers=2, min_interval=0, config_path=None)
    s.fake = fake
    yield s
    s.db.close()


def _fit(ts, steps, sid="watch"):
    """形状与真实 `_paged_all` 输出一致：`_t` 已由客户端补好。"""
    return {"time": ts, "_t": ts, "sid": sid,
            "value": json.dumps({"steps": steps})}


def _sport(ts, key="outdoor_running", sid="watch"):
    return {"time": ts, "_t": ts, "sid": sid, "key": key,
            "value": json.dumps({"distance": 5.0})}


def test_backfill_key_streams_pages_into_db(syncer):
    syncer.fake.fitness_pages["steps"] = [[_fit(100, 1), _fit(200, 2)],
                                          [_fit(300, 3)]]
    n, more = syncer._backfill_key("steps", 0, 10**15)
    assert n == 3 and more is False
    assert syncer.db.count("fitness", "steps") == 3
    assert syncer.db.get_state("ts_fitness_steps", "last_ts") == 300


def test_backfill_key_reports_partial_when_max_pages_hit(syncer):
    syncer.fake.fitness_pages["steps"] = [[_fit(i, i)] for i in range(5)]
    syncer.fake.get_fitness_data_all = (
        lambda key, s, e, max_pages=1000, full_history=False, on_page=None:
        (on_page([_fit(1, 1)], "1", 0) if on_page else None) or {"has_more": True})
    n, more = syncer._backfill_key("steps", 0, 10**15)
    assert n == 1 and more is True


def test_backfill_key_keeps_time_zero(syncer):
    syncer.fake.fitness_pages["steps"] = [[_fit(0, 9)]]
    syncer._backfill_key("steps", 0, 10**15)
    assert syncer.db.rows("fitness", "steps")[0]["steps"] == 9


def test_sport_rows_split_by_key(syncer):
    syncer.fake.sport_pages = [[_sport(10, "outdoor_running"),
                                _sport(20, "outdoor_walking")]]
    n, more = syncer._backfill_sport(0, 10**15)
    assert n == 2 and more is False
    assert syncer.db.count("sport", "outdoor_running") == 1
    assert syncer.db.count("sport", "outdoor_walking") == 1
    assert syncer.db.get_state("ts_sport_sport_records", "last_ts") == 20


def test_upsert_sport_defaults_to_unknown_key(syncer):
    assert syncer._upsert_sport([{"time": 5, "sid": "s", "value": "{}"}]) == 1
    assert syncer.db.count("sport", "unknown") == 1


def test_backfill_diet_uses_seconds(syncer):
    syncer.fake.diet_rows = [{"time": 100, "key": "breakfast", "sid": "x",
                              "value": "{}"}]
    n = syncer._backfill_diet(5_000, 9_000)
    assert n == 1
    assert syncer.fake.calls[-1] == ("diet", 5, 9)
    assert syncer.db.count("diet", "breakfast") == 1


def test_backfill_goals(syncer):
    syncer.fake.goal_rows = [{"time": 100, "sid": "g", "value": "{}"}]
    assert syncer._backfill_goals(0, 10**9) == 1
    assert syncer.db.count("goal", "goal") == 1


def test_wm_family_initialises_cursor_without_pulling(syncer):
    assert syncer._wm_family("fitness") == 0
    assert syncer.db.get_state("wm_fitness", "last_wm") == 1000
    assert not [c for c in syncer.fake.calls if c[0] == "wm"]


def test_wm_family_pulls_and_persists_cursor(syncer):
    syncer.db.set_state("wm_fitness", last_wm=10)
    syncer.fake.wm_pages = [([{"time": 11, "_t": 11, "key": "steps", "sid": "w",
                              "value": json.dumps({"steps": 5})}], 20),
                            ([{"time": 12, "_t": 12, "key": "calories", "sid": "w",
                               "value": json.dumps({"calories": 2})}], 0)]
    n = syncer._wm_family("fitness")
    assert n == 2
    assert syncer.db.get_state("wm_fitness", "last_wm") == 20     # 0 标记不覆盖
    assert syncer.db.count("fitness", "steps") == 1
    assert syncer.db.count("fitness", "calories") == 1


def test_wm_family_sport_uses_own_upsert(syncer):
    syncer.db.set_state("wm_sport", last_wm=10)
    syncer.fake.wm_pages = [([_sport(11)], 30)]
    assert syncer._wm_family("sport") == 1
    assert syncer.db.count("sport", "outdoor_running") == 1
    assert syncer.db.get_state("wm_sport", "last_wm") == 30


def test_incremental_feeds_and_topups(syncer):
    syncer.db.set_state("wm_fitness", last_wm=10)
    syncer.db.set_state("wm_sport", last_wm=10)
    syncer.fake.wm_pages = [([{"time": 11, "_t": 11, "key": "steps", "sid": "w",
                              "value": json.dumps({"steps": 5})}], 20)]
    syncer.fake.fitness_pages = {k: [[_fit(100 + i, i)]] for i, k in
                                 enumerate(SY.FITNESS_KEYS)}
    syncer.fake.sport_pages = [[_sport(50)]]
    syncer.fake.goal_rows = [{"time": 60, "sid": "g", "value": "{}"}]
    syncer.fake.diet_rows = []
    total = syncer.incremental(hours=24)
    assert total > 0
    assert syncer.db.count("goal", "goal") == 1
    # 每个数据键都补过窗口
    topup_keys = {c[1] for c in syncer.fake.calls if c[0] == "fitness"}
    assert {k for k in SY.FITNESS_KEYS if k in topup_keys} == set(SY.FITNESS_KEYS)


def test_window_topup_advances_ts_cursor(syncer):
    syncer.fake.fitness_pages = {"steps": [[_fit(500, 4)]]}
    syncer.fake.sport_pages = []
    syncer.fake.goal_rows = []
    syncer.fake.diet_rows = []
    syncer._window_topup(1)
    assert syncer.db.get_state("ts_fitness_steps", "last_ts") == 500


def test_window_topup_survives_failing_key(syncer):
    def boom(*a, **k):
        raise RuntimeError("network down")

    original = syncer.fake.get_fitness_data_all
    syncer.fake.get_fitness_data_all = (
        lambda key, *a, **k: boom() if key == "steps" else original(key, *a, **k))
    syncer.fake.sport_pages = []
    syncer.fake.goal_rows = []
    syncer.fake.diet_rows = []
    syncer._window_topup(1)          # 不抛异常，其它键照旧


def test_auth_error_exits_with_actionable_message(syncer):
    syncer.fake.auth_error = AuthError("auth failed (HTTP 401)")
    with pytest.raises(SystemExit) as e:
        syncer._backfill_key("steps", 0, 10**15)
    assert e.value.code == 2


def test_on_auth_error_invokes_refresh(syncer):
    seen = []
    syncer.refresh = lambda client: seen.append(1) or True
    assert syncer._on_auth_error(syncer.fake, "401") is True
    assert seen == [1]


def test_on_auth_error_without_hook_returns_false(syncer):
    assert syncer._on_auth_error(syncer.fake, "401") is False


def test_on_auth_error_swallows_refresh_exception(syncer):
    def bad(client):
        raise RuntimeError("passToken dead")

    syncer.refresh = bad
    assert syncer._on_auth_error(syncer.fake, "401") is False


def test_status_prints_cursors_and_totals(syncer, capsys):
    syncer.db.upsert("fitness", "steps", [_fit(100, 1)])
    syncer.db.set_state("wm_fitness", last_wm=7, last_ts=100)
    syncer.status()
    out = capsys.readouterr().out
    assert "db:" in out and "cursors:" in out and "rows:" in out
    assert "wm_fitness" in out and "TOTAL 1" in out


def test_status_handles_empty_db(syncer, capsys):
    syncer.status()
    assert "TOTAL 0" in capsys.readouterr().out


def test_main_status_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(SY, "make_refresh", lambda *a, **k: None)
    db = tmp_path / "x.db"
    Store(str(db)).close()
    assert SY.main(["status", "--db", str(db)]) in (None, 0)
    assert "rows:" in capsys.readouterr().out


def test_main_export_requires_family_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(SY, "make_refresh", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        SY.main(["export", "--db", str(tmp_path / "x.db")])


def test_main_export_writes_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(SY, "make_refresh", lambda *a, **k: None)
    db = tmp_path / "x.db"
    st = Store(str(db))
    st.upsert("fitness", "steps", [_fit(100, 1)])
    st.close()
    out = tmp_path / "o.csv"
    SY.main(["export", "--db", str(db), "--family", "fitness",
             "--key", "steps", "--csv", str(out)])
    assert "wrote 1 rows" in capsys.readouterr().out


def test_main_no_auto_refresh_disables_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(SY, "make_refresh",
                        lambda *a, **k: pytest.fail("should not be called"))
    db = tmp_path / "x.db"
    Store(str(db)).close()
    SY.main(["status", "--db", str(db), "--no-auto-refresh"])


def test_make_refresh_delegates_to_auth(monkeypatch):
    from mihealth_api import auth as A
    monkeypatch.setattr(A, "make_refresh", lambda p, use_device=True: ("hook", p, use_device))
    assert SY.make_refresh("/x/c.json", use_device=False) == ("hook", "/x/c.json", False)
