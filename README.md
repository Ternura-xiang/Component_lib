# 元件库（Component Library）

面向个人和小型实验室的电子元件库存管理项目，按电脑端、手机端和硬件组织。

| 模块 | 目录 | 内容 |
| --- | --- | --- |
| 电脑端 | [desktop/](desktop/README.md) | Python/PySide6 桌面程序、本机与局域网 API、数据库、OCR、测试、文档和打包工具 |
| 手机端 | [mobile/](mobile/README.md) | Android 工程、移动摄像头、扫码与 BOM 取货界面 |
| 硬件 | [hardware/](hardware/README.md) | 硬件相关文件 |

电脑端当前版本为 **0.9.7**，手机端当前版本为 **1.0.10**。库存与识别服务在电脑端运行，手机端通过局域网连接。

```text
Component_lib/
├── desktop/
│   ├── src/                 # 电脑端源码
│   ├── tests/               # Python 与 Qt 回归测试
│   ├── docs/                # 用户指南、开发文档、发行说明
│   ├── tools/               # 构建和 OCR 评估工具
│   ├── packaging/           # 打包配置与图标
│   ├── examples/            # 示例文件
│   ├── pyproject.toml
│   └── uv.lock
├── mobile/
│   ├── app/                 # Android 应用与测试
│   ├── gradle/              # Gradle Wrapper
│   └── build.gradle.kts
├── hardware/
├── .github/                 # 项目级 CI
├── .gitignore
├── .gitattributes
├── CONTRIBUTING.md
├── LICENSE
└── README.md
```

## 运行与构建

从仓库根目录进入电脑端模块，使用 Python 3.12 和 uv。

```bash
cd desktop
uv sync --locked --extra dev
uv run component-library
```

Android 构建在手机端模块执行。

```bash
cd mobile
./gradlew :app:assembleDebug :app:testDebugUnitTest
```

使用 PyCharm 时打开仓库根目录，将 Python 解释器设为 `desktop/.venv/bin/python`（Windows 为 `desktop/.venv/Scripts/python.exe`），并将 `desktop/src` 标记为源码目录、`desktop/tests` 标记为测试目录。仓库的 `.run/` 提供应用启动、Pytest 和电脑端打包配置，工作目录均为 `desktop/`；共享配置默认使用 Linux 解释器路径，其他平台需在运行配置中选择本机解释器。

电脑端构建产物位于 `desktop/dist/`，本地发布文件位于 `desktop/release/`。Android 构建产物位于 `mobile/app/build/outputs/`，本地发布 APK 位于 `mobile/release/`。这些生成文件由 Git 忽略。

详见[电脑端说明](desktop/README.md)、[手机端说明](mobile/README.md)、[用户指南](desktop/docs/user/README.md)、[开发者指南](desktop/docs/developer/README.md)和[参与开发](CONTRIBUTING.md)。

## 开源协议

项目自有源码采用 [MIT License](LICENSE)。第三方依赖许可见[第三方声明](THIRD_PARTY_NOTICES.md)。
