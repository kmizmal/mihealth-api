# 设计：网关迁到 FastAPI 并同进程提供 MCP 端点

- 日期：2026-11-05
- 状态：待批准
- 仓库：`kmizmal/mihealth-api`（HEAD `87fd0f0`）
- 相关：`docs/superpowers/specs/` 下无既往设计

## 1. 意图与背景

这个仓库是个人用的「小米运动健康」数据工具：云端客户端 → 同步器 → 本地 SQLite 库 → 看板/网关。目前网关是 Flask（`src/mihealth_api/server.py`，874 行，39 条路由），另外有 334 个单元测试、96% 覆盖率。

要加的能力：**让 AI 客户端（本机的 MCP 扩展）直接查这个库**。用户的原话是「编写 mcp 接口文档，准备扩展 HTTP 类型 mcp 接口」，后续又追加「把 Flask 迁移到 FastAPI」「合并端口」。

为什么合到 FastAPI，而不是给 Flask 旁边再起一个进程（这个选项讨论过并被否掉）：现在的 Flask 应用是 WSGI，MCP 的 Streamable HTTP 传输是 ASGI，两者不能自然地跑在一个进程里；装 `a2wsgi` 把 WSGI 塞进 ASGI 能work，但等于给一个即将被替换的框架再加一层适配。既然只换一次框架，不如一次换干净：一个 uvicorn 进程，一个端口，`/api/*` 和 `/mcp` 都在里面。

## 2. 目标 / 非目标 / 反目标

**目标**

1. 网关从 Flask 迁到 FastAPI，**行为和现有 39 条路由一一对应**，看板不改前端逻辑就能用。
2. 同一个进程、同一个端口提供 MCP 的 Streamable HTTP 端点（`/mcp`）。
3. `/api/*` 与 `/mcp` 共用同一个 token 鉴权中间件（可关闭）；`/docs`、`/openapi.json`、`/vendor/*`、`/` 不需要鉴权。
4. MCP 提供 6 个只读工具 + 1 个 `sync_now` 写工具。
5. 交付一份 MCP 接口文档（`MCP.md`），能被外部照着接入。
6. 现有 334 个测试全部继续通过，并补上 MCP + 鉴权的新测试。

**非目标**

- 不改数据库 schema、不改同步器逻辑、不改客户端加密实现。
- 不做多用户 / 不做公网部署加固（只保证 loopback 默认安全，加一层 token 防浏览器跨源请求）。
- 不给 MCP 复制 `/api/overview`、`/api/daily_goals` 这类派生视图（理由见 §4.3）。
- 不引入 pydantic 模型层重写所有响应体（保持 dict 直出，见 §5.2 的取舍）。

**反目标（做成这样算失败）**

- 迁移后看板任何一块数据不显示，或图表时间轴错位（说明毫秒/秒口径在迁移中弄丢了）。
- 为了迁框架而顺手改业务语义（比如把 `?limit=abc` 的旧行为静默改掉却不记录）。
- MCP 工具和网关各写一份取数逻辑，日后两边数字不一致。
- 依赖体积失控：不装 MCP 的人 `uv sync` 时不该被拖进 28 个包。

## 3. 约束

- **Python `>=3.14`**（当前 `.python-version` = 3.15）。实际运行环境是 3.15.0b3。
- 单文件 874 行不是本次拆分的重点，但迁框架时顺手把新逻辑放新模块，别继续往 server.py 里堆。
- 时间口径是硬约束，**写错就是静默错数据**：`store.records.ts` 存**秒**；`store.rows/aggregate` 的 `start_ms/end_ms` 参数是**毫秒**（内部 `//1000` 比较）；云端接口入参是毫秒；`/api/sport_detail` 与 `get_diet_*` 的入参是**秒**。
- `dedup` 是写入时维护的物化表，`v_dedup` 只是视图；查询默认走去重。
- 仓库无 `.gitattributes` 历史遗留问题已在上一轮修掉（`* text=auto eol=lf`），本次保持。

## 4. 方案

### 4.1 MCP 怎么承载（已选定）

