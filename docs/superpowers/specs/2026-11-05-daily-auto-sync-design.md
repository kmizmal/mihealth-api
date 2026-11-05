# 每日自动增量同步 · 设计 + 交接

> **这份文档是给一个新会话用的。** 上一个会话负责把它写出来，但上下文已满，实施由接手会话完成。
> 读完这一份应该不需要再翻历史对话。所有结论都附了行号或命令，可自行复核。

- 写于：2026-11-05
- 仓库：`github.com/kmizmal/mihealth-api`，分支 `main`，写作时 HEAD = `d5a673b`
- 用户原话：
  - 需求（m03912）："编写交接文档,在后端补上每天自动增量更新的逻辑"
  - 选型（m03966）："A,编写交接文档.我要新开一个会话做,这个会话上下文要炸了"

---

## 1. 一句话

网关（`mihealth-api` 进程）现在**永远不会自己同步**——必须有人点看板上那个「同步」按钮，或手动跑 `mihealth-sync incremental`。
要让它在**后端自己每天跑一次增量**，方案已定为 **A：进程内 asyncio 调度，挂在 FastAPI 的 `lifespan` 上**。

---

## 2. 现状：为什么现在不自动（已实测，勿重复调查）

| 检查点 | 结论 | 证据 |
|---|---|---|
| 系统级定时任务 | 无 | Windows `Get-ScheduledTask` 无 mihealth 相关；`HKCU:\Software\Microsoft\Windows\CurrentVersion\Run` 无；启动项文件夹为空；无同名 Windows 服务 |
| 代码里有调度器吗 | 没有 | 全仓 `grep -rn "Timer\|schedule\|BackgroundTasks"` 无命中 |
| `sync.py` 那句 "cron/任务计划" | **只是注释，没实现** | `src/mihealth_api/sync.py:11` 的 `After that: mihealth-sync incremental  (cron/任务计划, a handful of requests)` |
| README 那句「之后定时增量」 | **同样没有落地步骤** | `README.md:63` |
| 唯一的同步触发点 | `POST /api/sync` | `src/mihealth_api/server.py:351 api_sync_run()` → `_job().start(hours=72)` |
| 前端定时器会不会触发同步 | 不会 | `src/mihealth_api/static/index.html:584` 的 `setInterval(loadAll,300000)` 只重画看板、只读本地库，**不调 `POST /api/sync`** |
| 用户在 Windows 上的实际状态 | 网关常驻但从不自动同步 | `mihealth-api` 进程 PID 32140，启动于 2026-11-05 16:56:42（手工启动，非开机自启） |

**结论：用户说的「每天自动增量更新」目前完全不存在，是从零加。**

---

## 3. 已经存在的、应该复用的东西

不要新写同步器。整条链路已经通了，只缺「谁来按时按一下按钮」。

```
runtime.get_sync_job(db, cfg)        runtime.py:225   进程级单例
  └── SyncJob.start(hours=72)        runtime.py:176   起 daemon 线程，立刻返回；已在跑返回 False
        └── SyncJob._run()           runtime.py:185
              └── Syncer.incremental(hours)  sync.py:205
        └── SyncJob._finish(result)  runtime.py:204   写 result + reset_store() 让新数据可见
```

关键性质（都已在 `tests/test_runtime.py` 里被 19 条用例锁死）：

- **单例按 `job_key(db, cfg)` 归一化**（`runtime.py:212`）。`config.json` 里 `db` 常是 `null`，所以 `None` 必须在这里被归一化成真实路径，否则 HTTP 与 MCP 会各持一把锁——这个 bug 已经踩过一次（提交 `d5a673b`）。
- `start()` 是**非阻塞**的：起线程就返回，不会卡住调用它的协程。
- 重复 `start()` 返回 `False`，不会并发写库。
- `SystemExit(2)` 被映射成 `{"ok": false, "err": "auth/credentials (2)"}`（凭据全废时的表现）。

**所以调度器要做的事只有一件：每天到点调一次 `get_sync_job(...).start(hours=72)`。** 不要碰锁、不要碰线程、不要自己开 Syncer。

---

## 4. 目标 / 非目标

