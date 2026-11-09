# Third-party notices

Component Library 的自有代码使用 [MIT License](LICENSE)。安装包同时包含或依赖以下第三方软件，其版权及许可归各自权利人所有。本文件不替代各第三方软件自带的完整许可文本。

## Runtime dependencies

| 组件 | 许可证 |
| --- | --- |
| PySide6, Shiboken6, Qt 6 | LGPL-3.0-only（也提供 GPL/商业许可选项；本项目按 LGPL 选项使用动态库） |
| FastAPI, Pydantic, ONNX Runtime, openpyxl, RapidFuzz, AnyIO, Beautiful Soup | MIT |
| Starlette, Uvicorn, HTTPX, NumPy | BSD/MIT 等宽松许可 |
| python-multipart, OpenCV Python, zxing-cpp, RapidOCR | Apache-2.0 |

PySide6/Qt 的 LGPL-3.0 许可文本及对应源码获取方式可见：

- <https://www.gnu.org/licenses/lgpl-3.0.html>
- <https://code.qt.io/cgit/>
- <https://code.qt.io/cgit/pyside/pyside-setup.git/>

分发者不得禁止用户为调试 LGPL 库的修改而替换包中的 Qt/PySide6 动态库。

## Build-only dependency

PyInstaller 使用 GPL-2.0-or-later 并附带特殊例外，该例外允许使用 PyInstaller 构建和分发非 GPL 程序，不要求 Component Library 改用 GPL。`pyinstaller-hooks-contrib` 仅在构建阶段生效，其钩子许可信息不会将生成的应用转换为 GPL。

## Audit scope

本清单按 v0.8.9 的 `pyproject.toml` 直接依赖及当前锁定环境元数据整理。实际分发时仍应保留各 wheel/动态库自带的 LICENSE 和 NOTICE 文件。
