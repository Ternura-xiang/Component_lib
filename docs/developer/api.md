# 本机与局域网 API

桌面应用通过 `api.py` 托管两个监听器。本机工具使用 `http://127.0.0.1:17635`，已配对设备使用 `https://<桌面地址>:17636`。两个监听器共享业务服务和库存。

## 启动、认证与幂等

启动桌面程序后可以访问无需认证的 `GET /health`。OpenAPI 交互页面在 `/docs`，模式文件在 `/openapi.json`。

除能力查询与配对外，业务接口使用 Bearer 认证。本机工具使用应用数据目录的 `api-token`，安卓使用配对返回的设备令牌。

```http
Authorization: Bearer <本机 api-token 的内容>
```

入库、出库、创建会话等使用请求幂等键的接口还需要每次业务操作唯一的请求头。

```http
Idempotency-Key: <UUID>
```

Token 在应用数据目录 `api-token`。不得将其提交到 Git、日志或截图；本机恶意软件仍可能读取本机令牌，API 不等于隔离沙箱。

举例：

```bash
# 在应用已经运行时检查状态
curl http://127.0.0.1:17635/health

# Linux 示例：列出元件（token 请仅保存在本机）
TOKEN="$(cat "${XDG_DATA_HOME:-$HOME/.local/share}/local.component-library.app/api-token")"
curl -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:17635/api/v1/components?q=0402'
```

幂等结果持久化到 SQLite；相同操作重试应保留原幂等键，新业务操作必须使用新键。不要将“一次入库”的重试变成生成全新键后的二次入库。

## 接口按模块索引

| 模块 | 端点 |
| --- | --- |
| 元件 | `GET/POST /api/v1/components`，`GET/PUT/DELETE /api/v1/components/{component_id}` |
| 位置与仓库 | `GET/POST /api/v1/warehouses`、`GET/POST /api/v1/locations`，各自 `PUT/DELETE /{id}` |
| 优先级 | `GET/PUT /api/v1/storage-priorities` |
| 库存、统计与流水 | `GET /api/v1/stock`、`GET /api/v1/statistics`、`POST /api/v1/inventory/{receive,issue,transfer,adjust}`、`GET /api/v1/inventory/transactions`、`POST /api/v1/inventory/transactions/{group_id}/reverse` |
| 批次扫码 | `POST /api/v1/inventory/batches/resolve`、`GET /api/v1/inventory/batches/{batch_id}`；解析接口只返回批次，不扣库存 |
| BOM | `POST /api/v1/bom/import/preview`、`POST /api/v1/bom/preview`、`POST /api/v1/bom/commit` |
| 离线订单 | `POST /api/v1/order-import/preview`、`POST /api/v1/order-import/commit` |
| 扫描/OCR | `POST /api/v1/scan-sessions/analyze`、`/parse`、`/corrections`，`GET /api/v1/scan-sessions/corrections`、`GET /api/v1/ocr/status`，另有 `POST /api/v1/scan-sessions` |
| 网页预览 | `POST /api/v1/web-enrichment/preview` |
| 数据快照 | `POST /api/v1/backups`、`POST /api/v1/backups/restore` |
| 设备连接 | `GET /api/v1/capabilities`、`POST /api/v1/pairing`、`GET /api/v1/devices`、`DELETE /api/v1/devices/{device_id}`、`POST /api/v1/devices/heartbeat` |
| 统一会话 | `GET/POST /api/v1/sessions`、`GET /api/v1/sessions/{id}`、`POST /api/v1/sessions/{id}/commands`、`POST /api/v1/sessions/{id}/image`、`WS /api/v1/sessions/{id}/events` |

## 局域网配对与共享会话

配对成功后，每台设备获得独立令牌。列出和删除设备需要桌面令牌，删除设备后其令牌失效。会话命令携带 `command_id` 和 `expected_revision`；重复命令返回首次响应，旧修订返回 `REVISION_CONFLICT`（HTTP 409）。

会话类型包括 `RECEIVE`、`ISSUE` 和 `BOM_PICK`。图片接口接收 multipart 的 `file` 字段并生成 OCR 草稿。扫码命令提交条码原文，出入库确认和 BOM 最终提交才改变库存。

WebSocket 使用同一 Bearer 令牌握手，推送带 `revision` 的完整快照。断线时客户端可以 GET 当前会话再重建订阅。连续相机会话可重复扫码或拍照，`CANCEL` 关闭当前会话。

配对二维码协议 v2 只携带服务器证书与 CA 指纹、局域网地址、端口和一次性配对码，不再嵌入完整 CA PEM，以降低二维码密度。Android 首次连接时固定二维码中的服务器证书指纹；配对成功响应再返回 CA，客户端校验 CA 指纹后加密保存。协议 v1 仅为旧二维码保留客户端兼容解析。

当前 API 为 1.1。同一主版本内允许新增可选字段，客户端应忽略未知字段，并从 `/api/v1/capabilities` 检查功能与主版本；安卓客户端遇到不支持的主版本时提示升级。

详情（入参、响应模型、字段是否必填）以应用实际运行的 `/docs` 为准，避免复制示例导致接口漂移。

## 命名兼容与订单提交

接口的 `warehouses` 表示界面**上级位置**；`locations` 表示界面**具体仓库**。订单提交支持选中 `lines` 与 `location_overrides`（按 `订单号:立创编号` 指定），并可提供默认 `location_id`。优先级中的 `category` 为空表示全局设置，数值越小越靠前。

## 错误处理

业务异常的响应结构形如：

```json
{"error":{"code":"INSUFFICIENT_STOCK","message":"库存不足：需要 10，可用 4"}}
```

认证缺失时通常是 HTTP 401，库存冲突可能返回 409。独立库存写路由使用认证和 `ApiContext.mutate()` 的幂等机制，会话命令使用 `SessionService` 的命令幂等与修订校验。