**目标**
1. 网关进程活着时，每天自动跑一次 `incremental(hours=72)`。
2. 与手动按钮、MCP `sync_now` **共用同一个 `SyncJob`**——任何时刻最多一个同步在跑。
3. 可通过环境变量关闭（**测试必须能关**，理由见 §8）。
4. 错过的时间窗能补：网关关了几天，重开后要么立刻补跑，要么清楚知道下次何时跑。
5. 不引入任何新依赖（只要标准库 `asyncio` / `datetime`）。

**非目标**
- ❌ 不做系统级定时任务（`schtasks` / systemd timer）。那是方案 B，用户已否。**但要在 README 里留一句"网关不能常开时用 `schtasks` 跑 `mihealth-sync incremental`"作为备选说明**（见 §10 文档任务）。
- ❌ 不做全量回填（那是 `mihealth-sync backfill`）。
- ❌ 不改 `SyncJob` 的内部语义。
- ❌ 不做「同步失败自动重试 N 次」——失败就记日志，等下一次。

**反目标（这样就算失败）**
- 测试跑起来真的往云端发请求。
- 自动同步与手动按钮撞车，出现两个 Syncer 同时写库。
- 网关启动日志里看不出「自动同步开着没有、下次几点」。

---

## 5. 设计

### 5.1 新模块 `src/mihealth_api/scheduler.py`

单独一个文件，理由是：**纯决策逻辑要能脱离 FastAPI 和 asyncio 单测**（本仓覆盖率守到 96%，新模块不能拖后腿）。

```python
# 纯函数，无 IO，最好测
def parse_at(spec: str) -> tuple[int, int] | None
    """'04:00' -> (4, 0)；'off'/'none'/'' -> None（表示禁用）；格式非法抛 ValueError。"""

def next_run_at(now: float, hour: int, minute: int) -> float
    """返回 >= now 的下一个 HH:MM 时间戳（本地时区，秒）。
    注意 now 正好落在 HH:MM 上时返回 now 本身还是 +1 天——由实现者定，
    建议返回 now（"刚到点就跑"），并用测试固定住。"""

def should_catch_up(last_ts: float | None, now: float, gap_s: float = 20 * 3600) -> bool
    """启动时判断要不要立刻补跑。
    last_ts 为 None（从未跑过）-> True
    now - last_ts >= gap_s      -> True
    否则 False。"""

# 带 IO 的部分
class DailySyncer:
    at: tuple[int, int]                 # (hour, minute)，已解析
    catchup_gap_s: float

    def __init__(self, at="04:00", catchup_gap_s=20*3600):
        """at 接受 '04:00' / (4, 0)；'off' 不该走到这里（由 from_env 拦掉）。"""

    def describe_next(self) -> str
        """给启动日志用，例如 '下次 2026-11-06 04:00'。
        纯展示，出错也不能抛（包一层 try，异常时返回 '下次时间未知'）。"""

    def due_now(self) -> bool           # 读库里的 last_ts，用 should_catch_up 判断
    def mark_ran(self, ts: float)       # 写回 last_ts
    async def run(self) -> None         # 无限循环，被 lifespan 包在 task 里

def from_env(env=None) -> DailySyncer | None
    """读 MIH_DAILY_SYNC（默认 '04:00'）；off/none/'' 返回 None。
    解析失败（如 '25:00'）时**不要崩网关**：打印一行警告并返回 None。
    env 参数默认 os.environ，方便测试注入而不用 monkeypatch。"""
```

**`describe_next()` / `from_env()` 都是上面 §5.3 代码里直接用到的，必须实现**，别漏。

### 5.2 持久化：复用 `sync_state` 表

`Store` 已有 KV 接口，不要新建表：

- `store.set_state(name, last_ts=...)` — `src/mihealth_api/store.py:181`
- `store.get_state(name, field="last_ts")` — `src/mihealth_api/store.py:197`
- `store.states()` — `src/mihealth_api/store.py:202`

用 **name = `"auto_daily"`**，`last_ts` 存「上次自动同步启动的 unix 秒」。

