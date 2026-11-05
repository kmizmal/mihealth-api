#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程内共享的活对象：数据库连接、云端客户端、后台同步任务。

为什么单独一个模块
------------------
网关（`server.py`）要挂载 MCP 子应用，MCP 工具又要读同一个库。如果让
`mcp_server` 反过来 import `server`，就是循环依赖。两边都 import 本模块，
依赖方向就固定成：`server → mcp_server → runtime`、`server → runtime`。

为什么要惰性
------------
库可能在网关启动之后才由 `mihealth-sync backfill` 建出来。原来 `server.py`
在 import 时打开一次 Store 就再也不重开，于是先起网关、后建库的进程会永远
`404 no db`。`get_store()` 每次都检查文件、必要时才打开；`reset_store()`
让同步/登录之后立刻能看到新数据。
"""
import os
import threading
import time

from . import paths
from .client import client_from_config, load_config
from .store import Store

_lock = threading.RLock()
_store = None          # 已打开的 Store（或 None）
_store_path = None     # _store 对应的路径，用于判断是否需要重开
_client = None
_client_hook = None
_config = None
_jobs = {}


# ------------------------------------------------------------------ config

def get_config():
    """进程级配置缓存（第一次读盘，之后复用同一个 dict）。"""
    global _config
    with _lock:
        if _config is None:
            _config = load_config()
        return _config


def reload_config():
    """重新读盘（登录/导入设备后调用）。"""
    global _config
    with _lock:
        _config = load_config()
        return _config


# ------------------------------------------------------------------ store

def _open(path):
    if path and os.path.exists(path):
        try:
            st = Store(path)
            print(f"[store] using {path}", flush=True)
            return st
        except Exception as e:                                   # pragma: no cover
            print(f"[store] cannot open {path}: {e}", flush=True)
            return None
    print("[store] no db yet — run `mihealth-sync backfill` for cached mode; "
          "serving live API", flush=True)
    return None


def get_store():
    """当前库连接；库文件后出现时自动打开。

    惰性 + 线程安全：MCP 工具跑在线程池里，并发进来只会开一次。
    """
    global _store, _store_path
    db = get_config().get("db") or paths.db_path()
    with _lock:
        if _store is not None:
            # 同一个文件就复用；配置里换了路径则重开
            if _store_path == db:
                return _store
            try:
                _store.close()
            except Exception:                                    # pragma: no cover
                pass
            _store = None
        _store_path = db
        _store = _open(db)
        return _store


def reset_store():
    """丢弃缓存连接，下次 get_store() 重新打开（同步/登录后调用）。"""
    global _store
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:                                    # pragma: no cover
                pass
        _store = None


def set_store(store):
    """测试/嵌入用：直接换成一个现成的 Store。"""
    global _store, _store_path
    with _lock:
        _store = store
        _store_path = getattr(store, "path", None)


# ------------------------------------------------------------------ client

def get_client():
    """云端客户端单例。配置变了就重建（reload_credentials 走 client 自身）。"""
    global _client
    with _lock:
        if _client is None:
            _client = client_from_config(get_config())
            if _client_hook is not None:
                _client.on_auth_error = _client_hook
        return _client


def reset_client():
    global _client
    with _lock:
        _client = None


def set_client(client):
    """测试/嵌入用：直接换成一个现成的客户端。"""
    global _client
    with _lock:
        _client = client
        if client is not None and _client_hook is not None:
            client.on_auth_error = _client_hook


def set_client_hook(fn):
    """注册 401 自愈回调，作用于当前与将来重建的 client。

    不能只写 `get_client().on_auth_error = fn` —— 任何地方 `reset_client()`
    之后新建的 client 都会丢掉回调，401 就再也不自动续期了。
    """
    global _client_hook
    with _lock:
        _client_hook = fn
        if _client is not None:
            _client.on_auth_error = fn


# ------------------------------------------------------------------ sync job

class SyncJob:
    """后台增量同步任务。同一时刻只跑一个。

    `/api/sync`（HTTP）与 MCP 的 `sync_now` 都走这里，所以共用一个锁和
    一份 result —— 否则两个同步器会同时往库里写。
    """

    def __init__(self, db_path, config_path=None, workers=2, min_interval=0.5):
        self.db_path = db_path
        self.config_path = config_path
        self.workers = workers
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._state = {"running": False, "started": None, "result": None}

    @property
    def state(self):
        # 返回副本，调用方拿到后随便改不会影响内部状态
        with self._lock:
            return dict(self._state)

    def start(self, hours=72, families=None):
        """启动后台同步；已在跑则返回 False。"""
        with self._lock:
            if self._state["running"]:
                return False
            self._state.update(running=True, started=int(time.time() * 1000), result=None)
        threading.Thread(target=self._run, args=(hours, families), daemon=True).start()
        return True

    def _run(self, hours, families):                             # pragma: no cover
        try:
            from . import sync as sync_mod
            S = sync_mod.Syncer(self.db_path, workers=self.workers,
                                min_interval=self.min_interval,
                                refresh=sync_mod.make_refresh(self.config_path))
            # 同步器只认 fitness/sport；families=None 时用它的默认值
            kw = {"families": tuple(families)} if families else {}
            n = S.incremental(hours=hours, **kw)
            try:
                S.db.close()
            except Exception:
                pass
            self._finish({"ok": True, "rows": n})
        except SystemExit as e:
            self._finish({"ok": False, "err": f"auth/credentials ({e})"})
        except Exception as e:                                   # pragma: no cover
            self._finish({"ok": False, "err": str(e)})

    def _finish(self, result):
        with self._lock:
            self._state["result"] = result
            self._state["running"] = False
        # 新数据落库了，让下一次 get_store() 重开，读到最新内容
        reset_store()


def get_sync_job(db_path=None, config_path=None):
    """进程级 SyncJob 单例（按 (库, 配置) 缓存）。

    HTTP 与 MCP 必须拿到同一个实例，否则各持一把锁 = 没有锁。
    """
    key = (db_path, config_path)
    with _lock:
        job = _jobs.get(key)
        if job is None:
            job = SyncJob(db_path, config_path)
            _jobs[key] = job
        return job
