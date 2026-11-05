# -*- coding: utf-8 -*-
"""凭据自愈：passToken 优先，失败才回落 adb；两者都失败返回 False。"""
import pytest

from mihealth_api import auth
from mihealth_api.login import LoginError


class FakeClient:
    def __init__(self):
        self.reloaded = []

    def reload_credentials(self, security=None, service_token=None, cuser=None):
        self.reloaded.append((security, service_token, cuser))


@pytest.fixture
def patched(monkeypatch):
    """把 refresh_session 与 fetch_from_device 都换成可观测的假实现。"""
    state = {"calls": [], "session": None, "login_error": None, "creds": None,
             "device_error": None}

    def fake_refresh(cfg, cfg_path=None, proxies=None):
        state["calls"].append(("passToken", cfg_path))
        if state["login_error"]:
            raise state["login_error"]
        return state["session"]

    def fake_fetch():
        state["calls"].append(("adb",))
        if state["device_error"]:
            raise state["device_error"]
        return state["creds"]

    monkeypatch.setattr("mihealth_api.login.refresh_session", fake_refresh)
    monkeypatch.setattr("mihealth_api.credentials.fetch_from_device", fake_fetch)
    monkeypatch.setattr("mihealth_api.credentials.write_config",
                        lambda creds, path=None: state["calls"].append(("write", path)))
    return state


def _cfg(tmp_path, **extra):
    import json
    p = tmp_path / "config.json"
    cfg = {"pass_token": "PT", "cuser_id": "CU", "user_id": "1",
           "ssecurity": "S", "service_token": "T"}
    cfg.update(extra)
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_refresh_via_config_success(tmp_path, patched):
    patched["session"] = {"ssecurity": "S2", "service_token": "T2", "cuser_id": "CU"}
    c = FakeClient()
    out = auth.refresh_via_config(str(_cfg(tmp_path)), c)
    assert out["service_token"] == "T2"
    assert c.reloaded == [("S2", "T2", "CU")]
    assert patched["calls"][0][0] == "passToken"


def test_refresh_via_config_without_pass_token(tmp_path, patched):
    """没有 passToken 直接返回 None，不发起请求。"""
    c = FakeClient()
    assert auth.refresh_via_config(str(_cfg(tmp_path, pass_token="")), c) is None
    assert patched["calls"] == [] and c.reloaded == []


def test_refresh_via_config_swallows_login_error(tmp_path, patched, capsys):
    patched["login_error"] = LoginError("70016", code=70016)
    c = FakeClient()
    assert auth.refresh_via_config(str(_cfg(tmp_path)), c) is None
    assert c.reloaded == []
    assert "passToken refresh failed" in capsys.readouterr().out


def test_refresh_via_device_success(tmp_path, patched):
    patched["creds"] = {"ssecurity": "DS", "service_token": "DT", "cuser_id": "DC"}
    c = FakeClient()
    out = auth.refresh_via_device(str(_cfg(tmp_path)), c)
    assert out["service_token"] == "DT"
    assert c.reloaded == [("DS", "DT", "DC")]
    assert ("adb",) in patched["calls"]


def test_refresh_via_device_swallows_errors(tmp_path, patched, capsys):
    patched["device_error"] = RuntimeError("adb: no devices")
    assert auth.refresh_via_device(str(_cfg(tmp_path)), FakeClient()) is None
    assert "device import failed" in capsys.readouterr().out


def test_refresh_via_device_empty_creds(tmp_path, patched):
    patched["creds"] = None
    assert auth.refresh_via_device(str(_cfg(tmp_path)), FakeClient()) is None
    assert not [c for c in patched["calls"] if c[0] == "write"]


def test_make_refresh_prefers_passtoken(tmp_path, patched):
    patched["session"] = {"ssecurity": "S2", "service_token": "T2", "cuser_id": "CU"}
    called = []
    hook = auth.make_refresh(str(_cfg(tmp_path)), on_success=lambda: called.append(1))
    assert hook(FakeClient(), why="401") is True
    assert [c[0] for c in patched["calls"]] == ["passToken"]       # 没走 adb
    assert called == [1]


def test_make_refresh_falls_back_to_device(tmp_path, patched):
    patched["login_error"] = LoginError("expired")
    patched["creds"] = {"ssecurity": "DS", "service_token": "DT", "cuser_id": "DC"}
    called = []
    hook = auth.make_refresh(str(_cfg(tmp_path)), on_success=lambda: called.append(1))
    assert hook(FakeClient()) is True
    assert [c[0] for c in patched["calls"]][:2] == ["passToken", "adb"]
    assert called == [1]


def test_make_refresh_device_disabled(tmp_path, patched):
    patched["login_error"] = LoginError("expired")
    hook = auth.make_refresh(str(_cfg(tmp_path)), use_device=False)
    assert hook(FakeClient()) is False
    assert "adb" not in [c[0] for c in patched["calls"]]


def test_make_refresh_both_fail(tmp_path, patched):
    patched["login_error"] = LoginError("expired")
    patched["creds"] = None
    called = []
    hook = auth.make_refresh(str(_cfg(tmp_path)), on_success=lambda: called.append(1))
    assert hook(FakeClient()) is False
    assert called == []


def test_default_config_path_comes_from_env(tmp_path, patched, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(cfg))
    patched["session"] = {"ssecurity": "S", "service_token": "T", "cuser_id": "C"}
    hook = auth.make_refresh()
    assert hook(FakeClient()) is True
    assert patched["calls"][0] == ("passToken", str(cfg))
