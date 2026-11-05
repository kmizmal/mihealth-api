# -*- coding: utf-8 -*-
"""token 鉴权中间件：/api/* 与 /mcp 共用同一把锁，只有白名单放行。

口径（用户定过两次，最终版是「MCP 也要走中间件」）：
  * 豁免：`/`、`/vendor/*`、`/api/login/captcha`、`/docs`、`/redoc`、`/openapi.json`
  * 其余全部校验，包括 `/mcp`、`/mcp/` 和不存在的路径（不泄露路由存在性）
  * `/api/login/captcha` 必须豁免：看板用 `<img src>` 加载它，`<img>` 发不出自定义头
"""
import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient

TOKEN = "unit-test-token"


@pytest.fixture
def app(tmp_path, monkeypatch):
    from mihealth_api import runtime, server as SV

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("MIH_API_TOKEN", TOKEN)
    # 这些用例会 `with TestClient(app)`，也就是真的跑 lifespan；别让调度器起来
    monkeypatch.setenv("MIH_DAILY_SYNC", "off")
    SV = importlib.reload(SV)
    runtime.reset_store()
    runtime.reload_config()
    runtime.set_client(_Fake())
    SV._cache.clear()
    SV._TOKEN, SV._AUTH_OFF = TOKEN, False
    return SV


class _Fake:
    base = "https://hlth.io.mi.com"
    cuser = "C"
    service_token = "t"
    security = "s"
    on_auth_error = None

    def reload_credentials(self, *a, **k):
        pass

    def call(self, sub, params, retries=0, decrypt=True):
        return {"code": 0, "result": {"data_list": []}}


def test_missing_token_is_401(app):
    with TestClient(app.app) as c:
        r = c.get("/api/health")
    assert r.status_code == 401 and r.json() == {"ok": False, "err": "unauthorized"}


def test_wrong_token_is_401(app):
    with TestClient(app.app, headers={"X-Api-Token": "nope"}) as c:
        assert c.get("/api/health").status_code == 401


