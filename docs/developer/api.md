# 本机 HTTP API

代码：`api.py`、`models.py`；接口由桌面应用托管，默认监听 `http://127.0.0.1:17635`，不是远程同步服务。

## 启动、认证与幂等

启动桌面程序后可以访问无需认证的 `GET /health`。OpenAPI 交互页面在 `/docs`，模式文件在 `/openapi.json`。

`/api/v1/*` 接口需要：

```http
Authorization: Bearer <本机 api-token 的内容>
```

**写接口**还需要每次业务操作唯一的：

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

详情（入参、响应模型、字段是否必填）以应用实际运行的 `/docs` 为准，避免复制示例导致接口漂移。

## 命名兼容与订单提交

接口的 `warehouses` 表示界面**上级位置**；`locations` 表示界面**具体仓库**。订单提交支持选中 `lines` 与 `location_overrides`（按 `订单号:立创编号` 指定），并可提供默认 `location_id`。优先级中的 `category` 为空表示全局设置，数值越小越靠前。

## 错误处理

业务异常的响应结构形如：

```json
{"error":{"code":"INSUFFICIENT_STOCK","message":"库存不足：需要 10，可用 4"}}
```

认证缺失时通常是 HTTP 401；库存冲突可能返回 409；具体 HTTP 状态和字段以 `api.py` 为准。新增写路由应依赖认证和 `Idempotency-Key`，走 `ApiContext.mutate()`，不能绕过 `Database` 写事务。
