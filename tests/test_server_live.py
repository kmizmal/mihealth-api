# -*- coding: utf-8 -*-
"""网关的「无本地库」实时模式 + 各条错误分支。"""
import importlib
import json
import time

import pytest

from test_server import FakeClient, get, post


@pytest.fixture
def live(tmp_path, monkeypatch):
    """没有数据库、凭据也不全：所有读库接口应当退化到实时调用或报 404。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    from mihealth_api import server as SV
    SV = importlib.reload(SV)
    fake = FakeClient()
    SV.client = fake
    SV._cache.clear()
    SV._store = None
    SV._login_state.update(session=None, captcha_url=None, username=None,
                           **{"2fa_context": None, "2fa_flag": None})
    return SV, fake


def test_health_live_mode(live):
    SV, _ = live
    body = get(live, "/api/health").get_json()
    assert body["mode"] == "live" and body["db"] is None
    assert "rows" not in body


def test_health_reports_generic_error(live):
    SV, fake = live

    def boom(sub, params, retries=0, decrypt=True):
        raise RuntimeError("dns down")

    fake.call = boom
    body = get(live, "/api/health").get_json()
    assert body["auth"] == "error" and "dns down" in body["auth_error"]


def test_health_reports_syncing(live):
    SV, _ = live
    SV._sync_job["running"] = True
    try:
        assert get(live, "/api/health").get_json()["syncing"] is True
    finally:
        SV._sync_job["running"] = False


def test_db_routes_404_in_live_mode(live):
    for path in ("/api/families", "/api/db/fitness", "/api/db/fitness/keys",
                 "/api/agg/fitness/steps?field=steps", "/api/export.csv"):
        assert get(live, path).status_code == 404, path


def test_series_live_passthrough(live):
    SV, fake = live
    fake.fitness = {"steps": [{"time": 1, "sid": "w", "value": '{"steps":5}'}]}
    body = get(live, "/api/series/steps?hours=24").get_json()
    assert body["source"] == "live"
    assert body["items"][0]["steps"] == 5
    assert body["items"][0]["_sid"] == "w"


def test_series_live_bad_json_does_not_crash(live):
    SV, fake = live
    fake.fitness = {"steps": [{"time": 1, "sid": "w", "value": "not-json"}]}
    body = get(live, "/api/series/steps?hours=24").get_json()
    assert body["items"][0]["_t"] == 1


def test_series_null_response(live):
    SV, fake = live
    fake.get_fitness_data_all = lambda *a, **k: None
    body = get(live, "/api/series/steps?hours=24").get_json()
    assert body["ok"] is False and body["err"] == "null"


def test_daily_goals_live(live):
    SV, fake = live
    fake.get_aggregated_all = lambda *a, **k: {"data_list": [
        {"time": 100, "value": json.dumps({"goal_items": [
            {"field": 1, "achieved_value": 1, "target_value": 2}]})},
        {"time": 100, "value": json.dumps({"goal_items": []})},
    ]}
    body = get(live, "/api/daily_goals?days=7").get_json()
    assert body["ok"] is True and body["count"] == 1      # 同一时刻只留一条


def test_daily_goals_live_unknown_field(live):
    SV, fake = live
    fake.get_aggregated_all = lambda *a, **k: {"data_list": [
        {"time": 1, "value": json.dumps({"goal_items": [{"field": 9,
                                                         "achieved_value": 3,
                                                         "target_value": 4}]})}]}
    body = get(live, "/api/daily_goals").get_json()
    assert body["items"][0]["goals"]["field_9"]["target"] == 4


def test_sport_records_live_widens_window(live):
    SV, fake = live
    seen = []

    def fake_all(start, end, max_pages=500, full_history=False):
        seen.append((start, end, full_history))
        if len(seen) == 1:
            return {"sport_records": []}         # 第一次空 → 放宽窗口
        return {"sport_records": [{"time": 1, "key": "walking", "value": "{}"}]}

    fake.get_sport_records_all = fake_all
    body = get(live, "/api/sport_records?days=30").get_json()
    assert body["source"] == "live" and body["days_used"] == 90
    assert body["items"][0]["_key"] == "walking"


def test_sport_records_live_all_empty(live):
    SV, fake = live
    fake.get_sport_records_all = lambda *a, **k: {"sport_records": []}
    body = get(live, "/api/sport_records?days=30").get_json()
    assert body["items"] == [] and body["source"] == "live"


def test_sport_types_live(live):
    SV, fake = live
    fake.pages["data/get_sport_records_by_time"] = [
        {"sport_records": [{"time": 1, "key": "walking"}], "has_more": False}]
    body = get(live, "/api/sport_types").get_json()
    assert body["ok"] is True and body["has_more"] is False


def test_diet_live(live):
    SV, fake = live
    fake.get_diet_records_by_time = lambda s, e: {"diet_records": [{"time": 1}]}
    body = get(live, "/api/diet?days=3").get_json()
    assert body["source"] == "live" and body["items"] == [{"time": 1}]


def test_fitness_live_passthrough_with_limit(live):
    SV, fake = live
    fake.fitness = {"stress": [{"time": i, "value": "{}"} for i in range(5)]}
    body = get(live, "/api/fitness/stress?pages=1&limit=2").get_json()
    assert body["source"] == "live" and body["total"] == 5 and len(body["items"]) == 2


def test_fitness_full_history_flag(live):
    SV, fake = live
    seen = {}
    fake.get_fitness_data_all = lambda key, s, e, max_pages=0, full_history=False: (
        seen.update(start=s, pages=max_pages, full=full_history)
        or {"data_list": [], "has_more": False})
    get(live, "/api/fitness/steps?start=0")
    assert seen["full"] is True and seen["pages"] == 400


def test_fitness_short_window_default(live):
    SV, fake = live
    seen = {}
    fake.get_fitness_data_all = lambda key, s, e, max_pages=0, full_history=False: (
        seen.update(end=e, start=s) or {"data_list": [], "has_more": False})
    get(live, "/api/fitness/steps")
    assert seen["end"] - seen["start"] == 86400000


def test_latest_live_fetches_missing_keys(live):
    SV, fake = live
    fake.get_fitness_data_by_time = lambda k, s, e, reverse=True, next_key=None: {
        "result": {"data_list": [{"key": k, "time": 5, "sid": "w",
                                  "value": json.dumps({k: 7})}]}}
    body = get(live, "/api/latest?keys=steps,spo2").get_json()
    assert body["source"] == "db" or body["source"] == "mixed" or body["source"] == "db"
    assert body["items"]["steps"]["parsed"]["steps"] == 7


def test_latest_live_empty_and_error(live):
    SV, fake = live

    def partial(k, s, e, reverse=True, next_key=None):
        if k == "steps":
            return {"result": {"data_list": []}}
        raise RuntimeError("upstream 500")

    fake.get_fitness_data_by_time = partial
    body = get(live, "/api/latest?keys=steps,spo2").get_json()
    assert body["items"]["steps"] is None
    assert "upstream 500" in body["items"]["spo2"]["err"]


def test_relatives_with_target(live):
    SV, fake = live
    seen = {}
    original = fake.call

    def spy(sub, params, retries=0, decrypt=True):
        seen.update(sub=sub, payload=json.loads(params.get("data") or "{}"))
        return original(sub, params, retries=retries, decrypt=decrypt)

    fake.call = spy
    get(live, "/api/relatives/weight?target=42")
    assert seen["sub"] == "relatives/get_weight"
    assert seen["payload"]["target"] == "42"


def test_db_family_seconds_param(tmp_path, monkeypatch):
    """?seconds=1 时 start/end 按秒解释，要乘 1000。"""
    from mihealth_api import server as SV
    from mihealth_api.store import Store
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "m.db"
    st = Store(str(db))
    st.upsert("fitness", "steps", [{"time": 1000, "sid": "w",
                                    "value": json.dumps({"steps": 5})}])
    st.close()
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MIH_DB", str(db))
    SV = importlib.reload(SV)
    SV.client = FakeClient()
    SV._cache.clear()
    body = get((SV, None), "/api/db/fitness?key=steps&start=1000&end=2000&seconds=1"
               ).get_json()
    assert body["count"] == 1
    empty = get((SV, None), "/api/db/fitness?key=steps&start=1&end=2&seconds=1"
                ).get_json()
    assert empty["count"] == 0


def test_sync_job_error_branches(live, monkeypatch):
    SV, _ = live

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("cannot create")

    monkeypatch.setattr("mihealth_api.sync.Syncer", Boom)
    assert post(live, "/api/sync").get_json()["ok"] is True
    for _ in range(100):
        if not SV._sync_job["running"]:
            break
        time.sleep(0.02)
    assert SV._sync_job["result"]["ok"] is False
    assert "cannot create" in SV._sync_job["result"]["err"]


def test_sync_job_auth_exit(live, monkeypatch):
    SV, _ = live

    class ExitSyncer:
        def __init__(self, *a, **k):
            self.db = type("D", (), {"close": lambda s: None})()

        def incremental(self, hours=72):
            raise SystemExit(2)

    monkeypatch.setattr("mihealth_api.sync.Syncer", ExitSyncer)
    post(live, "/api/sync")
    for _ in range(100):
        if not SV._sync_job["running"]:
            break
        time.sleep(0.02)
    assert "auth/credentials" in SV._sync_job["result"]["err"]


def test_captcha_proxies_image(live, monkeypatch):
    SV, _ = live

    class ImgResp:
        content = b"\xff\xd8JPEGDATA"
        headers = {"Content-Type": "image/png"}

    class Sess:
        def get(self, url, timeout=None):
            return ImgResp()

    SV._login_state.update(session=Sess(), captcha_url="/passport/captcha")
    r = get(live, "/api/login/captcha")
    assert r.status_code == 200 and r.data.startswith(b"\xff\xd8")
    assert r.headers["Content-Type"].startswith("image/png")


def test_2fa_finish_requires_service_token(live, monkeypatch):
    SV, _ = live
    SV._login_state.update(session=object(), **{"2fa_context": "C", "2fa_flag": 4})
    monkeypatch.setattr("mihealth_api.login.finish_2fa",
                        lambda *a, **k: {"ssecurity": "S", "cuser_id": "C"})
    r = post(live, "/api/login", json={"code": "1"})
    assert r.status_code == 400 and "未取得 serviceToken" in r.get_json()["err"]


def test_2fa_finish_error(live, monkeypatch):
    from mihealth_api.login import LoginError
    SV, _ = live
    SV._login_state.update(session=object(), **{"2fa_context": "C", "2fa_flag": 4})
    monkeypatch.setattr("mihealth_api.login.finish_2fa",
                        lambda *a, **k: (_ for _ in ()).throw(LoginError("码错了", code=1)))
    r = post(live, "/api/login", json={"code": "1"})
    body = r.get_json()
    assert r.status_code == 400 and body["need_2fa"] is True and body["code"] == 1


def test_2fa_trigger_failure(live, monkeypatch):
    from mihealth_api.login import LoginError, Need2FA
    SV, _ = live
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())
    monkeypatch.setattr("mihealth_api.login.login_with_password",
                        lambda *a, **k: (_ for _ in ()).throw(Need2FA("https://n")))
    monkeypatch.setattr("mihealth_api.login.start_2fa",
                        lambda *a, **k: (_ for _ in ()).throw(LoginError("短信通道不可用")))
    r = post(live, "/api/login", json={"username": "u", "password": "p"})
    assert r.status_code == 400 and "触发二次验证失败" in r.get_json()["err"]


def test_password_login_generic_failure(live, monkeypatch):
    from mihealth_api.login import LoginError
    SV, _ = live
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())
    monkeypatch.setattr("mihealth_api.login.login_with_password",
                        lambda *a, **k: (_ for _ in ()).throw(LoginError("账号不存在", code=1)))
    r = post(live, "/api/login", json={"username": "u", "password": "p"})
    assert r.status_code == 400 and r.get_json()["err"] == "账号不存在"
    assert SV._login_state["session"] is None


def test_password_login_without_token(live, monkeypatch):
    SV, _ = live
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())
    monkeypatch.setattr("mihealth_api.login.login_with_password",
                        lambda *a, **k: {"ssecurity": "S", "cuser_id": "C"})
    r = post(live, "/api/login", json={"username": "u", "password": "p"})
    assert r.status_code == 400 and "未取得 serviceToken" in r.get_json()["err"]


def test_raw_without_data_param(live):
    SV, fake = live
    seen = {}
    original = fake.call

    def spy(sub, params, retries=0, decrypt=True):
        seen["params"] = params
        return original(sub, params, retries=retries, decrypt=decrypt)

    fake.call = spy
    get(live, "/api/raw/data/whatever")
    assert seen["params"] == {}
