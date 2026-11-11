"""本机 FastAPI 接口。

桌面界面直接调用同一业务对象，外部设备通过本模块调用，因此库存规则只有一份。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .bom import parse_bom, preview_bom
from .database import Database, DomainError
from .label_parser import parse_label_text
from .models import (
    AdjustRequest,
    BomCommitRequest,
    BomInputRow,
    ComponentCreate,
    CorrectionRequest,
    IssueRequest,
    LocationCreate,
    OrderImportLine,
    PairDeviceRequest,
    ReceiveRequest,
    SessionCommandRequest,
    SessionCreateRequest,
    TransferRequest,
    WarehouseCreate,
)
from .order_import import parse_saved_orders
from .recognition import RecognitionEngine
from .sessions import API_CAPABILITIES, SessionService
from .web_sources import PublicWebEnricher

if TYPE_CHECKING:
    from .connection import ConnectionManager

logger = logging.getLogger(__name__)
T = TypeVar("T")


class TextParseRequest(BaseModel):
    """外部扫描器提交的原始文字。"""

    raw_text: str


class OrderImportCommitRequest(BaseModel):
    """外部工具确认后的离线订单入库请求。"""

    location_id: str | None = None
    location_overrides: dict[str, str] = Field(default_factory=dict)
    lines: list[OrderImportLine]


class StoragePriorityRequest(BaseModel):
    """整体或某类别的库位优先级；数值越小，推荐顺序越靠前。"""

    category: str | None = None
    priorities: dict[str, int] = Field(default_factory=dict)


class ScanSessionRequest(BaseModel):
    """保存扫描草稿的输入。"""

    kind: str
    raw_text: str
    confidence: float | None = None


class ReverseRequest(BaseModel):
    """冲销请求。"""

    reason: str


class BackupRequest(BaseModel):
    """备份目录为空时使用应用默认目录。"""

    directory: str | None = None


class RestoreRequest(BaseModel):
    """待恢复的数据库文件。"""

    path: str


class BomPreviewRequest(BaseModel):
    """外部调用方直接提交的结构化 BOM。"""

    rows: list[BomInputRow]


class WebPreviewRequest(BaseModel):
    """用户明确选择的单个公开页面。"""

    url: str


class ApiContext:
    """API 路由共享依赖。"""

    def __init__(
        self,
        database: Database,
        token: str,
        recognition: RecognitionEngine,
        connection: ConnectionManager | None = None,
        sessions: SessionService | None = None,
    ) -> None:
        """保存共享服务并创建写操作锁。

        ``token`` 只在内存中用于 Bearer 校验；``recognition`` 延迟加载模型，构造
        上下文本身不会触发 OCR 初始化或网络访问。
        """

        self.database = database
        self.token = token
        self.recognition = recognition
        self.connection = connection
        self.sessions = sessions or SessionService(database)
        self.web = PublicWebEnricher(database)
        self._mutation_lock = threading.RLock()

    def mutate(self, key: str, operation: str, work: Callable[[], T]) -> T:
        """执行带持久幂等记录的 API 写操作。

        :param key: 客户端提供的幂等键；同一业务重试必须保持不变
        :param operation: 稳定的操作名称，用于防止同一键被不同接口复用
        :param work: 尚未执行的无参数写操作回调
        :return: 首次执行结果，或数据库中缓存的历史响应
        :raises DomainError: 同一幂等键被用于不同操作时抛出

        进程锁防止两个相同幂等键并发穿过首次查询。库存服务内部仍使用独立的
        ``BEGIN IMMEDIATE`` 事务保证库存一致性；幂等记录负责应用重试语义。
        """

        ### 串行检查幂等记录 ###
        with self._mutation_lock:
            with self.database.read() as connection:
                existing = connection.execute(
                    "SELECT operation,response_json FROM idempotency_records WHERE key=?", (key,)
                ).fetchone()
            if existing:
                if existing["operation"] != operation:
                    raise DomainError("CONFLICT", "同一幂等键不能用于不同操作")
                cached: T = json.loads(existing["response_json"])
                return cached
            ### 首次请求执行操作并持久化响应 ###
            result = work()
            with self.database.write() as connection:
                connection.execute(
                    "INSERT INTO idempotency_records(key,operation,response_json,created_at) VALUES(?,?,?,datetime('now'))",
                    (key, operation, json.dumps(result, ensure_ascii=False)),
                )
            return result


def create_api(context: ApiContext) -> FastAPI:
    """创建并注册本机 FastAPI 应用的全部接口。

    :param context: 数据库、认证令牌、识别器和幂等锁组成的共享上下文
    :return: 已注册异常处理、认证依赖和业务路由的 FastAPI 实例

    注意：应用不配置 CORS，浏览器跨域默认不可访问；除健康检查外，业务接口均需
    Bearer Token，所有写接口还必须提供 ``Idempotency-Key``。
    """

    ### 创建应用并注册统一错误处理 ###
    app = FastAPI(title="Component Library Local API", version=__version__)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Any, error: DomainError) -> JSONResponse:
        """把稳定的领域错误码映射为 HTTP 状态码和统一 JSON 错误结构。"""

        status = (
            404
            if error.code == "NOT_FOUND"
            else 409
            if error.code in {"CONFLICT", "INSUFFICIENT_STOCK", "REVISION_CONFLICT"}
            else 400
        )
        return JSONResponse(
            status_code=status, content={"error": {"code": error.code, "message": error.message}}
        )

    async def auth(authorization: str | None = Header(default=None)) -> str:
        """校验本机 API 的 Bearer 令牌；失败时立即终止依赖链。"""

        supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if supplied == context.token:
            return "desktop"
        device_id = context.connection.authenticate(supplied) if context.connection and supplied else None
        if not device_id:
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "UNAUTHORIZED", "message": "API 令牌无效"}},
            )
        return device_id

    async def admin(authorization: str | None = Header(default=None)) -> None:
        """设备管理只接受桌面本机令牌。"""

        if authorization != f"Bearer {context.token}":
            raise HTTPException(status_code=401, detail="需要桌面管理员令牌")

    async def key(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> str:
        """读取写接口幂等键，避免客户端重试造成重复入库或重复扣料。"""

        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "写接口必须提供 Idempotency-Key")
        return idempotency_key

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """返回服务版本和 OCR 自检结果；该端点无需认证，且不修改业务数据。"""

        return {
            "status": "ok",
            "service": "component-library",
            "version": __version__,
            "ocr": context.recognition.status(),
        }

    @app.get("/api/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        """返回同主版本能力协商声明。"""

        return API_CAPABILITIES

    @app.post("/api/v1/pairing")
    async def pair_device(data: PairDeviceRequest) -> dict[str, Any]:
        """使用一次性配对码换取设备令牌。"""

        if context.connection is None:
            raise DomainError("NOT_FOUND", "局域网连接模块未启用")
        return context.connection.pair(
            data.pairing_code, data.device_name, data.device_type, data.client_version
        )

    @app.get("/api/v1/devices", dependencies=[Depends(admin)])
    async def devices() -> list[dict[str, Any]]:
        return context.connection.list_devices() if context.connection else []

    @app.delete("/api/v1/devices/{device_id}", dependencies=[Depends(admin)])
    async def delete_device(device_id: str) -> dict[str, bool]:
        if context.connection is None:
            raise DomainError("NOT_FOUND", "局域网连接模块未启用")
        context.connection.delete(device_id)
        return {"deleted": True}

    @app.post("/api/v1/devices/heartbeat")
    async def heartbeat(
        actor: str = Depends(auth), x_client_version: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if actor != "desktop" and context.connection and x_client_version:
            context.connection.update_client_version(actor, x_client_version)
        if actor != "desktop":
            context.sessions.report_device_activity(actor, x_session_id)
        return {"status": "ok", "actor": actor, "capabilities": API_CAPABILITIES}

    def can_access_session(session: dict[str, Any], actor: str) -> bool:
        snapshot = session["snapshot"]
        if actor == "desktop" or snapshot["kind"] == "BOM_PICK":
            return True
        target = snapshot["form"].get("target_device_id")
        return (target or session["created_by"]) == actor or (
            not target and session["created_by"] == "desktop"
        )

    def authorized_session(session_id: str, actor: str) -> dict[str, Any]:
        session = context.sessions.get(session_id)
        if not can_access_session(session, actor):
            raise DomainError("NOT_FOUND", "操作会话不存在")
        return session

    @app.get("/api/v1/sessions")
    async def active_sessions(actor: str = Depends(auth)) -> list[dict[str, Any]]:
        return [s for s in context.sessions.list_active() if can_access_session(s, actor)]

    @app.post("/api/v1/sessions")
    async def create_session(
        data: SessionCreateRequest, actor: str = Depends(auth), request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "create_session",
            lambda: context.sessions.create(data.kind, data.payload, actor),
        )

    @app.get("/api/v1/sessions/{session_id}")
    async def session_snapshot(session_id: str, actor: str = Depends(auth)) -> dict[str, Any]:
        return authorized_session(session_id, actor)

    @app.post("/api/v1/sessions/{session_id}/commands")
    async def session_command(
        session_id: str, data: SessionCommandRequest, actor: str = Depends(auth)
    ) -> dict[str, Any]:
        authorized_session(session_id, actor)
        return context.sessions.command(
            session_id,
            data.command_id,
            data.expected_revision,
            data.command,
            data.payload,
            actor,
        )

    @app.post("/api/v1/sessions/{session_id}/image")
    async def session_image(
        session_id: str, file: UploadFile = File(...), actor: str = Depends(auth)
    ) -> dict[str, Any]:
        import anyio

        authorized_session(session_id, actor)
        image_bytes = await file.read()
        started_at = time.perf_counter()
        draft_model = await anyio.to_thread.run_sync(context.recognition.analyze, image_bytes)
        logger.info(
            "Android image OCR completed: session=%s bytes=%d duration_ms=%d",
            session_id,
            len(image_bytes),
            round((time.perf_counter() - started_at) * 1000),
        )
        draft = draft_model.model_dump(
            exclude={"annotated_image_png"}
        )
        return context.sessions.apply_recognition(session_id, draft)

    @app.websocket("/api/v1/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str, token: str = "") -> None:
        """修订变化时推送快照，并用周期快照保持移动网络连接存活。"""

        import anyio

        authorization = websocket.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else token
        actor = "desktop" if token == context.token else (
            context.connection.authenticate(token) if context.connection else None
        )
        if not actor:
            await websocket.close(code=4401)
            return
        try:
            authorized_session(session_id, actor)
        except DomainError:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        revision = -1
        last_sent = 0.0
        try:
            while True:
                current = context.sessions.get(session_id)
                now = time.monotonic()
                if current["revision"] != revision or now - last_sent >= 10:
                    revision = current["revision"]
                    await websocket.send_json(current)
                    last_sent = now
                with anyio.move_on_after(0.35):
                    if (await websocket.receive())["type"] == "websocket.disconnect":
                        return
        except (WebSocketDisconnect, OSError, RuntimeError):
            return

    @app.get("/api/v1/components", dependencies=[Depends(auth)])
    async def components(q: str = Query(default="")) -> list[dict[str, Any]]:
        """按可选关键字列出未归档元件；空字符串表示全部元件。"""

        return context.database.list_components(q)

    @app.get("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def component_detail(component_id: str) -> dict[str, Any]:
        """返回元件主数据以及可用的类别专属参数。"""

        return context.database.get_component(component_id)

    @app.post("/api/v1/components", dependencies=[Depends(auth)])
    async def create_component(
        data: ComponentCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """创建元件主数据，并以请求键缓存成功响应。"""

        return context.mutate(
            request_key, "create_component", lambda: context.database.create_component(data)
        )

    @app.put("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def update_component(
        component_id: str, data: ComponentCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """用完整 ``ComponentCreate`` 数据更新指定元件；不存在时返回领域错误。"""

        return context.mutate(
            request_key,
            "update_component",
            lambda: context.database.update_component(component_id, data),
        )

    @app.delete("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def delete_component(
        component_id: str, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """归档指定元件；存在库存或引用时由数据库层拒绝不安全删除。"""

        return context.mutate(
            request_key,
            "delete_component",
            lambda: context.database.delete_component(component_id),
        )

    @app.get("/api/v1/warehouses", dependencies=[Depends(auth)])
    async def warehouses() -> list[dict[str, Any]]:
        """列出仓库及其基础属性。"""

        return context.database.list_warehouses()

    @app.post("/api/v1/warehouses", dependencies=[Depends(auth)])
    async def create_warehouse(
        data: WarehouseCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """创建仓库；名称等业务约束由模型和数据库共同校验。"""

        return context.mutate(
            request_key, "create_warehouse", lambda: context.database.create_warehouse(data)
        )

    @app.delete("/api/v1/warehouses/{warehouse_id}", dependencies=[Depends(auth)])
    async def delete_warehouse(
        warehouse_id: str, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """删除空仓库；仍含库位时返回冲突，避免级联丢失库存位置。"""

        return context.mutate(
            request_key,
            "delete_warehouse",
            lambda: context.database.delete_warehouse(warehouse_id),
        )

    @app.put("/api/v1/warehouses/{warehouse_id}", dependencies=[Depends(auth)])
    async def update_warehouse(
        warehouse_id: str, data: WarehouseCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """更新仓库名称等可编辑属性。"""

        return context.mutate(
            request_key,
            "update_warehouse",
            lambda: context.database.update_warehouse(warehouse_id, data),
        )

    @app.get("/api/v1/locations", dependencies=[Depends(auth)])
    async def locations() -> list[dict[str, Any]]:
        """列出所有库位，并附带所属仓库信息。"""

        return context.database.list_locations()

    @app.get("/api/v1/storage-priorities", dependencies=[Depends(auth)])
    async def storage_priorities(category: str | None = None) -> list[dict[str, Any]]:
        """读取整体或指定类别的库位推荐顺序。"""

        return context.database.storage_priority_rows(category)

    @app.put("/api/v1/storage-priorities", dependencies=[Depends(auth)])
    async def update_storage_priorities(
        data: StoragePriorityRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """保存优先级；空映射会清除对应范围内的显式配置。"""

        return context.mutate(
            request_key,
            "update_storage_priorities",
            lambda: context.database.save_storage_priorities(data.category, data.priorities),
        )

    @app.post("/api/v1/locations", dependencies=[Depends(auth)])
    async def create_location(
        data: LocationCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """在指定仓库中新建库位。"""

        return context.mutate(
            request_key, "create_location", lambda: context.database.create_location(data)
        )

    @app.delete("/api/v1/locations/{location_id}", dependencies=[Depends(auth)])
    async def delete_location(location_id: str, request_key: str = Depends(key)) -> dict[str, Any]:
        """删除空库位；有库存批次时必须先转移或清空。"""

        return context.mutate(
            request_key,
            "delete_location",
            lambda: context.database.delete_location(location_id),
        )

    @app.put("/api/v1/locations/{location_id}", dependencies=[Depends(auth)])
    async def update_location(
        location_id: str, data: LocationCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """更新库位名称、仓库归属和容量等属性。"""

        return context.mutate(
            request_key,
            "update_location",
            lambda: context.database.update_location(location_id, data),
        )

    @app.get("/api/v1/stock", dependencies=[Depends(auth)])
    async def stock() -> list[dict[str, Any]]:
        """返回按元件、库位和批次聚合后的当前库存快照。"""

        return context.database.list_stock()

    @app.get("/api/v1/statistics", dependencies=[Depends(auth)])
    async def statistics() -> dict[str, Any]:
        """返回全库库存总量、估值以及分库和分类汇总。"""

        return context.database.inventory_statistics()

    @app.post("/api/v1/inventory/receive")
    async def receive(
        data: ReceiveRequest, request_key: str = Depends(key), actor: str = Depends(auth)
    ) -> dict[str, Any]:
        """执行入库事务；数量必须为正，成功后返回事务组信息。"""

        return context.mutate(
            request_key, "receive",
            lambda: context.database.receive(data, platform="PC" if actor == "desktop" else "安卓"),
        )

    @app.post("/api/v1/inventory/issue")
    async def issue(
        data: IssueRequest, request_key: str = Depends(key), actor: str = Depends(auth)
    ) -> dict[str, Any]:
        """按数据库层的批次策略扣减库存；库存不足时整个事务回滚。"""

        return context.mutate(
            request_key, "issue",
            lambda: context.database.issue(data, platform="PC" if actor == "desktop" else "安卓"),
        )

    @app.post("/api/v1/inventory/batches/resolve", dependencies=[Depends(auth)])
    async def resolve_batch(data: TextParseRequest) -> dict[str, Any]:
        """解析二维码并返回对应批次；该操作不会扣减库存。"""

        return context.database.resolve_batch_code(data.raw_text)

    @app.get("/api/v1/inventory/batches/{batch_id}", dependencies=[Depends(auth)])
    async def batch(batch_id: str) -> dict[str, Any]:
        """读取单个批次及其元件、库位和剩余数量。"""

        return context.database.get_batch(batch_id)

    @app.post("/api/v1/inventory/transfer", dependencies=[Depends(auth)])
    async def transfer(data: TransferRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        """在两个库位间原子转移库存，保持总量不变。"""

        return context.mutate(request_key, "transfer", lambda: context.database.transfer(data))

    @app.post("/api/v1/inventory/adjust", dependencies=[Depends(auth)])
    async def adjust(data: AdjustRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        """把库存调整到盘点值，并记录可追溯的差额事务。"""

        return context.mutate(request_key, "adjust", lambda: context.database.adjust(data))

    @app.get("/api/v1/inventory/transactions", dependencies=[Depends(auth)])
    async def transactions() -> list[dict[str, Any]]:
        """按时间倒序返回库存流水，供审计和冲销选择。"""

        return context.database.list_transactions()

    @app.post("/api/v1/inventory/transactions/{group_id}/reverse", dependencies=[Depends(auth)])
    async def reverse(
        group_id: str, data: ReverseRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """以新的反向流水冲销事务组；原流水始终保留以满足审计要求。"""

        return context.mutate(
            request_key, "reverse", lambda: context.database.reverse_group(group_id, data.reason)
        )

    @app.post("/api/v1/bom/preview", dependencies=[Depends(auth)])
    async def bom_preview(data: BomPreviewRequest) -> list[dict[str, Any]]:
        """匹配结构化 BOM 与现有元件，并计算候选、需求量和库存缺口。"""

        return preview_bom(context.database, data.rows)

    @app.post("/api/v1/bom/import/preview", dependencies=[Depends(auth)])
    async def bom_import(
        file: UploadFile = File(...), sheet_name: str | None = None
    ) -> dict[str, Any]:
        """暂存上传的 CSV/XLSX，解析后返回原始行和匹配预览。

        临时文件位于数据库目录并在 ``finally`` 中删除；此步骤不扣减库存。
        """

        suffix = Path(file.filename or "bom.csv").suffix
        temporary = context.database.path.parent / f".bom-import-{threading.get_ident()}{suffix}"
        try:
            temporary.write_bytes(await file.read())
            rows = parse_bom(temporary, sheet_name)
            return {
                "rows": [row.model_dump() for row in rows],
                "preview": preview_bom(context.database, rows),
            }
        finally:
            temporary.unlink(missing_ok=True)

    @app.post("/api/v1/bom/commit")
    async def bom_commit(
        data: BomCommitRequest, request_key: str = Depends(key), actor: str = Depends(auth)
    ) -> dict[str, Any]:
        """提交用户确认后的 BOM 领料方案，并原子扣减相关批次库存。"""

        return context.mutate(
            request_key, "bom_commit",
            lambda: context.database.commit_bom(data, platform="PC" if actor == "desktop" else "安卓"),
        )

    @app.post("/api/v1/order-import/preview", dependencies=[Depends(auth)])
    async def order_import_preview(file: UploadFile = File(...)) -> dict[str, Any]:
        """安全解析离线订单文件并返回逐行推荐库位，不写入元件或库存。"""

        suffix = Path(file.filename or "orders.zip").suffix.casefold()
        temporary = context.database.path.parent / f".order-import-{threading.get_ident()}{suffix}"
        try:
            temporary.write_bytes(await file.read())
            lines = parse_saved_orders(temporary)
            return {
                "lines": [line.model_dump() for line in lines],
                "preview": context.database.preview_order_import(lines),
            }
        finally:
            temporary.unlink(missing_ok=True)

    @app.post("/api/v1/order-import/commit", dependencies=[Depends(auth)])
    async def order_import_commit(
        data: OrderImportCommitRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """导入用户确认的订单行，可用 ``location_overrides`` 覆盖逐行库位。"""

        return context.mutate(
            request_key,
            "order_import_commit",
            lambda: context.database.import_order_lines(
                data.lines,
                data.location_id,
                data.location_overrides,
            ),
        )

    @app.post("/api/v1/scan-sessions/analyze", dependencies=[Depends(auth)])
    async def scan_analyze(file: UploadFile = File(...)) -> dict[str, Any]:
        """对上传图片执行离线条码与 OCR 识别，返回需人工确认的草稿。"""

        return context.recognition.analyze(await file.read()).model_dump()

    @app.post("/api/v1/scan-sessions/parse", dependencies=[Depends(auth)])
    async def scan_parse(data: TextParseRequest) -> dict[str, Any]:
        """解析外部扫码器提供的纯文本，不加载图像识别模型。"""

        return parse_label_text(data.raw_text).model_dump()

    @app.post("/api/v1/scan-sessions", dependencies=[Depends(auth)])
    async def save_scan(
        data: ScanSessionRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """持久化一次扫描会话，用于审计识别来源和置信度。"""

        return context.mutate(
            request_key,
            "save_scan",
            lambda: context.database.save_scan_session(data.kind, data.raw_text, data.confidence),
        )

    @app.post("/api/v1/scan-sessions/corrections", dependencies=[Depends(auth)])
    async def save_correction(
        data: CorrectionRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        """记录人工修正样本；仅保存数据，不在运行时自动训练模型。"""

        return context.mutate(
            request_key,
            "save_correction",
            lambda: context.database.save_correction(
                data.field_name,
                data.original_text,
                data.corrected_text,
                data.model_version,
                data.confidence,
                data.quality,
            ),
        )

    @app.get("/api/v1/scan-sessions/corrections", dependencies=[Depends(auth)])
    async def corrections() -> list[dict[str, Any]]:
        """列出已保存的 OCR 人工修正样本。"""

        return context.database.list_corrections()

    @app.get("/api/v1/ocr/status", dependencies=[Depends(auth)])
    async def ocr_status() -> dict[str, Any]:
        """校验本地 OCR 依赖和模型摘要，不下载缺失文件。"""

        return context.recognition.status()

    @app.post("/api/v1/web-enrichment/preview", dependencies=[Depends(auth)])
    async def web_preview(data: WebPreviewRequest) -> dict[str, Any]:
        """获取用户指定的公开商品页并提取预览；URL 会先经过 SSRF 校验。"""

        return context.web.preview(data.url)

    @app.post("/api/v1/backups", dependencies=[Depends(auth)])
    async def backup(data: BackupRequest, request_key: str = Depends(key)) -> dict[str, str]:
        """创建 SQLite 一致性备份；未给目录时使用应用默认备份位置。"""

        return context.mutate(
            request_key,
            "backup",
            lambda: context.database.backup(Path(data.directory) if data.directory else None),
        )

    @app.post("/api/v1/backups/restore", dependencies=[Depends(auth)])
    async def restore(data: RestoreRequest, request_key: str = Depends(key)) -> dict[str, str]:
        """验证外部数据库并登记下次启动恢复，不在当前连接上直接覆盖文件。"""

        return context.mutate(
            request_key,
            "restore",
            lambda: context.database.prepare_restore(Path(data.path)),
        )

    return app


class ApiServer(threading.Thread):
    """由桌面应用托管的 Uvicorn 后台线程。

    构造时先在调用线程绑定监听端口，使“端口已占用”能够在窗口出现前同步报告，
    而不是被 Uvicorn 后台线程记录后悄悄退出。预绑定套接字也消除了检查与启动之间
    的竞态窗口。
    """

    def __init__(
        self,
        app: FastAPI,
        host: str = "127.0.0.1",
        port: int = 17635,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        """预绑定 ``host:port`` 并构造后台服务器。

        默认只监听回环地址；调用方若改为非回环地址，必须自行评估令牌保管、主机
        防火墙和明文 HTTP 的风险。绑定失败会同步抛出 ``OSError``。
        """

        super().__init__(name="component-library-api", daemon=True)
        import uvicorn

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind((host, port))
            self._socket.listen(128)
        except OSError:
            self._socket.close()
            raise
        self._server = uvicorn.Server(
            # 桌面程序共用 app.configure_logging 的文件日志；Windows windowed
            # 产物的 stdout/stderr 为 None，不能使用 Uvicorn 的控制台 formatter。
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                log_config=None,
                ws="wsproto",
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
            )
        )

    def run(self) -> None:
        """运行阻塞式 Uvicorn 循环。"""

        self._server.run(sockets=[self._socket])

    def stop(self) -> None:
        """请求 API 完成当前请求后退出。"""

        self._server.should_exit = True