> ⚠️ **已知副作用，必须写进文档**：`mihealth-sync status` 会把 `sync_state` 里所有行当成 cursor 打印（`sync.py:318 status()` 遍历 `db.states()`），于是 `cursors:` 列表里会多出一行 `auto_daily`。`/api/sync/status` 的 `cursors` 与 MCP `status` 工具的 `cursors` 同理（`mcp_server.py:62`）。
> 处理方式二选一，**推荐「保留可见」**——它本身是有用信息（"上次自动同步是什么时候"），只需在文档里写明这一行是什么。若要隐藏，就在 `status()` 里过滤掉非 `wm_` 前缀的名字，但那会动到既有输出格式，需要同步改测试。
>
> 另一个备选：用 `updated_at` 列而不是 `last_ts`（两列都在表里），语义更贴切，但 `set_state` 没有暴露 `updated_at` 入参，得加参数。**除非遇到麻烦，不要走这条路。**

### 5.3 挂进 lifespan

改 `src/mihealth_api/server.py:224` 的 `lifespan`。现在长这样：

```python
async def lifespan(app):
    tok = init_token()
    ...
    _st = runtime.get_store()
    print(f"[gateway] db={...}", flush=True)
    if MCP_APP is not None:
        from .mcp_server import mcp as _mcp
        async with _mcp.session_manager.run():
            yield
    else:
        yield
```

要改成：**先起调度 task，再进 MCP 的 session manager，退出时取消 task**。

```python
async def lifespan(app):
    ...原有 token / db 打印...
    task = None
    syncer = scheduler.from_env()          # MIH_DAILY_SYNC 解析；off 时返回 None
    if syncer is not None:
        task = asyncio.create_task(syncer.run(), name="daily-sync")
        print(f"[auto] 每日自动同步已启用（{syncer.at}，{syncer.describe_next()}；关闭用 MIH_DAILY_SYNC=off）", flush=True)
    else:
        print("[auto] 每日自动同步已关闭（MIH_DAILY_SYNC=off）", flush=True)
    try:
        if MCP_APP is not None:
            from .mcp_server import mcp as _mcp
            async with _mcp.session_manager.run():
                yield
        else:
            yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
```

要点：
- `asyncio.create_task` 必须在 `yield` 之前、且在事件循环里。
- 必须 `finally` 里 cancel，否则 `TestClient` 退出时会留下 pending task 警告。
- 打印的**那两行日志是验收标准**，不能省。

### 5.4 调度循环

```python
async def run(self):
    # 1) 启动时先判断补跑
    try:
        if self.due_now():
            print("[auto] 距上次自动同步超过 20 小时，立即补跑一次", flush=True)
            self._kick("catch-up")
    except Exception as e:
        print(f"[auto] 补跑判断失败，跳过: {e}", flush=True)

    # 2) 之后每天定点
    while True:
        delay = max(60.0, next_run_at(time.time(), *self.at) - time.time())
        await asyncio.sleep(delay)
        self._kick("scheduled")

def _kick(self, why):
    job = runtime.get_sync_job(...)        # 与 server._job() 同一个 key
    if not job.start(hours=72):
        print(f"[auto] {why}: 已有同步在跑，跳过", flush=True)
        return
    self.mark_ran(time.time())
    print(f"[auto] {why}: 已启动增量同步（hours=72）", flush=True)
```

- **`job.start()` 不要 await**，它本身就非阻塞。
- 若要「跑完记日志」，可以在 `_kick` 里再 `await asyncio.sleep(5)` 后读 `job.state`——**不推荐**，会让一次同步占用协程几十秒，收益不大。日志交给同步器自己打（`sync.py:42 log()`）。
- 捕获所有异常，**绝不能让调度 task 死掉**：整个 `while True` 包 `try/except Exception: print(...) ; await asyncio.sleep(60)`。
- **取 job 的正确写法**（唯一正确，别自创）：`job = runtime.get_sync_job(runtime.get_config().get("db"), runtime.get_config().get("config_path"))`。
  等价于 `server.py:332 _job()` 的一行版。`job_key()` 会在内部把 `None` 归一化成 `paths.db_path()` / `paths.config_path()`，所以千万别在外面先 `or paths.db_path()` 再传（那样反而不会出错，但也别两边写法不一致——踩过一次就够）。