| 方案 | 说明 | 取舍 |
|---|---|---|
| A. SDK + 单 ASGI 应用（**选它**） | 一个 uvicorn 进程同时提供 `/api/*` 与 `/mcp` | 一个端口、一份库、一个开关；代价是必须先把 Flask 换掉（本次任务正是这个） |
| B. SDK + 独立进程 | 网关不动，另起 `mihealth-mcp`（8568）读同一个库 | 不用换框架、测试零改动；代价是两个进程两个端口两套启动方式，还得处理"哪个先起" |
| C. 手写 JSON-RPC | 不装 mcp SDK | 省 28 个包；代价是自己维护协议实现（初始化握手、能力协商、SSE 分帧），本机自用不划算 |

选 A 的直接后果：**MCP 依赖必须是可选的**（`[project.optional-dependencies] mcp`），否则每个只想用网关的人都要拖 28 个包。未安装时 `/mcp` 不注册，启动日志明说一句怎么装。

### 4.2 FastAPI 怎么迁（已选定）

| 方案 | 说明 | 取舍 |
|---|---|---|
| A. 真 FastAPI 化（**选它**） | 路径/查询参数改成类型注解，错误改 `HTTPException`，`jsonify` → 直接 `return dict` | 拿到 `/docs`、`/openapi.json`、参数校验；代价是 39 条路由逐条改，且 `?limit=abc` 会从"静默忽略"变成 422（见 §5.3） |
| B. 直接平移 | `jsonify`→dict、`request.args`→`request.query_params` 一比一替换 | 风险最低；代价是 FastAPI 只剩个壳，除了能挂 MCP 没有任何收益 |
| C. 分层混用 | 数据路由用类型注解，登录/同步保持旧写法 | 一个文件两套风格，最难维护 |

### 4.3 MCP 工具的边界（已选定）

只做**对着库的通用查询**，6 个只读 + 1 个写：

- 不复制 `/api/overview`、`/api/daily_goals`、`/api/series/<key>` 这类派生视图。它们是 server.py 里五六段手算逻辑（今日步数求和、最近心率、睡眠最后一条、PAI 取非空…），复刻进 MCP 就是两份会漂移的实现。
- 模型拿到 `query_records` + `aggregate` 之后，"今天走了多少步"自己求和即可，这本来就是模型擅长的。
- 代价明确写在这里：问"今天怎么样"会比调一个现成接口多花一两次工具调用。

## 5. 已决定的实现细节

### 5.1 进程与启动

- 入口 `mihealth-api`（`mihealth_api:main` → `server.main(argv)`）。参数：`--host`（`MIH_HOST`，默认 `127.0.0.1`）、`--port`（`MIH_PORT`，默认 `8567`）、`--reload`（开发用，替代原 `--debug`）。
- 启动用 `uvicorn.run(app, host=..., port=...)`。**日志改为 uvicorn 的**，原有的 `[gateway] ...` / `[store] ...` 打印保留在模块加载期。
- 单进程单 worker。多 worker 会各自开一个 SQLite 连接和一份 `_cache`，没必要也容易误解。
- **`--reload` 要用 import string**：`uvicorn.run("mihealth_api.server:app", reload=True, ...)` —— 传 app 对象时 uvicorn 会拒（reload 需要能重新导入）。`--reload` 与 `--workers>1` 互斥，本设计只用 1。
- **现有两个 `main()` 测试要改**：`tests/test_server.py:577` 的 `test_main_defaults` 与 `:588` 的 `test_main_flags` 现在 monkeypatch `SV.app.run`；迁移后改成 monkeypatch `uvicorn.run`，断言 `host`/`port`/`reload`。`test_main_flags` 原来传 `--debug`，改为 `--reload`。

### 5.2 路由与响应

- 全部 39 条保留原路径与语义。`jsonify(x)` → `return x`（FastAPI 自动 JSON 化）；`jsonify(x), 404` → `raise HTTPException(404, detail=x)`。
- **错误体形状必须保住**：现有 22 处提前返回是 `{"ok": false, "err": "..."}`，而 FastAPI 的 `HTTPException(detail=dict)` 会包成 `{"detail": {...}}`。对策：注册

  ```python
  @app.exception_handler(HTTPException)
  async def _unwrap(request, exc):
      if isinstance(exc.detail, dict):
          return JSONResponse(exc.detail, status_code=exc.status_code)
      return await http_exception_handler(request, exc)
  ```

  实测：`raise HTTPException(404, detail={"ok": False, "err": "no db"})` → 响应体 `{"ok": false, "err": "no db"}`，与 Flask 完全一致。（未实测到的部分：`/api/nodb` 那种 404 在 `TestClient` 里也回同一形状，已验。）
