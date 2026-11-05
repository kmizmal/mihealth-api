# -*- coding: utf-8 -*-
"""adb 侧逻辑：设备选择、二进制安全拉取、WAL 边车、失败路径。

真机不可能在 CI 里，所以 `subprocess.run` 全换成可编排的假实现。
"""
import base64
import sqlite3
import subprocess

import pytest

from mihealth_api import credentials as CR

MAGIC = CR.SQLITE_MAGIC


class FakeRun:
    """按命令行前缀匹配预置结果，其余返回失败。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, cmd, capture_output=True, timeout=None):
        self.calls.append(cmd)
        for matcher, result in self.routes:
            if matcher(cmd):
                out, rc = result
                return subprocess.CompletedProcess(cmd, rc, out, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")


def cookie_bytes(tmp_path, rows=None):
    """生成一个真的 SQLite Cookies 库并返回字节。"""
    p = tmp_path / "src.db"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT)")
    db.executemany("INSERT INTO cookies VALUES(?,?,?)", rows or [
        ("sts-hlth.io.mi.com", "serviceToken", "T"),
        ("sts-hlth.io.mi.com", "cUserId", "U"),
        (".wear.mi.com.internal.yrn.net", "ssecurity", "S"),
    ])
    db.commit()
    db.close()
    return p.read_bytes()


# ------------------------------------------------------------ 设备选择
def test_pick_serial_prefers_explicit():
    assert CR.pick_serial("adb", "emulator-5554") == "emulator-5554"


def test_pick_serial_reads_device_list(monkeypatch):
    monkeypatch.setattr(CR, "_run",
                        lambda *a, **k: ("List of devices attached\n"
                                         "127.0.0.1:5555\tdevice\n", None))
    assert CR.pick_serial("adb") == "127.0.0.1:5555"


def test_pick_serial_reports_no_device(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda *a, **k: ("List of devices attached\n", None))
    with pytest.raises(RuntimeError, match="no adb device"):
        CR.pick_serial("adb")


def test_pick_serial_reports_adb_failure(monkeypatch):
    monkeypatch.setattr(CR, "_run", lambda *a, **k: (None, "adb: not found"))
    with pytest.raises(RuntimeError, match="adb failed"):
        CR.pick_serial("adb")


# ------------------------------------------------------------ 拉取
def test_pull_bytes_uses_base64_and_avoids_pty(monkeypatch, tmp_path):
    raw = cookie_bytes(tmp_path)
    encoded = base64.b64encode(raw) + b"\n"
    fake = FakeRun([(lambda cmd: "base64" in " ".join(cmd), (encoded, 0))])
    monkeypatch.setattr(CR, "subprocess", type("S", (), {"run": staticmethod(fake)}))
    out = CR.pull_bytes("adb", "dev", CR.COOKIE_DB)
    assert out == raw
    # 绝不能用 exec-out cat（pty 会改写 \n）
    assert not any("cat" in " ".join(c) for c in fake.calls)


def test_pull_bytes_falls_back_to_pull(monkeypatch, tmp_path):
    raw = cookie_bytes(tmp_path)
    local_file = tmp_path / "pulled"

    def fake_run(cmd, capture_output=True, timeout=None):
        cmd = list(cmd)
        if "base64" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, b"", b"")
        if "cp" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if cmd[0] == "adb" and "pull" in cmd:
            local_file.write_bytes(raw)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(CR, "subprocess", type("S", (), {"run": staticmethod(fake_run)}))
    monkeypatch.setattr(CR, "tempfile",
                        type("T", (), {"mktemp": staticmethod(lambda prefix="": str(local_file))}))
    out = CR.pull_bytes("adb", "dev", CR.COOKIE_DB)
    assert out == raw


def test_pull_bytes_returns_none_when_all_paths_fail(monkeypatch):
    fake = FakeRun([])          # 全部 rc=1
    monkeypatch.setattr(CR, "subprocess", type("S", (), {"run": staticmethod(fake)}))
    assert CR.pull_bytes("adb", "dev", CR.COOKIE_DB) is None


def test_pull_bytes_rejects_non_sqlite_payload(monkeypatch):
    fake = FakeRun([(lambda cmd: "base64" in " ".join(cmd),
                     (base64.b64encode(b"garbage"), 0))])
    monkeypatch.setattr(CR, "subprocess", type("S", (), {"run": staticmethod(fake)}))
    assert CR.pull_bytes("adb", "dev", CR.COOKIE_DB) is None


# ------------------------------------------------------------ 完整流程
def test_fetch_from_device_end_to_end(monkeypatch, tmp_path):
    raw = cookie_bytes(tmp_path)
    encoded = base64.b64encode(raw) + b"\n"

    def fake_run(cmd, capture_output=True, timeout=None):
        joined = " ".join(cmd)
        if "devices" in joined:
            return subprocess.CompletedProcess(cmd, 0,
                                               b"List of devices attached\ndev1\tdevice\n", b"")
        if "base64" in joined and "-wal" not in joined and "-shm" not in joined:
            return subprocess.CompletedProcess(cmd, 0, encoded, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    monkeypatch.setattr(CR, "subprocess", type("S", (), {"run": staticmethod(fake_run)}))
    creds = CR.fetch_from_device()
    assert creds["service_token"] == "T" and creds["cuser_id"] == "U"
    assert creds["ssecurity"] == "S"


def test_fetch_from_device_requires_root(monkeypatch):
    monkeypatch.setattr(CR, "subprocess",
                        type("S", (), {"run": staticmethod(
                            lambda cmd, capture_output=True, timeout=None:
                            subprocess.CompletedProcess(cmd, 1, b"", b""))}))
    monkeypatch.setattr(CR, "pick_serial", lambda adb, serial=None: "dev")
    with pytest.raises(RuntimeError, match="device needs root"):
        CR.fetch_from_device(adb="adb")


def test_fetch_from_device_pulls_wal_sidecars(monkeypatch, tmp_path):
    raw = cookie_bytes(tmp_path)
    seen = []

    def fake_pull(adb, serial, remote):
        seen.append(remote)
        return raw if remote == CR.COOKIE_DB else None

    monkeypatch.setattr(CR, "pick_serial", lambda adb, serial=None: "dev")
    monkeypatch.setattr(CR, "pull_bytes", fake_pull)
    CR.fetch_from_device(adb="adb")
    assert seen == [CR.COOKIE_DB, CR.COOKIE_DB + "-wal", CR.COOKIE_DB + "-shm"]


# ------------------------------------------------------------ 其它
def test_main_uses_adb_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(CR, "fetch_from_device",
                        lambda serial=None, adb=None: {
                            "service_token": "TOKEN", "cuser_id": "U",
                            "ssecurity": "S", "user_id": "1", "pass_token": None})
    cfg = tmp_path / "c.json"
    assert CR.main(["--config", str(cfg), "--serial", "dev1"]) == 0
    assert "updated" in capsys.readouterr().out


def test_main_print_only_shows_all_fields(monkeypatch, capsys):
    monkeypatch.setattr(CR, "fetch_from_device",
                        lambda serial=None, adb=None: {
                            "service_token": "TOKEN", "cuser_id": "U",
                            "ssecurity": "S", "user_id": "1", "pass_token": "PT"})
    assert CR.main(["--print"]) == 0
    out = capsys.readouterr().out
    for k in ("service_token", "cuser_id", "ssecurity", "user_id", "pass_token"):
        assert k in out


def test_normalize_sqlite_keeps_short_payload():
    short = MAGIC + b"\x00" * 10
    assert CR.normalize_sqlite(short) == short
