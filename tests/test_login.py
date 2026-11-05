# -*- coding: utf-8 -*-
"""登录：密码哈希口径、响应前缀剥离、passToken 换票、session 落盘、CLI 返回码。"""
import json

import pytest

from mihealth_api import login as L


def test_hash_password_is_single_md5_upper():
    import hashlib
    assert L.hash_password("secret") == hashlib.md5(b"secret").hexdigest().upper()
    assert L.hash_password("secret") == L.md5_upper("secret")


def test_hash_password_is_not_double_md5():
    import hashlib
    double = hashlib.md5(hashlib.md5(b"secret").hexdigest().upper().encode()).hexdigest().upper()
    assert L.hash_password("secret") != double


def test_hash_variants_contains_three_flavours():
    v = L.hash_password_variants("pw")
    assert set(v) == {"md5_upper", "md5_md5_upper_upper", "md5_lower"}
    assert v["md5_lower"] == v["md5_upper"].lower()


def test_json_body_strips_miagic_prefix():
    assert L._json_body('&&&START&&&{"code":0,"_sign":"s"}') == {"code": 0, "_sign": "s"}


def test_json_body_handles_garbage():
    assert L._json_body("") == {}
    assert L._json_body("no json here") == {}
    assert L._json_body("{broken") == {}


def test_sdk_version_constant():
    assert L.SDK_VERSION == "accountsdk-18.8.15"


def test_session_sets_required_cookies():
    s = L._session(device_id="DEV1")
    for dom in ("mi.com", "xiaomi.com"):
        assert s.cookies.get("sdkVersion", domain=dom) == L.SDK_VERSION
        assert s.cookies.get("deviceId", domain=dom) == "DEV1"
    assert "APPV/10.5.201" in s.headers["User-Agent"]


def test_session_omits_device_id_when_absent():
    s = L._session()
    assert s.cookies.get("deviceId", domain="mi.com") is None


class _Resp:
    def __init__(self, text="", status_code=200, headers=None):
        self.text, self.status_code = text, status_code
        self.headers = headers or {}


def test_exchange_pass_token_success(monkeypatch):
    calls = []

    def fake_req(session, method, url, **kw):
        calls.append((method, url, kw.get("allow_redirects")))
        if "serviceLogin" in url:
            return _Resp('&&&START&&&{"code":0,"ssecurity":"NEWSEC","cUserId":"CU",'
                         '"userId":42,"location":"https://sts.example/cb"}')
        return _Resp("", 302, {"Set-Cookie": "serviceToken=NEWTOK; Path=/; Domain=sts-hlth.io.mi.com"})

    monkeypatch.setattr(L, "_req", fake_req)
    out = L.exchange_pass_token("PT", "CU", "42")
    assert out["ssecurity"] == "NEWSEC"
    assert out["service_token"] == "NEWTOK"
    assert out["cuser_id"] == "CU" and out["user_id"] == "42"
    assert out["sid"] == L.SID_HEALTH
    assert isinstance(out["ts"], int)
    assert calls[1][2] is False          # STS 那一步不能跟随跳转


def test_exchange_pass_token_reports_70016(monkeypatch):
    monkeypatch.setattr(L, "_req",
                        lambda s, m, u, **kw: _Resp('{"code":70016,"desc":"登录验证失败"}'))
    with pytest.raises(L.LoginError) as e:
        L.exchange_pass_token("PT", "CU", "")
    assert e.value.code == 70016
    assert "70016" in str(e.value)


def test_exchange_pass_token_without_location_raises(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: _Resp('{"code":0}'))
    with pytest.raises(L.LoginError, match="passToken"):
        L.exchange_pass_token("PT", "CU", "1")


def test_exchange_pass_token_cookie_sets_are_strings(monkeypatch):
    """cookies=ck 里不能出现 None（requests 会炸）。"""
    captured = {}

    def fake_req(session, method, url, **kw):
        captured.update(kw.get("cookies") or {})
        return _Resp('{"code":0,"ssecurity":"S","location":"https://x"}')

    monkeypatch.setattr(L, "_req", fake_req)
    monkeypatch.setattr(L, "_session", lambda proxies=None: __import__("requests").Session())
    with pytest.raises(L.LoginError):
        L.exchange_pass_token("PT", "CU", None)
    assert captured["userId"] == ""


def test_save_session_writes_expected_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"region": "abroad", "extra": 1}), encoding="utf-8")
    L.save_session(str(p), {"ssecurity": "S", "service_token": "T", "cuser_id": "C",
                            "user_id": "9", "pass_token": "PT", "sid": "miothealth"})
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["ssecurity"] == "S" and got["service_token"] == "T"
    assert got["pass_token"] == "PT" and got["user_id"] == "9"
    assert got["region"] == "abroad"          # 其它字段保留
    assert got["session_at"] > 0


def test_save_session_keep_pins_old_values(tmp_path):
    """keep 里的字段保留旧值（例如不想让一次登录改掉 sid）。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"sid": "old-sid", "region": "abroad"}), encoding="utf-8")
    L.save_session(str(p), {"ssecurity": "S", "service_token": "T", "cuser_id": "C",
                            "sid": "new-sid"}, keep=("sid",))
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["sid"] == "old-sid" and got["region"] == "abroad"
    assert got["service_token"] == "T"


def test_save_session_without_keep_overwrites_sid(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"sid": "old-sid"}), encoding="utf-8")
    L.save_session(str(p), {"ssecurity": "S", "service_token": "T", "cuser_id": "C",
                            "sid": "new-sid"})
    assert json.loads(p.read_text(encoding="utf-8"))["sid"] == "new-sid"


def test_refresh_session_calls_exchange(monkeypatch):
    seen = {}

    def fake_exchange(pt, cu, uid, sid=L.SID_HEALTH, proxies=None):
        seen.update(pt=pt, cu=cu, uid=uid, sid=sid)
        return {"ssecurity": "S2", "service_token": "T2", "cuser_id": cu,
                "user_id": uid, "sid": sid, "ts": 1}

    monkeypatch.setattr(L, "exchange_pass_token", fake_exchange)
    out = L.refresh_session({"pass_token": "PT", "cuser_id": "CU", "user_id": "9",
                             "sid": "miothealth"}, None)
    assert seen == {"pt": "PT", "cu": "CU", "uid": "9", "sid": "miothealth"}
    assert out["service_token"] == "T2" and out["ssecurity"] == "S2"


def test_main_refresh_failure_returns_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(L, "refresh_session",
                        lambda cfg, path, proxies=None: (_ for _ in ()).throw(
                            L.LoginError("bad", code=70016)))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"pass_token": "PT", "cuser_id": "CU", "user_id": "1"}),
                   encoding="utf-8")
    assert L.main(["--refresh", "--config", str(cfg)]) == 1
    assert "70016" in capsys.readouterr().out


def test_main_without_creds_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "refresh_session",
                        lambda *a, **k: (_ for _ in ()).throw(L.LoginError("no creds")))
    assert L.main(["--refresh", "--config", str(tmp_path / "absent.json")]) == 1


def test_need2fa_carries_notification_url():
    e = L.Need2FA("https://notify")
    assert isinstance(e, L.LoginError)
    assert e.notification_url == "https://notify"
    assert "二次验证" in str(e)