- 静态资源：`/` → `FileResponse(static/index.html)`；`/vendor/*` → `StaticFiles(directory=paths.static_dir())` 挂载；验证码代理 → `Response(content=r.content, media_type=...)`。
- `/api/export.csv`：**顺手修掉临时文件**。现在是 `store.to_csv(tmp, ...)` 再 `send_from_directory`，并发会互相覆盖同名文件，也违背"不落多余文件"的直觉。改为写进临时目录后 `FileResponse(..., filename="...", media_type="text/csv")`，并在返回后删除 —— 或者用 `Store.to_csv` 新增一个返回 `str` 的兄弟方法 `to_csv_string()` 直接 `Response(text=...)`。**采用后者**（`to_csv_string`），因为它同样让 MCP/脚本可复用，且没有清理问题。这是本次唯一的计划外功能改动，会在提交信息里写明。
- 明确保留的"旧怪癖"（不趁迁移改）：
  - **`_ms` 保留原语义**：签名改成 `_ms(request, name, default=None)`，读 `request.query_params`，`None`/`""` 都返回 `default`；`int()` 失败时抛 `HTTPException(422, detail={"ok": False, "err": f"{name} 不是整数: {v!r}"})`。
    - 为什么不用类型注解：**`?start=&end=` 是 API.md 里成文并示例的写法**（§7 有 6 处），类型注解会把空串判成 422，等于打断文档里所有示例。实测对比（`/tmp/mcptest/spike_ms3.py`）：`/a?start=&end=` → 200 `{"start":null,"end":null}`；`/a?limit=abc` → 422 `{"ok":false,"err":"limit 不是整数: 'abc'"}`；`/a?limit=0` → 200 `{"limit":0}`（`0` 不被吞）。
    - **行为变化收窄为**：只有真正非法的值（`abc`）从"静默 500/忽略"变成 422；空串与缺省完全不变。API.md 的「行为变化」只写这一条。
  - `/api/routes`、`/api/raw/*` 是纯透传（不加 `ok` 包装），保持不变。

### 5.3 鉴权

- **`/api/*` 与 `/mcp`：同一个 token 中间件**。
  - token 来源优先级：`MIH_API_TOKEN` 环境变量 > `config.json` 的 `api_token` > 首次启动自动生成（`secrets.token_urlsafe(24)`），写回 `config.json`（权限 0600），并在启动日志打印一次。
  - 校验：`Authorization: Bearer <t>` **或** `X-Api-Token: <t>`。
  - 失败：`401 {"ok": false, "err": "unauthorized"}`。
  - **匹配规则**：`path == "/"`、`path.startswith("/vendor/")`、`path in ("/docs","/redoc","/openapi.json")` 直接放行；`path.startswith("/api/")` 或 `path == "/mcp"` 或 `path.startswith("/mcp/")` 要校验；其它路径（404 的杂项）也一并校验，避免泄露路由是否存在。
  - **`/api/login/captcha` 必须豁免**（实现时才发现，原设计漏了）：看板用 `<img src="/api/login/captcha">` 加载验证码图（`static/index.html:546`），**`<img>` 发不出自定义 header**。不豁免就是「第一次登录需要验证码 → 验证码图 401 → 永远登不上」。这一条只有跑起来看前端才发现，写进文档以防日后被"安全洁癖"改回去：验证码图本身不含账号数据，且它要求服务端 `_login_state` 里有待处理会话，泄露面为零。
  - **取消 `MIH_API_TOKEN=off` 之外的「测试默认关鉴权」**：测试用 `X-Api-Token` 显式带 header，不靠关中间件（关掉就等于不测它）。
  - **豁免**：`/`、`/vendor/*`、`/api/login/captcha`、`/docs`、`/redoc`、`/openapi.json`。**`/mcp*` 不豁免**。
  - 看板取 token：`/` 返回 `index.html` 时在 `<head>` 后注入 `<script>window.__MIH_TOKEN__="..."</script>`（原文里放一个 `<!--MIH_TOKEN-->` 占位注释用于替换）。前端 `api()` 与两处 `fetch()` 带上 `X-Api-Token` 头。**已知局限**（写进文档）：能访问 `http://127.0.0.1:8567/` 的本地进程仍可拿到 token，token 挡的是浏览器跨源请求（CSRF），不是本地恶意进程；要更强就绑 unix socket 或反向代理，不在本次范围。
  - 关闭方式：`MIH_API_TOKEN=off`（显式关掉中间件，测试与临时调试用）。
