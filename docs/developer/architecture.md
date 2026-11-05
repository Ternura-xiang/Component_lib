# 架构与模块边界

## 整体结构

```text
桌面 UI (PySide6)                 本机 HTTP API (FastAPI)
        │                                  │
        ├────────────┬─────────────────────┤
        │            │                     │
  识别/解析草稿    Pydantic 模型        幂等请求包装
        │            │                     │
        └────────────┴──────────────┬──────┘
                                    │
                            Database 业务方法
                                    │
                            SQLite / 事务 / 流水
```

`component_library.app:main` 创建 QApplication、路径和日志，使用 `QLockFile` 限制同数据目录的多开；然后执行待恢复任务、初始化数据库和识别器，在独立线程启动本机 API，最后展示主窗口。关闭窗口时释放摄像头、停止 API。

## 线程与共享资源

- Qt 主线程：窗口与摄像头画面。
- `QThreadPool`：OCR、文件解析等耗时工作。
- Uvicorn 后台线程：仅本机 API。
- `Database` 对外提供一致的连接与事务入口；不同线程不得共享 SQLite 原始连接。

## 业务数据流

```text
摄像头 / 图片 → 识别 + 校验 → LabelDraft ─┐
本地订单文件 → 安全解析 → 订单预览 ───────┤
BOM 文件 → 解析 + 匹配 → BOM 预览 ───────┼→ 用户确认 → Database 写事务
商品页 → 用户选择 → 资料合并草稿 ─────────┘                 │
                                                    批次 + 不可变流水
```

**预览阶段不可有库存写副作用**。实际写入必须通过 `Database` 的 `receive`、`issue`、`import_order_lines`、`commit_bom` 等方法。

## 包结构

```text
src/component_library/
├── app.py                # 入口与服务生命周期
├── paths.py              # 数据目录、令牌
├── models.py             # 公共数据模型
├── database.py           # SQLite 与库存事务
├── api.py                # 本机 REST API
├── recognition.py        # OCR、图像与条码
├── label_parser.py       # 字段解析与校验
├── bom.py                # BOM 读取与匹配
├── order_import.py       # 离线订单解析
├── jlc_import.py         # 立创标签/基础字段辅助
├── jlc_product.py        # 已确认的立创当前商品页解析
├── web_sources.py        # 允许的公开商品页元数据
├── assets/               # 打包后仍需使用的界面资源
└── ui/
    ├── main_window.py
    ├── jlc_browser.py
    └── workers.py
```

依赖规则：UI 和 API 不应私自维护另一套库存规则；识别、BOM 和商城解析器不应持有 SQLite 写连接。更多事务细节见 [数据库模块](database.md)。
