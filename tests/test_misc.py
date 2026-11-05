# -*- coding: utf-8 -*-
"""零散分支：包入口、加密库回退、store 的 sid/key 组合、导出元数据失败等。"""
import json

import pytest

import mihealth_api
from mihealth_api import client as C
from mihealth_api import export as EX
from mihealth_api import login as L
from mihealth_api import sync as SY
from mihealth_api.store import Store


def test_package_main_delegates_to_server(monkeypatch):
    seen = {}
    from mihealth_api import server as SV
    monkeypatch.setattr(SV, "main", lambda argv=None: seen.update(argv=argv) or 0)
    assert mihealth_api.main(["--port", "1"]) == 0
    assert seen["argv"] == ["--port", "1"]


def test_package_exports_public_names():
    for name in ("MiHealthClient", "Store", "main"):
        assert hasattr(mihealth_api, name), name


def test_rc4_pure_python_matches_accelerated(monkeypatch):
    """没有 pycryptodome 时走纯 Python PRGA，输出必须完全一致。"""
    key = b"0123456789abcdef" * 2      # rc4Key 必须 32 字节
    fast = C.RC4(key).crypt(b"hello world")
    monkeypatch.setattr(C, "_ARC4", None)          # 模拟导入失败
    assert C.RC4(key).crypt(b"hello world") == fast


def test_rc4_drop_length_respected(monkeypatch):
    key = b"k" * 32
    monkeypatch.setattr(C, "_ARC4", None)
    full = C.RC4(key, drop=0).crypt(b"x" * 8)
    dropped = C.RC4(key, drop=1024).crypt(b"x" * 8)
    assert full != dropped


def test_store_aggregate_with_sid_filter(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    st.upsert("fitness", "steps", [
        {"time": 100, "sid": "watch", "value": json.dumps({"steps": 10})},
        {"time": 110, "sid": "phone", "value": json.dumps({"steps": 99})}])
    assert st.aggregate("fitness", "steps", "steps", bucket_s=86400,
                        agg="sum", sid="watch")[0]["v"] == 10
    st.close()


def test_store_sources_filtered_by_key(tmp_path):
    st = Store(str(tmp_path / "b.db"))
    st.upsert("fitness", "steps", [{"time": 1, "sid": "watch", "value": "{}"}])
    st.upsert("fitness", "calories", [{"time": 2, "sid": "phone", "value": "{}"}])
    assert [s["sid"] for s in st.sources("fitness", "steps")] == ["watch"]
    assert {s["sid"] for s in st.sources()} == {"watch", "phone"}
    st.close()


def test_export_metadata_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(EX.time, "sleep", lambda *_: None)
    e = EX.Exporter(type("Cl", (), {
        "call": lambda self, sub, params: (_ for _ in ()).throw(RuntimeError("meta down")),
        "get_fitness_data_all": lambda *a, **k: {"data_list": []},
        "get_sport_records_all": lambda *a, **k: {"sport_records": []},
    })(), str(tmp_path / "o"))
    manifest = e.run(now=1)
    assert "meta_sport_categories" not in manifest["datasets"]


def test_export_stat_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(EX.time, "sleep", lambda *_: None)

    def call(self, sub, params):
        if "get_stat_data_by_time" in sub:
            raise RuntimeError("stat down")
        return {"code": 0, "result": {"data_list": [], "has_more": False}}

    e = EX.Exporter(type("Cl", (), {
        "call": call,
        "get_fitness_data_all": lambda *a, **k: {"data_list": []},
        "get_sport_records_all": lambda *a, **k: {"sport_records": []},
    })(), str(tmp_path / "o"))
    e.run(now=1)
    assert "[skip] stat" in capsys.readouterr().out


def test_login_req_uses_given_cookies_and_headers(monkeypatch):
    """`_req` 只是把 kw 透传给 requests，别吞掉 cookies/allow_redirects。"""
    seen = {}

    class S:
        def get(self, url, **kw):
            seen.update(method="GET", url=url, **kw)
            return "resp"

        def post(self, url, **kw):
            seen.update(method="POST", url=url, **kw)
            return "resp"

    assert L._req(S(), "get", "https://x", params={"a": 1}, cookies={"b": 2}) == "resp"
    assert seen["params"] == {"a": 1} and seen["cookies"] == {"b": 2}
    L._req(S(), "post", "https://y", data={"k": "v"}, timeout=5)
    assert seen["method"] == "POST" and seen["timeout"] == 5


def test_login_session_sets_cookies_and_proxies(monkeypatch):
    class Cookies:
        def __init__(self):
            self.d = {}

        def set(self, k, v, domain=None):
            self.d[(domain, k)] = v

    class S:
        def __init__(self):
            self.headers = {}
            self.cookies = Cookies()
            self.proxies = {}

    monkeypatch.setattr(L.requests, "Session", S)
    s = L._session(proxies={"https": "http://p:1"}, device_id="DEV")
    assert s.headers.get("User-Agent")
    assert s.proxies["https"] == "http://p:1"
    assert ("mi.com", "sdkVersion") in s.cookies.d
    assert ("mi.com", "deviceId") in s.cookies.d


def test_sync_sport_topup_failure_logged(syncer_factory, capsys):
    s = syncer_factory()
    s.fake.get_sport_records_all = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("sport down"))
    s._window_topup(1)
    assert "sport topup failed" in capsys.readouterr().out
    s.db.close()