- **为什么 `/mcp` 也走它**：用户原以为 mcp 包自带鉴权——确认没有。`MCPServer` 的 `token_verifier` / `auth_server_provider` 是 OAuth 资源服务器那一套（要求实现 `OAuthAuthorizationServerProvider` 或 JWT 校验器），不是我们要的静态 token；用它反而要为一个本机自用工具引入 OAuth 流程。所以直接用应用层中间件，`/mcp` 与 `/api/*` 一条规则。
  - **`/mcp` 无尾斜杠会 307 到 `/mcp/`**（已实测）。中间件**不特判重定向**：它只做「这个前缀要 token」，所以无 token 打 `/mcp` 得到 **401 而不是 307**；带 token 时先拿到 307，客户端跟随到 `/mcp/` 才继续。这样不会出现"重定向绕开鉴权"的口子，代价是客户端必须**在第一次请求就带 header**（`dsh-mcp-client` 的 `headers` 是静态配置，满足）。
  - **实测记录**（`/tmp/mcptest/spike_auth_mcp.py`，中间件套在 `app.mount("/mcp", inner)` 外层）：
    ```
    POST /mcp   无 token            -> 401 {"ok":false,"err":"unauthorized"}
    POST /mcp/  无 token            -> 401
    POST /mcp   带 Bearer           -> 307（跟随到 /mcp/）
    POST /mcp/  带 Bearer + initialize -> 200 {"jsonrpc":"2.0","id":1,"result":{"capabilities":...}}
    GET  /api/thing 无 token        -> 401
    GET  /api/thing 带 X-Api-Token  -> 200
    ```
    结论：中间件确实罩住了 mount 进去的 MCP 子应用，`/api/*` 与 `/mcp` 一条规则可行，401 的 JSON 形状客户端当 HTTP 层错误处理。
  - MCP 客户端在 `initialize` 与每次 `tools/call` 都带 `Authorization: Bearer <t>`（`dsh-mcp-client` 的 `transport: streamable-http` + `headers`），服务端只做 header 校验，不涉及 MCP 协议层的认证协商。
  - 401 形状对 MCP 保持 JSON `{"ok": false, "err": "unauthorized"}`，MCP 客户端会把它当作 HTTP 层失败，不进入 JSON-RPC 解析。
- **`/docs`、`/openapi.json`：不加鉴权**（用户明确）。

### 5.4 MCP 装配

本节的参数不是猜的，全部在 VPS 上跑过 spike（FastAPI 0.162.0 + mcp 2.3.0 + Python 3.15）：`/api/*` 中间件 + `/mcp` 挂载同进程可行；`TestClient` 里 `/mcp/` 返回 200 且工具清单正确；`@app.middleware("http")` **确实会包住 `app.mount()` 的子应用**（实测日志打印了 mounted 路径）——这既是「`/mcp` 能被中间件鉴权」的依据，也意味着豁免必须按前缀显式写，漏写一个前缀就是漏一道门。

```python
# src/mihealth_api/mcp_server.py（新文件）
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

mcp = MCPServer("mihealth", instructions="小米运动健康本地库的只读查询工具。")

def build_app(allowed_hosts=("127.0.0.1", "localhost")):
    inner = mcp.streamable_http_app(
        json_response=True,          # 不需要 SSE 流
        stateless_http=True,         # 单机单用户，省一套会话状态
        streamable_http_path="/",    # 挂载点下必须写 "/"，否则变 /mcp/mcp
        transport_security=TransportSecuritySettings(
            allowed_hosts=[h for x in allowed_hosts for h in (x, f"{x}:*")] + ["testserver", "testserver:*"]),
    )
    return inner
```

