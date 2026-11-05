#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mi Health HTTP gateway + dashboard（FastAPI）.

Reads from the local SQLite mirror (`mihealth-sync backfill/incremental`) when
the database exists and falls back to live API calls otherwise — so the
dashboard stays fast and only the syncer talks to the cloud in bulk.

Routes
------
  GET  /                        dashboard
  GET  /api/health              auth + storage status
  GET  /api/overview            today's cards
  GET  /api/series/<key>        deduped series (?hours=24|168|720)
  GET  /api/daily_goals         daily goal summary (?days=14)
  GET  /api/sport_records       sport list (?days=30)
  GET  /api/sync/status         cursors + row counts
  POST /api/sync                run incremental sync in background
  GET  /api/fitness/<key>       live: raw minute data (?start=&end=)
  GET  /api/medical /api/project /api/stat/<key> /api/aggregated
  GET  /api/sport_summary /api/max_watermark /api/watermark/<key>
  GET  /api/latest /api/relatives/<sub> /api/raw/<path> /api/keys
  *    /mcp                     MCP (Streamable HTTP)，需 `uv sync --extra mcp`
  GET  /docs, /openapi.json     OpenAPI 自动文档（无需 token）

鉴权
----
`/api/*` 与 `/mcp` 都要求 token（`X-Api-Token: <t>` 或 `Authorization: Bearer <t>`）。
token 取自 `MIH_API_TOKEN` 环境变量，其次 `config.json` 的 `api_token`，
都没有就自动生成并写回 config（0600）。`MIH_API_TOKEN=off` 可整体关闭。
豁免：`/`、`/vendor/*`、`/api/login/captcha`（`<img>` 发不了 header）、`/docs`、
`/redoc`、`/openapi.json`。
"""
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import paths, runtime
from .client import AuthError, SCALAR_FIELDS, load_config

CFG = runtime.get_config()
CFG_PATH = CFG.get("config_path")
DAY = 86400000

_cache = {}


# ---------------------------------------------------------------- helpers

def ms_now():
    return int(time.time() * 1000)


def today_start_ms():
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


def _ms(request: Request, name, default=None):
    """读一个整数查询参数。

    语义与 Flask 版一致：缺省与空串都返回 default（`?start=&end=` 是 API.md
    里成文的写法，不能变成 422）；但真正非法的值（`?limit=abc`）现在明确
    报 422，而不是静默当成 None。
    """
    v = request.query_params.get(name)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise HTTPException(422, detail={"ok": False, "err": f"{name} 不是整数: {v!r}"})


def _int_arg(request: Request, name, default):
    """读一个整数查询参数，非法时保留旧行为（回落到 default）。"""
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_arg(request: Request, name, default):
    try:
        return float(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def cached(key, ttl_ms, fn):
    ent = _cache.get(key)
    if ent and time.time() * 1000 - ent[0] < ttl_ms:
        return ent[1]
    val = fn()
    _cache[key] = (time.time() * 1000, val)
    return val


def _parse_items(res):
    items = []
    for it in res.get("data_list", []):
        try:
            inner = json.loads(it.get("value") or "{}")
        except Exception:
            inner = {}
        inner["_t"] = it.get("time")
        inner["_sid"] = it.get("sid")
        items.append(inner)
    return items


def fetch_series_range(key, start_ms, end_ms):
    """DB first, live fallback. Items are deduped in both paths."""
    store = runtime.get_store()
    if store is not None:
        return {"ok": True, "key": key, "items": store.rows("fitness", key, start_ms, end_ms),
                "source": "db"}
    res = runtime.get_client().get_fitness_data_all(key, start_ms, end_ms, max_pages=400,
                                                    full_history=start_ms <= 0)
    if res is None:
        return {"ok": False, "err": "null"}
    return {"ok": True, "key": key, "items": _parse_items(res), "source": "live",
            "has_more": res.get("has_more")}


def fetch_series(key, hours):
    end = ms_now()
    return fetch_series_range(key, 0 if hours <= 0 else end - int(hours * 3600000), end)


def J(x):
    return json.dumps(x, separators=(",", ":"))


def call_paged(sub, mk_params, list_field, max_pages=100):
    client = runtime.get_client()
    items, nk = [], None
    for _ in range(max_pages):
        r = client.call(sub, {"data": J(mk_params(nk))})
        res = (r or {}).get("result") or {}
        items += res.get(list_field) or []
        nk = res.get("next_key")
        if not res.get("has_more") or not nk:
            return {"ok": True, "items": items, "has_more": False}
    return {"ok": True, "items": items, "has_more": True}


# ---------------------------------------------------------------- auth

_TOKEN: str | None = None
_AUTH_OFF = False
EXEMPT_EXACT = ("/", "/docs", "/redoc", "/openapi.json", "/api/login/captcha")
EXEMPT_PREFIX = ("/vendor/",)


def init_token():
    """确定本进程的 token。

    优先级：`MIH_API_TOKEN`（`off` = 关闭鉴权）> config 的 `api_token`
    > 自动生成并写回 config（0600）。
    """
    global _TOKEN, _AUTH_OFF
    env = os.environ.get("MIH_API_TOKEN")
    if env and env.strip().lower() == "off":
        _AUTH_OFF, _TOKEN = True, None
        return None
    if env:
        _TOKEN = env.strip()
        return _TOKEN
    cfg = runtime.reload_config()
    tok = cfg.get("api_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        try:
            # 不能借 login.save_session：那只认凭据字段，api_token 会被丢掉
            # config_path 取 reload 后的（模块级 CFG_PATH 在测试里可能是旧路径）
            path = cfg.get("config_path") or CFG_PATH or paths.config_path()
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["api_token"] = tok
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:                                     # pragma: no cover
                pass
        except Exception as e:                                   # pragma: no cover
            print(f"[auth] 无法把 token 写入 config: {e}", flush=True)
        runtime.reload_config()
    _TOKEN = tok
    return tok


def _presented_token(request: Request) -> str:
    hdr = request.headers.get("authorization") or ""
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()
    return (request.headers.get("x-api-token") or "").strip()


def _needs_auth(path: str) -> bool:
    if path in EXEMPT_EXACT or path.startswith(EXEMPT_PREFIX):
        return False
    return True


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app):
    tok = init_token()
    if _AUTH_OFF:
        print("[auth] token 鉴权已关闭（MIH_API_TOKEN=off）", flush=True)
    elif tok:
        print(f"[auth] token 已启用（前 6 位 {tok[:6]}…）；请求带 X-Api-Token 或 Authorization: Bearer", flush=True)
    print(f"[gateway] db={runtime.get_config().get('db') if runtime.get_store() else '(live)'}", flush=True)
    if MCP_APP is not None:
        # app.mount() 不会跑子应用的 lifespan，会话管理器必须由宿主驱动
        from .mcp_server import mcp as _mcp
        async with _mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="Mi Health Gateway", version="0.3.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _unwrap_http_exception(request: Request, exc: HTTPException):
    """HTTPException(detail=dict) 默认包成 {"detail": {...}}。

    本项目的错误体一直是 {"ok": false, "err": "..."}，直接摊平以保持兼容。
    """
    if isinstance(exc.detail, dict):
        return JSONResponse(exc.detail, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _AUTH_OFF or not _needs_auth(request.url.path):
        return await call_next(request)
    if _TOKEN and _presented_token(request) != _TOKEN:
        return JSONResponse({"ok": False, "err": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------- mount /mcp

MCP_APP = None
try:                                          # 未安装 mcp 时网关照常可用
    from .mcp_server import build_app as _build_mcp

    MCP_APP = _build_mcp(os.environ.get("MIH_HOST", "127.0.0.1"))
    app.mount("/mcp", MCP_APP)
    print("[mcp] streamable-http at /mcp（与 /api/* 同一 token 中间件）", flush=True)
except ImportError as _e:                     # pragma: no cover
    print(f"[mcp] disabled ({_e}) — install with: uv sync --extra mcp", flush=True)


# ---------------------------------------------------------------- static

@app.get("/", include_in_schema=False)
def index():
    html = Path(paths.static_dir(), "index.html").read_text(encoding="utf-8")
    if _TOKEN and not _AUTH_OFF:
        snippet = f'<script>window.__MIH_TOKEN__={json.dumps(_TOKEN)}</script>'
        html = html.replace("<!--MIH_TOKEN-->", snippet, 1)
    return HTMLResponse(html)


@app.get("/vendor/{f:path}", include_in_schema=False)
def vendor(f: str):
    base = Path(paths.static_dir()).resolve()
    p = (base / f).resolve()
    if not str(p).startswith(str(base)) or not p.is_file():
        raise HTTPException(404, detail={"ok": False, "err": "not found"})
    return FileResponse(str(p))


# ---------------------------------------------------------------- status

@app.get("/api/health")
def api_health():
    store = runtime.get_store()
    client = runtime.get_client()
    st = {"ok": True, "mode": "db" if store else "live",
          "db": runtime.get_config().get("db") if store else None,
          "host": client.base, "cuser_id": client.cuser,
          "token_prefix": (client.service_token or "")[:16] + "...",
          "sseccurity_set": bool(client.security)}
    if store:
        st["rows"] = store.count()
        st["cursors"] = store.states()
    try:
        client.call("data/get_max_watermark", {"data": '{"type":0}'}, retries=0)
        st["auth"] = "ok"
    except AuthError as e:
        st["auth"] = "expired"
        st["auth_error"] = str(e)
    except Exception as e:
        st["auth"] = "error"
        st["auth_error"] = str(e)
    if _job().state["running"]:
        st["syncing"] = True
    return st


def _job():
    cfg = runtime.get_config()
    return runtime.get_sync_job(cfg.get("db") or paths.db_path(), CFG_PATH)


@app.get("/api/sync/status")
def api_sync_status():
    st = _job().state
    out = {"ok": True, "running": st["running"], "started": st["started"],
           "result": st["result"]}
    store = runtime.get_store()
    if store:
        out["rows"] = store.count()
        out["cursors"] = store.states()
    return out


@app.post("/api/sync")
def api_sync_run():
    if not _job().start(hours=72):
        raise HTTPException(409, detail={"ok": False, "err": "already running"})
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- login

def _apply_session(sess):
    """把新会话写回 config 并热更新 client。"""
    from .login import save_session
    save_session(CFG_PATH, sess)
    runtime.get_client().reload_credentials(sess.get("ssecurity"), sess.get("service_token"),
                                            sess.get("cuser_id"))
    _cache.clear()


def _auth_selfheal(cl, why):
    """401 自愈：优先 passToken 免密续期，其次从 adb 设备导入。"""
    from .auth import make_refresh
    return make_refresh(CFG_PATH, on_success=_cache.clear)(cl, why)


runtime.set_client_hook(_auth_selfheal)


@app.get("/api/login/status")
def api_login_status():
    """会话状态：能否免密续期、上次登录时间、账号 id。"""
    cfg = load_config(CFG_PATH)
    return {"ok": True, "can_refresh": bool(cfg.get("pass_token")),
            "session_at": cfg.get("session_at"), "sid": cfg.get("sid") or "miothealth",
            "user_id": cfg.get("user_id"), "cuser_id": cfg.get("cuser_id"),
            "password_stored": False,
            "modes": ["passToken 免密续期", "从 adb 设备导入", "账号密码登录"]}


@app.post("/api/login/refresh")
def api_login_refresh():
    """免密续期（用已保存的 passToken，不需要密码/模拟器）。"""
    cfg = load_config(CFG_PATH)
    if not cfg.get("pass_token"):
        raise HTTPException(400, detail={"ok": False,
                                         "err": "config 里没有 pass_token：请先账号密码登录或从设备导入"})
    from .login import LoginError, refresh_session
    try:
        sess = refresh_session(cfg, CFG_PATH, proxies=cfg.get("proxies"))
    except LoginError as e:
        raise HTTPException(400, detail={"ok": False, "err": str(e), "code": e.code})
    _apply_session(sess)
    return {"ok": True, "mode": "passToken", "token_prefix": sess["service_token"][:14]}


# 账号密码登录的两段式状态：验证码/2FA 要跨请求保留同一个
# requests.Session（Xiaomi 把验证码绑在会话 cookie `ick` 上）。
_login_state = {"session": None, "captcha_url": None, "username": None,
                "2fa_context": None, "2fa_flag": None}


def _clear_login_state():
    _login_state.update(session=None, captcha_url=None, username=None,
                        **{"2fa_context": None, "2fa_flag": None})




@app.post("/api/login")
async def api_login_password(request: Request):
    """账号密码登录（首次）。密码仅用于当次请求，不落盘；可能要求验证码。

    验证码流程：同一登录会话（requests.Session）在两次请求间保留，
    因为 Xiaomi 把验证码绑定在会话 cookie（ick）上。
    """
    body = await request.json() if await request.body() else {}
    body = body or {}
    username = body.get("username") or _login_state["username"]
    password = body.get("password")
    captcha, code2fa = body.get("captcha"), body.get("code")
    proxies = CFG.get("proxies") or None
    from .login import (LoginError, Need2FA, finish_2fa, login_with_password,
                        new_login_session, start_2fa)

    # 第二段：带 2FA 验证码，直接走完换取 serviceToken
    if code2fa and _login_state.get("2fa_context"):
        try:
            sess = finish_2fa(_login_state["session"], code2fa, _login_state["2fa_context"],
                              flag=_login_state.get("2fa_flag") or 4)
        except LoginError as e:
            raise HTTPException(400, detail={"ok": False, "err": str(e), "need_2fa": True,
                                             "code": e.code})
        _clear_login_state()
        if not sess.get("service_token"):
            raise HTTPException(400, detail={"ok": False, "err": "验证通过但未取得 serviceToken"})
        _apply_session(sess)
        return {"ok": True, "mode": "password+2fa",
                "pass_token_saved": bool(sess.get("pass_token")),
                "token_prefix": sess["service_token"][:14]}

    if not (username and password):
        raise HTTPException(400, detail={"ok": False, "err": "需要 username 与 password"})
    if _login_state["session"] is None:
        _login_state["session"] = new_login_session(proxies=proxies)
    try:
        sess = login_with_password(username, password, captcha=captcha,
                                   proxies=proxies, session=_login_state["session"])
    except Need2FA as e:
        _login_state["username"] = username
        try:
            info = start_2fa(_login_state["session"], e.notification_url)
            _login_state["2fa_context"] = info["context"]
            _login_state["2fa_flag"] = info.get("flag") or 4
            chan = "短信" if info.get("method") == "Phone" else "邮箱"
            msg = f"需要二次验证：验证码已发送到你的{chan}"
            if info.get("hint"):
                msg += f"（{info['hint']}）"
            raise HTTPException(400, detail={"ok": False, "need_2fa": True, "err": msg,
                                             "hint": info.get("hint"), "channel": chan,
                                             "flag": info.get("flag")})
        except LoginError as e2:
            _login_state["session"] = None
            raise HTTPException(400, detail={"ok": False, "err": f"触发二次验证失败：{e2}"})
    except LoginError as e:
        _login_state["username"] = username
        if e.captcha_url:
            _login_state["captcha_url"] = e.captcha_url
            raise HTTPException(400, detail={"ok": False, "err": str(e), "code": e.code,
                                             "captcha_url": "/api/login/captcha"})
        _login_state["session"] = None
        raise HTTPException(400, detail={"ok": False, "err": str(e), "code": e.code})
    _clear_login_state()
    if not sess.get("service_token"):
        raise HTTPException(400, detail={"ok": False,
                                         "err": "登录成功但未取得 serviceToken（可能需要二次验证）"})
    _apply_session(sess)
    return {"ok": True, "mode": "password",
            "pass_token_saved": bool(sess.get("pass_token")),
            "token_prefix": sess["service_token"][:14]}


@app.get("/api/login/captcha")
def api_login_captcha():
    """代理小米验证码图片（必须用同一登录会话的 cookie 取，浏览器直连会失配）。

    这条路由**必须免鉴权**：看板用 `<img src>` 加载它，`<img>` 发不出自定义
    header。它也要求服务端确实存在待处理会话，本身不含任何账号数据。
    """
    url = _login_state.get("captcha_url")
    if not url or _login_state["session"] is None:
        raise HTTPException(404, detail={"ok": False, "err": "没有待处理的验证码"})
    if url.startswith("/"):
        url = "https://account.xiaomi.com" + url
    r = _login_state["session"].get(url, timeout=25)
    return Response(r.content, media_type=r.headers.get("Content-Type", "image/jpeg"))


@app.post("/api/login/import_device")
def api_login_import_device(request: Request):
    """从 adb 设备（已登录的 App）导入会话，含 passToken —— 之后即可免密续期。"""
    from . import credentials
    try:
        creds = credentials.fetch_from_device(serial=request.query_params.get("serial") or None)
        credentials.write_config(creds, CFG_PATH)
    except Exception as e:
        raise HTTPException(400, detail={"ok": False, "err": str(e)})
    runtime.get_client().reload_credentials(creds["ssecurity"], creds["service_token"],
                                            creds["cuser_id"])
    _cache.clear()
    return {"ok": True, "mode": "device",
            "pass_token_saved": bool(creds.get("pass_token")),
            "token_prefix": (creds.get("service_token") or "")[:14]}


# ---------------------------------------------------------------- dashboard

@app.get("/api/overview")
def api_overview():
    now = ms_now()
    t0 = today_start_ms()

    def grab(key, hours):
        r = fetch_series(key, hours)
        return r.get("items", []) if r.get("ok") else []

    steps_today = fetch_series_range("steps", t0, now).get("items", [])
    cals_today = fetch_series_range("calories", t0, now).get("items", [])
    hr = grab("heart_rate", 24)
    spo2 = grab("spo2", 48)
    sleep = grab("sleep", 72)
    weight = grab("weight", 90 * 24)
    pai = grab("pai", 24 * 14)
    return {
        "ok": True, "ts": now, "today_start": t0,
        "source": "db" if runtime.get_store() else "live",
        "steps_today": sum(int(x.get("steps") or 0) for x in steps_today),
        "calories_today": round(sum(float(x.get("calories") or 0) for x in cals_today), 1),
        "steps_today_points": len(steps_today),
        "hr_last": next((x["bpm"] for x in reversed(hr) if x.get("bpm")), None),
        "hr_min_24h": min([x["bpm"] for x in hr if x.get("bpm")] or [None]),
        "spo2_last": next((x["spo2"] for x in reversed(spo2) if x.get("spo2")), None),
        "sleep_last": sleep[-1] if sleep else None,
        "weight_last": next((x.get("weight") for x in reversed(weight) if x.get("weight")), None),
        "pai": next((x for x in reversed(pai) if x.get("daily_pai") or x.get("pai")), None),
        "counts": {"steps": len(steps_today), "hr": len(hr), "sleep": len(sleep), "spo2": len(spo2)},
    }


@app.get("/api/series/{key}")
def api_series(key: str, request: Request):
    hours = _float_arg(request, "hours", 24)
    return cached(f"s:{key}:{int(hours)}", 5 * 60000,
                  lambda: fetch_series(key, 0 if hours <= 0 else hours))


@app.get("/api/daily_goals")
def api_daily_goals(request: Request):
    days = _int_arg(request, "days", 14)
    start = ms_now() - days * DAY

    def go():
        FIELD = {1: "steps", 2: "calories", 3: "stand_hours", 4: "active_minutes"}
        store = runtime.get_store()
        if store is not None:
            raw = store.rows("goal", "goal", start, ms_now())
        else:
            res = runtime.get_client().get_aggregated_all(None, start, ms_now(), "daily_fitness", 100)
            raw = []
            for it in res["data_list"]:
                try:
                    obj = json.loads(it.get("value") or "{}")
                except Exception:
                    obj = {}
                obj["_t"] = it.get("time")
                raw.append(obj)
        out, seen = [], set()
        for it in sorted(raw, key=lambda x: x.get("_t") or 0, reverse=True):
            t = it.get("_t")
            if t in seen:
                continue
            seen.add(t)
            rows = {}
            for g in it.get("goal_items", []):
                f = FIELD.get(g.get("field"), f"field_{g.get('field')}")
                rows[f] = {"achieved": g.get("achieved_value"), "target": g.get("target_value")}
            sg = it.get("stand_goal") or {}
            rows["stand_hours"] = {"achieved": sg.get("achieved_value"),
                                   "target": sg.get("target_value")}
            out.append({"time": t, "goals": rows})
        return {"ok": True, "count": len(out), "items": out}

    return cached(f"goals:{days}", 10 * 60000, go)


@app.get("/api/sport_records")
def api_sport(request: Request):
    days = _int_arg(request, "days", 30)
    stype = request.query_params.get("type") or None   # 运动类型过滤: outdoor_running ...
    start = ms_now() - days * DAY

    def go():
        store = runtime.get_store()
        client = runtime.get_client()
        if store is not None:
            items = store.rows("sport", stype, start, ms_now())
            if items or stype or days >= 3650:
                return {"ok": True, "items": items, "type": stype, "source": "db"}
        for d in sorted({days, 90, 365, 3650}):
            res = client.get_sport_records_all(ms_now() - d * DAY, ms_now(),
                                               full_history=(d >= 3650))
            if res and res.get("sport_records"):
                out = []
                for it in res["sport_records"]:
                    try:
                        inner = json.loads(it.get("value") or "{}")
                    except Exception:
                        inner = {}
                    inner["_key"] = it.get("key")
                    inner["_t"] = it.get("time")
                    out.append(inner)
                return {"ok": True, "items": out, "days_used": d, "source": "live"}
        return {"ok": True, "items": [], "source": "live"}

    return cached(f"sport:{days}", 10 * 60000, go)


@app.get("/api/keys")
def api_keys():
    return {"keys": ["steps", "calories", "heart_rate", "sleep", "spo2", "stress",
                     "pai", "weight", "energy", "vo2_max", "blood_pressure",
                     "blood_sugar", "valid_stand", "intensity", "menstruation",
                     "headset", "goal"],
            "families": ["fitness", "sport", "diet", "goal", "medical", "project"]}


# ---------------------------------------------------------------- generic query

@app.get("/api/families")
def api_families():
    """库内数据清单：family / key / 行数 / 时间范围 + 数据源。

    全表分组约数秒，缓存 5 分钟（同步后会自动失效）。
    """
    if not runtime.get_store():
        raise HTTPException(404, detail={"ok": False, "err": "no db (run mihealth-sync backfill)"})
    return cached("families", 5 * 60000, _families_payload)


def _families_payload():
    store = runtime.get_store()
    by_family = {}
    for r in store.stats(dedup=True):
        f = by_family.setdefault(r["family"], {"family": r["family"], "rows": 0,
                                               "first": r["first"], "last": r["last"], "keys": []})
        f["rows"] += r["count"]
        f["keys"].append({"key": r["key"], "count": r["count"],
                          "first": r["first"], "last": r["last"]})
        f["first"] = min(f["first"] or r["first"] or 0, r["first"] or f["first"] or 0)
        f["last"] = max(f["last"] or 0, r["last"] or 0)
    return {"ok": True,
            "total_rows": store.count(dedup=True),
            "raw_rows": store.count(),
            "dedup": "materialized",
            "families": list(by_family.values()),
            "sources": store.sources()}


@app.get("/api/db/{family}")
def api_db_family(family: str, request: Request):
    """任意数据族的多维查询。

    ?key=     数据键/运动类型（可逗号分隔，省略=全部）
    ?start=&end=  毫秒时间戳（或 seconds=1 表示秒）
    ?sid=     只看某个数据源（手表/手机）
    ?dedup=0  返回原始行（含各源重复），默认 1=去重
    ?order=desc&limit=  倒序/条数上限
    """
    store = runtime.get_store()
    if not store:
        raise HTTPException(404, detail={"ok": False,
                                         "err": "no db (run mihealth-sync backfill)"})
    sec = request.query_params.get("seconds") == "1"
    start, end = _ms(request, "start"), _ms(request, "end")
    if sec:
        start = start * 1000 if start is not None else None
        end = end * 1000 if end is not None else None
    keys = [k for k in (request.query_params.get("key") or "").split(",") if k]
    dedup = request.query_params.get("dedup", "1") != "0"
    order = request.query_params.get("order", "asc")
    limit = _ms(request, "limit")
    sid = request.query_params.get("sid")
    meta_only = request.query_params.get("fields") == "meta"
    if keys:
        # push the limit into SQL per key when a single key is requested;
        # multi-key merges keep the caller's limit as a post-slice
        per_key = limit if len(keys) == 1 else None
        items = []
        for k in keys:
            items += store.rows(family, k, start, end, dedup=dedup, sid=sid,
                                order=order, limit=per_key)
        items.sort(key=lambda x: x.get("_t") or 0, reverse=(order == "desc"))
        if limit:
            items = items[:limit]
    else:
        items = store.rows(family, None, start, end, dedup=dedup, sid=sid,
                           order=order, limit=limit)
    if meta_only:
        items = [{k: v for k, v in it.items() if k.startswith("_")} for it in items]
    return {"ok": True, "family": family, "count": len(items),
            "dedup": dedup, "items": items}


@app.get("/api/db/{family}/keys")
def api_db_keys(family: str):
    store = runtime.get_store()
    if not store:
        raise HTTPException(404, detail={"ok": False, "err": "no db"})
    return cached(f"keys:{family}", 5 * 60000,
                  lambda: {"ok": True, "family": family,
                           "keys": store.keys_of(family),
                           "sources": store.sources(family)})


@app.get("/api/agg/{family}/{key}")
def api_agg(family: str, key: str, request: Request):
    """服务端聚合（不解码行，一次 SQL）：任意键 × 任意内层字段 × 任意桶宽。

    ?field=steps&agg=sum|max|min|avg|count&bucket=3600&start=&end=&sid=&dedup=0|1
    例：/api/agg/fitness/steps?field=steps&agg=sum&bucket=86400&start=...&end=...
    """
    store = runtime.get_store()
    if not store:
        raise HTTPException(404, detail={"ok": False, "err": "no db"})
    field = request.query_params.get("field")
    if not field:
        raise HTTPException(400, detail={"ok": False, "err": "need field=<inner json field>"})
    agg = request.query_params.get("agg", "sum")
    bucket = _ms(request, "bucket", 3600)
    points = store.aggregate(family, key, field,
                             bucket_s=bucket,
                             agg=agg,
                             start_ms=_ms(request, "start"), end_ms=_ms(request, "end"),
                             sid=request.query_params.get("sid"),
                             dedup=request.query_params.get("dedup", "1") != "0")
    return {"ok": True, "family": family, "key": key, "field": field,
            "agg": agg, "bucket": bucket, "count": len(points),
            "points": points}


@app.get("/api/export.csv")
def api_export_csv(request: Request):
    """任意族/键导出 CSV：/api/export.csv?family=fitness&key=steps&start=&end=

    直接返回 CSV 文本，不再落临时文件 —— 并发请求各自独立，不会互相覆盖。
    """
    store = runtime.get_store()
    if not store:
        raise HTTPException(404, detail={"ok": False, "err": "no db"})
    family = request.query_params.get("family", "fitness")
    key = request.query_params.get("key")
    text, n = store.to_csv_string(family, key, _ms(request, "start"), _ms(request, "end"),
                                  dedup=request.query_params.get("dedup", "1") != "0")
    name = f"mihealth_{family}_{key or 'all'}.csv"
    return Response(text.encode("utf-8-sig"), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="{name}"',
                             "x-row-count": str(n)})


@app.get("/api/sport_types")
def api_sport_types():
    """运动类型清单（库内实测有数据的）。"""
    store = runtime.get_store()
    if store:
        return {"ok": True, "types": store.keys_of("sport"),
                "sources": store.sources("sport")}
    return call_paged("data/get_sport_records_by_time",
                      lambda nk: {"startTime": 1, "endTime": ms_now(),
                                  "next_key": nk, "limit": 100, "reverse": True},
                      "sport_records", max_pages=1)


@app.get("/api/sport_detail")
def api_sport_detail(request: Request):
    """单条运动扩展数据（轨迹引用 route_info / 课程 course_data）。
    ?sid=&key=outdoor_running&start=&end=（秒）"""
    sid = request.query_params.get("sid")
    key = request.query_params.get("key")
    start = _ms(request, "start")
    end = _ms(request, "end")
    if not (sid and key and start and end):
        raise HTTPException(400, detail={"ok": False,
                                         "err": "need sid,key,start,end (seconds)"})
    return runtime.get_client().get_sport_operational_data(sid, key, start, end)


@app.get("/api/routes")
def api_routes(request: Request):
    """轨迹库（GPS 轨迹列表）。?start=<ms>&limit=&origin_type="""
    start = _ms(request, "start", 0)
    limit = _ms(request, "limit", 50)
    ot = _ms(request, "origin_type", 0)
    return runtime.get_client().get_routes_list(start, limit, True, ot)


@app.get("/api/routes/info")
def api_routes_info(request: Request):
    ids = [i for i in (request.query_params.get("ids") or "").split(",") if i]
    if not ids:
        raise HTTPException(400, detail={"ok": False, "err": "need ids=route_id,..."})
    return runtime.get_client().get_routes_info(ids)


@app.get("/api/fds_url")
def api_fds_url(request: Request):
    """FDS 预签名下载 URL。?sid=<数据源id>&suffix=csv|route&time=<秒>"""
    items = []
    for suffix in (request.query_params.get("suffix") or "csv").split(","):
        items.append({"suffix": suffix, "timeStamp": _ms(request, "time", int(time.time()))})
    return runtime.get_client().gen_fds_download_url(request.query_params.get("sid") or "", items)


@app.get("/api/diet")
def api_diet(request: Request):
    """饮食记录（秒级时间窗）。本地库优先，未同步时走实时。"""
    days = _int_arg(request, "days", 30)
    start_ms = ms_now() - days * DAY
    store = runtime.get_store()
    if store:
        items = store.rows("diet", None, start_ms, ms_now())
        return {"ok": True, "items": items, "source": "db", "count": len(items)}
    res = runtime.get_client().get_diet_records_by_time(start_ms // 1000, ms_now() // 1000)
    return {"ok": True, "items": res["diet_records"], "source": "live"}


# 水位流透传族（比 sync.WM_FAMILIES 宽：这里只转发，不落库）
WM_ROUTE_FAMILIES = {
    "fitness": ("data/get_fitness_data_by_watermark", "data_list", 0),
    "sport":   ("data/get_sport_records_by_watermark", "sport_records", 1),
    "medical": ("data/get_medical_data_by_watermark", "data_list", 5),
    "project": ("data/get_project_data_by_watermark", "data_list", 4),
}


@app.get("/api/watermark_feed/{family}")
def api_watermark_feed(family: str, request: Request):
    """通用水位流：?wm=<游标>，walk 到最新。
    family: fitness | sport | medical | project"""
    spec = WM_ROUTE_FAMILIES.get(family)
    if not spec:
        raise HTTPException(400, detail={"ok": False, "err": "unknown family"})
    sub, field, wtype = spec
    client = runtime.get_client()
    wm = _ms(request, "wm")
    if not wm:
        return {"ok": True, "cursor": client.get_max_watermark(wtype),
                "items": [], "note": "no wm given: cursor seeded at head"}
    items, new_wm, more = client.sync_by_watermark(sub, field, wm,
                                                   max_pages=_ms(request, "pages", 50))
    return {"ok": True, "cursor": new_wm, "count": len(items),
            "has_more": more, "items": items}


# ---------------------------------------------------------------- live passthrough

@app.get("/api/fitness/{key}")
def api_fitness(key: str, request: Request):
    """Raw minute data straight from the cloud (?start=&end=&limit=).

    limit truncates the result. Without an explicit start the range defaults to
    the last 24h so a bare call never triggers a multi-hundred-page backfill.
    """
    limit = _ms(request, "limit", 0) or 0
    raw_start = _ms(request, "start", None)
    end = _ms(request, "end", ms_now())

    # full history only on explicit opt-in; a bare call defaults to 24h so it
    # can never walk hundreds of pages.
    full = request.query_params.get("full") == "1" or (raw_start is not None and raw_start <= 0)
    start = 0 if full else (raw_start if raw_start else end - 86400000)

    # Serve from the local store when it covers the request — no cloud round-trip.
    store = runtime.get_store()
    if store is not None:
        cap = limit if limit > 0 else 5000
        rows = store.rows("fitness", key, start, end, limit=cap,
                          order="desc" if limit > 0 else "asc")
        if rows:
            return {"ok": True, "key": key, "count": len(rows),
                    "items": rows, "source": "db",
                    "has_more": len(rows) >= cap}

    pages = _ms(request, "pages", None)
    if pages is None:
        pages = 400 if full else 4
    res = runtime.get_client().get_fitness_data_all(key, start, end, max_pages=pages,
                                                    full_history=full)
    if res is None:
        return {"ok": False, "err": "null"}
    items = res["data_list"]
    total = len(items)
    if limit and limit > 0:
        items = items[:limit]
    return {"ok": True, "key": key, "count": len(items), "total": total,
            "items": items, "source": "live", "has_more": res.get("has_more")}


@app.get("/api/medical")
def api_medical(request: Request):
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    key = request.query_params.get("key") or None
    return call_paged("data/get_medical_data_by_time",
                      lambda nk: {"key": key, "startTime": max(1, start), "endTime": end,
                                  "reverse": True, "next_key": nk}, "data_list")


@app.get("/api/project")
def api_project(request: Request):
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    key = request.query_params.get("key") or None
    return call_paged("data/get_project_data_by_time",
                      lambda nk: {"key": key, "startTime": max(1, start), "endTime": end,
                                  "reverse": True, "next_key": nk}, "data_list")


@app.get("/api/stat/{key}")
def api_stat(key: str, request: Request):
    tag = request.query_params.get("tag", "daily")
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    return call_paged("statistics/get_stat_data_by_time",
                      lambda nk: {"tag": tag, "key": key, "next_key": nk,
                                  "startTime": max(1, start), "endTime": end,
                                  "limit": 50, "reverse": True}, "data_list")


@app.get("/api/aggregated")
def api_aggregated(request: Request):
    tag = request.query_params.get("tag", "daily_fitness")
    key = request.query_params.get("key") or None
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    return call_paged("data/get_aggregated_fitness_data_by_time",
                      lambda nk: {"tag": tag, "key": key, "startTime": max(1, start),
                                  "endTime": end, "cursor": nk, "reverse": True}, "data_list")


@app.get("/api/sport_summary")
def api_sport_summary(request: Request):
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    return call_paged("statistics/scan_sport_summary",
                      lambda nk: {"startTime": max(1, start), "endTime": end,
                                  "next_key": nk}, "data_list")


@app.get("/api/max_watermark")
def api_maxwm(request: Request):
    t = _ms(request, "type", 0)
    return runtime.get_client().call("data/get_max_watermark", {"data": J({"type": t})})


@app.get("/api/watermark/{key}")
def api_watermark(key: str, request: Request):
    wm = _ms(request, "wm", 0)
    limit = _ms(request, "limit")
    return runtime.get_client().get_fitness_data_by_watermark("", wm, limit)


@app.get("/api/latest")
def api_latest(request: Request):
    """Latest single sample per key.

    get_latest_fitness_data rejects every parameter shape we can derive (-8
    invalid params), so read the newest row per key through by_time instead.
    """
    keys = [k.strip() for k in
            (request.query_params.get("keys") or ",".join(SCALAR_FIELDS)).split(",") if k.strip()]
    span = _ms(request, "span", 30 * 86400000)
    end = ms_now()

    def work():
        # Local store answers in microseconds; only keys it has never seen go to
        # the cloud, and those are fetched concurrently. Both paths answer the
        # same shape: {key, time, sid, parsed}.
        out, missing = {}, []
        store = runtime.get_store()
        if store is not None:
            for k in keys:
                rows = store.rows("fitness", k, end - span, end, limit=1, order="desc")
                if rows:
                    row = rows[0]
                    # store.rows already decodes `value`, so everything except its
                    # bookkeeping keys is the parsed payload.
                    out[k] = {"key": row.get("_key", k), "time": row.get("_t"),
                              "sid": row.get("_sid"),
                              "parsed": {kk: vv for kk, vv in row.items()
                                         if kk not in ("_t", "_sid", "_key")}}
                else:
                    missing.append(k)
        else:
            missing = list(keys)

        if missing:
            client = runtime.get_client()

            def fetch(k):
                try:
                    res = client.get_fitness_data_by_time(k, end - span, end, reverse=True) or {}
                    items = (res.get("result") or {}).get("data_list") or []
                    if not items:
                        return k, None
                    it = items[0]
                    try:
                        parsed = json.loads(it.get("value") or "{}")
                    except Exception:
                        parsed = {}
                    return k, {"key": it.get("key", k), "time": it.get("time"),
                               "sid": it.get("sid"), "parsed": parsed}
                except Exception as e:
                    return k, {"err": str(e)}

            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as ex:
                for k, val in ex.map(fetch, missing):
                    out[k] = val
        return out, ("db" if not missing else "mixed")

    items, src = cached("latest:" + ",".join(keys), 20000, work)
    return {"ok": True, "items": items, "source": src}


@app.get("/api/sport_categories")
def api_sport_cat():
    return runtime.get_client().call("data/get_sport_category_list", {"data": "{}"})


@app.get("/api/relatives/{sub}")
def api_relatives(sub: str, request: Request):
    start = _ms(request, "start", 0)
    end = _ms(request, "end", ms_now())
    params = {"startTime": max(1, start), "endTime": end, "reverse": True}
    tgt = request.query_params.get("target")
    if tgt:
        params["target"] = tgt
    return call_paged(f"relatives/get_{sub}",
                      lambda nk: dict(params, next_key=nk), "data_list")


@app.get("/api/raw/{sub:path}")
def api_raw(sub: str, request: Request):
    data = request.query_params.get("data")
    params = {"data": data} if data is not None else {}
    return runtime.get_client().call(sub, params, decrypt=False)


# ---------------------------------------------------------------- main

def main(argv=None):
    """`mihealth-api`: start the local gateway (uvicorn)."""
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="Mi Health 本地网关")
    ap.add_argument("--host", default=os.environ.get("MIH_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MIH_PORT", "8567")))
    ap.add_argument("--reload", action="store_true", help="开发用：改动自动重启")
    a = ap.parse_args(argv)
    os.environ["MIH_HOST"] = a.host
    target = "mihealth_api.server:app" if a.reload else app
    uvicorn.run(target, host=a.host, port=a.port, reload=a.reload, log_level="info")


if __name__ == "__main__":
    main()