- **`SyncJob` 的默认并发是 `workers=2`、`min_interval=0.5`**（`runtime.py:162`），比 CLI 的 `--workers 3` / `0.4` 更保守。这是 `get_sync_job()` 建的实例，**不要**在调度器里另建 `SyncJob` 去改这两个值。`/api/sync` 与 MCP `sync_now` 用的也是这份默认值，保持三者一致。

### 5.5 环境变量

| 变量 | 默认 | 取值 | 说明 |
|---|---|---|---|
| `MIH_DAILY_SYNC` | `04:00` | `off` / `HH:MM` | `off` 或 `none` 完全禁用；`HH:MM` 为本地时区定点 |
| （常量）`CATCHUP_GAP_S` | `20 * 3600` | 代码内常量 | 超过这个间隔就在启动时补跑。不必做成环境变量，除非用户要求 |

用环境变量而非 `config.json` 字段，理由有两个：① 测试里 `monkeypatch.setenv` 最干净；② 这是「进程行为」不是「账号配置」，不该跟凭据混在一个文件里（`config.json` 已经要存 token 了）。

### 5.6 待用户确认的一问（**接手会话必须问，或按推荐默认执行并明说**）

上个会话问过、用户还没答：

> **网关停着的那几天，重新起来后是「立刻补跑一次」，还是「等下一个 04:00」？**

- **推荐：立刻补跑**（本文档 §5.1 的 `should_catch_up` 就是按这个写的）。理由：用户要的是「数据始终最新」，等下一个 04:00 意味着最多再陈旧一整天，与需求相悖；开机多几次云端请求的代价远小于数据陈旧。
- 若用户选「等下一个 04:00」：把 `run()` 里的第 1 步整段删掉，`should_catch_up` / `due_now` 也一并删掉（连同它的测试），代码会少 20 行。
- 另外两个默认值可直接用、不必再问：**定点 04:00**、**`hours=72`**。

---

## 6. 实施清单（按顺序）

| # | 文件 | 动作 |
|---|---|---|
| 1 | `src/mihealth_api/scheduler.py` | 新建。§5.1 的纯函数 + `DailySyncer`。模块 docstring 用中文，说清「为什么不用 crontab」（网关未必常开 → 用进程内；网关不常开时用 schtasks 兜底）。 |
| 2 | `src/mihealth_api/server.py` | 改 `lifespan`（§5.3）。现有 import 在文件头 `:35-47`：已有 `import os` / `secrets` / `time`、`from contextlib import asynccontextmanager`（`:39`，被 `:223` 的 `@asynccontextmanager` 用着）。**要新增的只有 `import asyncio`，以及把 `:39` 改成 `from contextlib import asynccontextmanager, suppress`。** |
| 3 | `tests/test_scheduler.py` | 新建。§8 列出的用例。 |
| 4 | `tests/conftest.py` | 加 autouse fixture 设 `MIH_DAILY_SYNC=off`（**不做这一步，§8 的坑会炸**）。 |
| 5 | `tests/test_auth_middleware.py` | 检查 `app` fixture 是否需要在 reload 前显式设 `MIH_DAILY_SYNC=off`（它 `importlib.reload(SV)`，env 在 lifespan 启动时才读，conftest 的 autouse 够用；但显式写一行更保险）。 |
| 6 | `README.md` | §「同步与存储」表加一行「每日自动增量」；`README.md:63` 那句「之后定时增量」改成实话（现在真的会定时了，且说明 `MIH_DAILY_SYNC=off` 可关、网关不常开时用 `schtasks` 兜底）。 |
| 7 | `API.md` | §7 的 FastAPI 表加一行 `MIH_DAILY_SYNC`；`/api/sync/status` 返回体若因 `auto_daily` 游标变化，补一句说明。 |
| 8 | `MCP.md` | 工具目录里 `status` 那条提一句「`cursors` 里会看到 `auto_daily`，是自动同步的上次运行时间」。 |
| 9 | `pyproject.toml` | **不用改**。不引入新依赖，版本号是否 bump 到 0.3.1 由接手者定（建议 bump）。 |

---

## 7. 代码位置索引（省去重新探索）