- `stateless_http=True` + `json_response=True`：不需要 SSE、不需要会话恢复，响应是普通 JSON。
- **`streamable_http_path="/"` 是硬要求**（踩过：不写就成 `/mcp/mcp`）。
- **`transport_security` 的 `allowed_hosts` 必须按实际绑定地址生成，不能用通配 `["*"]`**：实测（`/tmp/mcptest/spike_host.py`）`allowed_hosts=["*"]` 对 `Host: 192.168.1.5:8567` 返回 **421**——它只支持精确匹配与 `host:*` 前缀两种形式，没有通配语义。所以 `build_app(allowed_hosts=(bind_host, "127.0.0.1", "localhost", "testserver"))`，由 `--host` 的值一起传进来。`enabled=False` 也能绕过，但那等于关掉 DNS-rebinding 防护，不做。
- **`enable_dns_rebinding_protection` 的默认值有陷阱**：SDK 在 `host in ("127.0.0.1","localhost","::1")` 时才自动开（`mcp/server/lowlevel/server.py:753`），其余情况**默认关**。显式传 `TransportSecuritySettings` 就统一了行为，不会因为换 `--host` 而静默改变安全姿态。
- lifespan：`FastAPI(lifespan=...)` 里 `async with mcp.session_manager.run(): yield`（挂载会关掉 MCP 自带 lifespan）。**不用 `with TestClient(app)` 直接 `TestClient(app)` 会抛 `RuntimeError: Task group is not initialized. Make sure to use run().`** —— 测试里必须写成 `with TestClient(app) as c:`，这条已实测。
- **客户端兼容性已实测**（`/tmp/mcptest/spike_real.py`，uvicorn 真起服务 + `mcp.client.streamable_http.streamable_http_client`）：`stateless_http=True` + `json_response=True` 下，`initialize` / `list_tools` / `call_tool` 全部成功，`/mcp` 与 `/mcp/` 两个 URL 都通（`/mcp` 靠客户端跟 307）。所以不需要 SSE，也不需要会话恢复。
- 工具函数都写成**同步函数**：SDK 的 `FuncMetadata.call_fn` 对同步函数走 `anyio.to_thread.run_sync`（`mcp/server/mcpserver/utilities/func_metadata.py:149`），同步读 SQLite 不会阻塞事件循环（已读源码确认）。

### 5.5 工具目录（7 个）

全部参数有类型注解和默认值；返回 `dict`。annotation 一律带 `read_only_hint`。

| 工具 | 入参 | 返回 | 说明 |
|---|---|---|---|
| `status` | — | `{ok, db_path, rows, raw_rows, mode, families: [...], cursors: {...}, sync: {running, started, result}}` | 库概览 + 同步任务当前状态 |
| `list_families` | — | `{ok, families: [{family, rows, first, last, keys: [{key, count, first, last}]}], sources: [...]}` | 有哪些数据族/键/时间范围 |
| `list_keys` | `family: str` | `{ok, family, keys: [...], sources: [...]}` | 单族键清单 |
| `query_records` | `family: str, key: str \| None = None, start_ms: int \| None = None, end_ms: int \| None = None, dedup: bool = True, sid: str \| None = None, order: str = "asc", limit: int = 500` | `{ok, family, count, dedup, items: [...]}` | 行查询；`limit` 上限 5000（防止把整库塞进上下文），默认 500 |
| `aggregate` | `family: str, key: str, field: str, agg: str = "sum", bucket_s: int = 3600, start_ms=None, end_ms=None, sid=None, dedup: bool = True` | `{ok, points: [{t, v}], count, bucket_s, agg}` | 服务端聚合，一次 SQL，不解码行 |
| `latest` | `keys: list[str] \| None = None, span_ms: int = 2592000000` | `{ok, items: {key: {key, time, sid, parsed}}}` | 每个键最新一条（默认 30 天窗口内） |
| `sync_now` | `hours: int = 72, families: list[str] \| None = None` | `{ok, started} \| {ok: false, err: "already running"}` | **唯一写工具**：触发增量同步，后台跑，立即返回；进度用 `status` 查 |

- 工具返回统一带 `ok`；异常在工具内 `try/except` 成 `{ok: false, err: "..."}`，不把栈吐给模型（SDK 自己会把未捕获异常包成 `Error executing tool ...`，那是给调试看的，不是给模型看的）。
- **没有本地库时**：工具返回 `{ok: false, err: "no db — 先跑 mihealth-sync backfill，或用 sync_now"}`，**不做云端实时回退**。理由：`/api/*` 的 live 回退要翻页、要 401 自愈、要把 `client` 拖进 MCP 层（§5.6 的依赖方向会被打破），而模型拿到一条明确的"库是空的，先同步"比拿到 300 页实时数据更有用。`sync_now` 是唯一能让库出现的路径，所以它必须在无库时也可用——`SyncJob` 自己会建库（`Syncer` 初始化即建 schema）。
- `sync_now` 的 annotation：`read_only_hint=False, destructive_hint=False, idempotent_hint=True`。
- 时间单位在**每个**工具的 docstring 里写明（`start_ms/end_ms` 是毫秒；返回的 `time` 是秒）。
- **返回体形状实测**（`/tmp/mcptest/spike_real.py`）：`json_response=True` 下 `call_tool` 的 `res.structured_content` 是 **`None`**，实际内容在 `res.content[0].text` 里，是格式化过的 JSON 字符串（`'{\n  "ok": true,\n  "text": "hi"\n}'`）。所以**不能**依赖 `structured_content` 取结果；`MCP.md` 里给客户端看的是"内容是 JSON 文本"，不是"结构化输出"。

