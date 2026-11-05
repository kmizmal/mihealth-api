# -*- coding: utf-8 -*-
"""密码登录 / 2FA / STS 回调：全部用假的 `_req`，不联网。"""
import json

import pytest

from mihealth_api import login as L


class Resp:
    def __init__(self, text="", status_code=200, headers=None):
        self.text, self.status_code = text, status_code
        self.headers = headers or {}


class FakeSession:
    """只实现 login.py 用到的那点 requests.Session 接口。"""

    def __init__(self):
        self.headers = {}
        self.cookies = _Cookies()
        self.proxies = {}
        self.__dict__["_mih_device_id"] = "DEV"
        self.__dict__["_mih_user_id"] = "42"

    def get(self, url, **kw):
        raise AssertionError("should not be hit directly")

    def post(self, url, **kw):
        raise AssertionError("should not be hit directly")


class _Cookies:
    def __init__(self):
        self._d = {}

    def set(self, k, v, domain=None):
        self._d[(domain, k)] = v

    def get(self, k, domain=None):
        return self._d.get((domain, k)) or self._d.get((None, k))

    def __repr__(self):
        return f"<Cookies {self._d}>"


def test_service_login_returns_json(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: Resp(
        '&&&START&&&{"_sign":"SIG","qs":"?sid=x","callback":"cb"}'))
    out = L.service_login(FakeSession())
    assert out["_sign"] == "SIG"


def test_service_login_requires_sign(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: Resp('{"code":1}'))
    with pytest.raises(L.LoginError, match="_sign"):
        L.service_login(FakeSession())


def test_login_with_password_happy_path(monkeypatch):
    calls = []

    def fake_req(session, method, url, **kw):
        calls.append((method, url, kw))
        if "serviceLoginAuth2" in url:
            return Resp('&&&START&&&{"ssecurity":"SECURITY-LONG","cUserId":"CU",'
                        '"userId":42,"passToken":"PT","location":"https://sts/cb"}')
        if "serviceLogin" in url:
            return Resp('{"_sign":"SIG","qs":"?sid=x","callback":"cb"}')
        return Resp("", 200)

    monkeypatch.setattr(L, "_req", fake_req)
    s = FakeSession()
    s.cookies.set("serviceToken", "TOK", domain="sts-hlth.io.mi.com")
    out = L.login_with_password("u", "p", session=s)
    assert out["ssecurity"] == "SECURITY-LONG" and out["pass_token"] == "PT"
    assert out["service_token"] == "TOK"
    # 密码只发哈希，绝不发明文
    fields = calls[1][2]["params"]
    assert fields["hash"] == L.hash_password("p") and "password" not in fields
    assert fields["user"] == "u" and fields["_sign"] == "SIG"


def test_login_with_password_sends_captcha_field(monkeypatch):
    sent = {}

    def fake_req(session, method, url, **kw):
        if "serviceLoginAuth2" in url:
            sent.update(kw.get("params") or {})
            return Resp('{"ssecurity":"SECURITY-LONG","location":"https://sts"}')
        return Resp('{"_sign":"SIG"}')

    monkeypatch.setattr(L, "_req", fake_req)
    L.login_with_password("u", "p", captcha="1234", session=FakeSession())
    assert sent["captCode"] == "1234"          # 是 captCode，不是 icode


def test_login_with_password_captcha_required(monkeypatch):
    def fake_req(session, method, url, **kw):
        if "serviceLoginAuth2" in url:
            return Resp('{"captchaUrl":"/passport/captcha","code":700}')
        return Resp('{"_sign":"SIG"}')

    monkeypatch.setattr(L, "_req", fake_req)
    with pytest.raises(L.LoginError) as e:
        L.login_with_password("u", "p", session=FakeSession())
    assert e.value.captcha_url.endswith("/passport/captcha")
    assert e.value.code == 700


def test_login_with_password_needs_2fa(monkeypatch):
    def fake_req(session, method, url, **kw):
        if "serviceLoginAuth2" in url:
            return Resp('{"notificationUrl":"https://notify/2fa","code":0}')
        return Resp('{"_sign":"SIG"}')

    monkeypatch.setattr(L, "_req", fake_req)
    with pytest.raises(L.Need2FA) as e:
        L.login_with_password("u", "p", session=FakeSession())
    assert e.value.notification_url == "https://notify/2fa"


def test_login_with_password_plain_failure(monkeypatch):
    def fake_req(session, method, url, **kw):
        if "serviceLoginAuth2" in url:
            return Resp('{"code":70016,"desc":"登录验证失败"}')
        return Resp('{"_sign":"SIG"}')

    monkeypatch.setattr(L, "_req", fake_req)
    with pytest.raises(L.LoginError, match="登录验证失败"):
        L.login_with_password("u", "p", session=FakeSession())


def test_identity_headers_carry_device_and_user():
    s = FakeSession()
    h = L._identity_headers(s)
    assert "MiHome" in h["User-Agent"] and "DeviceId/DEV" in h["User-Agent"]
    assert "UserId/42" in h["User-Agent"]
    assert L._identity_cookies(s) == {"deviceId": "DEV"}


