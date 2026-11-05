# -*- coding: utf-8 -*-
"""HTTP 层与各业务封装：不联网，`http` 换成假的 session。"""
import json
import time

import pytest

from mihealth_api import client as C
from mihealth_api.client import ApiError, AuthError, MiHealthClient


class Resp:
    def __init__(self, text="", status_code=200, headers=None):
        self.text, self.status_code = text, status_code
        self.headers = headers or {}


def enc(security, text, nonce):
    """按服务端视角把明文字节流加密（响应体是加密的）。"""
    rc4 = C.RC4(C.b64d(C.session_key(security, nonce)))
    return C.b64e(rc4.crypt(text.encode()))


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.proxies = {}

    def get(self, url, params=None, cookies=None, timeout=None):
        self.requests.append(("GET", url, params, cookies))
        return self._next()

    def post(self, url, data=None, cookies=None, timeout=None):
        self.requests.append(("POST", url, data, cookies))
        return self._next()

    def _next(self):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def mk(replies, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("max_retries", 0)
    c = MiHealthClient("c2Vjc2Vj", "tok", "cuser", **kw)
    c.http = FakeHTTP(replies)
    return c


def encrypted_reply(c, payload):
    """构造一个能被 client 解开的响应（nonce 从请求里反读）。"""
    nonce_holder = {}

    class Wrap(FakeHTTP):
        def get(self, url, params=None, cookies=None, timeout=None):
            nonce_holder["n"] = (params or {}).get("_nonce")
            return Resp(enc("c2Vjc2Vj", json.dumps(payload), nonce_holder["n"]))

    c.http = Wrap([])
    return c


def test_call_decrypts_response():
    c = mk([])
    nonce_holder = {}

    class Wrap(FakeHTTP):
        def get(self, url, params=None, cookies=None, timeout=None):
            nonce_holder["n"] = params["_nonce"]
            self.requests.append(("GET", url, params, cookies))
            return Resp(enc("c2Vjc2Vj", '{"code":0,"result":{"n":1}}', params["_nonce"]))

    c.http = Wrap([])
    assert c.call("data/x", {"data": "{}"}) == {"code": 0, "result": {"n": 1}}


def test_call_prefix_override():
    c = mk([Resp('{"code":0}')])
    c.call("service/gen_download_url", {"data": "{}"}, prefix="healthapp/")
    assert c.http.requests[0][1].startswith("https://hlth.io.mi.com/healthapp/")


def test_call_empty_prefix():
    c = mk([Resp('{"code":0}')])
    c.call("x", {"data": "{}"}, prefix="")
    assert c.http.requests[0][1] == "https://hlth.io.mi.com/x"


def test_call_prefix_without_slash_is_normalised():
    """`healthapp` 与 `healthapp/` 必须都拼出合法的绝对 URL。"""
    for pre in ("healthapp", "healthapp/", "/healthapp"):
        c = mk([Resp('{"code":0}')])
        c.call("svc/x", {"data": "{}"}, prefix=pre)
        assert c.http.requests[0][1] == "https://hlth.io.mi.com/healthapp/svc/x"


def test_default_prefix_is_app_v1():
    c = mk([Resp('{"code":0}')])
    c.call("data/x", {"data": "{}"})
    assert c.http.requests[0][1] == "https://hlth.io.mi.com/app/v1/data/x"


def test_call_decrypt_false_returns_raw_text():
    c = mk([Resp("plain-body")])
    assert c.call("x", {"data": "{}"}, decrypt=False) == "plain-body"


def test_call_undecodable_raises_api_error():
    c = mk([Resp("not-json-at-all")])
    with pytest.raises(ApiError, match="undecodable"):
        c.call("x", {"data": "{}"})


def test_call_plaintext_json_fallback():
    """response 解密失败但本体是明文 JSON（服务端错误页）时应能识别。"""
    c = mk([Resp('{"code":0,"result":{"plain":true}}')])
    assert c.call("x", {"data": "{}"}) == {"code": 0, "result": {"plain": True}}


def test_call_retries_connection_errors():
    c = mk([ConnectionError("boom"), Resp("plain")], max_retries=1)
    assert c.call("x", {"data": "{}"}, decrypt=False) == "plain"
    assert len(c.http.requests) == 2


def test_call_gives_up_after_retries():
    c = mk([ConnectionError("boom"), ConnectionError("boom2")], max_retries=1)
    with pytest.raises(ApiError, match="request failed after 2 tries"):
        c.call("x", {"data": "{}"})


def test_call_code2_triggers_auth_flow():
    c = mk([Resp('{"code":2,"result":{"m":"auth error"}}')])
    with pytest.raises(AuthError, match="code=2"):
        c.call("x", {"data": "{}"})


def test_throttle_sleeps_between_calls(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    c = mk([Resp("a"), Resp("b")], min_interval=0.5)
    c.call("x", {"data": "{}"}, decrypt=False)
    c._last = time.monotonic()
    c.call("x", {"data": "{}"}, decrypt=False)
    assert slept and slept[0] > 0


def test_proxies_applied_to_session():
    c = MiHealthClient("s", "t", "c", proxies={"http": "http://p:1"})
    assert c.http.proxies["http"] == "http://p:1"


def test_get_aggregated_all_filters_window_and_dedups(monkeypatch):
    c = mk([])
    pages = [
        {"data_list": [{"time": 100}, {"time": 200}], "next_key": "1", "has_more": True},
        {"data_list": [{"time": 200}, {"time": 900}], "next_key": None, "has_more": False},
    ]
    monkeypatch.setattr(c, "get_aggregated_by_time",
                        lambda *a, **k: {"result": pages.pop(0)})
    out = c.get_aggregated_all(None, 50_000, 500_000)
    assert [i["time"] for i in out["data_list"]] == [100, 200]


def test_get_aggregated_all_stops_when_window_covered(monkeypatch):
    c = mk([])
    monkeypatch.setattr(c, "get_aggregated_by_time",
                        lambda *a, **k: {"result": {"data_list": [{"time": 10}],
                                                    "next_key": "1", "has_more": True}})
    out = c.get_aggregated_all(None, 100_000, 200_000)
    assert out["data_list"] == []


def test_get_latest_fitness_data_shape():
    c = mk([])
    sent = {}

    def fake(sub, params, **kw):
        sent.update(sub=sub, payload=json.loads(params["data"]))
        return {"code": 0}

    c.call = fake
    c.get_latest_fitness_data([("steps", 1)])
    assert sent["sub"] == "data/get_latest_fitness_data"
    assert sent["payload"] == {"dataList": [{"key": "steps", "limit": 1, "time": None}]}


def test_get_max_watermark_reads_result():
    c = mk([])
    c.call = lambda *a, **k: {"result": {"watermark": 77}}
    assert c.get_max_watermark(1) == 77
    c.call = lambda *a, **k: {}
    assert c.get_max_watermark() == 0


def test_get_diet_records_filters_and_pages(monkeypatch):
    c = mk([])
    pages = [{"result": {"diet_records": [{"time": 50}, {"time": 150}],
                         "next_key": "1", "has_more": True}},
             {"result": {"diet_records": [{"time": 150}], "has_more": False}}]
    monkeypatch.setattr(c, "call", lambda *a, **k: pages.pop(0))
    out = c.get_diet_records_by_time(100, 200)
    # 分页重叠会产生重复行（写入 SQLite 时按 (family,key,sid,ts) 主键合并，
    # 所以这里只保证窗口过滤正确，不承诺去重）
    assert {i["time"] for i in out["diet_records"]} == {150}
    assert out["diet_records"][0]["_t"] == 150


def test_get_diet_summary():
    c = mk([])
    sent = {}
    c.call = lambda sub, params, **kw: (sent.update(sub=sub,
                                                    payload=json.loads(params["data"]))
                                       or {"code": 0})
    c.get_diet_summary(1, 2)
    assert sent["sub"] == "statistics/batch_get_diet_summary"
    assert sent["payload"] == {"start_time": 1, "end_time": 2}


def test_sport_operational_data_payload():
    c = mk([])
    sent = {}
    c.call = lambda sub, params, **kw: (sent.update(sub=sub,
                                                    payload=json.loads(params["data"]))
                                       or {"code": 0})
    c.get_sport_operational_data("sid1", "running", 10, 20)
    assert sent["sub"] == "operate/get_sport_operational_data"
    assert sent["payload"]["sid"] == "sid1"
    assert sent["payload"]["locale"] == "zh_cn"


def test_get_routes_list_pages(monkeypatch):
    c = mk([])
    pages = [{"result": {"route_list": [{"id": 1}], "next_key": "1", "has_more": True}},
             {"result": {"route_list": [{"id": 2}], "has_more": False}}]
    monkeypatch.setattr(c, "call", lambda *a, **k: pages.pop(0))
    out = c.get_routes_list()
    assert [r["id"] for r in out["route_list"]] == [1, 2]


def test_get_routes_info_payload():
    c = mk([])
    sent = {}
    c.call = lambda sub, params, **kw: (sent.update(sub=sub,
                                                    payload=json.loads(params["data"]))
                                       or {"code": 0})
    c.get_routes_info(["a", "b"])
    assert sent["sub"] == "running_route/get_user_routes_info"
    assert sent["payload"] == {"route_ids": ["a", "b"]}


def test_gen_fds_download_url_uses_healthapp_prefix():
    c = mk([])
    seen = {}

    def fake(sub, params, **kw):
        seen.update(sub=sub, prefix=kw.get("prefix"))
        return {"code": 0}

    c.call = fake
    c.gen_fds_download_url("sid", [{"suffix": "csv", "timeStamp": 1}])
    assert seen == {"sub": "service/gen_download_url", "prefix": "healthapp/"}


def test_healthapp_prefix_produces_valid_url():
    """回归：`healthapp/` 少个斜杠会拼成 https://hosthealthapp/...（实测 bug）。"""
    c = mk([Resp("plain")])
    c.call("service/gen_download_url", {"data": "{}"}, prefix="healthapp/",
           decrypt=False)
    assert c.http.requests[0][1] == (
        "https://hlth.io.mi.com/healthapp/service/gen_download_url")


def test_fitness_by_watermark_payload():
    c = mk([])
    sent = {}
    c.call = lambda sub, params, **kw: (sent.update(sub=sub,
                                                    payload=json.loads(params["data"]))
                                       or {"code": 0})
    c.get_fitness_data_by_watermark("phone", 5, limit=10)
    assert sent["sub"] == "data/get_fitness_data_by_watermark"
    assert sent["payload"] == {"phoneId": "phone", "waterMark": 5, "limit": 10}


def test_fitness_data_all_clamps_start_to_last_day(monkeypatch):
    c = mk([])
    seen = {}

    def fake(key, start, end, reverse=True, next_key=None):
        seen.update(start=start, end=end)
        return {"result": {"data_list": [], "has_more": False}}

    monkeypatch.setattr(c, "get_fitness_data_by_time", fake)
    end = 10 * 86400000
    c.get_fitness_data_all("steps", 0, end)
    assert seen["start"] == end - 86400000      # 最近起点让游标存活
    assert seen["end"] == end


def test_sport_records_all_clamps_start(monkeypatch):
    c = mk([])
    seen = {}

    def fake(start, end, nk=None, limit=50, reverse=True, category=None):
        seen.update(start=start, end=end, reverse=reverse, limit=limit)
        return {"result": {"sport_records": [], "has_more": False}}

    monkeypatch.setattr(c, "get_sport_records_by_time", fake)
    c.get_sport_records_all(0, 10 * 86400000)
    assert seen["start"] == 9 * 86400000 and seen["reverse"] is True


def test_client_main_smoke(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "s", "service_token": "t",
                               "cuser_id": "c"}), encoding="utf-8")
    c = mk([])
    monkeypatch.setattr(C, "client_from_config", lambda *a, **k: c)
    monkeypatch.setattr(c, "get_fitness_data_all",
                        lambda key, s, e: {"data_list": [{"value": '{"steps":1}'}]})
    assert C.main(["--config", str(cfg), "--keys", "steps"]) == 0
    out = capsys.readouterr().out
    assert "items=1" in out and "pycryptodome=" in out


def test_client_main_reports_failures(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "s", "service_token": "t",
                               "cuser_id": "c"}), encoding="utf-8")
    c = mk([])

    def bad(*a, **k):
        raise ApiError("nope")

    monkeypatch.setattr(C, "client_from_config", lambda *a, **k: c)
    monkeypatch.setattr(c, "get_fitness_data_all", bad)
    assert C.main(["--config", str(cfg), "--keys", "steps,spo2"]) == 1
    assert "ERR nope" in capsys.readouterr().out