### 5.6 store / client 的惰性化与同步任务收敛

**问题（builder-check 提出，成立）**：现在 `server.py:40-49` 在 import 时打开一次 `Store`，之后永不重开。后果是——先起网关、之后才 `mihealth-sync backfill` 建库，进程会一直 `_store is None` 地跑下去，`/api/db/*` 全 404，MCP 工具也永远是"没库"。`sync_now` 之后库里有了数据但工具还是看不到，这条链路直接不成立。

**解法：新增 `src/mihealth_api/runtime.py`**，把「可能没有、可能稍后才有」的东西收成惰性单例：

```python
def get_store():            # 有文件就打开并缓存；文件后出现会自动打开
def reset_store()           # 同步任务成功后调用，下次 get_store() 重开（读到新数据）
def get_client()            # 与现在 client_from_config(CFG) 等价，但可在登录后 reload
def get_sync_job(db_path, config_path=None)   # 进程级单例，按 (db_path, config_path) 缓存
class SyncJob:  @property state -> {running, started, result}
                def start(hours=72, families=None, workers=2) -> bool
```

- **为什么必须独立成模块**：`server.py` 要 `from .mcp_server import build_app` 才能挂 `/mcp`，而 `mcp_server.py` 的 7 个工具又要读库。让 `mcp_server` 反过来 import `server` 就是循环依赖。两边都 import `runtime`，依赖是单向的：`server → mcp_server → runtime`、`server → runtime`。
- **惰性打开必须线程安全**：`get_store()` 加模块级 `threading.Lock`，只在首次打开时持有。MCP 工具跑在线程池里（§5.4），并发进 `get_store()` 是常态。
- **`reset_store()` 的调用点**：`SyncJob` 任务成功后、`/api/login/import_device` 成功后、`/api/login` 成功后（这些都会让库内容或凭据变化，现行的 `_cache.clear()` 顺带一起清）。
- **`status` 工具与 `/api/health` 复用同一份 `get_store()`**，不再各持一个连接。
- **测试写法不用变**：`tests/test_server.py:126-136` 现在 reload 后赋 `SV.client` / `SV._store`；迁移后改为赋 `runtime` 模块的私有缓存（或直接 `monkeypatch.setattr(runtime, "_store", Store(...))`），语义一样。

### 5.7 模块布局（增量）

```
src/mihealth_api/
  server.py       # 改：Flask → FastAPI；路由全量迁移；挂 /mcp；auth 中间件
  mcp_server.py   # 新增：MCPServer 实例 + 7 个工具 + build_app()
  runtime.py      # 新增：get_store/get_client/get_sync_job/SyncJob（惰性 + 进程级单例）
  store.py        # 改：新增 to_csv_string()（供 /api/export.csv 与脚本复用）
  ...（其余不动）
```

`server.py` 预计从 874 行涨到 ~950 行（多了 `@app.get` 装饰器和异常处理器），可接受；不在本次拆分成 `routers/` 包。**`jobs.py` 不单独立文件**：`SyncJob` 只有 `runtime.py` 里 ~40 行，与 `get_store()` 同属「进程内共享的活对象」，合在一起比拆两个文件更清楚。

### 5.8 依赖

```toml
dependencies = ["fastapi>=0.162", "uvicorn>=0.54", "pycryptodome>=4.3", "requests>=2.34"]  # 去掉 flask
[project.optional-dependencies]
mcp = ["mcp>=2.3.0"]
[dependency-groups]
dev = ["pytest>=9.0", "pytest-cov>=7.0", "httpx>=0.28", "mcp>=2.3.0"]   # mcp 同时进 dev
```