def test_id_call_reports_empty_response(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: Resp(""))
    with pytest.raises(L.LoginError, match="空响应"):
        L._id_call(FakeSession(), "get", "https://x", "authStart")


def test_start_2fa_phone_channel(monkeypatch):
    seen = []

    def fake_req(session, method, url, **kw):
        seen.append((method, url, kw.get("params") or kw.get("data")))
        if "/identity/list" in url:
            return Resp('&&&START&&&{"flag":4,"mask":"138****8888"}')
        return Resp('{"code":0}')

    monkeypatch.setattr(L, "_req", fake_req)
    notify = "https://account.xiaomi.com/pass/2fa?context=CTX&sid=miothealth&userId=42"
    out = L.start_2fa(FakeSession(), notify)
    assert out["context"] == "CTX" and out["flag"] == 4
    assert out["method"] == "Phone" and out["hint"] == "138****8888"
    assert any("verifyPhone" in u for _, u, _ in seen)
    assert any("sendPhoneTicket" in u for _, u, _ in seen)


def test_start_2fa_email_channel(monkeypatch):
    seen = []

    def fake_req(session, method, url, **kw):
        seen.append(url)
        if "/identity/list" in url:
            return Resp('{"flag":8,"email":"a@b.c"}')
        return Resp('{"code":0}')

    monkeypatch.setattr(L, "_req", fake_req)
    out = L.start_2fa(FakeSession(), L.ACCOUNT_HOST + "/pass/2fa?context=C&sid=s")
    assert out["method"] == "Email" and out["hint"] == "a@b.c"
    assert not any("sendPhoneTicket" in u for u in seen)


def test_start_2fa_accepts_relative_notification_url(monkeypatch):
    seen = []

    def fake_req(session, method, url, **kw):
        seen.append(url)
        if "/identity/list" in url:
            return Resp('{"flag":4}')
        return Resp('{"code":0}')

    monkeypatch.setattr(L, "_req", fake_req)
    L.start_2fa(FakeSession(), "/pass/2fa?context=C&sid=s")
    assert seen[0].startswith("https://account.xiaomi.com/")


def test_finish_2fa_happy_path(monkeypatch):
    calls = []

    def fake_req(session, method, url, **kw):
        calls.append(url)
        if "verifyPhone" in url:
            return Resp('&&&START&&&{"code":0,"location":"https://jump"}')
        if "serviceLogin" in url:
            return Resp('{"code":0,"ssecurity":"SECURITY-2-LONG","cUserId":"CU",'
                        '"userId":9,"passToken":"PT2","location":"https://sts/cb"}')
        return Resp("", 200)

    monkeypatch.setattr(L, "_req", fake_req)
    s = FakeSession()
    s.cookies.set("serviceToken", "TOK2", domain="sts-hlth.io.mi.com")
    out = L.finish_2fa(s, " 123456 ", "CTX", flag=4)
    assert out["ssecurity"] == "SECURITY-2-LONG" and out["pass_token"] == "PT2"
    assert out["service_token"] == "TOK2"
    # 先访问 location，再重做 serviceLogin
    assert any("jump" in u for u in calls)


def test_finish_2fa_rejects_bad_code(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: Resp('{"code":700,"desc":"验证码错误"}'))
    with pytest.raises(L.LoginError, match="验证码校验未通过"):
        L.finish_2fa(FakeSession(), "000000", "CTX")


def test_finish_2fa_requires_location(monkeypatch):
    monkeypatch.setattr(L, "_req", lambda s, m, u, **kw: Resp('{"code":0}'))
    with pytest.raises(L.LoginError, match="未拿到跳转地址"):
        L.finish_2fa(FakeSession(), "123456", "CTX")


def test_finish_2fa_requires_ssecurity_after_relogin(monkeypatch):
    def fake_req(session, method, url, **kw):
        if "verifyPhone" in url:
            return Resp('{"code":0,"location":"https://jump"}')
        return Resp('{"code":0}')            # 没有 ssecurity

    monkeypatch.setattr(L, "_req", fake_req)
    with pytest.raises(L.LoginError, match="二次验证后登录未完成"):
        L.finish_2fa(FakeSession(), "123456", "CTX")


def test_finish_2fa_relative_location(monkeypatch):
    calls = []

    def fake_req(session, method, url, **kw):
        calls.append(url)
        if "verifyPhone" in url:
            return Resp('{"code":0,"location":"/pass/jump"}')
        if "serviceLogin" in url:
            return Resp('{"code":0,"ssecurity":"SECURITY-LONG","location":"https://sts"}')
        return Resp("", 200)

    monkeypatch.setattr(L, "_req", fake_req)
    L.finish_2fa(FakeSession(), "1", "C")
    assert any(u == L.ACCOUNT_HOST + "/pass/jump" for u in calls)


def test_device_id_is_16_uppercase_alnum():
    d = L._device_id()
    assert len(d) == 16 and d.isupper() and d.isalnum()
