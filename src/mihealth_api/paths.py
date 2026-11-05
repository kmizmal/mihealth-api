#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Filesystem layout of a mihealth-api deployment.

Installed code lives in site-packages, so **anything the user is expected to
edit or grow** (``config.json``, ``mihealth.db``, ``export/``) is resolved
against the current working directory — the repo root in normal use, or
whatever directory you launched the command from. Only read-only bundled
assets (the dashboard in ``static/``) come from the package itself.

Every helper accepts an explicit override and honours an env var, so nothing
has to be monkey-patched from tests:

    config.json  --config / MIH_CONFIG
    mihealth.db  --db     / MIH_DB
    export/               MIH_EXPORT_DIR
"""
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PKG_DIR, "static")


def config_path(explicit=None):
    """Where credentials are read from / written to."""
    return (explicit
            or os.environ.get("MIH_CONFIG")
            or os.path.join(os.getcwd(), "config.json"))


def db_path(explicit=None):
    """Where the SQLite cache lives."""
    return (explicit
            or os.environ.get("MIH_DB")
            or os.path.join(os.getcwd(), "mihealth.db"))


def export_dir(explicit=None):
    """Output directory for the JSON / CSV bulk exports."""
    return (explicit
            or os.environ.get("MIH_EXPORT_DIR")
            or os.path.join(os.getcwd(), "export"))


def static_dir():
    """Bundled dashboard assets (read-only)."""
    return STATIC_DIR
