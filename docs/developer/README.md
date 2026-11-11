# 开发者指南

本部分面向修改代码、添加识别规则、扩展导入源或集成本机 API 的开发者。**按模块查阅**，不必从头通读。

| 模块 | 说明 | 主要代码 |
| --- | --- | --- |
| [架构与边界](architecture.md) | 生命周期、线程、数据流与职责划分 | `app.py`、`models.py` |
| [数据库与库存事务](database.md) | SQLite、迁移、FIFO、不变量、冲销 | `database.py` |
| [OCR / 条码](recognition.md) | 图像处理、解析、模型、调试与回归样本 | `recognition.py`、`label_parser.py` |
| [BOM / 订单 / 商品页](imports.md) | 文件解析、匹配、网页访问合规边界 | `bom.py`、`order_import.py`、`jlc_product.py`、`web_sources.py` |
| [本机与局域网 API](api.md) | 认证、幂等、设备配对、REST 和 WebSocket | `api.py`、`connection.py`、`sessions.py` |
| [桌面界面和打包](ui-build.md) | 页面、后台任务、跨平台运行、构建 | `ui/`、`packaging/` |
| [安卓构建与诊断](../../android/README.md) | 移动端、取景、构建与扫码诊断 | `android/` |
| [测试与贡献](testing.md) | 开发流程、回归测试、发布检查 | `tests/` |

## 准备环境

```bash
# Python 3.12
uv sync --locked --extra dev
uv run component-library
QT_QPA_PLATFORM=offscreen uv run pytest
```

基础原则：**UI / API 只调用业务服务；数据库写入集中在 `Database`；来源数据先预览、人工确认后提交。** 引入新行为前优先补对应模块测试。

当前版本不需要 Rust/Node.js 工具链。项目自身不编写 C/C++ 扩展，OpenCV、ZXing、ONNX Runtime 和 Qt 的本地库通过 Python wheel 提供。所有公共类、函数和关键事务路径应保留中文 docstring 与类型标注。

返回 [项目首页](../../README.md) 或 [用户指南](../user/README.md)。