- 运行时 `uv sync` 默认不装 mcp；`uv sync --extra mcp` 才装。未装时 `/mcp` 不注册，启动打印 `[mcp] disabled — install with: uv sync --extra mcp`。
- **但 `mcp` 必须在 dev 组里也有一份**（builder-check 指出）：§6 要求 MCP 代码进覆盖率统计、`tests/test_mcp_server.py` 必须真跑。若只放在 optional extra，`uv sync` 的开发者跑测试时要么 collect error，要么得用 `importorskip` 静默跳过——后者会让「334 passed」变成一个假数字，覆盖率也不会算 MCP。放 dev 组 = 开发和 CI 默认就有，运行时可选择不装。
- 实测：fastapi 0.162.0 与 mcp 2.3.0 依赖的 starlette 都是 **1.10.0**，同进程无版本冲突。
- **体积账（实测 `uv pip install fastapi uvicorn pycryptodome requests`）**：19 个包。相比现在的 Flask 那套（flask/werkzeug/jinja2/markupsafe/blinker/itsdangerous + 现有 13 个）净增约 8 个，其中 `pydantic` + `pydantic-core` 是大头（`pydantic-core` 是编译扩展）。`opentelemetry-api` 是 **fastapi 0.162 自己拉进来的**（不是 mcp），这点值得知道——如果觉得这依赖来路不明，可以在 README 里说明它是 fastapi 的依赖而非本项目的可观测性选择。
- 已装 mcp 的人额外多 28 个包（含 `httpx2`、`pyjwt`、`cryptography`、`sse-starlette`、`rpds-py`）。这就是为什么它必须是 optional 而非常规依赖。
- **验收口径**：`uv sync` + `uv run pytest` 全绿（含 MCP 用例，因为 dev 组有 mcp）；「334 个原有用例全部通过」是硬指标，新用例另算（预期总数 360+）。

## 6. 测试计划

改造现有三个文件、新增三个：

| 文件 | 变化 |
|---|---|
| `tests/test_server.py` | `SV.app.test_client()` → `TestClient(SV.app)`；helper `get`/`post` 统一加 `X-Api-Token`；`.get_json()` → `.json()`（**53 处**）、`.data` → `.content`（**2 处**）；`importlib.reload(server)` 仍需配合 `chdir` + `MIH_CONFIG`/`MIH_DB`；`test_main_defaults`/`test_main_flags` 改打桩 `uvicorn.run` |
| `tests/test_server_live.py` | 同上（**23 处** `.get_json()`、**1 处** `.data` 断言 JPEG magic） |
| `tests/test_misc.py` | **1 处** `.get_json()`（`/api/latest` 那个用例）——容易漏，写在表里 |
| `tests/test_auth_middleware.py`（新） | 无 token 401、Bearer 与 `X-Api-Token` 两种写法、豁免路径（`/`、`/vendor/*`、`/api/login/captcha`、`/docs`、`/redoc`、`/openapi.json`）、**`/mcp/` 与 `/mcp` 无 token 也 401**、`/mcp` 有 token 时返回 307（重定向发生在放行之后）、`MIH_API_TOKEN=off` 全放开、index.html 注入 token、`HTTPException(detail=dict)` 解包形状 |
| `tests/test_mcp_server.py`（新） | 用 `with TestClient(app) as c:`（**裸 `TestClient(app)` 会抛 `RuntimeError: Task group is not initialized`**）+ `Authorization: Bearer <token>` 打 `/mcp/`：`tools/list` 恰好 7 个工具且名字/注解正确；`tools/call` 逐个工具打真实 SQLite（tmp 库）；`sync_now` 在 job 已在跑时返回 `{ok: false, err: "already running"}`；`sync_now` 正常路径只断言"启动了"并等 job 结束（打桩 Syncer，不发请求）；`/mcp`（无尾斜杠）307 → `/mcp/`；**无 header 时 401 而不是 421/307**；`res.structured_content is None`、内容在 `res.content[0].text` 的 JSON 字符串里 |
| `tests/test_runtime.py`（新） | `SyncJob` 并发只跑一个、`state` 形状、异常进 `result.err`、失败后 `running` 复位；`get_store()` 惰性打开（先无库后建库能读到）、线程安全、`reset_store()` 后重开 |

- 覆盖率目标：**维持 ≥96%**（新代码一起算）。MCP 模块里的 `build_app` 与 7 个工具都要被覆盖。
- 不写联网用例；`-m network` 那套不动。