def test_x_api_token_header_accepted(app):
    with TestClient(app.app, headers={"X-Api-Token": TOKEN}) as c:
        r = c.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_bearer_header_accepted(app):
    with TestClient(app.app, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.get("/api/health").status_code == 200


def test_bearer_is_case_insensitive(app):
    with TestClient(app.app, headers={"Authorization": f"bearer {TOKEN}"}) as c:
        assert c.get("/api/health").status_code == 200


def test_index_is_exempt_and_injects_token(app):
    """看板本身不需要 token，但它得拿到 token 才能调 /api/*。

    看板是 `<img src>` + `fetch` 混着用的，`<img>` 发不出自定义头，
    所以只能在 HTML 里注入 `window.__MIH_TOKEN__`，由页面自己带上。
    """
    with TestClient(app.app) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert f'window.__MIH_TOKEN__="{TOKEN}"' in r.text


def test_index_html_keeps_the_token_placeholder(app):
    """静态文件里必须留占位符 —— 删了它看板就静默退化成全 401。"""
    from pathlib import Path

    from mihealth_api import paths

    html = Path(paths.static_dir(), "index.html").read_text(encoding="utf-8")
    assert "<!--MIH_TOKEN-->" in html
    # 三处取数都要带 token，漏一处就有一条数据拿不到
    assert "const AUTH=TK?" in html
    assert "fetch(p,{headers:AUTH})" in html
    assert "headers:{...AUTH," in html


def test_index_does_not_inject_when_auth_off(app):
    app._AUTH_OFF = True
    with TestClient(app.app) as c:
        r = c.get("/")
    assert r.status_code == 200 and "<!--MIH_TOKEN-->" in r.text


def test_favicon_is_exempt(app):
    """浏览器无条件请求 favicon；401 只会在日志里刷一行噪音。"""
    with TestClient(app.app) as c:
        assert c.get("/favicon.ico").status_code == 204


def test_docs_and_openapi_are_exempt(app):
    with TestClient(app.app) as c:
        assert c.get("/docs").status_code == 200
        assert c.get("/openapi.json").status_code == 200


def test_captcha_is_exempt_because_img_cannot_send_headers(app):
    """`<img src>` 发不出自定义 header —— 拦了它等于首次登录永远失败。"""
    with TestClient(app.app) as c:
        r = c.get("/api/login/captcha")
    assert r.status_code == 404          # 没有待处理会话 → 404，而不是 401
    assert r.json()["err"] == "没有待处理的验证码"


def test_vendor_assets_are_exempt(app):
    with TestClient(app.app) as c:
        r = c.get("/vendor/chart.umd.min.js")
    assert r.status_code in (200, 404)
    assert r.status_code != 401


def test_mcp_requires_token(app):
    """用户明确要求 /mcp 也走同一个中间件。"""
    with TestClient(app.app) as c:
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp/", json={}).status_code == 401


def test_mcp_rejects_wrong_token(app):
    with TestClient(app.app, headers={"Authorization": "Bearer wrong"}) as c:
        assert c.post("/mcp/", json={}).status_code == 401


def test_mcp_redirect_only_after_auth(app):
    """无 token 打 /mcp 必须 401 而不是 307 —— 否则重定向能绕开鉴权。"""
    # 只能开一个 TestClient：MCP 的 session_manager 每个实例只能 run 一次
    with TestClient(app.app) as c:
        assert c.post("/mcp", json={}).status_code == 401
        # 带 token 就不该再是 401：TestClient 默认跟随 307，落到 /mcp/ 后
        # 空 JSON-RPC 被 MCP 判 400，两者都说明鉴权已通过
        assert c.post("/mcp", json={},
                      headers={"X-Api-Token": TOKEN}).status_code in (307, 400)


def test_unknown_path_also_needs_token(app):
    """404 杂项也拦：不向未授权方泄露哪些路由存在。"""
    with TestClient(app.app) as c:
        assert c.get("/api/definitely-not-a-route").status_code == 401
        assert c.get("/api/definitely-not-a-route",
                     headers={"X-Api-Token": TOKEN}).status_code == 404


def test_auth_off_disables_middleware(tmp_path, monkeypatch):
    from mihealth_api import runtime, server as SV

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("MIH_API_TOKEN", "off")
    SV = importlib.reload(SV)
    runtime.reset_store()
    runtime.reload_config()
    runtime.set_client(_Fake())
    SV._TOKEN, SV._AUTH_OFF = None, True
    with TestClient(SV.app) as c:
        assert c.get("/api/health").status_code == 200


def test_token_generated_and_persisted(tmp_path, monkeypatch):
    """没有 MIH_API_TOKEN 时自动生成并写回 config，下次启动复用同一个。"""
    from mihealth_api import runtime, server as SV

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIH_API_TOKEN", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "S", "service_token": "T",
                               "cuser_id": "C"}), encoding="utf-8")
    monkeypatch.setenv("MIH_CONFIG", str(cfg))
    monkeypatch.setenv("MIH_DB", str(tmp_path / "absent.db"))
    SV = importlib.reload(SV)
    runtime.reload_config()
    tok = SV.init_token()
    assert tok and len(tok) > 20
    assert json.loads(cfg.read_text(encoding="utf-8"))["api_token"] == tok
    if os.name == "posix":
        # Windows 没有 posix 权限位，chmod 基本是空操作 —— 只在 POSIX 上断言
        assert oct(cfg.stat().st_mode)[-3:] == "600"
    # 再来一次拿到同一个（从 config 读）
    SV2 = importlib.reload(SV)
    runtime.reload_config()
    assert SV2.init_token() == tok


def test_env_token_wins_over_config(tmp_path, monkeypatch):
    from mihealth_api import runtime, server as SV

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_token": "from-file", "ssecurity": "S",
                               "service_token": "T", "cuser_id": "C"}), encoding="utf-8")
    monkeypatch.setenv("MIH_CONFIG", str(cfg))
    monkeypatch.setenv("MIH_API_TOKEN", "from-env")
    SV = importlib.reload(SV)
    runtime.reload_config()
    assert SV.init_token() == "from-env"


def test_http_exception_detail_dict_is_unwrapped(app):
    """错误体形状必须还是 {"ok": false, "err": ...}，不能多一层 detail。"""
    with TestClient(app.app, headers={"X-Api-Token": TOKEN}) as c:
        body = c.get("/api/families").json()
    assert body == {"ok": False, "err": "no db (run mihealth-sync backfill)"}


def test_http_exception_detail_str_keeps_default_shape(app, tmp_path, monkeypatch):
    """非 dict 的 detail（框架自己的 422/404）保持 FastAPI 默认形状。"""
    from mihealth_api import runtime
    from mihealth_api.store import Store

    db = tmp_path / "auth-422.db"
    runtime.set_store(Store(str(db)))
    monkeypatch.setenv("MIH_DB", str(db))
    runtime.set_store(Store(str(db)))
    with TestClient(app.app, headers={"X-Api-Token": TOKEN}) as c:
        r = c.get("/api/db/fitness?limit=abc")
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and "limit" in body["err"]


def test_limit_zero_is_not_swallowed(app):
    """`?limit=0` 里的 0 是合法值，不能被 `or default` 吞掉。"""
    with TestClient(app.app, headers={"X-Api-Token": TOKEN}) as c:
        # 无库 → 404；关键是没走到 422/500
        assert c.get("/api/db/fitness?limit=0").status_code == 404
