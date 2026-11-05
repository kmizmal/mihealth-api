# -*- coding: utf-8 -*-
"""共享 fixture：所有用例都在离线状态下跑，真实网络调用一律打桩。"""
import json
import sqlite3

import pytest


@pytest.fixture
def cfg_file(tmp_path):
    """一份最小可用的 config.json（值都是假的，不会拿去发请求）。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "ssecurity": "c2VjcmV0LXNzZWN1cml0eQ==",
        "service_token": "tok-0123456789",
        "cuser_id": "cuser-abc",
        "user_id": "1287473516",
        "pass_token": "V1:fake-pass-token",
        "session_at": 1700000000,
        "sid": "miothealth",
    }, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def store(tmp_path):
    from mihealth_api.store import Store
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def cookie_db(tmp_path):
    """合成一个 Chromium 风格的 cookies 表，用来测离线解析。"""
    p = tmp_path / "Cookies"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT, "
               "encrypted_value BLOB DEFAULT NULL)")
    rows = [
        ("sts-hlth.io.mi.com", "serviceToken", "SVCTOK", None),
        ("sts-hlth.io.mi.com", "cUserId", "CUSER1", None),
        ("sts-hlth.io.mi.com", "userId", "1287473516", None),
        (".wear.mi.com.internal.yrn.net", "ssecurity", "SSEC==", None),
        (".hlth.io.mi.com", "serviceToken", "miothealth", None),
        (".account.xiaomi.com", "passToken", "PT-1", None),
        (".mi.com", "locale", "zh_CN", None),
    ]
    db.executemany("INSERT INTO cookies(host_key,name,value,encrypted_value) "
                   "VALUES(?,?,?,?)", rows)
    db.commit()
    db.close()
    return p
