"""跨平台应用数据路径和本机令牌管理。"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

APPLICATION_ID = "local.component-library.app"


def data_directory() -> Path:
    """返回与旧 Tauri 版本兼容的数据目录。"""

    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = root / APPLICATION_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_or_create_token(directory: Path) -> str:
    """读取或生成本机 API 令牌，并在 Unix 上限制文件权限。"""

    path = directory / "api-token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return token
