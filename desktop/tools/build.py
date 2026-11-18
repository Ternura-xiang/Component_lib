"""构建当前平台的 PyInstaller onedir 产物。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """清理旧缓存并运行固定 spec；失败码原样返回给 CI。"""

    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(root / "packaging" / "component-library.spec"),
    ]
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