| 符号 | 位置 |
|---|---|
| `lifespan` | `src/mihealth_api/server.py:224` |
| `app = FastAPI(...)` | `src/mihealth_api/server.py:241` |
| MCP 挂载块 | `src/mihealth_api/server.py:267-276` |
| `_job()` | `src/mihealth_api/server.py:332` |
| `api_sync_run` | `src/mihealth_api/server.py:351` |
| `api_sync_status` | `src/mihealth_api/server.py:339` |
| `main()`（CLI，`--host/--port/--reload`） | `src/mihealth_api/server.py:1067`（`import uvicorn` 在 `:1070`） |
| `SyncJob` | `src/mihealth_api/runtime.py:155` |
| `SyncJob.start` | `src/mihealth_api/runtime.py:176` |
| `job_key` / `get_sync_job` | `src/mihealth_api/runtime.py:212` / `:225` |
| `Store.set_state` / `get_state` / `states` | `src/mihealth_api/store.py:181` / `:197` / `:202` |
| `Syncer.incremental` | `src/mihealth_api/sync.py:205` |
| `Syncer.status` | `src/mihealth_api/sync.py:318` |
| MCP `status` 工具 | `src/mihealth_api/mcp_server.py:62` |
| MCP `sync_now` 工具 | `src/mihealth_api/mcp_server.py:215` |
| 前端同步按钮 | `src/mihealth_api/static/index.html:532` |

**codegraph 可用**（先 `export PATH="/root/.local/share/pnpm/bin:$PATH"`，二进制在 `/root/.local/share/pnpm/bin/codegraph`，1.6.2）。索引在 `/root/dsh/_repo/.codegraph/`。
> 注意：`codegraph sync` 曾报 `✗ Failed to sync: unable to open database file`（沙箱对 `.codegraph/codegraph.db` 的写权限），但 **`explore` / `node` 读是好的**。别在这上面纠缠超过 2 分钟，直接 `codegraph node <符号>` 拿原文，必要时再 `read`。

---

## 8. 测试计划

### 8.1 必测（新建 `tests/test_scheduler.py`）

纯函数（无 IO，秒级）：
- `parse_at("04:00")` → `(4, 0)`；`parse_at("4:5")` → `(4, 5)` 或抛（**定一个，写测试固定住**）
- `parse_at("off")` / `"none"` / `""` → `None`
- `parse_at("25:00")` / `"abc"` → `ValueError`
- `next_run_at(now=今天 03:00, 4, 0)` → 今天 04:00
- `next_run_at(now=今天 05:00, 4, 0)` → 明天 04:00
- `next_run_at(now=正好 04:00:00, 4, 0)` → 固定住边界语义
- `should_catch_up(None, now)` → True
- `should_catch_up(now - 21h, now)` → True
- `should_catch_up(now - 19h, now)` → False
- `should_catch_up(now - 3d, now)` → True

带 IO：
- `DailySyncer` 在 `MIH_DAILY_SYNC=off` 时不产生调度器（`from_env()` 返回 None）
- `mark_ran` / `due_now` 走 `sync_state` 的 `auto_daily` 行，来回一致
- 库不存在时 `due_now()` 不抛（应视为「从未跑过」→ True）

集成（用假 job，不碰网络）：
- `_kick` 在 `job.start()` 返回 True 时调了 `mark_ran`，返回 False（已在跑）时**不**调 `mark_ran`
- 用一个假的 job 对象 + 把 `at` 设成「1 秒后」跑一轮，断言 `start(hours=72)` 被调用一次

> **不要写「等真实时间到 04:00」的测试。** 一律注入 `now` 或把间隔压到 1 秒。

### 8.2 必须防的坑：现有测试会真的发请求

`tests/test_auth_middleware.py` 里有 **19 处 `with TestClient(app.app) as c:`**（从第 51 行起；`grep -c` 实测 19）。
`tests/test_server.py:152` 的 `_client()` 虽然也造 `TestClient`，但**从不在 `with` 里用**（`grep -rn "with _client" tests/` 零命中），所以那些用例不跑 lifespan——`test_server.py:138` 的注释写明了这一点：「lifespan 只在 with TestClient(...) 时跑；这些用例不走 lifespan，直接置 token」。

