# -*- coding: utf-8 -*-
"""backfill 编排：并发、失败隔离、游标播种、饮食/目标分支。"""
import json

import pytest

from mihealth_api import sync as SY
from mihealth_api.client import AuthError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.max_wm = 555
        self.wm_fail = False
        self.fail_key = None
        self.sport_pages = []
        self.diet_rows = []
        self.goal_rows = []
        self.auth_error = False

    def get_fitness_data_all(self, key, start, end, max_pages=3000,
                             full_history=False, on_page=None):
        self.calls.append(("fitness", key, start, end, max_pages, full_history))
        if self.auth_error:
            raise AuthError("auth failed (HTTP 401)")
        if key == self.fail_key:
            raise RuntimeError(f"{key} exploded")
        if on_page:
            on_page([{"time": 100 + len(self.calls), "_t": 100 + len(self.calls),
                      "sid": "w", "value": json.dumps({"v": 1})}], None, 0)
        return {"has_more": False}

    def get_sport_records_all(self, start, end, max_pages=500, full_history=False,
                              on_page=None):
        self.calls.append(("sport", start, end, max_pages))
        for i, batch in enumerate(self.sport_pages):
            if on_page:
                on_page(batch, None, i)
        return {"has_more": True}

    def get_diet_records_by_time(self, start_s, end_s):
        self.calls.append(("diet", start_s, end_s))
        return {"diet_records": self.diet_rows}

    def get_aggregated_all(self, _nil, start, end, tag, limit):
        self.calls.append(("goal", start, end, tag, limit))
        return {"data_list": self.goal_rows}

    def get_max_watermark(self, wtype):
        self.calls.append(("max_wm", wtype))
        if self.wm_fail:
            raise RuntimeError("watermark down")
        return self.max_wm


@pytest.fixture
def sy(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(SY, "client_from_config", lambda *a, **k: fake)
    monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
    s = SY.Syncer(str(tmp_path / "b.db"), workers=2, min_interval=0)
    s.fake = fake
    yield s
    s.db.close()


def test_backfill_runs_all_fitness_keys(sy):
    keys = ["steps", "calories"]
    sy.backfill(keys=keys, families=("fitness",))
    called = {c[1] for c in sy.fake.calls if c[0] == "fitness"}
    assert called == set(keys)
    assert sy.db.count("fitness", "steps") == 1


def test_backfill_default_keys_are_all(sy):
    sy.backfill(families=())
    called = {c[1] for c in sy.fake.calls if c[0] == "fitness"}
    assert called == set(SY.FITNESS_KEYS)


def test_backfill_days_sets_window(sy):
    sy.backfill(keys=["steps"], families=())
    start = [c[2] for c in sy.fake.calls if c[0] == "fitness"][0]
    assert start == 0                       # days=0 → 全历史


def test_backfill_with_days_window(sy):
    sy.backfill(keys=["steps"], families=(), days=7)
    start, end = [c[2:4] for c in sy.fake.calls if c[0] == "fitness"][0]
    assert abs((end - start) - 7 * SY.DAY) < 5000


def test_backfill_isolates_failing_key(sy, capsys):
    sy.fake.fail_key = "spo2"
    sy.backfill(keys=["steps", "spo2"], families=())
    out = capsys.readouterr().out
    assert "spo2: FAILED" in out
    assert sy.db.count("fitness", "steps") == 1


def test_backfill_sport_partial_flag(sy, capsys):
    sy.fake.sport_pages = [[{"time": 5, "_t": 5, "key": "walking", "sid": "w",
                             "value": "{}"}]]
    sy.backfill(keys=["steps"], families=("sport",))
    out = capsys.readouterr().out
    assert "sport_records: 1 rows (partial)" in out
    assert sy.db.count("sport", "walking") == 1


def test_backfill_goals_and_diet(sy):
    sy.fake.goal_rows = [{"time": 9, "sid": "g", "value": "{}"}]
    sy.fake.diet_rows = [{"time": 8, "key": "lunch", "sid": "x", "value": "{}"}]
    sy.backfill(keys=None, families=("fitness", "sport", "diet"))
    assert sy.db.count("goal", "goal") == 1
    assert sy.db.count("diet", "lunch") == 1


def test_backfill_seeds_watermark_cursors(sy):
    sy.backfill(keys=["steps"], families=())
    assert sy.db.get_state("wm_fitness", "last_wm") == 555
    assert sy.db.get_state("wm_sport", "last_wm") == 555


def test_backfill_reports_cursor_failure(sy, capsys):
    sy.fake.wm_fail = True
    sy.backfill(keys=["steps"], families=())
    assert "seed failed" in capsys.readouterr().out


def test_backfill_propagates_auth_failure(sy):
    sy.fake.auth_error = True
    with pytest.raises(SystemExit) as e:
        sy.backfill(keys=["steps"], families=())
    assert e.value.code == 2
