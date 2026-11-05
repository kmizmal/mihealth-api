# -*- coding: utf-8 -*-
"""路径解析：显式参数 > 环境变量 > 当前工作目录；static 始终来自包内。"""
import os

from mihealth_api import paths


def test_defaults_are_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MIH_CONFIG", raising=False)
    monkeypatch.delenv("MIH_DB", raising=False)
    monkeypatch.delenv("MIH_EXPORT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.config_path() == os.path.join(str(tmp_path), "config.json")
    assert paths.db_path() == os.path.join(str(tmp_path), "mihealth.db")
    assert paths.export_dir() == os.path.join(str(tmp_path), "export")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MIH_CONFIG", "/x/c.json")
    monkeypatch.setenv("MIH_DB", "/x/d.db")
    monkeypatch.setenv("MIH_EXPORT_DIR", "/x/out")
    assert paths.config_path() == "/x/c.json"
    assert paths.db_path() == "/x/d.db"
    assert paths.export_dir() == "/x/out"


def test_explicit_beats_env(monkeypatch):
    monkeypatch.setenv("MIH_DB", "/x/d.db")
    assert paths.db_path("/y/real.db") == "/y/real.db"


def test_static_dir_is_inside_package():
    assert paths.static_dir() == paths.STATIC_DIR
    assert os.path.isdir(paths.static_dir())
    assert os.path.basename(paths.static_dir()) == "static"
    assert os.path.exists(os.path.join(paths.static_dir(), "index.html"))