**`with TestClient(app)` 会跑 lifespan。** 也就是说一旦调度器默认开启，那 19 处会各自起一个真实的每日同步线程 → 测试去连云端、打真凭据、跑几十秒、覆盖率结果乱掉。

**防线（两道，都要）：**
1. `tests/conftest.py` 加
   ```python
   @pytest.fixture(autouse=True)
   def _no_daily_sync(monkeypatch):
       monkeypatch.setenv("MIH_DAILY_SYNC", "off")
   ```
2. 实施后**故意验证一次防线有效**：临时把 conftest 那行注释掉，跑 `pytest tests/test_auth_middleware.py -x`，应该能看到 `[auto] 每日自动同步已启用` 并且**变慢/失败**；确认后恢复。这是唯一能证明"测试不会偷偷同步"的办法。

另外注意：`test_auth_middleware.py:29` 的 `app` fixture 用 `importlib.reload(SV)` 重建模块。`lifespan` 读 env 的时机是「进入 lifespan 时」，不是 import 时，所以 conftest 的 autouse 已经生效——但如果你把 env 读取提到模块级常量，就会被 reload 语义坑到（reload 时会重新读，反而是对的）。**建议 env 就在 `from_env()` 里读，别缓存成模块常量。**

### 8.3 回归底线

实施完成后，`cd /root/dsh/_repo && .venv/bin/python -m pytest tests/ -q` 必须 **≥ 403 passed**（写作时是 403，新增用例后应变多），覆盖率 **不低于 96%**（当前 TOTAL 2255 stmts / 96 miss / 96%，`runtime.py`、`store.py`、`paths.py`、`auth.py`、`__init__.py` 都是 100%）。

新增的 `scheduler.py` 必须也守 96% 以上——纯函数多、分支少，做得到。

---

## 9. 验收

### 9.1 VPS 侧（`/root/dsh/_repo`）

```bash
export UV_CACHE_DIR=/root/dsh/.uvcache UV_PYTHON_INSTALL_DIR=/root/dsh/.uvpython UV_TOOL_DIR=/root/dsh/.uvtools
cd /root/dsh/_repo && .venv/bin/python -m pytest tests/ -q
```

**端到端验证（必须做，光跑单测不算）。好消息：不用等时间。**

因为 §5.1 的 `should_catch_up` 在 `last_ts is None`（库里没有 `auto_daily` 行）时返回 True，所以**用一个全新的空库起网关，补跑分支立刻就会触发**——天然就是 e2e 的触发手段，不需要 monkeypatch 时间、也不需要 `+30s` 这种污染生产语义的语法。

步骤：
1. `cd /tmp && mkdir -p e2e_auto && cd e2e_auto`（干净的 cwd → 干净的 `mihealth.db`），放一份假凭据 `config.json`（抄 `tests/conftest.py` 的 `cfg_file` 内容即可）。
2. `MIH_API_TOKEN=e2e-token MIH_DAILY_SYNC=04:00 /root/dsh/_repo/.venv/bin/mihealth-api --port 18811`
3. 看启动日志，应依次出现：

```
[auto] 每日自动同步已启用（04:00，... ；关闭用 MIH_DAILY_SYNC=off）
[auto] 距上次自动同步超过 20 小时，立即补跑一次      ← 新库里没有 last_ts，走这条
[auto] catch-up: 已启动增量同步（hours=72）
```

4. 紧接着（同步会因假凭据失败，这正是预期）：
   `GET /api/sync/status`（带 `X-Api-Token: e2e-token`）→ 先看到 `running: true`，几秒后看到 `result: {"ok": false, "err": "auth/credentials (2)"}`。
5. 确认 `sync_state` 真被写了：`sqlite3 mihealth.db "select * from sync_state where name='auto_daily'"` 应有一行。
6. 再起一次（库已存在、`last_ts` 刚写过）→ **不应**再出现「立即补跑」那行。这一步是验证 `due_now()` 真的读了库，而不是永远返回 True。
7. 想验证 `scheduled` 分支（定点触发）而不等一天：临时 `MIH_DAILY_SYNC=$(date -d '+2 minutes' +%H:%M)` 起网关，等两分钟看 `[auto] scheduled: ...`。**这是唯一需要等时间的场景，可选做。**

