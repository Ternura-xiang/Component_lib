"""用隔离数据目录验证 Windows onedir 产物启动、API 和离线模型。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def main() -> None:
    """启动真实 exe，避免接触用户库存；测试结束后回收测试进程。"""

    root = Path(__file__).resolve().parents[1]
    executable = root / "dist/component-library/component-library.exe"
    bundled = executable.parent / "_internal"
    assert not (bundled / "icuuc.dll").exists(), "不能覆盖 Windows 系统 ICU"
    assert (bundled / "component_library/assets/checkmark.svg").is_file()
    # 防止误把其他已运行实例的响应当成测试结果。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 17635))
    with tempfile.TemporaryDirectory(prefix="component-library-smoke-") as directory:
        environment = dict(os.environ, APPDATA=directory, QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen([str(executable)], env=environment, cwd=directory)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"打包程序提前退出：{process.returncode}")
                try:
                    with urlopen("http://127.0.0.1:17635/health", timeout=5) as response:
                        health = json.load(response)
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("等待打包程序 API 启动超时") from None
                    time.sleep(0.2)
            assert health["status"] == "ok", health
            assert health["ocr"]["available"], health
            token_path = Path(directory) / "local.component-library.app/api-token"
            request = Request(
                "http://127.0.0.1:17635/api/v1/components",
                headers={"Authorization": f"Bearer {token_path.read_text().strip()}"},
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
            assert process.poll() is None
            print(json.dumps(health, ensure_ascii=False))
            print("PASS: frozen startup, authenticated API, offline model checksums")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
