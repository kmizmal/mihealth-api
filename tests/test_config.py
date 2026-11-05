# -*- coding: utf-8 -*-
"""配置解析：显式路径 > MIH_CONFIG > ./config.json；环境变量兜底。"""
import json

from mihealth_api.client import client_from_config, load_config


def test_load_config_from_file(cfg_file):
    cfg = load_config(str(cfg_file))
    assert cfg["ssecurity"] == "c2VjcmV0LXNzZWN1cml0eQ=="
    assert cfg["service_token"] == "tok-0123456789"
    assert cfg["cuser_id"] == "cuser-abc"
    assert cfg["user_id"] == "1287473516"
    assert cfg["pass_token"] == "V1:fake-pass-token"
    assert cfg["sid"] == "miothealth"
    assert cfg["region"] == "cn"
    assert cfg["min_interval"] == 0.0
    assert cfg["proxies"] is None
    assert cfg["config_path"] == str(cfg_file)


def test_missing_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("MIH_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg["ssecurity"] is None and cfg["cuser_id"] is None


def test_broken_json_falls_back_to_empty(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert load_config(str(p))["service_token"] is None


def test_env_vars_fill_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("MIH_SSECURITY", "env-sec")
    monkeypatch.setenv("MIH_SERVICE_TOKEN", "env-tok")
    monkeypatch.setenv("MIH_CUSER_ID", "env-user")
    monkeypatch.setenv("MIH_PASS_TOKEN", "env-pass")
    monkeypatch.setenv("MIH_USER_ID", "42")
    monkeypatch.setenv("MIH_REGION", "abroad")
    cfg = load_config(str(tmp_path / "absent.json"))
    assert (cfg["ssecurity"], cfg["service_token"], cfg["cuser_id"]) == (
        "env-sec", "env-tok", "env-user")
    assert cfg["pass_token"] == "env-pass" and cfg["user_id"] == "42"
    assert cfg["region"] == "abroad"


def test_file_wins_over_env(cfg_file, monkeypatch):
    monkeypatch.setenv("MIH_SERVICE_TOKEN", "env-tok")
    assert load_config(str(cfg_file))["service_token"] == "tok-0123456789"


def test_mih_config_env_is_used(monkeypatch, cfg_file):
    monkeypatch.setenv("MIH_CONFIG", str(cfg_file))
    assert load_config()["cuser_id"] == "cuser-abc"


def test_client_from_config_wires_credentials(cfg_file):
    c = client_from_config(load_config(str(cfg_file)))
    assert c.security == "c2VjcmV0LXNzZWN1cml0eQ=="
    assert c.service_token == "tok-0123456789"
    assert c.cuser == "cuser-abc"
    assert c.base == "https://hlth.io.mi.com"
    assert c.min_interval == 0.0


def test_client_from_config_kwargs_override(cfg_file):
    c = client_from_config(load_config(str(cfg_file)), min_interval=1.5, region="abroad")
    assert c.min_interval == 1.5
    assert c.base == "https://region.hlth.io.mi.com"


def test_min_interval_string_is_cast(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"min_interval": "0.75"}), encoding="utf-8")
    assert load_config(str(p))["min_interval"] == 0.75
