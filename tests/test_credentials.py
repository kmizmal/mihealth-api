# -*- coding: utf-8 -*-
"""离线 Cookies 解析 + 写回配置 + SQLite 尺寸规整（不碰 adb）。"""
import json
import sqlite3

import pytest

from mihealth_api import credentials as CR


def test_parse_cookie_db(cookie_db):
    creds = CR.parse_cookie_db(str(cookie_db))
    assert creds["service_token"] == "SVCTOK"
    assert creds["cuser_id"] == "CUSER1"
    assert creds["user_id"] == "1287473516"
    assert creds["ssecurity"] == "SSEC=="
    assert creds["pass_token"] == "PT-1"


def test_hlth_sid_marker_is_not_used_as_token(cookie_db):
    """`.hlth.io.mi.com` 行的 serviceToken=miothealth 只是 sid 标记。"""
    assert CR.parse_cookie_db(str(cookie_db))["service_token"] != "miothealth"


def test_ssecurity_fallback_by_name(tmp_path):
    p = tmp_path / "Cookies"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT)")
    db.executemany("INSERT INTO cookies VALUES(?,?,?)", [
        ("sts-hlth.io.mi.com", "serviceToken", "T"),
        ("sts-hlth.io.mi.com", "cUserId", "U"),
        ("weird.host.example", "ssecurity", "S"),
    ])
    db.commit(); db.close()
    assert CR.parse_cookie_db(str(p))["ssecurity"] == "S"


def test_missing_cookies_report_hosts(tmp_path):
    p = tmp_path / "Cookies"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT)")
    db.execute("INSERT INTO cookies VALUES('.mi.com','locale','zh_CN')")
    db.commit(); db.close()
    with pytest.raises(RuntimeError) as e:
        CR.parse_cookie_db(str(p))
    assert "missing cookies" in str(e.value) and "locale" not in str(e.value)


def test_read_only_open_leaves_file_untouched(cookie_db):
    before = cookie_db.read_bytes()
    CR.read_cookie_rows(str(cookie_db))
    assert cookie_db.read_bytes() == before


def test_creds_from_rows_is_order_independent():
    rows = [(".mi.com", "userId", "7"),
            (".wear.mi.com.internal.yrn.net", "ssecurity", "S"),
            ("sts-hlth.io.mi.com", "cUserId", "U"),
            ("sts-hlth.io.mi.com", "serviceToken", "T")]
    c = CR.creds_from_rows(rows)
    assert (c["service_token"], c["cuser_id"], c["user_id"], c["ssecurity"]) == (
        "T", "U", "7", "S")


def test_write_config_merges_existing(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"region": "abroad", "proxies": {"http": "http://p:8080"},
                             "sid": "miothealth", "min_interval": 0.2}), encoding="utf-8")
    creds = {"service_token": "T", "cuser_id": "U", "ssecurity": "S",
             "user_id": "9", "pass_token": "PT"}
    CR.write_config(creds, str(p))
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["region"] == "abroad" and got["proxies"] == {"http": "http://p:8080"}
    assert got["min_interval"] == 0.2
    assert (got["service_token"], got["cuser_id"], got["ssecurity"]) == ("T", "U", "S")
    assert got["user_id"] == "9" and got["pass_token"] == "PT"
    assert isinstance(got["session_at"], int)


def test_write_config_creates_when_absent(tmp_path):
    p = tmp_path / "new.json"
    CR.write_config({"service_token": "T", "cuser_id": "U", "ssecurity": "S"}, str(p))
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["sid"] == "miothealth" and "user_id" not in got


def test_write_config_keeps_user_id_string(tmp_path):
    p = tmp_path / "c.json"
    CR.write_config({"service_token": "T", "cuser_id": "U", "ssecurity": "S",
                     "user_id": 1287473516}, str(p))
    assert json.loads(p.read_text(encoding="utf-8"))["user_id"] == "1287473516"


def test_normalize_sqlite_trims_declared_size():
    header = bytearray(b"SQLite format 3\x00" + b"\x00" * 16)
    data = bytes(header[:16]) + (4096).to_bytes(2, "big") + bytes(header[18:28]) \
        + (2).to_bytes(4, "big") + b"\x00" * 6 + b"P" * 8192 + b"TRAILING-JUNK"
    out = CR.normalize_sqlite(data)
    assert out.startswith(CR.SQLITE_MAGIC)
    assert not out.endswith(b"TRAILING-JUNK")


def test_normalize_sqlite_strips_leading_pty_noise():
    data = b"some pty noise\r\n" + CR.SQLITE_MAGIC + b"\x00" * 40
    assert CR.normalize_sqlite(data).startswith(CR.SQLITE_MAGIC)


def test_normalize_sqlite_passes_through_garbage():
    assert CR.normalize_sqlite(b"not a database") == b"not a database"


def test_page_size_one_means_65536():
    data = bytearray(CR.SQLITE_MAGIC + b"\x00" * 40)
    data[16:18] = (1).to_bytes(2, "big")
    data[28:32] = (1).to_bytes(4, "big")
    out = CR.normalize_sqlite(bytes(data) + b"X" * 70000)
    assert len(out) == 65536


def test_main_print_only_from_db(cookie_db, capsys):
    assert CR.main(["--db", str(cookie_db), "--print"]) == 0
    out = capsys.readouterr().out
    assert "service_token = SVCTOK" in out and "user_id = 1287473516" in out


def test_main_writes_config(cookie_db, tmp_path, capsys):
    cfg = tmp_path / "written.json"
    assert CR.main(["--db", str(cookie_db), "--config", str(cfg)]) == 0
    got = json.loads(cfg.read_text(encoding="utf-8"))
    assert got["service_token"] == "SVCTOK" and got["pass_token"] == "PT-1"


def test_main_reports_failure(tmp_path, capsys):
    assert CR.main(["--db", str(tmp_path / "nope")]) == 1
    assert "FAILED" in capsys.readouterr().err
