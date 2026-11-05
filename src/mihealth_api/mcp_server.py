#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 端点：把本地库暴露成 7 个工具（Streamable HTTP，挂在 `/mcp`）。

设计要点
--------
* **只做通用查询**。`/api/overview`、`/api/daily_goals`、`/api/series/<key>` 那些
  派生视图是 `server.py` 里的多段手算逻辑，复刻进来就是两份会漂移的实现。
  模型拿到 `query_records` + `aggregate` 之后自己算"今天走了多少步"更省事。
* **依赖单向**：本模块只 import `runtime`，不 import `server`，否则 `server`
  挂载本模块就成了循环依赖。
* **工具函数都是同步的**。SDK 对同步函数走 `anyio.to_thread.run_sync`
  （`mcp/server/mcpserver/utilities/func_metadata.py` 的 `call_fn`），所以同步
  SQLite 读不会阻塞事件循环 —— 这比自己写 `asyncio.to_thread` 包裹更省事，
  也避免了"异步函数里偷偷阻塞"的隐患。
* **鉴权不在这里做**。`server.py` 的 `@app.middleware("http")` 会罩住
  `app.mount()` 的子应用（实测过），`/mcp` 与 `/api/*` 共用同一个 token。

时间口径（写错就是静默错数据）
------------------------------
* 入参 `start_ms` / `end_ms` 是**毫秒**。
* 返回的 `time` 是**秒**（库里的 `records.ts` 存秒）。
* `aggregate` 的 `bucket_s` 是秒。
"""
import time

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import runtime

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                        idempotent_hint=True, open_world_hint=False)

MAX_LIMIT = 5000
DEFAULT_LIMIT = 500

mcp = MCPServer(
    "mihealth",
    instructions=(
        "小米运动健康本地库。先用 status / list_families 看有什么，"
        "再用 query_records / aggregate / latest 取数。"
        "时间口径：入参 start_ms/end_ms 是毫秒，返回的 time 是秒。"
    ),
)


def _no_db():
    return {"ok": False,
            "err": "no db — 先跑 `mihealth-sync backfill`，或调用 sync_now 拉最近 72 小时"}


def _store():
    return runtime.get_store()


# ---------------------------------------------------------------- tools

@mcp.tool(annotations=READ_ONLY)
def status() -> dict:
    """库与同步任务的整体状态。

    返回 mode（db/live）、库路径、行数、各族游标、以及后台同步任务是否在跑。
    不返回任何凭据（token 前缀也不给）。
    """
    try:
        store = _store()
        cfg = runtime.get_config()
        job = runtime.get_sync_job(cfg.get("db"), cfg.get("config_path"))
        out = {"ok": True, "mode": "db" if store else "live",
               "db_path": (store.path if store is not None else None),
               "sync": job.state}
        if store is not None:
            out["rows"] = store.count()
            out["raw_rows"] = store.count(dedup=False)
            out["families"] = sorted({r["family"] for r in store.stats()})
            out["cursors"] = store.states()
        return out
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=READ_ONLY)
def list_families() -> dict:
    """有哪些数据族（family）、每个族有多少行、时间范围、包含哪些键。

    探索库的第一步。返回 families[] 与 sources[]（数据源 id）。
    """
    try:
        store = _store()
        if store is None:
            return _no_db()
        by_family = {}
        for r in store.stats(dedup=True):
            f = by_family.setdefault(r["family"], {"family": r["family"], "rows": 0,
                                                   "first": None, "last": None, "keys": []})
            f["rows"] += r["count"]
            f["keys"].append({"key": r["key"], "count": r["count"],
                              "first": r["first"], "last": r["last"]})
            if r["first"] is not None:
                f["first"] = r["first"] if f["first"] is None else min(f["first"], r["first"])
            if r["last"] is not None:
                f["last"] = r["last"] if f["last"] is None else max(f["last"], r["last"])
        return {"ok": True, "total_rows": store.count(dedup=True),
                "raw_rows": store.count(), "families": list(by_family.values()),
                "sources": store.sources()}
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=READ_ONLY)
def list_keys(family: str) -> dict:
    """某个数据族里有哪些键（key）与数据源。

    family 取值：fitness / sport / diet / goal / medical / project。
    """
    try:
        store = _store()
        if store is None:
            return _no_db()
        return {"ok": True, "family": family, "keys": store.keys_of(family),
                "sources": store.sources(family)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=READ_ONLY)
def query_records(family: str, key: str | None = None,
                  start_ms: int | None = None, end_ms: int | None = None,
                  dedup: bool = True, sid: str | None = None,
                  order: str = "asc", limit: int = DEFAULT_LIMIT) -> dict:
    """查明细行。返回去重后（默认）的记录数组，按时间升序。

    - family/key：数据族与键，key 省略表示全族
    - start_ms/end_ms：毫秒时间戳（不是秒）
    - dedup：True 只留各源中的代表行；False 连手表+手机的重复行一起返回
    - sid：只看某个数据源
    - limit：最多返回多少行，上限 5000，默认 500
    """
    try:
        store = _store()
        if store is None:
            return _no_db()
        n = int(DEFAULT_LIMIT if limit is None else limit)
        n = max(1, min(n, MAX_LIMIT))
        items = store.rows(family, key or None, start_ms, end_ms,
                           dedup=bool(dedup), sid=sid, order=order, limit=n)
        return {"ok": True, "family": family, "key": key, "count": len(items),
                "dedup": bool(dedup), "limit": n, "items": items}
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=READ_ONLY)
def aggregate(family: str, key: str, field: str, agg: str = "sum",
              bucket_s: int = 3600, start_ms: int | None = None,
              end_ms: int | None = None, sid: str | None = None,
              dedup: bool = True) -> dict:
    """服务端聚合：一次 SQL 出结果，不用把明细行拖进上下文。

    - field：内层 JSON 里的字段名（steps / calories / bpm / spo2 …）
    - agg：sum | max | min | avg | count
    - bucket_s：分桶宽度（秒），86400 = 按天
    返回 points: [{t, v}]，t 是桶起点（秒）。
    """
    try:
        store = _store()
        if store is None:
            return _no_db()
        points = store.aggregate(family, key, field, bucket_s=int(bucket_s or 3600),
                                 agg=agg, start_ms=start_ms, end_ms=end_ms,
                                 sid=sid, dedup=bool(dedup))
        return {"ok": True, "family": family, "key": key, "field": field,
                "agg": agg, "bucket_s": bucket_s, "count": len(points),
                "points": points}
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=READ_ONLY)
def latest(keys: list[str] | None = None,
           span_ms: int = 2592000000) -> dict:
    """每个键的最新一条记录（默认只在最近 30 天里找）。

    keys 省略时取所有有标量的键。返回 items: {key: {key, time, sid, parsed}}，
    time 是秒。某个键在窗口内没数据时该键为 null。
    """
    try:
        store = _store()
        if store is None:
            return _no_db()
        from .client import SCALAR_FIELDS
        want = [k.strip() for k in (keys or list(SCALAR_FIELDS)) if k and k.strip()]
        end = int(time.time() * 1000)
        out = {}
        for k in want:
            rows = store.rows("fitness", k, end - int(span_ms or 2592000000), end,
                              limit=1, order="desc")
            if not rows:
                out[k] = None
                continue
            row = rows[0]
            out[k] = {"key": row.get("_key", k), "time": row.get("_t"),
                      "sid": row.get("_sid"),
                      "parsed": {kk: vv for kk, vv in row.items()
                                 if kk not in ("_t", "_sid", "_key")}}
        return {"ok": True, "count": len([v for v in out.values() if v]), "items": out}
    except Exception as e:
        return {"ok": False, "err": str(e)}


@mcp.tool(annotations=WRITE)
def sync_now(hours: int = 72, families: list[str] | None = None) -> dict:
    """触发一次增量同步（后台跑，立即返回，不阻塞）。

    这是**唯一会写库**的工具。同一时刻只允许一个同步任务：已在跑时返回
    {"ok": false, "err": "already running"}。进度用 status 查。
    无库时也能用 —— 同步器初始化就会建 schema。
    """
    try:
        cfg = runtime.get_config()
        job = runtime.get_sync_job(cfg.get("db"), cfg.get("config_path"))
        if not job.start(hours=int(hours or 72), families=families):
            return {"ok": False, "err": "already running", "state": job.state}
        return {"ok": True, "started": True, "hours": int(hours or 72),
                "note": "后台已启动；用 status 查进度"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ---------------------------------------------------------------- app

def _allowed_hosts(bind_host):
    """按实际绑定地址生成 Host 白名单。

    `TransportSecuritySettings.allowed_hosts` **不支持 `*` 通配**（源码只做精确
    匹配与 `host:*` 前缀匹配，实测 `["*"]` 对 `Host: 192.168.1.5:8567` 仍 421）。
    所以列不出来的时候只能关掉 DNS-rebinding 防护，而不是假装配了个通配。
    """
    hosts = ["127.0.0.1", "localhost", "::1"]
    if bind_host and bind_host not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(bind_host)
    if bind_host in ("0.0.0.0", "::", ""):
        # 绑全网卡时无法枚举，只能关防护；这也是官方 SDK 的默认行为
        return None
    wild = [h for x in hosts for h in (x, f"{x}:*")]
    # TestClient 的 Host 是 testserver，不加就 421 Misdirected Request
    wild += ["testserver", "testserver:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=wild)


def build_app(bind_host="127.0.0.1"):
    """给 `server.py` 挂载用的 Starlette 子应用。"""
    security = _allowed_hosts(bind_host)
    kwargs = {}
    if security is not None:
        kwargs["transport_security"] = security
    return mcp.streamable_http_app(
        json_response=True,          # 不用 SSE 流，普通 JSON 更好调试
        stateless_http=True,         # 单机单用户，省一套会话状态
        streamable_http_path="/",    # 挂在 /mcp 下必须写 "/"，否则变成 /mcp/mcp
        **kwargs,
    )

