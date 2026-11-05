"""本机 FastAPI 接口。

桌面界面直接调用同一业务对象，外部设备通过本模块调用，因此库存规则只有一份。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
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
    ReceiveRequest,
    TransferRequest,
    WarehouseCreate,
)
from .order_import import parse_saved_orders
from .recognition import RecognitionEngine
from .web_sources import PublicWebEnricher

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

    def __init__(self, database: Database, token: str, recognition: RecognitionEngine) -> None:
        self.database = database
        self.token = token
        self.recognition = recognition
        self.web = PublicWebEnricher(database)
        self._mutation_lock = threading.RLock()

    def mutate(self, key: str, operation: str, work: Callable[[], T]) -> T:
        """执行带持久幂等记录的 API 写操作。

        进程锁防止两个相同幂等键并发穿过首次查询。库存服务内部仍使用独立的
        ``BEGIN IMMEDIATE`` 事务保证库存一致性；幂等记录负责应用重试语义。
        """

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
            result = work()
            with self.database.write() as connection:
                connection.execute(
                    "INSERT INTO idempotency_records(key,operation,response_json,created_at) VALUES(?,?,?,datetime('now'))",
                    (key, operation, json.dumps(result, ensure_ascii=False)),
                )
            return result


def create_api(context: ApiContext) -> FastAPI:
    """创建本机 API；不配置 CORS，浏览器跨域默认不可访问。"""

    app = FastAPI(title="Component Library Local API", version=__version__)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Any, error: DomainError) -> JSONResponse:
        status = (
            404
            if error.code == "NOT_FOUND"
            else 409
            if error.code in {"CONFLICT", "INSUFFICIENT_STOCK"}
            else 400
        )
        return JSONResponse(
            status_code=status, content={"error": {"code": error.code, "message": error.message}}
        )

    async def auth(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {context.token}":
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "UNAUTHORIZED", "message": "API 令牌无效"}},
            )

    async def key(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> str:
        if not idempotency_key:
            raise DomainError("VALIDATION_ERROR", "写接口必须提供 Idempotency-Key")
        return idempotency_key

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "component-library",
            "version": __version__,
            "ocr": context.recognition.status(),
        }

    @app.get("/api/v1/components", dependencies=[Depends(auth)])
    async def components(q: str = Query(default="")) -> list[dict[str, Any]]:
        return context.database.list_components(q)

    @app.get("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def component_detail(component_id: str) -> dict[str, Any]:
        """返回元件主数据以及可用的类别专属参数。"""

        return context.database.get_component(component_id)

    @app.post("/api/v1/components", dependencies=[Depends(auth)])
    async def create_component(
        data: ComponentCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key, "create_component", lambda: context.database.create_component(data)
        )

    @app.put("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def update_component(
        component_id: str, data: ComponentCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "update_component",
            lambda: context.database.update_component(component_id, data),
        )

    @app.delete("/api/v1/components/{component_id}", dependencies=[Depends(auth)])
    async def delete_component(
        component_id: str, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "delete_component",
            lambda: context.database.delete_component(component_id),
        )

    @app.get("/api/v1/warehouses", dependencies=[Depends(auth)])
    async def warehouses() -> list[dict[str, Any]]:
        return context.database.list_warehouses()

    @app.post("/api/v1/warehouses", dependencies=[Depends(auth)])
    async def create_warehouse(
        data: WarehouseCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key, "create_warehouse", lambda: context.database.create_warehouse(data)
        )

    @app.delete("/api/v1/warehouses/{warehouse_id}", dependencies=[Depends(auth)])
    async def delete_warehouse(
        warehouse_id: str, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "delete_warehouse",
            lambda: context.database.delete_warehouse(warehouse_id),
        )

    @app.put("/api/v1/warehouses/{warehouse_id}", dependencies=[Depends(auth)])
    async def update_warehouse(
        warehouse_id: str, data: WarehouseCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "update_warehouse",
            lambda: context.database.update_warehouse(warehouse_id, data),
        )

    @app.get("/api/v1/locations", dependencies=[Depends(auth)])
    async def locations() -> list[dict[str, Any]]:
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
        return context.mutate(
            request_key, "create_location", lambda: context.database.create_location(data)
        )

    @app.delete("/api/v1/locations/{location_id}", dependencies=[Depends(auth)])
    async def delete_location(location_id: str, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "delete_location",
            lambda: context.database.delete_location(location_id),
        )

    @app.put("/api/v1/locations/{location_id}", dependencies=[Depends(auth)])
    async def update_location(
        location_id: str, data: LocationCreate, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "update_location",
            lambda: context.database.update_location(location_id, data),
        )

    @app.get("/api/v1/stock", dependencies=[Depends(auth)])
    async def stock() -> list[dict[str, Any]]:
        return context.database.list_stock()

    @app.post("/api/v1/inventory/receive", dependencies=[Depends(auth)])
    async def receive(data: ReceiveRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(request_key, "receive", lambda: context.database.receive(data))

    @app.post("/api/v1/inventory/issue", dependencies=[Depends(auth)])
    async def issue(data: IssueRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(request_key, "issue", lambda: context.database.issue(data))

    @app.post("/api/v1/inventory/transfer", dependencies=[Depends(auth)])
    async def transfer(data: TransferRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(request_key, "transfer", lambda: context.database.transfer(data))

    @app.post("/api/v1/inventory/adjust", dependencies=[Depends(auth)])
    async def adjust(data: AdjustRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(request_key, "adjust", lambda: context.database.adjust(data))

    @app.get("/api/v1/inventory/transactions", dependencies=[Depends(auth)])
    async def transactions() -> list[dict[str, Any]]:
        return context.database.list_transactions()

    @app.post("/api/v1/inventory/transactions/{group_id}/reverse", dependencies=[Depends(auth)])
    async def reverse(
        group_id: str, data: ReverseRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key, "reverse", lambda: context.database.reverse_group(group_id, data.reason)
        )

    @app.post("/api/v1/bom/preview", dependencies=[Depends(auth)])
    async def bom_preview(data: BomPreviewRequest) -> list[dict[str, Any]]:
        return preview_bom(context.database, data.rows)

    @app.post("/api/v1/bom/import/preview", dependencies=[Depends(auth)])
    async def bom_import(
        file: UploadFile = File(...), sheet_name: str | None = None
    ) -> dict[str, Any]:
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

    @app.post("/api/v1/bom/commit", dependencies=[Depends(auth)])
    async def bom_commit(data: BomCommitRequest, request_key: str = Depends(key)) -> dict[str, Any]:
        return context.mutate(request_key, "bom_commit", lambda: context.database.commit_bom(data))

    @app.post("/api/v1/order-import/preview", dependencies=[Depends(auth)])
    async def order_import_preview(file: UploadFile = File(...)) -> dict[str, Any]:
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
        return context.recognition.analyze(await file.read()).model_dump()

    @app.post("/api/v1/scan-sessions/parse", dependencies=[Depends(auth)])
    async def scan_parse(data: TextParseRequest) -> dict[str, Any]:
        return parse_label_text(data.raw_text).model_dump()

    @app.post("/api/v1/scan-sessions", dependencies=[Depends(auth)])
    async def save_scan(
        data: ScanSessionRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
        return context.mutate(
            request_key,
            "save_scan",
            lambda: context.database.save_scan_session(data.kind, data.raw_text, data.confidence),
        )

    @app.post("/api/v1/scan-sessions/corrections", dependencies=[Depends(auth)])
    async def save_correction(
        data: CorrectionRequest, request_key: str = Depends(key)
    ) -> dict[str, Any]:
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
        return context.database.list_corrections()

    @app.get("/api/v1/ocr/status", dependencies=[Depends(auth)])
    async def ocr_status() -> dict[str, Any]:
        return context.recognition.status()

    @app.post("/api/v1/web-enrichment/preview", dependencies=[Depends(auth)])
    async def web_preview(data: WebPreviewRequest) -> dict[str, Any]:
        return context.web.preview(data.url)

    @app.post("/api/v1/backups", dependencies=[Depends(auth)])
    async def backup(data: BackupRequest, request_key: str = Depends(key)) -> dict[str, str]:
        return context.mutate(
            request_key,
            "backup",
            lambda: context.database.backup(Path(data.directory) if data.directory else None),
        )

    @app.post("/api/v1/backups/restore", dependencies=[Depends(auth)])
    async def restore(data: RestoreRequest, request_key: str = Depends(key)) -> dict[str, str]:
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

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 17635) -> None:
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
            uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        )

    def run(self) -> None:
        """运行阻塞式 Uvicorn 循环。"""

        self._server.run(sockets=[self._socket])

    def stop(self) -> None:
        """请求 API 完成当前请求后退出。"""

        self._server.should_exit = True
