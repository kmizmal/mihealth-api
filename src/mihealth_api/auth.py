#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Credential self-healing shared by the sync loop and the HTTP gateway.

One call to :func:`make_refresh` returns a ``refresh(client) -> bool`` callback
suitable for ``MiHealthClient(on_auth_error=...)``. The order matters:

1. **passToken** (免密续期) — needs only ``config.json``; the usual path, no
   device involved.
2. **adb** (``credentials.fetch_from_device``) — only when passToken is absent
   or rejected; requires a rooted device with the app signed in.

Both steps write the new session back to ``config.json`` and hot-patch the live
client, so the retried request uses the fresh ticket.
"""
from . import paths
from .client import load_config


def refresh_via_config(config_path=None, client=None):
    """Renew with the stored passToken. Returns the new session or None."""
    from .login import LoginError, refresh_session
    cfg = load_config(config_path)
    if not cfg.get("pass_token"):
        return None
    try:
        sess = refresh_session(cfg, config_path)
    except LoginError as e:
        print(f"[auth] passToken refresh failed: {e}", flush=True)
        return None
    if client is not None:
        client.reload_credentials(sess["ssecurity"], sess["service_token"],
                                  sess["cuser_id"])
    print("[auth] refreshed via passToken", flush=True)
    return sess


def refresh_via_device(config_path=None, client=None):
    """Pull fresh cookies off a rooted device through adb. Returns creds or None."""
    from . import credentials
    try:
        creds = credentials.fetch_from_device()
    except Exception as e:
        print(f"[auth] device import failed: {e}", flush=True)
        return None
    if not creds:
        return None
    credentials.write_config(creds, config_path)
    if client is not None:
        client.reload_credentials(creds["ssecurity"], creds["service_token"],
                                  creds["cuser_id"])
    print("[auth] refreshed from device", flush=True)
    return creds


def make_refresh(config_path=None, use_device=True, on_success=None):
    """Build the ``on_auth_error`` hook.

    config_path  -- config file to read/write (default: ./config.json)
    use_device   -- allow the adb fallback when passToken cannot renew
    on_success   -- optional no-arg callback (e.g. clear a cache) after success
    """
    path = config_path or paths.config_path()

    def try_refresh(client, why=""):
        if why:
            print(f"[auth] {why} -> self-heal", flush=True)
        ok = refresh_via_config(path, client) is not None
        if not ok and use_device:
            ok = refresh_via_device(path, client) is not None
        if ok and on_success is not None:
            on_success()
        return ok

    return try_refresh
