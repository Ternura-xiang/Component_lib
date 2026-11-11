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

Android 客户端通过独立的局域网 HTTPS 监听器访问同一个 FastAPI 应用。`SessionService` 是桌面 UI、REST 和 WebSocket 的共同状态机；客户端只提交命令并渲染服务器快照，不能在本地推导库存结果。

连接模块使用持久本地 CA、一次性配对码、每设备令牌和 `_componentlib._tcp.local` mDNS 服务。服务类型采用短名称以满足 DNS-SD 标签最多 15 字节的限制。`127.0.0.1:17635` 继续服务旧本机调用，`0.0.0.0:17636` 只提供 TLS 设备连接。

`component_library.app:main` 创建 QApplication、路径和日志，使用 `QLockFile` 限制同数据目录的多开。完成待恢复任务后初始化数据库与识别器，启动本机 HTTP 和局域网 HTTPS 监听。关闭窗口时释放摄像头并停止服务。

## 线程与共享资源

- Qt 主线程：窗口与摄像头画面。
- `QThreadPool`：OCR、文件解析等耗时工作。
- Uvicorn 后台线程：本机 HTTP 和局域网 HTTPS／WebSocket。手机上传的入库图片在线程池完成 OCR。
- `Database` 对外提供一致的连接与事务入口；不同线程不得共享 SQLite 原始连接。

## 业务数据流

```text
摄像头 / 图片 → 识别 + 校验 → LabelDraft ─┐
本地订单文件 → 安全解析 → 订单预览 ───────┤
BOM 文件 → 匹配 + 选仓 → 取货任务 ────────┼→ 用户确认 → SQLite 写事务
商品页 → 用户选择 → 资料合并草稿 ─────────┘                 │
                                                    批次 + 不可变流水
```

**预览阶段不写库存**。入库、出库与订单导入通过 `Database` 写入。BOM 取货最终提交由 `SessionService` 在同一数据库事务内校验取货数量、扣减指定批次并保存流水。

## 包结构

```text
src/component_library/
├── app.py                # 入口与服务生命周期
├── paths.py              # 数据目录、令牌
├── models.py             # 公共数据模型
├── database.py           # SQLite 与库存事务
├── api.py                # 本机与局域网 API
├── connection.py         # 证书、配对、设备认证与发现
├── sessions.py           # 共享扫码、入出库与 BOM 取货会话
├── export.py             # 库存 XLSX / CSV 导出
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
