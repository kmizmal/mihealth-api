# MCP 接口（Streamable HTTP）

把本地 SQLite 库暴露成 **7 个 MCP 工具**，让支持 MCP 的客户端（DSH / Claude Desktop /
Cursor 等）直接查小米运动健康数据，不用自己拼 HTTP 请求。

端点与网关**同进程同端口**（默认 `http://127.0.0.1:8567/mcp`），共用同一把 token。

---

## 1. 启用

MCP 依赖是**可选**的，正常装包不会被拖进 28 个包：

```bash
uv sync --extra mcp          # 装 mcp>=2.3.0（含 starlette / pydantic / uvicorn 依赖树）
uv run mihealth-api          # 起网关，日志会打 [mcp] streamable-http at /mcp
```

没装 `mcp` 时网关照常工作，只是不打那行日志、`/mcp` 不存在（其余 39 条路由不受影响）。

```bash
# 确认端点活着（不带 token 必须 401，这是设计）
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8567/mcp
# → 401
curl -s -X POST http://127.0.0.1:8567/mcp/ \
     -H "Authorization: Bearer $MIH_API_TOKEN" \
     -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## 2. 鉴权

与 `/api/*` **完全同一套**：`server.py` 的 `@app.middleware("http")` 会罩住
`app.mount()` 的子应用，所以 `/mcp` 没有自己的鉴权逻辑。

| 方式 | 写法 |
|---|---|
| 请求头 | `X-Api-Token: <token>` |
| 请求头 | `Authorization: Bearer <token>` |
| 失败 | `401 {"ok": false, "err": "unauthorized"}` |

token 来源优先级（`server.init_token()`）：

1. 环境变量 `MIH_API_TOKEN`；值 `off` = **整体关闭鉴权**
2. `config.json` 的 `api_token`
3. 都没有 → 自动生成 `secrets.token_urlsafe(24)` 并写回 config（权限 `0600`）

**免 token 的路径**只有：`/`、`/vendor/*`、`/api/login/captcha`、`/docs`、`/redoc`、
`/openapi.json`。`/api/login/captcha` 必须豁免，因为看板用 `<img src>` 加载验证码图，
`<img>` 发不出自定义请求头。

> 无 token 打 `/mcp`（无尾斜杠）返回的是 **401 而不是 307** —— 先鉴权再重定向，
> 杜绝"跟一次重定向就绕过鉴权"。

已知局限：token 落在 `config.json` 里，本机进程仍读得到。它挡的是浏览器里的跨源
请求，不是同机恶意进程。

---

## 3. 客户端配置

### DSH（`@deepseek-ai/dsh-mcp-client`）

```yaml
- id: mcp-mihealth
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    transport: streamable-http
    url: http://127.0.0.1:8567/mcp
    headers:
      Authorization: Bearer <token>
```

### Claude Desktop / 其它只支持 stdio 的客户端

```jsonc
{
  "mcpServers": {
    "mihealth": {
      "command": "npx",
      "args": ["-y", "mcp-remote",
               "http://127.0.0.1:8567/mcp",
               "--header", "Authorization: Bearer <token>"]
    }
  }
}
```

### Python 客户端

```python
import asyncio
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

TOKEN = open("token.txt").read().strip()

async def main():
    async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with streamable_http_client(
                "http://127.0.0.1:8567/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = await s.list_tools()
                print([t.name for t in tools.tools])
                res = await s.call_tool("status", {})
                # 注意：res.structured_content 通常是 None，
                # 内容在 res.content[0].text 里（格式化好的 JSON 字符串）
                print(res.content[0].text)

asyncio.run(main())
```

---

## 4. 工具目录

时间口径（**写错就是静默错数据**）：入参 `start_ms` / `end_ms` 是**毫秒**，
返回的 `time` 是**秒**（库里的 `records.ts` 存秒），`bucket_s` 是**秒**。

| 工具 | 读/写 | 参数 | 返回 |
|---|---|---|---|
| `status` | 读 | — | `mode`（`db`/`live`）、`db_path`、`rows`/`raw_rows`、`families`、`cursors`、`sync` 任务状态。`cursors` 里除了 `wm_fitness`/`wm_sport` 还有 `auto_daily`：网关每日自动增量上次启动的秒级时间戳（见 README「每日自动增量」） |
| `list_families` | 读 | — | `families[]`（每族行数、时间范围、各键计数）、`sources[]` |
| `list_keys` | 读 | `family` | 该族的 `keys[]`（key + count）与 `sources[]`（sid + count） |
| `query_records` | 读 | `family`, `key?`, `start_ms?`, `end_ms?`, `dedup=true`, `sid?`, `order="asc"`, `limit=500` | `items[]`（原始行 + `_t`/`_sid`/`_key`） |
| `aggregate` | 读 | `family`, `key`, `field`, `agg="sum"`, `bucket_s=3600`, `start_ms?`, `end_ms?`, `sid?`, `dedup=true` | `points: [{t, v}]`（服务端一次 SQL，不拖明细行） |
| `latest` | 读 | `keys?`, `span_ms=2592000000` | `items: {key: {key, time, sid, parsed}}`，窗口内无数据为 `null` |
| `sync_now` | **写** | `hours=72`, `families?` | `{ok, started, hours}`；已在跑返回 `{"ok": false, "err": "already running"}` |

`limit` 夹在 `[1, 5000]`（`limit=0` 视为 1，不是默认值）；`dedup=true` 只留各数据源
（手表 / 手机）中的代表行。

**只读 6 个 + 1 个写**，写工具 `sync_now` 的 annotation 是
`read_only_hint=false, destructive_hint=false, idempotent_hint=true` ——
重复调用不会重复拉数据，只会在已有任务在跑时被拒。

### 为什么没有 `overview` / `daily_goals` / `series`

那些是 `server.py` 里的多段手算（今日卡片、目标达成率、按 key 聚合的 series）。
复刻进 MCP 就是两份会各自漂移的实现。模型拿到 `query_records` + `aggregate`
之后自己算"今天走了多少步"更省事，也更不容易算错。

### 无库时

网关可以在没有本地库的情况下起来（此时走实时云端 API）。MCP 工具**不做实时回退**
——回退要翻页 + 401 自愈，会把云端客户端拖进 MCP 层，破坏
`server → mcp_server → runtime` 的单向依赖。所以：

```json
{"ok": false, "err": "no db — 先跑 `mihealth-sync backfill`，或调用 sync_now 拉最近 72 小时"}
```

**例外是 `sync_now`**：它在无库时也能用，因为 `Syncer` 初始化就会建 schema。
先 `sync_now`、再 `query_records`，是从零开始的推荐路径。

库是**惰性打开**的（`runtime.get_store()`）：网关先起、库后建也能读到；同步完成后
`reset_store()` 让下一次查询看到新数据，不需要重启进程。

---

## 5. 传输细节

- 协议：**Streamable HTTP**，`json_response=True` + `stateless_http=True`
  （单机单用户，省一套会话状态，调试时直接看 JSON）
- 路径：`/mcp/`（`Mount("/mcp")` + `streamable_http_path="/"`）。
  打 `/mcp`（无尾斜杠）会 307 到 `/mcp/`，客户端跟一次即可
- Host 校验（DNS rebinding 防护）：按 `--host` 生成白名单。
  **`allowed_hosts` 不支持 `*` 通配**（SDK 只做精确匹配与 `host:*` 前缀匹配），
  所以绑 `0.0.0.0` / `::` 时只能关掉防护 —— 那是官方 SDK 的默认行为，
  真要暴露到局域网请自己在前面加反向代理做 Host 校验
- 工具函数都是**同步**的：SDK 对同步函数走 `anyio.to_thread.run_sync`，
  同步 SQLite 读不会阻塞事件循环

---

## 6. 排错

| 现象 | 原因 |
|---|---|
| `POST /mcp` → 401 | 没带 token（正确行为）。带 `Authorization: Bearer <token>` |
| `POST /mcp` → 307 | 少了尾斜杠。客户端跟随重定向即可，或直接写 `/mcp/` |
| `421 Misdirected Request` | Host 头不在白名单。本机访问用 `127.0.0.1`/`localhost`；绑了别的网卡要确认 `--host` 与访问用的 host 一致 |
| 工具都返回 `no db` | 没库。先 `uv run mihealth-sync backfill`，或调 `sync_now` |
| 日志没有 `[mcp]` 行 | 没装可选依赖。`uv sync --extra mcp` |
| `RuntimeError: StreamableHTTPSessionManager .run() can only be called once` | 同一进程里重复进入 lifespan（只会在自定义测试里出现）。每次重新 `build_app()` |

---

## 7. 从网关 HTTP 换成 MCP 的对照

| 想要 | HTTP | MCP |
|---|---|---|
| 看有什么数据 | `GET /api/families` | `list_families()` |
| 某族有哪些键 | `GET /api/db/<family>/keys` | `list_keys(family)` |
| 查明细 | `GET /api/db/<family>?key=&start=&end=` | `query_records(family, key, start_ms, end_ms)` |
| 按天求和 | `GET /api/agg/<family>/<key>?bucket=86400` | `aggregate(family, key, field, bucket_s=86400)` |
| 最新一条 | `GET /api/latest?keys=` | `latest(keys)` |
| 拉最近数据 | `POST /api/sync` | `sync_now(hours=72)` |

`/api/overview`、`/api/daily_goals`、`/api/series/<key>`、`/api/export.csv`
**没有** MCP 对应物：前者是派生视图（见 §4 末），后者是文件下载（MCP 传不了附件，
模型要 CSV 就自己从 `query_records` 拼）。
