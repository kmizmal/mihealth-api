# -*- coding: utf-8 -*-
"""本地网关：全部 39 条路由用 Flask test_client 打一遍，云端调用一律打桩。"""
import importlib
import json
import time

import pytest

from mihealth_api.store import Store


class FakeClient:
    """冒充 MiHealthClient：只有被显式编排过的方法才返回值。"""

    def __init__(self):
        self.calls = []
        self.base = "https://hlth.io.mi.com"
        self.cuser = "CUSER"
        self.service_token = "tok-0123456789abcdef"
        self.security = "SEC"
        self.on_auth_error = None
        self.fitness = {}
        self.pages = {}
        self.sport = []
        self.raise_auth = False

    def reload_credentials(self, security=None, service_token=None, cuser=None):
        if security:
            self.security = security
        if service_token:
            self.service_token = service_token
        if cuser:
            self.cuser = cuser

    def call(self, sub, params, retries=0, decrypt=True):
        self.calls.append((sub, json.loads(params.get("data") or "{}")))
        if self.raise_auth:
            from mihealth_api.client import AuthError
            raise AuthError("auth failed (HTTP 401)")
        queue = self.pages.get(sub)
        if queue:
            return {"code": 0, "result": queue.pop(0)}
        return {"code": 0, "result": {"data_list": [], "has_more": False}}

    def get_fitness_data_all(self, key, start, end, max_pages=400, full_history=False):
        if self.raise_auth:
            from mihealth_api.client import AuthError
            raise AuthError("auth failed (HTTP 401)")
        return {"data_list": self.fitness.get(key, []), "has_more": False}

    def get_fitness_data_by_time(self, key, start, end, reverse=True, next_key=None):
        return {"result": {"data_list": self.fitness.get(key, [])}}

    def get_fitness_data_by_watermark(self, key, wm, limit=None):
        return {"code": 0, "result": {"data_list": [], "watermark": wm}}

    def get_sport_records_all(self, start, end, max_pages=500, full_history=False):
        return {"sport_records": self.sport, "has_more": False}

    def get_sport_category_list(self):
        return {"code": 0, "result": {"categories": []}}

    def get_max_watermark(self, wtype):
        return 4242

    def sync_by_watermark(self, sub, field, wm, max_pages=50):
        return [{"time": 1, "_t": 1}], wm + 1, False

    def get_aggregated_all(self, _nil, start, end, tag, limit):
        return {"data_list": []}

    def get_diet_records_by_time(self, start_s, end_s):
        return {"diet_records": []}

    def get_sport_operational_data(self, sid, key, start, end):
        return {"code": 0, "result": {"sid": sid}}

    def get_routes_list(self, start, limit, reverse=True, origin_type=0):
        return {"code": 0, "result": {"data_list": []}}

    def get_routes_info(self, ids):
        return {"code": 0, "result": {"items": ids}}

    def gen_fds_download_url(self, sid, items):
        return {"code": 0, "result": {"sid": sid, "items": items}}


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """在干净的 cwd 里重新导入 server，并把 client 换成假的。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "ssecurity": "SEC", "service_token": "tok-0123456789abcdef",
        "cuser_id": "CUSER", "user_id": "1", "pass_token": "PT",
        "sid": "miothealth"}), encoding="utf-8")
    db = tmp_path / "mihealth.db"

    # 注意：库里 `time` 存的是**秒**，查询参数是毫秒（store.rows 内部 ×1000）。
    st = Store(str(db))
    now = int(time.time())
    st.upsert("fitness", "steps", [
        {"time": now - 3600, "sid": "watch", "value": json.dumps({"steps": 100})},
        {"time": now - 1800, "sid": "watch", "value": json.dumps({"steps": 200})},
    ])
    st.upsert("fitness", "heart_rate", [
        {"time": now - 60, "sid": "watch", "value": json.dumps({"bpm": 66})}])
    st.upsert("fitness", "sleep", [
        {"time": now - 7200, "sid": "watch",
         "value": json.dumps({"sleep_deep_duration": 30})}])
    st.upsert("sport", "outdoor_running", [
        {"time": now - 86400, "sid": "watch",
         "value": json.dumps({"distance": 5.2, "duration": 1800})}])
    st.upsert("goal", "goal", [{"time": now - 7200, "sid": "g",
                                "value": json.dumps({
                                    "goal_items": [{"field": 1,
                                                    "achieved_value": 8000,
                                                    "target_value": 10000}],
                                    "stand_goal": {"achieved_value": 10,
                                                   "target_value": 12}})}])
    st.close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(cfg))
    monkeypatch.setenv("MIH_DB", str(db))
    from mihealth_api import server as SV
    SV = importlib.reload(SV)
    fake = FakeClient()
    SV.client = fake
    SV._cache.clear()
    SV._store = Store(str(db))
    fake._cfg = cfg
    yield SV, fake
    SV._store.close()


def get(srv, path, **kw):
    SV, _ = srv
    return SV.app.test_client().get(path, **kw)


def post(srv, path, **kw):
    SV, _ = srv
    return SV.app.test_client().post(path, **kw)


# ------------------------------------------------------------ 静态 / 状态
def test_index_serves_dashboard(srv):
    r = get(srv, "/")
    assert r.status_code == 200 and b"<" in r.data


def test_vendor_serves_chart_bundle(srv):
    import os
    name = "chart.umd.min.js"
    from mihealth_api import paths
    if not os.path.exists(os.path.join(paths.static_dir(), name)):
        pytest.skip("bundle missing")
    r = get(srv, "/vendor/" + name)
    assert r.status_code == 200


def test_health_reports_db_mode(srv):
    body = get(srv, "/api/health").get_json()
    assert body["ok"] is True and body["mode"] == "db"
    assert body["auth"] == "ok"
    assert body["token_prefix"].endswith("...")
    assert body["sseccurity_set"] is True
    assert body["rows"] > 0 and "cursors" in body


def test_health_reports_expired_on_auth_error(srv):
    SV, fake = srv
    fake.raise_auth = True
    body = get(srv, "/api/health").get_json()
    assert body["auth"] == "expired" and "auth_error" in body


def test_sync_status_shape(srv):
    body = get(srv, "/api/sync/status").get_json()
    assert body["ok"] is True and body["running"] is False
    assert body["rows"] > 0


def test_sync_post_starts_job(srv, monkeypatch):
    SV, fake = srv
    ran = {}

    class FakeSyncer:
        def __init__(self, *a, **k):
            self.db = type("D", (), {"close": lambda self_: None})()

        def incremental(self, hours=72):
            ran["hours"] = hours
            return 7

    monkeypatch.setattr("mihealth_api.sync.Syncer", FakeSyncer)
    body = post(srv, "/api/sync").get_json()
    assert body["ok"] is True
    for _ in range(100):
        if not SV._sync_job["running"]:
            break
        time.sleep(0.02)
    assert SV._sync_job["result"] == {"ok": True, "rows": 7}


def test_sync_post_conflict_when_running(srv):
    SV, _ = srv
    SV._sync_job["running"] = True
    try:
        r = post(srv, "/api/sync")
        assert r.status_code == 409 and r.get_json()["err"] == "already running"
    finally:
        SV._sync_job["running"] = False


# ------------------------------------------------------------ 登录 / 会话
def test_login_status(srv):
    body = get(srv, "/api/login/status").get_json()
    assert body["ok"] is True and body["can_refresh"] is True
    assert body["password_stored"] is False
    assert len(body["modes"]) == 3


def test_login_refresh_without_pass_token(srv):
    SV, _ = srv
    cfg = json.loads(SV.CFG_PATH and open(SV.CFG_PATH, encoding="utf-8").read())
    cfg.pop("pass_token")
    open(SV.CFG_PATH, "w", encoding="utf-8").write(json.dumps(cfg))
    r = post(srv, "/api/login/refresh")
    assert r.status_code == 400 and "pass_token" in r.get_json()["err"]


def test_login_refresh_success_applies_session(srv, monkeypatch):
    SV, fake = srv
    monkeypatch.setattr("mihealth_api.login.refresh_session",
                        lambda cfg, path=None, proxies=None: {
                            "ssecurity": "NEWSEC", "service_token": "NEWTOK1234567890",
                            "cuser_id": "CUSER"})
    monkeypatch.setattr("mihealth_api.login.save_session", lambda p, s, keep=None: None)
    body = post(srv, "/api/login/refresh").get_json()
    assert body["ok"] is True and body["mode"] == "passToken"
    assert fake.service_token == "NEWTOK1234567890"


def test_login_refresh_reports_login_error(srv, monkeypatch):
    from mihealth_api.login import LoginError
    monkeypatch.setattr("mihealth_api.login.refresh_session",
                        lambda *a, **k: (_ for _ in ()).throw(
                            LoginError("bad", code=70016)))
    r = post(srv, "/api/login/refresh")
    assert r.status_code == 400 and r.get_json()["code"] == 70016


def test_login_password_requires_credentials(srv):
    r = post(srv, "/api/login", json={})
    assert r.status_code == 400 and "username" in r.get_json()["err"]


def test_login_password_success(srv, monkeypatch):
    SV, fake = srv
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())
    monkeypatch.setattr("mihealth_api.login.login_with_password",
                        lambda u, p, captcha=None, proxies=None, session=None: {
                            "ssecurity": "S", "service_token": "TOKEN-ABCDEFGHIJ",
                            "cuser_id": "C", "pass_token": "PT2"})
    monkeypatch.setattr("mihealth_api.login.save_session", lambda p, s, keep=None: None)
    body = post(srv, "/api/login", json={"username": "u", "password": "p"}).get_json()
    assert body["ok"] is True and body["mode"] == "password"
    assert body["pass_token_saved"] is True
    assert fake.service_token == "TOKEN-ABCDEFGHIJ"


def test_login_password_captcha_flow(srv, monkeypatch):
    from mihealth_api.login import LoginError
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())

    def bad(*a, **k):
        raise LoginError("需要验证码", code=700, captcha_url="/passport/captcha")

    monkeypatch.setattr("mihealth_api.login.login_with_password", bad)
    r = post(srv, "/api/login", json={"username": "u", "password": "p"})
    body = r.get_json()
    assert r.status_code == 400 and body["captcha_url"] == "/api/login/captcha"
    assert body["code"] == 700


def test_login_password_2fa_flow(srv, monkeypatch):
    from mihealth_api.login import Need2FA
    monkeypatch.setattr("mihealth_api.login.new_login_session", lambda proxies=None: object())
    monkeypatch.setattr("mihealth_api.login.login_with_password",
                        lambda *a, **k: (_ for _ in ()).throw(Need2FA("https://notify")))
    monkeypatch.setattr("mihealth_api.login.start_2fa",
                        lambda session, url: {"context": "CTX", "flag": 4,
                                              "method": "Phone", "hint": "尾号 8888"})
    body = post(srv, "/api/login", json={"username": "u", "password": "p"}).get_json()
    assert body["need_2fa"] is True and body["channel"] == "短信"
    assert "8888" in body["err"]


def test_login_password_2fa_finish(srv, monkeypatch):
    SV, fake = srv
    SV._login_state.update(session=object(), **{"2fa_context": "CTX", "2fa_flag": 4})
    monkeypatch.setattr("mihealth_api.login.finish_2fa",
                        lambda session, code, ctx, flag=4: {
                            "ssecurity": "S", "service_token": "T-2FA",
                            "cuser_id": "C", "pass_token": "PT"})
    monkeypatch.setattr("mihealth_api.login.save_session", lambda p, s, keep=None: None)
    body = post(srv, "/api/login", json={"code": "123456"}).get_json()
    assert body["ok"] is True and body["mode"] == "password+2fa"
    assert SV._login_state["2fa_context"] is None


def test_captcha_without_pending(srv):
    r = get(srv, "/api/login/captcha")
    assert r.status_code == 404


def test_import_device_writes_config(srv, monkeypatch):
    SV, fake = srv
    creds = {"ssecurity": "DS", "service_token": "DT", "cuser_id": "DC",
             "pass_token": "DPT"}
    monkeypatch.setattr("mihealth_api.credentials.fetch_from_device",
                        lambda serial=None: creds)
    monkeypatch.setattr("mihealth_api.credentials.write_config",
                        lambda c, p=None: None)
    body = post(srv, "/api/login/import_device").get_json()
    assert body["ok"] is True and body["mode"] == "device"
    assert fake.service_token == "DT"


def test_import_device_failure(srv, monkeypatch):
    monkeypatch.setattr("mihealth_api.credentials.fetch_from_device",
                        lambda serial=None: (_ for _ in ()).throw(RuntimeError("adb gone")))
    r = post(srv, "/api/login/import_device")
    assert r.status_code == 400 and "adb gone" in r.get_json()["err"]


# ------------------------------------------------------------ 看板数据
def test_overview_reads_store(srv):
    body = get(srv, "/api/overview").get_json()
    assert body["ok"] is True and body["source"] == "db"
    assert body["steps_today"] >= 0
    assert body["hr_last"] == 66
    assert set(body["counts"]) == {"steps", "hr", "sleep", "spo2"}


def test_series_uses_store(srv):
    body = get(srv, "/api/series/steps?hours=24").get_json()
    assert body["ok"] is True and body["source"] == "db"
    assert len(body["items"]) == 2


def test_series_is_cached(srv):
    SV, _ = srv
    get(srv, "/api/series/steps?hours=24")
    assert any(k.startswith("s:steps") for k in SV._cache)


def test_daily_goals_from_store(srv):
    """goal 行 → 按 type 映射成人可读字段（这个接口没有 source 字段）。"""
    body = get(srv, "/api/daily_goals?days=14").get_json()
    assert body["ok"] is True and body["count"] == 1
    item = body["items"][0]
    assert item["goals"]["steps"] == {"achieved": 8000, "target": 10000}
    assert item["goals"]["stand_hours"] == {"achieved": 10, "target": 12}


def test_sport_records_from_store(srv):
    body = get(srv, "/api/sport_records?days=30").get_json()
    assert body["ok"] is True and body["source"] == "db"
    assert body["items"][0]["_key"] == "outdoor_running"


def test_sport_records_live_fallback(srv):
    SV, fake = srv
    fake.sport = [{"time": 1, "key": "walking", "value": json.dumps({"distance": 1.0})}]
    body = get(srv, "/api/sport_records?days=1&type=nonexistent").get_json()
    assert body["ok"] is True
    if body["source"] == "live":
        assert body["items"][0]["_key"] == "walking"


def test_keys_route(srv):
    body = get(srv, "/api/keys").get_json()
    assert "steps" in body["keys"] and "fitness" in body["families"]


def test_families_route(srv):
    body = get(srv, "/api/families").get_json()
    assert body["ok"] is True and "families" in body


def test_db_family_all(srv):
    body = get(srv, "/api/db/fitness?limit=5").get_json()
    assert body["ok"] is True and body["family"] == "fitness"
    assert body["count"] == len(body["items"])


def test_db_family_filters_by_key(srv):
    body = get(srv, "/api/db/fitness?key=steps").get_json()
    assert all(it["_key"] == "steps" for it in body["items"])


def test_db_family_meta_only(srv):
    body = get(srv, "/api/db/fitness?fields=meta&limit=3").get_json()
    for it in body["items"]:
        assert set(it) <= {"_t", "_sid", "_key"}


def test_db_family_dedup_off(srv):
    assert get(srv, "/api/db/fitness?key=steps&dedup=0").get_json()["dedup"] is False


def test_db_family_order_desc_limit(srv):
    items = get(srv, "/api/db/fitness?key=steps&order=desc&limit=1").get_json()["items"]
    assert len(items) == 1


def test_db_keys_route(srv):
    body = get(srv, "/api/db/fitness/keys").get_json()
    assert body["ok"] is True
    assert {k["key"] for k in body["keys"]} >= {"steps", "heart_rate"}


def test_agg_requires_field(srv):
    r = get(srv, "/api/agg/fitness/steps")
    assert r.status_code == 400 and "field" in r.get_json()["err"]


def test_agg_sum(srv):
    body = get(srv, "/api/agg/fitness/steps?field=steps&agg=sum&bucket=86400").get_json()
    assert body["ok"] is True and body["agg"] == "sum"
    assert body["points"][0]["v"] == 300.0


def test_agg_max_and_count(srv):
    assert get(srv, "/api/agg/fitness/steps?field=steps&agg=max&bucket=86400"
               ).get_json()["points"][0]["v"] == 200
    assert get(srv, "/api/agg/fitness/steps?field=steps&agg=count&bucket=86400"
               ).get_json()["points"][0]["v"] == 2


def test_export_csv(srv):
    r = get(srv, "/api/export.csv?family=fitness&key=steps")
    assert r.status_code == 200
    assert b"family,key" in r.data


def test_sport_types_from_store(srv):
    body = get(srv, "/api/sport_types").get_json()
    assert body["ok"] is True
    assert any(t["key"] == "outdoor_running" for t in body["types"])


def test_sport_detail_requires_params(srv):
    r = get(srv, "/api/sport_detail")
    assert r.status_code == 400


def test_sport_detail_ok(srv):
    body = get(srv, "/api/sport_detail?sid=w&key=outdoor_running&start=1&end=2").get_json()
    assert body["result"]["sid"] == "w"


def test_routes_list(srv):
    """纯透传接口：原样返回云端 JSON（不加 ok 包装）。"""
    body = get(srv, "/api/routes?limit=5").get_json()
    assert body["code"] == 0
    assert body["result"]["data_list"] == []


def test_routes_info_requires_ids(srv):
    assert get(srv, "/api/routes/info").status_code == 400


def test_routes_info_ok(srv):
    body = get(srv, "/api/routes/info?ids=1,2").get_json()
    assert body["result"]["items"] == ["1", "2"]


def test_fds_url(srv):
    body = get(srv, "/api/fds_url?sid=w&suffix=csv,route&time=5").get_json()
    assert [i["suffix"] for i in body["result"]["items"]] == ["csv", "route"]


def test_diet_from_store(srv):
    body = get(srv, "/api/diet?days=30").get_json()
    assert body["ok"] is True and body["source"] == "db"


def test_watermark_feed_seeds_cursor(srv):
    body = get(srv, "/api/watermark_feed/fitness").get_json()
    assert body["ok"] is True and body["cursor"] == 4242 and body["items"] == []


def test_watermark_feed_unknown_family(srv):
    r = get(srv, "/api/watermark_feed/nope")
    assert r.status_code == 400


def test_watermark_feed_pulls(srv):
    body = get(srv, "/api/watermark_feed/medical?wm=10").get_json()
    assert body["ok"] is True and body["cursor"] == 11


def test_fitness_live_passthrough(srv):
    SV, fake = srv
    fake.fitness = {"stress": [{"time": 1, "value": "{}"}]}
    body = get(srv, "/api/fitness/stress?pages=1").get_json()
    assert body["ok"] is True and body["source"] in ("db", "live")


def test_medical_passthrough(srv):
    body = get(srv, "/api/medical").get_json()
    assert body["ok"] is True and "items" in body


def test_project_passthrough(srv):
    assert get(srv, "/api/project").get_json()["ok"] is True


def test_stat_passthrough(srv):
    assert get(srv, "/api/stat/steps").get_json()["ok"] is True


def test_aggregated_passthrough(srv):
    assert get(srv, "/api/aggregated").get_json()["ok"] is True


def test_sport_summary_passthrough(srv):
    assert get(srv, "/api/sport_summary").get_json()["ok"] is True


def test_max_watermark(srv):
    assert get(srv, "/api/max_watermark?type=1").get_json()["result"]["data_list"] == []


def test_watermark_fitness(srv):
    assert get(srv, "/api/watermark/steps?wm=3").get_json()["result"]["watermark"] == 3


def test_latest_reads_store(srv):
    body = get(srv, "/api/latest?keys=steps,heart_rate").get_json()
    assert body["ok"] is True
    assert body["items"]["steps"]["parsed"]["steps"] == 200


def test_sport_categories(srv):
    assert get(srv, "/api/sport_categories").get_json()["code"] == 0


def test_relatives(srv):
    assert get(srv, "/api/relatives/steps").get_json()["ok"] is True


def test_raw_passthrough(srv):
    SV, fake = srv
    get(srv, "/api/raw/data/get_max_watermark?data=%7B%7D")
    assert any(c[0] == "data/get_max_watermark" for c in fake.calls)


def test_no_db_mode_returns_404(tmp_path, monkeypatch):
    """没有数据库时，读库的接口必须明确报 404 而不是 500。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    from mihealth_api import server as SV
    SV = importlib.reload(SV)
    SV.client = FakeClient()
    c = SV.app.test_client()
    for path in ("/api/families", "/api/db/fitness", "/api/db/fitness/keys",
                 "/api/agg/fitness/steps?field=steps", "/api/export.csv"):
        assert c.get(path).status_code == 404, path
    assert SV.app.test_client().get("/api/health").get_json()["mode"] == "live"


def test_main_defaults(monkeypatch):
    from mihealth_api import server as SV
    monkeypatch.setenv("MIH_PORT", "9999")
    monkeypatch.setenv("MIH_HOST", "0.0.0.0")
    seen = {}
    monkeypatch.setattr(SV.app, "run", lambda **kw: seen.update(kw))
    SV.main([])
    assert seen["port"] == 9999 and seen["host"] == "0.0.0.0"
    assert seen["debug"] is False


def test_main_flags(monkeypatch):
    from mihealth_api import server as SV
    seen = {}
    monkeypatch.setattr(SV.app, "run", lambda **kw: seen.update(kw))
    SV.main(["--port", "18080", "--host", "127.0.0.1", "--debug"])
    assert seen == {"host": "127.0.0.1", "port": 18080, "debug": True}