def test_sync_goal_topup_failure_logged(syncer_factory, capsys):
    s = syncer_factory()
    s.fake.get_aggregated_all = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("goal down"))
    s._window_topup(1)
    assert "goals topup failed" in capsys.readouterr().out
    s.db.close()


def test_sync_diet_topup_failure_logged(syncer_factory, capsys):
    s = syncer_factory()
    s.fake.get_sport_records_all = lambda *a, **k: {"sport_records": []}
    s.fake.get_aggregated_all = lambda *a, **k: {"data_list": []}

    def boom(*a, **k):
        raise RuntimeError("diet down")

    s._backfill_diet = boom
    s._window_topup(1)
    assert "diet topup failed" in capsys.readouterr().out
    s.db.close()


def test_server_auth_selfheal_delegates(monkeypatch):
    from mihealth_api import server as SV
    seen = {}
    monkeypatch.setattr("mihealth_api.auth.make_refresh",
                        lambda path, on_success=None: (lambda cl, why: seen.update(
                            path=path, hook=on_success is not None) or True))
    assert SV._auth_selfheal(object(), "401") is True
    assert seen["hook"] is True


def test_server_latest_concurrent_fetch(tmp_path, monkeypatch):
    """latest 在库里查不到的键会并发去云端取。"""
    import importlib
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    from mihealth_api import server as SV
    SV = importlib.reload(SV)

    class Cl:
        def get_fitness_data_by_time(self, k, s, e, reverse=True, next_key=None):
            return {"result": {"data_list": [{"key": k, "time": 1, "sid": "w",
                                              "value": json.dumps({k: 1})}]}}

    SV.client = Cl()
    SV._cache.clear()
    body = SV.app.test_client().get("/api/latest?keys=steps,spo2,stress").get_json()
    assert set(body["items"]) == {"steps", "spo2", "stress"}


@pytest.fixture
def syncer_factory(tmp_path, monkeypatch):
    from test_sync import FakeClient
    state = {}

    def make():
        fake = FakeClient()
        fake.fitness_pages = {}
        monkeypatch.setattr(SY, "client_from_config", lambda *a, **k: fake)
        monkeypatch.setattr(SY, "load_config", lambda *a, **k: {})
        s = SY.Syncer(str(tmp_path / f"s{len(state)}.db"), workers=1, min_interval=0)
        s.fake = fake
        state[len(state)] = s
        return s

    return make