### 9.2 Windows 侧（`D:\Tools\mihealth-api`）

```powershell
cd D:\Tools\mihealth-api
git pull --ff-only
uv sync
uv run pytest -p no:cacheprovider
uv run mihealth-api --port 18575
```
启动日志必须出现 `[auto] ...` 那行。然后**等一次真实触发**（用 §9.1 的测试手段压时间），确认真库有新行写入、`/api/sync/status` 能看到结果、看板右上角时间刷新。

### 9.3 验收口径

- [ ] `pytest` 全绿，覆盖率 ≥ 96%
- [ ] 启动日志有 `[auto]` 行，能看出「启用/关闭 + 下次时间」
- [ ] `MIH_DAILY_SYNC=off` 时那行显示「已关闭」，且确实不跑
- [ ] 手动点按钮与到点自动触发**不会并发**（一个在跑时另一个被 `start()` 拒绝并打日志）
- [ ] `sync_state` 里出现 `auto_daily` 行，重开网关后 `due_now` 判断正确（关机超过 20h → 立刻补跑）
- [ ] 清空 `auto_daily` 行模拟「首次」→ 启动时补跑一次
- [ ] 原有 403 条用例一条不少地通过

---

## 10. 文档任务（容易被漏）

1. **`README.md:63`** 现在是「① 之后定时增量（水位流 + 时间窗补拉，通常只需个位数请求）」，读者会以为已经自动了——改成实话：网关会每天自动跑，`MIH_DAILY_SYNC` 控制。
2. **`README.md` §同步与存储表** 加一行：

   | 每日自动增量 | 网关进程内每天定点（默认 04:00）自动跑一次增量，与手动按钮共用同一把锁；`MIH_DAILY_SYNC=off` 关闭 |
3. **网关不能常开时的兜底**（方案 B 的说明，只写文档不实现）：
   ```powershell
   schtasks /Create /TN "mihealth-incremental" /SC HOURLY /MO 6 `
     /TR "cmd /c cd /d D:\Tools\mihealth-api && uv run mihealth-sync incremental --hours 72" /F
   ```
   并注明**不要与进程内调度同时开**（两把锁、白跑请求）。
4. **`sync.py:11` 的 docstring** 那句 "cron/任务计划" 也可以顺手改成指向 `MIH_DAILY_SYNC`。
5. `MCP.md` / `API.md` 的补充见 §6 表格第 7、8 行。

---

## 11. 提交与推送工艺（这个仓库特有，必读）

- **VPS 的 `origin` 是 https 且没有凭据**，`git push` 会失败。必须显式：
  ```bash
  git push git@github.com:kmizmal/mihealth-api.git HEAD:main
  ```
- VPS 上 git 身份是 `dsh / dsh@zmal.top`，提交要显式指定：
  ```bash
  git -c user.name=kmizmal -c user.email=zmal@zmal.top commit -F /root/dsh/mihealth-cookies/msg_<topic>.txt
  ```
- **提交信息最后一行必须是**（前面空一行）：
  ```
  协作模型: deepseek-v4.1-flash
  ```
  （模型名从 `/root/.dsh/storages/session_projcache/sessions/$DSH_SESSION_ID.json` 的 `record.rows.modelSelection.val.lastUsed` 取；如果接手会话换了模型，改成它自己的。）
- **长中文提交信息不要 heredoc 内联**——写到文件再 `-F`。
- **新建文件后 `chmod 644`**。本仓库踩过：新建的 `.py` / `.md` 落成 `600`，git 只记录可执行位所以不影响提交，但影响别人读。
- `.gitattributes` 已经加了 `* text=auto eol=lf`，`git ls-files --eol` 应全是 `i/lf w/lf`。**不要**再手工转 CRLF。
- 推完用 GitHub 干净克隆复核一次（`/tmp/verify_*`），比只信本地 `git status` 可靠。
- 收工后按 `agentmemory-library` 技能回写 AgentMemory 并跑 `publish.sh`。**如果 `publish.sh` 报「本地和远端都改过（真冲突）」，先跑 `node /root/.agents/skills/agentmemory-library/scripts/sync.mjs` 拉远端再 publish，别用 `--overwrite-remote`。**

---

## 12. 坑清单（前几个会话踩过，别重踩）

1. **`with TestClient(app)` 会跑 lifespan** — 这是本文档 §8.2 的整个风险来源。反过来也是好事：可以用它测调度器启动。
2. **MCP `session_manager.run()` 每个实例只能进一次** — 第二次 `with TestClient` 会 `RuntimeError: Task group is not initialized`。所以现有测试已经改成「一个用例只开一个 `with`」。`lifespan` 改动时别把它搞成可重入。
3. **`SyncJob` 单例的 key 必须归一化** — 见 §3。调度器里取 job 时，**不要自己拼 `cfg.get("db")`**，直接照抄 `server.py:332 _job()` 的写法（`runtime.get_sync_job(cfg.get("db"), cfg.get("config_path") or CFG_PATH)`），或者更干净地把 `_job()` 从 `server.py` 挪到 `runtime.py` 供两边共用（那样 `server.py` 改一行即可，但会动现有测试的 import，收益不大）。
4. **`uv` 的绝对路径与三个环境变量** — VPS 上 `uv` 在 `/root/.local/bin/uv`（不在 PATH），且必须设 `UV_CACHE_DIR` / `UV_PYTHON_INSTALL_DIR` / `UV_TOOL_DIR` 到 `/root/dsh/` 下面，否则 `Permission denied`。
5. **Python 是 3.15.0b3**（`.venv` 由 `uv sync` 建）。旧的 `/tmp/tenv` 是 3.11，装不了本包（`requires-python >= 3.14`）。
6. **`time` 单位**：`store.records.ts` 存**秒**，`store.rows/aggregate` 的 `start_ms/end_ms` 是**毫秒**（内部 `//1000`），云端接口入参毫秒。`sync_state.last_ts` 也是秒。写调度器时 `time.time()` 是秒，天然对得上，**但别顺手传给 `start_ms`**。
7. **`_wm_family` 首次跑一行不拉** — 游标为空时只 `get_max_watermark` 播种再 `return 0`（带 `cursor initialised at ... (next run pulls changes)` 日志）。所以「第一次自动同步看起来没数据」是正常的，真正拉数据的是 `_window_topup`（72h）。

