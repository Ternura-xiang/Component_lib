# 元件库（Component Library）

元件库是一个面向个人和小型实验室的本地电子元件库存管理程序。它把“元件资料”、“实际存放位置”和“每一次库存变动”分开记录，适合管理抽屉、料盒、零件柜中的散装元件。

当前版本为 **0.9.0**，详见 [v0.9.0 发行说明](docs/releases/v0.9.0.md)。主要目标平台是 Windows 10/11 和 Ubuntu 24.04+。项目业务代码使用 Python 3.12，桌面界面使用 PySide6，数据保存在 SQLite；不使用 Rust、Tauri、React 或 Node.js。

## 项目说明

主要工作流程是：

```text
建立位置/仓库 → 建立或匹配元件 → 入库 → 查询定位 → 出库 → 查看流水
```

当前已实现：

- **元件与库存**：名称搜索，类别/品牌/封装过滤，列排序，商品与数据手册链接，整数数量，批次及 FIFO 出库。
- **位置与仓库**：按“位置 → 仓库”两级管理实物存放点，支持全局及元件类别优先级。
- **多种入库方式**：手工录入，名称下拉匹配，袋贴摄像头/图片 OCR，立创商品页确认，已保存订单批量导入。
- **BOM 出库**：读取 CSV/XLSX，根据编号和名称生成匹配预览，显示缺料并原子提交。
- **导出与统计**：把当前筛选的库存导出为 Excel/CSV，并按仓库、元件类别统计数量与库存价值。
- **流水与数据安全**：禁止负库存，已提交操作使用冲销更正，支持 SQLite 快照备份和重启后恢复。
- **本机 API**：桌面程序运行时在 `127.0.0.1:17635` 提供 FastAPI 接口，写操作需 Bearer Token 和幂等键。

识别、商品页、订单和 BOM 首先产生**可编辑预览**，不会在用户确认前改变库存。

## 快速启动

源码运行需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run component-library
```

Linux 还需安装 Qt Multimedia/GStreamer 与 Qt WebEngine 的系统运行库。摄像头不可用时可导入图片；内嵌商城不可用时可使用剪贴板资料或手工录入。

首次使用：

1. 在 **位置与仓库** 新建上级位置和具体仓库。
2. 在 **入库** 输入元件名称；可从下拉列表选择已有元件，也可直接新建。
3. 填写正整数数量，选择位置/仓库后确认入库。
4. 在 **元件库存** 核对总数和存放点，再在 **出库** 完成一次测试出库。

完整步骤见 [快速开始](docs/user/quick-start.md)。

## 使用要点

- 已选已有元件时，确认入库会同时保存当前表单对该元件的修改。
- “类别参数”前的对号用于展开/收起参数区；收起不会删除已填内容。
- 立创商品资料默认全部勾选应用，包括封装和已有字段；如需保留现有值，应在确认窗口取消相应对号。
- 订单导入的每一行默认勾选，可独立选择仓库、取消导入或修改单元格。
- 商城价格不会自动当作实际购入单价；单价和购买日期由用户确认或由订单导入提供。

## 文档导航

| 阅读对象 | 入口 | 详细文档 |
| --- | --- | --- |
| 使用者 | [用户指南](docs/user/README.md) | [元件与库存](docs/user/inventory.md) · [位置与仓库](docs/user/storage.md) · [扫描入库](docs/user/scanning.md) · [商城与订单](docs/user/imports.md) · [BOM](docs/user/bom.md) · [数据与备份](docs/user/data.md) |
| 开发者 | [开发者指南](docs/developer/README.md) | [架构](docs/developer/architecture.md) · [数据库](docs/developer/database.md) · [OCR](docs/developer/recognition.md) · [导入解析](docs/developer/imports.md) · [API](docs/developer/api.md) · [UI 与打包](docs/developer/ui-build.md) · [测试](docs/developer/testing.md) |

## 数据与隐私

- Windows 数据目录：`%APPDATA%\local.component-library.app`。
- Linux 数据目录：`$XDG_DATA_HOME/local.component-library.app`，默认为 `~/.local/share/local.component-library.app`。
- 主数据库为 `inventory.db`，API 令牌为 `api-token`，日志位于 `logs/`。
- OCR 和订单解析在本机进行；应用不启用遥测，不上传库存、图片、BOM 或订单。
- 只有用户主动打开商品页或请求公开网页预览时才会访问网络。

## 开发说明

安装开发依赖：

```bash
uv sync --extra dev
```

常用命令：

```bash
# 运行应用
uv run component-library

# 格式与静态检查
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src

# Qt 无显示环境下运行全部测试
QT_QPA_PLATFORM=offscreen uv run pytest

# 构建当前平台的 PyInstaller onedir 产物
uv run python tools/build.py
```

构建结果位于 `dist/component-library/`。Windows 安装器定义在 `packaging/windows/component-library.iss`；Linux 与 Windows 必须分别在目标系统构建和验收。

发布目录 `release/<版本号>/` 仅保留 `win-<版本号>.zip` 和 `linux-<版本号>.tar.gz`。当前 Windows 包已通过启动、API 认证及离线模型校验；当前 Linux 包为历史源码归档，尚未完成 Linux 构建与验收。

详细模块边界、事务不变量和扩展步骤见 [开发者指南](docs/developer/README.md)。

## 开源协议

项目自有源码使用 [MIT License](LICENSE)。发布或修改本项目时应保留许可证原文和版权声明。

项目依赖的 PySide6、OpenCV、ONNX Runtime、RapidOCR 等第三方软件不因本项目采用 MIT 而改变其各自的许可条款；分发打包程序时仍需遵守相应依赖的许可要求。
