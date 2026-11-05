"""mihealth-api — 小米运动健康（com.mi.health）云端数据客户端 + 本地网关 + 同步器.

公开入口（都注册成控制台脚本，见 pyproject `[project.scripts]`）:

    mihealth-api          本地网关（FastAPI）：看板 + REST 代理 + 登录接口 + /mcp
    mihealth-sync         同步编排：backfill / incremental / status / export
    mihealth-login        账号登录：密码 / passToken 免密续期 / 2FA
    mihealth-credentials  凭据重取：adb 设备 或 离线 Cookies 库
    mihealth-export       全量导出为 JSON

也都可以 `python -m mihealth_api.<module>` 调用。库内使用时直接 import：

    from mihealth_api import MiHealthClient, load_config, Store
"""
__version__ = "0.3.0"

from .client import (ApiError, AuthError, MiHealthClient, SCALAR_FIELDS,
                     canonical_number, client_from_config, dedup_items, load_config)
from .store import Store

__all__ = [
    "ApiError", "AuthError", "MiHealthClient", "SCALAR_FIELDS", "Store",
    "canonical_number", "client_from_config", "dedup_items", "load_config",
    "main",
]


def main(argv=None):
    """`mihealth-api` 的默认入口：起本地网关。"""
    from .server import main as serve
    return serve(argv)
