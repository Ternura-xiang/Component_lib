# 开发、测试与贡献约定

## 本地工作流

```bash
uv sync --locked --extra dev
uv run ruff check src tests
uv run mypy src
# 无图形桌面的 Linux 测试环境
QT_QPA_PLATFORM=offscreen uv run pytest
```

开发环境需要 Python 3.12。提交改动前保证测试通过；涉及 Qt GUI 的测试若依赖系统插件，需在具备必要运行库的机器上验证。

## 按模块补测试

| 修改范围 | 首先检查 |
| --- | --- |
| 库存、批次、迁移、冲销 | `tests/test_database.py` |
| API 认证、幂等、错误 | `tests/test_api.py` |
| 配对、共享会话和 BOM 取货 | `tests/test_sessions.py`、`tests/test_api.py` |
| 安卓协议兼容 | `android/app/src/test/`，运行 `./gradlew :app:testDebugUnitTest` |
| OCR 字段规则 | `tests/test_label_parser.py` |
| 立创标签/商品页 | `tests/test_jlc_import.py`、`tests/test_jlc_product.py` |
| 离线订单 | `tests/test_order_import.py` |
| BOM 匹配和文件解析 | `tests/test_bom.py` |
| 主窗口或交互 | `tests/test_ui.py` |
| 说明文档版本与本地链接 | `tests/test_documentation.py` |

建议流程：先写复现问题的测试，再改最小范围代码，最后运行所有测试和静态检查。

## 代码风格与兼容性

- 公共模块、类和函数保留中文 docstring，重点解释**设计理由、数据约束和失败行为**。
- 不得更改已发布字段语义；数据库迁移必须可重复执行，保留老库升级测试。
- 不得绕过库存事务，也不得将 OCR、BOM、商城预览直接升级为隐式入库。
- 来源访问必须尊重网站政策；不加入自动登录、验证码绕过、代理规避等逻辑。
- 本机 API 需要认证和写操作幂等，不开放到公网。

## 发布前核对

```bash
uv run ruff check src tests
uv run mypy src
QT_QPA_PLATFORM=offscreen uv run pytest
uv run python tools/build.py
```

套件包含回环 HTTP、TLS 和 WebSocket 测试，运行环境需允许创建本机套接字。安卓打包和单元测试见[安卓说明](../../android/README.md)。相机画面、局域网配对与掉线恢复需要真机验证。

发布前在目标系统检查新库入出库、BOM 缺料回滚、订单预览、扫码草稿、备份恢复及打包程序启动。提交检查见[参与开发](../../CONTRIBUTING.md)。
