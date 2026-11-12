# 元件库（Component Library）

元件库是一个面向个人和小型实验室的本地电子元件库存管理程序。它把“元件资料”、“实际存放位置”和“每一次库存变动”分开记录，适合管理抽屉、料盒、零件柜中的散装元件。

桌面端当前版本为 **0.9.6**，主要目标平台为 Windows 10/11 和 Ubuntu 24.04+。安卓端当前版本为 **1.0.10**，支持 Android 8.0（API 26）及以上。桌面端使用 Python 3.12、PySide6 和 SQLite，安卓端通过局域网连接桌面端。详见 [v0.9.6 发行说明](docs/releases/v0.9.6.md)和[安卓说明](android/README.md)。

## 项目说明

主要工作流程是：

```text
建立位置/仓库 → 建立或匹配元件 → 入库 → 查询定位 → 出库 → 查看流水
```

当前已实现：

- **元件与库存**：名称搜索、类别/品牌/封装筛选、按批次查看库存和总库存、批量移库、FIFO 出库。
- **位置与仓库**：按“位置 → 仓库”两级管理实物存放点，支持仓库色标、全局优先级和元件类别优先级。
- **多种入库方式**：手工录入，名称下拉匹配，袋贴摄像头/图片 OCR，立创商品页确认，已保存订单批量导入。
- **BOM 出库**：读取 CSV/XLSX，支持逐行勾选、缺料提示和输入筛选匹配候选。推荐仓库仅供参考，用户逐行或批量选仓后创建取货任务。
- **移动取货**：设置页显示 Android 在线状态；在线手机可作为入库识别、批次扫码和出库扫码摄像头，并同步 BOM 取货进度。
- **导出与统计**：把当前筛选的库存导出为 Excel/CSV，并按仓库、元件类别统计数量与库存价值。
- **流水与数据安全**：禁止负库存，同一次入库/出库默认合并显示并可展开，已提交操作使用冲销更正，支持 SQLite 快照备份和重启后恢复。
- **本机与设备 API**：本机 HTTP 监听 `127.0.0.1:17635`，配对设备通过局域网 HTTPS 端口 `17636` 操作。库存写操作需认证，并通过幂等键或会话命令编号防止重复执行。

识别、商品页、订单和 BOM 首先产生**可编辑预览**，不会在用户确认前改变库存。

## 快速启动

源码运行需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --locked
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
- 点击「类别参数」前的三角箭头展开或收起参数，已填内容会保留。
- 立创商品资料默认全部勾选应用，包括封装和已有字段；如需保留现有值，应在确认窗口取消相应对号。
- 「从文件导入」支持立创订单 HTML 和物料明细对账单 XLS，可分别导入或同时选择后匹配。XLS 含税单价作为批次购入单价，每批最多 1000 条。
- 商城价格不会自动当作实际购入单价；单价和购买日期由用户确认或由订单导入提供。

### 商城后台匹配

在 **设置 → 资料匹配** 启用商城更新和后台匹配后，程序在随机初始时刻启动，每分钟随机决定是否处理一个未匹配元件。Qt WebEngine 优先无窗口读取，缺少可识别资料时在屏幕右下角显示小窗并复用当前页面。程序按立创编号精确匹配，遇到登录、验证或访问限制时停止。

## 文档导航

| 阅读对象 | 入口 | 详细文档 |
| --- | --- | --- |
| 使用者 | [用户指南](docs/user/README.md) | [元件与库存](docs/user/inventory.md) · [位置与仓库](docs/user/storage.md) · [扫描入库](docs/user/scanning.md) · [商城与订单](docs/user/imports.md) · [BOM](docs/user/bom.md) · [数据与备份](docs/user/data.md) |
| 开发者 | [开发者指南](docs/developer/README.md) | [架构](docs/developer/architecture.md) · [数据库](docs/developer/database.md) · [OCR](docs/developer/recognition.md) · [导入解析](docs/developer/imports.md) · [API](docs/developer/api.md) · [UI 与打包](docs/developer/ui-build.md) · [测试](docs/developer/testing.md) |
| 安卓用户与开发者 | [安卓说明](android/README.md) | [局域网配对与扫码](docs/user/scanning.md#使用-android-手机作为移动摄像头) |

## 数据与隐私

- Windows 数据目录：`%APPDATA%\local.component-library.app`。
- Linux 数据目录：`$XDG_DATA_HOME/local.component-library.app`，默认为 `~/.local/share/local.component-library.app`。
- 主数据库为 `inventory.db`，API 令牌为 `api-token`，日志位于 `logs/`，局域网证书及私钥位于 `connection/`。
- OCR 和订单解析在本机进行；应用不启用遥测，不上传库存、图片、BOM 或订单。
- 桌面和手机通过局域网传输操作数据。用户主动访问商城商品页时，程序会访问对应站点。

## 开发说明

安装开发依赖：

```bash
uv sync --locked --extra dev
```

常用命令：

```bash
# 运行应用
uv run component-library

# 格式与静态检查
uv run ruff check src tests
uv run mypy src

# Qt 无显示环境下运行全部测试
QT_QPA_PLATFORM=offscreen uv run pytest

# 构建当前平台的 PyInstaller onedir 产物
uv run python tools/build.py
```

构建结果位于 `dist/component-library/`。Windows 安装器定义在 `packaging/windows/component-library.iss`；Linux 与 Windows 必须分别在目标系统构建和验收。

安卓构建、安装与诊断见 [Android 移动端](android/README.md)。`dist/`、`release/` 和安卓 `build/` 用于本地生成产物，版本说明在 [docs/releases/](docs/releases/)。

详细模块边界与扩展步骤见 [开发者指南](docs/developer/README.md)，提交要求见[参与开发](CONTRIBUTING.md)。

## 开源协议

项目自有源码使用 [MIT License](LICENSE)。发布或修改本项目时应保留许可证原文和版权声明。

项目依赖的 PySide6、OpenCV、ONNX Runtime、RapidOCR 等第三方软件不因本项目采用 MIT 而改变其各自的许可条款；分发打包程序时仍需遵守相应依赖的许可要求。