## 7. 文档计划

1. **`MCP.md`（新，本次的核心交付物）** —— 结构对齐 `API.md`：
   - §1 传输与端点（Streamable HTTP，`http://127.0.0.1:8567/mcp`，`stateless`、`json_response`）
   - §2 鉴权（`/api/*` 与 `/mcp` 都要 token；两种 header 写法；`MIH_API_TOKEN=off`；本地进程仍可读配置文件的局限）
   - §3 工具目录（7 个，逐个给入参表、返回示例、时间单位）
   - §4 典型用法（"最近三天步数"、"这周心率最高"、"现在库里有什么"三个完整对话式示例）
   - §5 接入配置（`dsh-mcp-client` 插件 YAML 片段，`transport: streamable-http` + `url` + `headers: {Authorization: "Bearer <token>"}`）
   - §6 错误与限流（`{ok: false, err}`、`limit` 上限 5000、`sync_now` 的 already-running）
   - §7 与 HTTP API 的对应关系（哪个工具对应 `/api/db/*` 哪条）
2. `README.md`：命令不变；「接口文档」小节加 MCP.md 入口；目录树加 `mcp_server.py`/`runtime.py`/`MCP.md`；「依赖」段说明 `--extra mcp`；测试数字更新。
3. `API.md` §7：Flask → FastAPI 表述；新增 `/api/*` 需要 `X-Api-Token`；`?limit=abc` 现在 422；`/docs` 入口；`/api/export.csv` 不再留临时文件。
4. `src/mihealth_api/__init__.py` docstring：去掉 "Flask"。
5. `NOTES.md`：不动（它记的是云端接口逆向，与本次无关）。

## 8. 交付顺序

1. 依赖与骨架：`pyproject.toml` 换依赖、`runtime.py`、`store.to_csv_string()`。
2. `server.py` 全量迁 FastAPI + auth 中间件；跑通 `tests/test_server*.py`。
3. `mcp_server.py` + 挂载；`tests/test_mcp_server.py`。
4. `MCP.md`、README/API.md/`__init__` 更新。
5. 全量测试 + `uv build` + 双端验证（VPS 干净克隆、Windows 真库跑一次 `mihealth-api` 与 MCP 握手）。
6. 提交（尾行 `协作模型: deepseek-v4.1-flash`）并推 `git@github.com:kmizmal/mihealth-api.git`。

## 9. 留给实现者的判断

- `query_records` 的 `limit` 上限定 5000 还是 2000（看上下文预算）。
- 工具名是否加前缀（`mcp__mihealth__` 那种由宿主加，仓库内不加）。
- `status` 里要不要把 `token_prefix` 这类敏感字段带上（我倾向不带；库路径可以带）。
- `sync_now` 的 `families` 只允许 `fitness`/`sport`（同步器当前只支持这两个），还是也让 `medical`/`project` 走透传族。
- 迁移中若发现某条路由的旧行为本身就错（比如 `/api/daily_goals` 的窗口口径），**不在本次修**，记进 `NOTES.md` 另开一轮。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| 迁移漏改导致看板静默不显示 | 39 条路由逐条对照表；用测试覆盖 + 真库手工开一次看板 |
| `?limit=abc` 从静默变 422 破坏外部调用 | 空串/缺省行为**完全不变**（只改非法值）；写进 API.md「行为变化」；影响面只有直接手敲 URL 的场景 |
| 网关先起、库后建，进程一直看不到库 | `runtime.get_store()` 惰性打开 + `reset_store()`（§5.6） |
| mcp 未装时开发者跑测试静默跳过、覆盖率假高 | mcp 同时进 dev 组（§5.8） |
| 第一次登录要验证码、验证码图被鉴权拦 | `/api/login/captcha` 豁免（§5.3） |
| `--host 0.0.0.0` 后 `/mcp` 从局域网 421 | `allowed_hosts` 按实际绑定地址生成（§5.4） |
| MCP 端点泄露 | 与 `/api/*` 共用 token 中间件（§5.3）；默认绑 127.0.0.1；对外暴露必须加反代 |
| 两处同步器抢写 | `runtime.get_sync_job()` 进程级单例锁（§5.6） |
| `/mcp/mcp`、421 Host 头这类装配坑 | 已在 §5.4 固化参数，`test_mcp_server.py` 覆盖 |