---

## 13. 给新会话的开场 prompt（可直接粘贴）

> 仓库 `/root/dsh/_repo`（GitHub `kmizmal/mihealth-api`）。本文件随提交 `c1240cb` 入库，开工前先 `git pull` 取到它；HEAD 应 >= `c1240cb`（若已有人实施完，HEAD 会更新）。
> 请先读 `docs/superpowers/specs/2026-11-05-daily-auto-sync-design.md`，然后按它的 §6 清单实施：
> 新增 `src/mihealth_api/scheduler.py`，把每日自动增量挂在 `server.py` 的 `lifespan` 上，`MIH_DAILY_SYNC` 控制（默认 04:00，`off` 关闭）。
> 文档 §5.6 有一个待人确认的问题（关机重开后是否立刻补跑），如果你不确定就按文档的推荐默认执行并明确告诉我。
> 完成后：`pytest` 全绿且覆盖率 ≥96%、双端（VPS + Windows `D:\Tools\mihealth-api`）各跑一次冒烟、按 §11 提交推送、按文档 §10 更新 README/API.md/MCP.md。

---

## 14. 参考：本次调研用过的命令

```bash
# 确认没有任何定时任务（Windows 侧，通过 VPS ssh 到 frps 20022）
#   Get-ScheduledTask | ? { $_.TaskName -match 'mihealth' }
#   Get-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run
#   Get-CimInstance Win32_Process -Filter "Name like '%python%'"

# 确认代码里没有调度器
grep -rn "Timer\|schedule\|BackgroundTasks\|setInterval" --include=*.py src/

# 确认哪些测试会跑 lifespan
grep -rc "with TestClient" tests/*.py

# 当前基线
cd /root/dsh/_repo && .venv/bin/python -m pytest tests/ -q
# -> 403 passed in 18.20s
.venv/bin/python -m pytest tests/ -q --cov=mihealth_api --cov-report=term
# -> TOTAL 2255 stmts / 96 miss / 96%
```
