"""SQLite 持久化与库存事务。

本模块是库存一致性的唯一写入口。每个写操作都显式使用 ``BEGIN IMMEDIATE``，
这样库存检查和扣减位于同一个事务中，不会出现两个并发请求都看到旧库存的情况。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Collection, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .catalogs import (
    CATALOGS,
    SNAPSHOT_TABLE,
    Catalog,
    catalog_path,
    initialize_catalog,
    store_catalog_entry,
)
from .datasheet import (
    can_match_jlc_component,
    jlc_datasheet_url,
    jlc_product_key,
    normalize_datasheet_url,
)
from .label_parser import parse_qr_payload
from .models import (
    ORDER_IMPORT_BATCH_SIZE,
    AdjustRequest,
    BomCommitRequest,
    ComponentCreate,
    IssueRequest,
    JlcParameterGroup,
    JlcProductDraft,
    LocationCreate,
    MosfetDetails,
    OrderImportLine,
    ReceiveRequest,
    ResistorDetails,
    TransactionDetails,
    TransferRequest,
    WarehouseCreate,
)
from .operation_log import log_operation

logger = logging.getLogger(__name__)

EPSILON = 1e-9
EDITABLE_TRANSACTION_QUANTITY = {
    "RECEIVE", "ISSUE", "ADJUST_IN", "ADJUST_OUT", "TRANSFER_IN", "TRANSFER_OUT",
}
TRANSACTION_RECEIPT_KINDS = {"RECEIVE", "ADJUST_IN"}
TRANSACTION_MERGE_SECONDS = 60
CUSTOM_BATCH_CODE = re.compile(r"^CL\d{10}$")
LOCATION_COLORS = (
    "#2563eb", "#c2410c", "#15803d", "#9333ea", "#be123c", "#0e7490",
    "#a16207", "#4338ca", "#047857", "#a21caf", "#b91c1c", "#0369a1",
)
ORDER_SUPPLEMENT_FIELDS = (
    ("manufacturer_part_number", "制造商料号"),
    ("category", "类别"),
    ("brand", "品牌"),
    ("package", "封装"),
    ("product_url", "商品链接"),
    ("datasheet_url", "数据手册"),
    ("notes", "备注"),
    ("specification", "类别参数"),
    ("purchase_date", "购买日期"),
    ("purchase_price", "购入单价"),
)


class DomainError(RuntimeError):
    """可安全展示给用户的业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        """创建可跨 UI/API 边界传递的领域错误。

        ``code`` 供程序稳定判断错误类型，``message`` 面向用户显示；调用方不应
        解析自然语言文本来决定控制流。
        """

        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    """返回可排序的 UTC ISO-8601 时间。"""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: str | None) -> str | None:
    """去除空白并把空字符串统一转换为 ``None``。"""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _datasheet_for_save(
    url: str | None, product_url: str | None, manufacturer_part_number: str | None
) -> str | None:
    return normalize_datasheet_url(url) or jlc_datasheet_url(product_url, manufacturer_part_number)


def _clean_aliases(aliases: list[str]) -> list[str]:
    return [alias for value in aliases if (alias := clean(value)) is not None]


def _transaction_kind(kind: str, source: str | None) -> str:
    if kind.startswith(("TRANSFER_", "ADJUST_")):
        return kind.partition("_")[0]
    if kind == "RECEIVE" and source == "离线订单导入":
        return "ORDER_IMPORT"
    return kind


def _storage_tokens(category: str | None) -> list[str]:
    """把商城类别转换成可用于仓库名称推荐的保守关键字集合。"""

    value = category or ""
    mapping = (
        (("电阻",), ["电阻", "RLC"]),
        (("电容",), ["电容", "RLC"]),
        (("电感", "磁珠"), ["电感", "磁珠", "RLC"]),
        (("MOS", "场效应"), ["MOS", "场效应", "晶体管"]),
        (("二极管", "LED"), ["二极管", "LED"]),
        (("连接器", "端子"), ["连接器", "端子"]),
        (("芯片", "集成电路"), ["芯片", "IC", "集成"]),
    )
    tokens = [value] if value else []
    for markers, aliases in mapping:
        if any(marker.casefold() in value.casefold() for marker in markers):
            tokens.extend(aliases)
    return [token for token in dict.fromkeys(tokens) if token]


def _order_specification(line: OrderImportLine) -> str | None:
    """为通用库存表生成一行类别参数摘要，详细字段仍保存在子表。"""

    if line.resistor:
        values = [
            clean(line.resistor.resistance),
            f"±{line.resistor.tolerance_percent:g}%"
            if line.resistor.tolerance_percent is not None
            else None,
            f"{line.resistor.power_mw:g}mW" if line.resistor.power_mw is not None else None,
        ]
        return " ".join(value for value in values if value) or None
    if line.mosfet:
        values = [
            f"{channel}沟道" if (channel := clean(line.mosfet.channel_type)) else None,
            f"{line.mosfet.mosfet_count}个MOS" if line.mosfet.mosfet_count else None,
            f"{line.mosfet.voltage_v:g}V" if line.mosfet.voltage_v is not None else None,
            f"{line.mosfet.current_a:g}A" if line.mosfet.current_a is not None else None,
        ]
        return " ".join(value for value in values if value) or None
    return None


class Database:
    """线程安全的 SQLite 服务入口。

    每次操作创建独立连接，避免把 ``sqlite3.Connection`` 跨 Qt、API 和工作线程共享。
    进程内写锁用于减少无意义的 SQLite 锁竞争；真正的跨进程保护仍由
    ``BEGIN IMMEDIATE`` 和数据库锁负责。
    """

    def __init__(self, path: Path) -> None:
        """记录数据库路径并创建父目录。

        构造函数不执行迁移；应用启动流程必须显式调用 ``migrate``，便于测试在
        迁移前配置数据库文件。每次读写都会创建独立 SQLite 连接。
        """

        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        # 回滚日志模式支持主库和两个 ATTACH 资料库一起原子提交。
        with closing(sqlite3.connect(self.path)) as setup:
            if setup.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise DomainError("CONFLICT", "无法为元件资料库启用原子提交，请关闭其他实例")
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        """创建配置一致的新连接，调用方负责关闭。"""

        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        self._attach_catalogs(connection, self.path)
        return connection

    @staticmethod
    def _attach_catalogs(connection: sqlite3.Connection, inventory_path: Path) -> None:
        for catalog in CATALOGS:
            connection.execute(
                f"ATTACH DATABASE ? AS {catalog}", (str(catalog_path(inventory_path, catalog)),)
            )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """提供只读连接上下文。"""

        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """开启立即写事务并保证异常时回滚。

        ``BEGIN IMMEDIATE`` 必须早于库存查询执行，否则查询和扣减之间可能被另一写入
        插入，从而违反“库存永不为负”的核心不变量。
        """

        with self._write_lock:
            connection = self.connect()
            started = time.perf_counter()
            try:
                connection.execute("BEGIN IMMEDIATE")
                logger.debug("库存写事务已开始")
                yield connection
                connection.commit()
                logger.debug(
                    "库存写事务已提交；耗时=%d 毫秒",
                    round((time.perf_counter() - started) * 1000),
                )
            except Exception:
                connection.rollback()
                logger.debug("库存写事务已回滚；本次更改未提交", exc_info=True)
                raise
            finally:
                connection.close()

    @log_operation("检查并升级库存数据库")
    def migrate(self) -> None:
        """创建或升级数据库；所有迁移均设计为可重复执行。"""

        with self.write() as connection:
            connection.executescript(SCHEMA)
            self._migrate_v3(connection)
            self._migrate_v6(connection)
            self._migrate_v8(connection)
            self._migrate_v9(connection)
            self._migrate_v10(connection)
            self._migrate_v11(connection)
            self._migrate_v12(connection)
            self._migrate_v13(connection)
            self._migrate_v14(connection)
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            self._migrate_transaction_associations(connection)
            self._migrate_order_transaction_notes(connection)
            self._initialize_catalogs(connection)

    @staticmethod
    def _migrate_order_transaction_notes(connection: sqlite3.Connection) -> None:
        if not connection.execute("SELECT 1 FROM schema_migrations WHERE version=16").fetchone():
            connection.execute(
                """UPDATE inventory_transactions SET notes=NULL
                WHERE kind='RECEIVE' AND (
                    (source='离线订单导入' AND notes='立创订单 ' || (
                        SELECT b.order_number FROM stock_lots s JOIN inventory_batches b ON b.id=s.batch_id
                        WHERE s.id=inventory_transactions.lot_id
                    ))
                    OR (source LIKE '立创订单 %' AND notes IN ('离线订单导入',source))
                )"""
            )
            connection.execute("INSERT INTO schema_migrations VALUES(16,datetime('now'))")

    @staticmethod
    def _migrate_transaction_associations(connection: sqlite3.Connection) -> None:
        if connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=15"
        ).fetchone():
            return
        groups = connection.execute(
            """SELECT t.group_id,t.kind,t.source,span.started,span.ended
            FROM (
                SELECT min(rowid) first_row,min(julianday(created_at)) started,
                max(julianday(created_at)) ended FROM inventory_transactions GROUP BY group_id
            ) span JOIN inventory_transactions t ON t.rowid=span.first_row
            ORDER BY span.first_row"""
        )
        anchor = ""
        previous_end: float | None = None
        previous_kind = ""
        for group_id, kind, source, started, ended in groups:
            operation = _transaction_kind(kind, source)
            if (
                previous_end is not None and started is not None
                and operation == previous_kind
                and 0 <= round((started - previous_end) * 86400) <= TRANSACTION_MERGE_SECONDS
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO transaction_group_associations(group_id,display_group_id)
                    VALUES(?,?)""", (group_id, anchor)
                )
            else:
                anchor = group_id
            previous_kind, previous_end = operation, ended
        connection.execute("INSERT INTO schema_migrations VALUES(15,datetime('now'))")

    def _initialize_catalogs(self, connection: sqlite3.Connection) -> None:
        for catalog in CATALOGS:
            initialize_catalog(connection, catalog)
        connection.execute("UPDATE components SET price=NULL WHERE price=0")
        connection.execute("UPDATE stock_lots SET unit_price=NULL WHERE unit_price=0")
        connection.execute(
            """DELETE FROM component_category_parameters WHERE parameter_key='purchase_price'
            AND trim(value) IN ('0','0.0','0.00','0.000','0.0000','0.00000','0.000000')"""
        )
        has_snapshot = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SNAPSHOT_TABLE,)
        ).fetchone()
        if has_snapshot:
            for catalog in CATALOGS:
                connection.execute(f"DELETE FROM {catalog}.catalog_components")
                connection.execute(
                    f"""INSERT INTO {catalog}.catalog_components(id,category,name,payload)
                    SELECT id,category,name,payload FROM {SNAPSHOT_TABLE} WHERE kind=?""", (catalog,)
                )
            connection.execute(f"DROP TABLE {SNAPSHOT_TABLE}")
        for row in connection.execute(
            """SELECT c.id,c.jlc_matched,EXISTS(
                SELECT 1 FROM order_import_items oi WHERE oi.component_id=c.id
            ) from_order FROM components c
            WHERE NOT EXISTS (SELECT 1 FROM marketplace.catalog_components m WHERE m.id=c.id)
            AND NOT EXISTS (SELECT 1 FROM user.catalog_components u WHERE u.id=c.id)"""
        ).fetchall():
            self._save_catalog_snapshot(
                connection, row["id"], "marketplace" if row["jlc_matched"] or row["from_order"] else "user"
            )

    def _catalog_snapshot(self, connection: sqlite3.Connection, component_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM components WHERE id=?", (component_id,)).fetchone()
        component = self._component(row)
        for key in ("id", "created_at", "updated_at", "archived_at"):
            component.pop(key, None)
        parameters = [dict(item) for item in connection.execute(
            """SELECT parameter_key,label,value,group_name,sort_order FROM component_category_parameters
            WHERE component_id=? ORDER BY sort_order,parameter_key""", (component_id,)
        )]
        details = {}
        for table in ("resistor_details", "mosfet_details"):
            detail = connection.execute(
                f"SELECT * FROM {table} WHERE component_id=?", (component_id,)
            ).fetchone()
            if detail:
                details[table] = dict(detail)
                details[table].pop("component_id")
        return {"component": component, "parameters": parameters, **details}

    def _save_catalog_snapshot(
        self, connection: sqlite3.Connection, component_id: str, catalog: Catalog,
        *, replace_user: bool = False,
    ) -> None:
        store_catalog_entry(connection, catalog, component_id, self._catalog_snapshot(connection, component_id))
        if replace_user:
            connection.execute("DELETE FROM user.catalog_components WHERE id=?", (component_id,))

    def list_catalog(self, catalog: Catalog) -> list[dict[str, Any]]:
        if catalog not in CATALOGS:
            raise ValueError("未知元件资料库")
        with self.read() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT id,category,name FROM {catalog}.catalog_components "
                "ORDER BY category COLLATE NOCASE,name COLLATE NOCASE,id"
            )]

    def get_catalog_component(self, catalog: Catalog, component_id: str) -> dict[str, Any]:
        if catalog not in CATALOGS:
            raise ValueError("未知元件资料库")
        with self.read() as connection:
            row = connection.execute(
                f"SELECT payload FROM {catalog}.catalog_components WHERE id=?", (component_id,)
            ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "元件资料不存在，请刷新")
        payload = json.loads(row["payload"])
        return {
            **payload["component"],
            "category_parameters": payload.get("parameters", []),
            "resistor_details": payload.get("resistor_details"),
            "mosfet_details": payload.get("mosfet_details"),
        }

    @log_operation("导出元件资料库")
    def export_catalog(self, catalog: Catalog, destination: Path) -> int:
        """导出指定资料库的独立 SQLite 文件，保留库存数据库的内部编号。"""

        if catalog not in CATALOGS:
            raise ValueError("未知元件资料库")
        managed = [self.path, *(catalog_path(self.path, kind) for kind in CATALOGS)]
        if destination.resolve() in {path.resolve() for path in managed}:
            raise DomainError("VALIDATION_ERROR", "请选择独立的导出文件")
        with TemporaryDirectory(dir=destination.parent) as directory, self.read() as source:
            path = Path(directory) / "catalog.db"
            with closing(sqlite3.connect(path)) as target:
                source.backup(target, name=catalog)
                count = int(target.execute("SELECT count(*) FROM catalog_components").fetchone()[0])
            path.replace(destination)
            return count

    @log_operation("导入元件资料库")
    def import_catalog(self, catalog: Catalog, source: Path) -> int:
        """合并资料库，以稳定编号或精确的立创/自定义编号对齐现有库存元件。"""

        if catalog not in CATALOGS:
            raise ValueError("未知元件资料库")
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as file:
                if file.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("资料库没有通过完整性检查")
                metadata = file.execute("SELECT kind,version FROM catalog_metadata").fetchall()
                if metadata != [(catalog, 1)]:
                    raise ValueError("资料库类型或版本不匹配")
                rows = file.execute("SELECT id,payload FROM catalog_components").fetchall()
            with self.write() as connection:
                seen: set[str] = set()
                for source_id, raw in rows:
                    payload = json.loads(raw)
                    data = ComponentCreate.model_validate(payload["component"])
                    component_id = self._catalog_component_id(connection, source_id, data)
                    if component_id in seen:
                        raise ValueError("多个导入元件指向同一库存元件")
                    seen.add(component_id)
                    values = data.model_dump()
                    values["name"] = clean(data.name)
                    values["aliases"] = _clean_aliases(data.aliases)
                    for field, value in values.items():
                        if isinstance(value, str):
                            values[field] = clean(value)
                    values["datasheet_url"] = _datasheet_for_save(
                        data.datasheet_url, data.product_url, data.manufacturer_part_number
                    )
                    payload["component"] = values
                    if catalog == "user" or not connection.execute(
                        "SELECT 1 FROM user.catalog_components WHERE id=?", (component_id,)
                    ).fetchone():
                        self._apply_catalog_entry(connection, component_id, payload)
                        payload = self._catalog_snapshot(connection, component_id)
                    store_catalog_entry(connection, catalog, component_id, payload)
            return len(rows)
        except (sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as error:
            raise DomainError("VALIDATION_ERROR", f"无法导入元件资料库：{error}") from error

    @staticmethod
    def _catalog_component_id(
        connection: sqlite3.Connection, source_id: str, data: ComponentCreate
    ) -> str:
        identifier = str(uuid.UUID(source_id))
        matches = connection.execute(
            """SELECT id FROM components WHERE id=?
            OR upper(lcsc_part_number)=upper(?) OR custom_part_number=?""",
            (identifier, clean(data.lcsc_part_number), clean(data.custom_part_number)),
        ).fetchall()
        if len(matches) > 1:
            raise ValueError("元件编号存在冲突")
        return str(matches[0][0]) if matches else identifier

    @staticmethod
    def _apply_catalog_entry(
        connection: sqlite3.Connection, component_id: str, payload: dict[str, Any]
    ) -> None:
        values = dict(payload["component"])
        values["aliases"] = json.dumps(values["aliases"], ensure_ascii=False)
        values["jlc_matched"] = int(bool(values["jlc_matched"]))
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        assignments = ", ".join(f"{field}=excluded.{field}" for field in values)
        timestamp = utc_now()
        connection.execute(
            f"""INSERT INTO components(id,created_at,updated_at,{columns})
            VALUES(?,?,?,{placeholders}) ON CONFLICT(id) DO UPDATE SET
            updated_at=excluded.updated_at,{assignments}""",
            (component_id, timestamp, timestamp, *values.values()),
        )
        connection.execute("DELETE FROM component_category_parameters WHERE component_id=?", (component_id,))
        parameters = [
            (component_id, str(item["parameter_key"]), str(item["label"]).strip(),
             str(item["value"]).strip(), str(item.get("group_name") or "").strip() or "类别参数", index)
            for index, item in enumerate(payload.get("parameters", []))
            if str(item["label"]).strip() and str(item["value"]).strip()
        ]
        connection.executemany(
            """INSERT INTO component_category_parameters
            (component_id,parameter_key,label,value,group_name,sort_order) VALUES(?,?,?,?,?,?)""",
            parameters,
        )
        for table, model in (("resistor_details", ResistorDetails), ("mosfet_details", MosfetDetails)):
            connection.execute(f"DELETE FROM {table} WHERE component_id=?", (component_id,))
            if detail := payload.get(table):
                fields = model.model_validate(detail).model_dump()
                fields["source_description"] = clean(detail.get("source_description"))
                names = ", ".join(fields)
                placeholders = ", ".join("?" for _ in fields)
                connection.execute(
                    f"INSERT INTO {table}(component_id,{names}) VALUES(?,{placeholders})",
                    (component_id, *fields.values()),
                )

    @staticmethod
    def _next_location_color(used: set[str]) -> str:
        for color in LOCATION_COLORS:
            if color not in used:
                return color
        color = f"#{uuid.uuid4().hex[:6]}"
        while color in used:
            color = f"#{uuid.uuid4().hex[:6]}"
        return color

    def _migrate_v14(self, connection: sqlite3.Connection) -> None:
        """为已有仓库补齐持久化的默认颜色。"""

        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        if not any(row["name"] == "color" for row in connection.execute("PRAGMA table_info(locations)")):
            connection.execute("ALTER TABLE locations ADD COLUMN color TEXT")
        used = {row[0] for row in connection.execute("SELECT color FROM locations WHERE color IS NOT NULL")}
        missing = connection.execute(
            "SELECT id FROM locations WHERE color IS NULL ORDER BY created_at,id"
        ).fetchall()
        for row in missing:
            color = self._next_location_color(used)
            connection.execute("UPDATE locations SET color=? WHERE id=?", (color, row["id"]))
            used.add(color)
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(14,datetime('now'))")

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """补齐元件采购字段，并禁止后续写入小数库存。

        SQLite 不能安全地原地修改旧表字段类型，所以旧库保留其 REAL 列，但通过模型
        和触发器双重保证新写入都是整数；新建数据库则直接使用 INTEGER。
        """

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(components)").fetchall()
        }
        additions = {
            "category": "TEXT",
            "price": "REAL",
            "purchase_date": "TEXT",
            "product_url": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE components ADD COLUMN {name} {sql_type}")
        lot_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(stock_lots)").fetchall()
        }
        if "purchase_date" not in lot_columns:
            connection.execute("ALTER TABLE stock_lots ADD COLUMN purchase_date TEXT")
        connection.executescript(INTEGER_TRIGGERS)
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3,datetime('now'))")

    @staticmethod
    def _migrate_v6(connection: sqlite3.Connection) -> None:
        """增加安全归档标记和通用类别参数表。"""

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(components)").fetchall()
        }
        if "archived_at" not in columns:
            connection.execute("ALTER TABLE components ADD COLUMN archived_at TEXT")
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6,datetime('now'))")

    @staticmethod
    def _migrate_v10(connection: sqlite3.Connection) -> None:
        """记录元件是否已由商品详情页补全，避免反复访问相同页面。"""

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(components)").fetchall()
        }
        if "jlc_matched" not in columns:
            connection.execute(
                "ALTER TABLE components ADD COLUMN jlc_matched INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(10,datetime('now'))")

    @staticmethod
    def _migrate_v11(connection: sqlite3.Connection) -> None:
        """增加移动设备、统一会话和 BOM 取货任务持久化结构。"""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paired_devices(
             id TEXT PRIMARY KEY,name TEXT NOT NULL,device_type TEXT NOT NULL,
             token_hash TEXT NOT NULL UNIQUE,client_version TEXT NOT NULL,
             created_at TEXT NOT NULL,last_seen_at TEXT,revoked_at TEXT);
            CREATE TABLE IF NOT EXISTS operation_sessions(
             id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL,
             revision INTEGER NOT NULL DEFAULT 0,snapshot_json TEXT NOT NULL,
             result_json TEXT,created_by TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS session_commands(
             session_id TEXT NOT NULL REFERENCES operation_sessions(id),command_id TEXT NOT NULL,
             response_json TEXT NOT NULL,created_at TEXT NOT NULL,
             PRIMARY KEY(session_id,command_id));
            CREATE TABLE IF NOT EXISTS bom_pick_tasks(
             id TEXT PRIMARY KEY,session_id TEXT NOT NULL UNIQUE REFERENCES operation_sessions(id),
             name TEXT NOT NULL,source_filename TEXT,state TEXT NOT NULL,
             transaction_group_id TEXT,created_at TEXT NOT NULL,completed_at TEXT);
            CREATE TABLE IF NOT EXISTS bom_pick_items(
             id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES bom_pick_tasks(id),
             row_number INTEGER NOT NULL,mapping_key TEXT,raw_json TEXT NOT NULL,
             planned_component_id TEXT NOT NULL REFERENCES components(id),
             required_quantity INTEGER NOT NULL CHECK(required_quantity>0));
            CREATE TABLE IF NOT EXISTS bom_pick_entries(
             id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES bom_pick_items(id),
             actual_component_id TEXT NOT NULL REFERENCES components(id),
             location_id TEXT REFERENCES locations(id),batch_id TEXT REFERENCES inventory_batches(id),
             quantity INTEGER NOT NULL CHECK(quantity>0),method TEXT NOT NULL,
             substitute_reason TEXT,actor TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bom_substitution_mappings(
             id TEXT PRIMARY KEY,mapping_key TEXT NOT NULL,
             substitute_component_id TEXT NOT NULL REFERENCES components(id),
             reason TEXT,created_at TEXT NOT NULL,
             UNIQUE(mapping_key,substitute_component_id));
            CREATE INDEX IF NOT EXISTS idx_pick_items_task ON bom_pick_items(task_id,row_number);
            CREATE INDEX IF NOT EXISTS idx_pick_entries_item ON bom_pick_entries(item_id,created_at);
            INSERT OR IGNORE INTO schema_migrations VALUES(11,datetime('now'));
            """
        )

    @staticmethod
    def _migrate_v12(connection: sqlite3.Connection) -> None:
        """为 BOM 取货行保存用户指定的具体仓库约束。"""

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(bom_pick_items)").fetchall()
        }
        if "preferred_location_id" not in columns:
            connection.execute(
                "ALTER TABLE bom_pick_items ADD COLUMN preferred_location_id TEXT "
                "REFERENCES locations(id)"
            )
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(12,datetime('now'))")

    @staticmethod
    def _migrate_v13(connection: sqlite3.Connection) -> None:
        """记录新流水的确认端，保留原有来源和备注。"""

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(inventory_transactions)")}
        if "operation_platform" not in columns:
            connection.execute("ALTER TABLE inventory_transactions ADD COLUMN operation_platform TEXT")
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(13,datetime('now'))")

    @staticmethod
    def _migrate_v8(connection: sqlite3.Connection) -> None:
        """为动态商城参数保留页面分组，旧参数归入通用类别参数组。"""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(component_category_parameters)"
            ).fetchall()
        }
        if "group_name" not in columns:
            connection.execute(
                "ALTER TABLE component_category_parameters "
                "ADD COLUMN group_name TEXT NOT NULL DEFAULT '类别参数'"
            )
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(8,datetime('now'))")

    @staticmethod
    def _migrate_v9(connection: sqlite3.Connection) -> None:
        """增加可跨仓库追踪的采购批次，并回填可唯一定位的立创订单库存。"""

        connection.execute(
            """CREATE TABLE IF NOT EXISTS inventory_batches(
            id TEXT PRIMARY KEY,component_id TEXT NOT NULL REFERENCES components(id),
            source_type TEXT NOT NULL CHECK(source_type IN ('JLC','CUSTOM')),
            external_key TEXT NOT NULL UNIQUE,order_number TEXT,lcsc_part_number TEXT,
            custom_code TEXT UNIQUE,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)"""
        )
        lot_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(stock_lots)").fetchall()
        }
        if "batch_id" not in lot_columns:
            connection.execute(
                "ALTER TABLE stock_lots ADD COLUMN batch_id TEXT REFERENCES inventory_batches(id)"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_lots_batch ON stock_lots(batch_id,received_at)"
        )
        rows = connection.execute(
            """SELECT oi.order_number,oi.lcsc_part_number,oi.component_id,
            oi.transaction_group_id FROM order_import_items oi"""
        ).fetchall()
        for row in rows:
            external_key = f"JLC:{row['order_number'].upper()}:{row['lcsc_part_number'].upper()}"
            batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, external_key))
            connection.execute(
                """INSERT OR IGNORE INTO inventory_batches(
                id,component_id,source_type,external_key,order_number,lcsc_part_number,
                metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    row["component_id"],
                    "JLC",
                    external_key,
                    row["order_number"].upper(),
                    row["lcsc_part_number"].upper(),
                    "{}",
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE stock_lots SET batch_id=? WHERE batch_id IS NULL AND id IN (
                SELECT lot_id FROM inventory_transactions
                WHERE group_id=? AND component_id=? AND quantity_delta>0)""",
                (batch_id, row["transaction_group_id"], row["component_id"]),
            )
        connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(9,datetime('now'))")

    @log_operation("检查数据库完整性")
    def integrity_check(self) -> None:
        """验证数据库物理完整性，不通过时抛出业务错误。"""

        with self.read() as connection:
            for schema in ("main", *CATALOGS):
                result = connection.execute(f"PRAGMA {schema}.integrity_check").fetchone()[0]
                if result != "ok":
                    raise DomainError("DATABASE_CORRUPT", f"{schema} 数据库完整性检查失败：{result}")

    @log_operation("创建元件")
    def create_component(self, data: ComponentCreate) -> dict[str, Any]:
        """创建元件并返回完整记录。"""

        component_id = str(uuid.uuid4())
        timestamp = utc_now()
        values = (
            component_id,
            data.name.strip(),
            json.dumps(_clean_aliases(data.aliases), ensure_ascii=False),
            clean(data.lcsc_part_number),
            clean(data.manufacturer_part_number),
            clean(data.custom_part_number),
            clean(data.brand),
            clean(data.package),
            clean(data.category),
            clean(data.specification),
            data.price,
            clean(data.purchase_date),
            clean(data.product_url),
            _datasheet_for_save(data.datasheet_url, data.product_url, data.manufacturer_part_number),
            clean(data.notes),
            int(bool(data.jlc_matched)),
            timestamp,
        )
        try:
            with self.write() as connection:
                connection.execute(
                    """INSERT INTO components(
                    id,name,aliases,lcsc_part_number,manufacturer_part_number,
                    custom_part_number,brand,package,category,specification,price,
                    purchase_date,product_url,datasheet_url,notes,jlc_matched,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, timestamp),
                )
                self._save_catalog_snapshot(
                    connection, component_id, "marketplace" if data.jlc_matched else "user"
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "元件名称或编号与现有记录冲突") from error
        return self.get_component(component_id)

    def get_component(self, component_id: str) -> dict[str, Any]:
        """按 ID 读取元件，并附带已经存在的类别专属参数。"""

        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM components WHERE id=?", (component_id,)
            ).fetchone()
            resistor = connection.execute(
                "SELECT * FROM resistor_details WHERE component_id=?", (component_id,)
            ).fetchone()
            mosfet = connection.execute(
                "SELECT * FROM mosfet_details WHERE component_id=?", (component_id,)
            ).fetchone()
            parameters = connection.execute(
                """SELECT parameter_key,label,value,group_name
                FROM component_category_parameters
                WHERE component_id=? ORDER BY sort_order,parameter_key""",
                (component_id,),
            ).fetchall()
        if row is None:
            raise DomainError("NOT_FOUND", "元件不存在")
        result = self._component(row)
        result["resistor_details"] = dict(resistor) if resistor else None
        result["mosfet_details"] = dict(mosfet) if mosfet else None
        result["category_parameters"] = [dict(item) for item in parameters]
        return result

    def list_component_categories(self) -> list[str]:
        """返回现有元件使用过的类别，供可编辑类别下拉框复用。"""

        with self.read() as connection:
            rows = connection.execute(
                """SELECT DISTINCT trim(category) AS category
                FROM components
                WHERE archived_at IS NULL AND category IS NOT NULL AND trim(category)<>''
                ORDER BY category COLLATE NOCASE"""
            ).fetchall()
        return [str(row["category"]) for row in rows]

    def list_category_parameter_definitions(self, category: str) -> list[dict[str, str]]:
        """汇总同一元件类别已经使用的参数定义。

        参数值仍属于单个元件，本方法只返回稳定键、显示名称和分组。入库表单使用
        这些定义生成同类别的公共参数列表，并让当前元件没有的参数保持为空。
        """

        if not category.strip():
            return []
        with self.read() as connection:
            rows = connection.execute(
                """SELECT p.parameter_key,p.label,p.group_name,p.sort_order,c.updated_at
                FROM component_category_parameters AS p
                JOIN components AS c ON c.id=p.component_id
                WHERE c.archived_at IS NULL
                  AND trim(c.category)=trim(?) COLLATE NOCASE
                ORDER BY p.sort_order,c.updated_at DESC,p.parameter_key""",
                (category,),
            ).fetchall()
        definitions: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            key = str(row["parameter_key"])
            if key in seen:
                continue
            seen.add(key)
            definitions.append(
                {
                    "parameter_key": key,
                    "label": str(row["label"]),
                    "group_name": str(row["group_name"] or "类别参数"),
                }
            )
        return definitions

    @log_operation("保存元件参数")
    def save_component_parameters(
        self, component_id: str, parameters: dict[str, tuple[str, str]]
    ) -> list[dict[str, str]]:
        """保存手工入库表单中的结构化类别参数。

        ``parameters`` 的键为稳定字段名，值为 ``(显示名称, 用户输入)``。空值不会写入；
        主表中的 ``specification`` 只保留供搜索和列表展示使用的派生摘要，不再作为输入项。
        """

        cleaned = [
            (key, label.strip(), value.strip())
            for key, (label, value) in parameters.items()
            if value.strip()
        ]
        with self.write() as connection:
            self._require(connection, "components", component_id, "元件")
            previous = self._catalog_snapshot(connection, component_id)
            connection.execute(
                "DELETE FROM component_category_parameters WHERE component_id=?", (component_id,)
            )
            for order, (key, label, value) in enumerate(cleaned):
                connection.execute(
                    """INSERT INTO component_category_parameters(
                    component_id,parameter_key,label,value,sort_order) VALUES(?,?,?,?,?)""",
                    (component_id, key, label, value, order),
                )
            summary = "；".join(f"{label}: {value}" for _, label, value in cleaned) or None
            connection.execute(
                "UPDATE components SET specification=?,updated_at=? WHERE id=?",
                (summary, utc_now(), component_id),
            )
            if self._catalog_snapshot(connection, component_id) != previous:
                self._save_catalog_snapshot(connection, component_id, "user")
        return [
            {"parameter_key": key, "label": label, "value": value} for key, label, value in cleaned
        ]

    @log_operation("保存元件参数分组")
    def save_component_parameter_groups(
        self, component_id: str, groups: list[JlcParameterGroup], *, marketplace: bool = False
    ) -> list[dict[str, str]]:
        """保存带页面分组的动态参数，并同步派生规格摘要。

        调用方必须传入用户最终确认后的完整参数集合；本方法与旧接口一样采用整体替换，
        从而避免已删除的表单字段残留在数据库中。
        """

        with self.write() as connection:
            self._require(connection, "components", component_id, "元件")
            previous = self._catalog_snapshot(connection, component_id)
            cleaned = self._replace_component_parameter_groups(connection, component_id, groups)
            if marketplace:
                self._save_catalog_snapshot(connection, component_id, "marketplace", replace_user=True)
            elif self._catalog_snapshot(connection, component_id) != previous:
                self._save_catalog_snapshot(connection, component_id, "user")
        return [
            {
                "parameter_key": key,
                "label": label,
                "value": value,
                "group_name": group_name or "类别参数",
            }
            for key, label, value, group_name in cleaned
        ]

    @staticmethod
    def _replace_component_parameter_groups(
        connection: sqlite3.Connection, component_id: str, groups: list[JlcParameterGroup]
    ) -> list[tuple[str, str, str, str]]:
        cleaned = [
            (parameter.key, parameter.label.strip(), value, group.name.strip() or "类别参数")
            for group in groups
            for parameter in group.parameters
            if parameter.label.strip() and (value := clean(parameter.value)) is not None
        ]
        connection.execute(
            "DELETE FROM component_category_parameters WHERE component_id=?", (component_id,)
        )
        connection.executemany(
            """INSERT INTO component_category_parameters(
            component_id,parameter_key,label,value,sort_order,group_name) VALUES(?,?,?,?,?,?)""",
            [(component_id, key, label, value, index, group_name)
             for index, (key, label, value, group_name) in enumerate(cleaned)],
        )
        summary = "；".join(f"{label}: {value}" for key, label, value, _ in cleaned
                           if key != "purchase_price") or None
        connection.execute(
            "UPDATE components SET specification=?,updated_at=? WHERE id=?",
            (summary, utc_now(), component_id),
        )
        return cleaned

    @log_operation("删除或归档元件")
    def delete_component(self, component_id: str) -> dict[str, Any]:
        """删除未使用元件，或安全归档库存为零但已有历史的元件。

        有可用库存时拒绝操作。完全没有业务引用的误建元件会物理删除；已有不可变流水、
        BOM 或订单记录的元件仅从活动库存列表归档，从而保留审计历史。
        """

        with self.write() as connection:
            self._require(connection, "components", component_id, "元件")
            remaining = connection.execute(
                "SELECT COALESCE(SUM(remaining_quantity),0) FROM stock_lots WHERE component_id=?",
                (component_id,),
            ).fetchone()[0]
            if int(remaining or 0) > 0:
                raise DomainError("CONFLICT", "元件仍有库存，请先出库或盘点调整为零")
            reference_tables = (
                "stock_lots",
                "inventory_transactions",
                "supplier_parts",
                "bom_lines",
                "part_mappings",
                "order_import_items",
            )
            referenced = any(
                connection.execute(
                    f"SELECT 1 FROM {table} WHERE component_id=? LIMIT 1", (component_id,)
                ).fetchone()
                for table in reference_tables
            )
            if referenced:
                archived_at = utc_now()
                connection.execute(
                    "UPDATE components SET archived_at=?,updated_at=? WHERE id=?",
                    (archived_at, archived_at, component_id),
                )
                return {"id": component_id, "deleted": True, "mode": "archived"}
            connection.execute(
                "DELETE FROM component_category_parameters WHERE component_id=?", (component_id,)
            )
            connection.execute("DELETE FROM resistor_details WHERE component_id=?", (component_id,))
            connection.execute("DELETE FROM mosfet_details WHERE component_id=?", (component_id,))
            connection.execute("DELETE FROM components WHERE id=?", (component_id,))
            for catalog in CATALOGS:
                connection.execute(f"DELETE FROM {catalog}.catalog_components WHERE id=?", (component_id,))
            return {"id": component_id, "deleted": True, "mode": "deleted"}

    @log_operation("保存元件资料")
    def update_component(
        self, component_id: str, data: ComponentCreate,
        *, parameter_groups: list[JlcParameterGroup] | None = None, force_user_copy: bool = False,
    ) -> dict[str, Any]:
        """保存用户确认后的元件主数据，并刷新最后更改时间。"""

        try:
            with self.write() as connection:
                self._update_component_fields(
                    connection, component_id, data, parameter_groups, force_user_copy=force_user_copy
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "元件名称或编号与现有记录冲突") from error
        return self.get_component(component_id)

    def _update_component_fields(
        self, connection: sqlite3.Connection, component_id: str, data: ComponentCreate,
        parameter_groups: list[JlcParameterGroup] | None, *, force_user_copy: bool = False,
    ) -> None:
        self._require(connection, "components", component_id, "元件")
        previous = self._catalog_snapshot(connection, component_id)
        old_part = previous["component"].get("lcsc_part_number")
        new_part = clean(data.lcsc_part_number)
        if (old_part or "").upper() != (new_part or "").upper():
            self._rekey_component_orders(connection, component_id, new_part)
        connection.execute(
            """UPDATE components SET name=?,aliases=?,lcsc_part_number=?,
            manufacturer_part_number=?,custom_part_number=?,brand=?,package=?,category=?,
            specification=?,price=?,purchase_date=?,product_url=?,datasheet_url=?,notes=?,
            jlc_matched=COALESCE(?,jlc_matched),
            updated_at=? WHERE id=?""",
            (
                data.name.strip(),
                json.dumps(_clean_aliases(data.aliases), ensure_ascii=False),
                clean(data.lcsc_part_number),
                clean(data.manufacturer_part_number),
                clean(data.custom_part_number),
                clean(data.brand),
                clean(data.package),
                clean(data.category),
                clean(data.specification),
                data.price,
                clean(data.purchase_date),
                clean(data.product_url),
                _datasheet_for_save(
                    data.datasheet_url, data.product_url, data.manufacturer_part_number
                ),
                clean(data.notes),
                int(data.jlc_matched) if data.jlc_matched is not None else None,
                utc_now(),
                component_id,
            ),
        )
        if parameter_groups is not None:
            self._replace_component_parameter_groups(connection, component_id, parameter_groups)
        if data.jlc_matched:
            self._save_catalog_snapshot(connection, component_id, "marketplace", replace_user=True)
        elif force_user_copy or self._catalog_snapshot(connection, component_id) != previous:
            self._save_catalog_snapshot(connection, component_id, "user")

    @staticmethod
    def _rekey_component_orders(
        connection: sqlite3.Connection, component_id: str, part_number: str | None
    ) -> None:
        linked_batch = connection.execute(
            "SELECT 1 FROM inventory_batches WHERE component_id=? AND source_type='JLC' LIMIT 1",
            (component_id,),
        ).fetchone()
        if not linked_batch:
            return
        if part_number is None or not re.fullmatch(r"C[0-9]{4,10}", part_number, re.I):
            raise DomainError("CONFLICT", "已有立创订单批次，必须保留有效的立创编号")
        part = part_number.upper()
        connection.execute(
            """UPDATE inventory_batches SET lcsc_part_number=?,
            external_key='JLC:' || order_number || ':' || ? WHERE component_id=? AND source_type='JLC'""",
            (part, part, component_id),
        )
        connection.execute(
            "UPDATE order_import_items SET lcsc_part_number=? WHERE component_id=?",
            (part, component_id),
        )

    @log_operation("规格化旧版元件资料")
    def normalize_component_data(self) -> int:
        """在同一事务内规格化旧版元件主资料、别名及类别参数，返回更新的元件数。"""

        component_fields = (
            "name", "lcsc_part_number", "manufacturer_part_number", "custom_part_number",
            "brand", "package", "category", "specification", "purchase_date", "product_url",
            "notes",
        )
        parameter_tables = (
            ("component_category_parameters", ("label", "value", "group_name")),
            ("resistor_details", ("resistance", "technology", "source_description")),
            ("mosfet_details", ("channel_type", "source_description")),
        )
        updated: set[str] = set()
        try:
            with self.write() as connection:
                for row in connection.execute("SELECT * FROM components").fetchall():
                    fields = {field: clean(row[field]) for field in component_fields}
                    fields["datasheet_url"] = _datasheet_for_save(
                        row["datasheet_url"], fields["product_url"], fields["manufacturer_part_number"]
                    )
                    aliases = self._component(row)["aliases"]
                    if isinstance(aliases, list) and all(isinstance(alias, str) for alias in aliases):
                        cleaned_aliases = _clean_aliases(aliases)
                        if cleaned_aliases != aliases:
                            fields["aliases"] = json.dumps(cleaned_aliases, ensure_ascii=False)
                    changes = {field: value for field, value in fields.items() if value != row[field]}
                    if changes:
                        assignments = ", ".join(f"{field}=?" for field in changes)
                        connection.execute(
                            f"UPDATE components SET {assignments} WHERE id=?",
                            (*changes.values(), row["id"]),
                        )
                        updated.add(row["id"])
                for table, fields_to_trim in parameter_tables:
                    assignments = ", ".join(f"{field}=?" for field in fields_to_trim)
                    for row in connection.execute(f"SELECT rowid,* FROM {table}").fetchall():
                        values = tuple(
                            row[field].strip() if row[field] is not None else None
                            for field in fields_to_trim
                        )
                        if values != tuple(row[field] for field in fields_to_trim):
                            connection.execute(
                                f"UPDATE {table} SET {assignments} WHERE rowid=?",
                                (*values, row["rowid"]),
                            )
                            updated.add(row["component_id"])
                timestamp = utc_now()
                connection.executemany(
                    "UPDATE components SET updated_at=? WHERE id=?",
                    [(timestamp, component_id) for component_id in updated],
                )
                for catalog in CATALOGS:
                    for entry in connection.execute(
                        f"SELECT id,payload FROM {catalog}.catalog_components"
                    ).fetchall():
                        payload = json.loads(entry["payload"])
                        component = payload["component"]
                        for field in component_fields:
                            component[field] = clean(component.get(field))
                        if isinstance(component.get("aliases"), list):
                            component["aliases"] = _clean_aliases(component["aliases"])
                        for parameter in payload.get("parameters", []):
                            for field in ("label", "value", "group_name"):
                                if isinstance(parameter.get(field), str):
                                    parameter[field] = parameter[field].strip()
                        for table, fields_to_trim in parameter_tables[1:]:
                            if detail := payload.get(table):
                                for field in fields_to_trim:
                                    detail[field] = clean(detail.get(field))
                        datasheet = _datasheet_for_save(
                            component.get("datasheet_url"), component.get("product_url"),
                            component.get("manufacturer_part_number"),
                        )
                        component["datasheet_url"] = datasheet
                        if payload != json.loads(entry["payload"]):
                            store_catalog_entry(connection, catalog, entry["id"], payload)
                            updated.add(entry["id"])
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "旧版元件清理后有空名称或重复编号，规格化已回滚") from error
        return len(updated)

    def list_components(
        self, query: str = "", *, include_purchase_price: bool = False
    ) -> list[dict[str, Any]]:
        """按元件资料搜索，并附加由订单导入记录汇总出的订单号。"""

        needle = query.strip()
        price_column = (
            """(SELECT p.value FROM component_category_parameters p
                WHERE p.component_id=c.id AND p.parameter_key='purchase_price') purchase_price,"""
            if include_purchase_price else ""
        )
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT c.*, {price_column}
                EXISTS(SELECT 1 FROM user.catalog_components u WHERE u.id=c.id) user_catalog,
                (SELECT GROUP_CONCAT(DISTINCT oi.order_number)
                 FROM order_import_items oi WHERE oi.component_id=c.id) order_numbers,
                (SELECT MAX(received_at) FROM stock_lots s WHERE s.component_id=c.id) last_received_at
                FROM components c WHERE c.archived_at IS NULL AND (?='' OR instr(lower(name),lower(?))>0
                OR instr(lower(aliases),lower(?))>0
                OR instr(lower(COALESCE(lcsc_part_number,'')),lower(?))>0
                OR instr(lower(COALESCE(manufacturer_part_number,'')),lower(?))>0
                OR instr(lower(COALESCE(custom_part_number,'')),lower(?))>0
                OR instr(lower(COALESCE(brand,'')),lower(?))>0
                OR instr(lower(COALESCE(package,'')),lower(?))>0
                OR instr(lower(COALESCE(category,'')),lower(?))>0
                OR instr(lower(COALESCE(notes,'')),lower(?))>0
                OR EXISTS(SELECT 1 FROM order_import_items oi
                          WHERE oi.component_id=c.id
                            AND instr(lower(oi.order_number),lower(?))>0))
                ORDER BY updated_at DESC""",
                (needle, *([needle] * 10)),
            ).fetchall()
        return [self._component(row) for row in rows]

    def next_unmatched_jlc_component(self, after_id: str) -> dict[str, Any] | None:
        """轮换待匹配元件，使暂时查不到的商品不会挡住队列。"""

        with self.read() as connection:
            items = connection.execute(
                """SELECT id,name,lcsc_part_number,product_url FROM components
                WHERE archived_at IS NULL AND jlc_matched=0
                ORDER BY (id<=?),id""",
                (after_id,),
            ).fetchall()
        return next(
            (dict(item) for item in items
             if can_match_jlc_component(item["lcsc_part_number"], item["product_url"])),
            None,
        )

    def jlc_match_counts(self) -> tuple[int, int]:
        with self.read() as connection:
            rows = connection.execute(
                "SELECT lcsc_part_number,product_url FROM components WHERE archived_at IS NULL AND jlc_matched=0"
            ).fetchall()
        return len(rows), sum(can_match_jlc_component(row[0], row[1]) for row in rows)

    @log_operation("保存立创商城匹配资料")
    def apply_jlc_product_match(self, component_id: str, draft: JlcProductDraft) -> bool:
        """按已保存商品链接或立创编号校验后，补齐仍未匹配的元件资料。"""

        with self.write() as connection:
            item = connection.execute(
                """SELECT * FROM components WHERE id=?
                AND archived_at IS NULL AND jlc_matched=0""",
                (component_id,),
            ).fetchone()
            if item is None:
                return False
            part_number = (item["lcsc_part_number"] or "").strip().upper()
            product_key = jlc_product_key(item["product_url"])
            if product_key:
                draft_key = jlc_product_key(draft.product_url)
                if product_key != draft_key and product_key != draft.lcsc_part_number:
                    return False
            if re.fullmatch(r"C[0-9]{4,10}", part_number):
                if part_number != draft.lcsc_part_number:
                    return False
            elif not product_key:
                return False
            else:
                self._rekey_component_orders(connection, component_id, draft.lcsc_part_number)
                connection.execute(
                    "UPDATE components SET lcsc_part_number=? WHERE id=?",
                    (draft.lcsc_part_number, component_id),
                )
            self._apply_jlc_draft(connection, item, draft)
            self._save_catalog_snapshot(connection, component_id, "marketplace", replace_user=True)
            return True

    @staticmethod
    def _apply_jlc_draft(
        connection: sqlite3.Connection, item: sqlite3.Row, draft: JlcProductDraft
    ) -> None:
        component_id = str(item["id"])
        fields: dict[str, str | int] = {
            field: value for field in (
                "manufacturer_part_number", "brand", "package", "category",
                "product_url",
            )
            if not clean(item[field]) and (value := clean(getattr(draft, field))) is not None
        }
        if not clean(item["datasheet_url"]):
            datasheet = _datasheet_for_save(
                draft.datasheet_url, draft.product_url, draft.manufacturer_part_number
            )
            if datasheet:
                fields["datasheet_url"] = datasheet
        parameters = [
            (parameter.key, parameter.label.strip(), parameter.value.strip(), group.name.strip() or "类别参数")
            for group in draft.parameter_groups for parameter in group.parameters
            if parameter.label.strip() and parameter.value.strip()
        ]
        connection.executemany(
            """INSERT OR IGNORE INTO component_category_parameters(
            component_id,parameter_key,label,value,group_name,sort_order)
            VALUES(?,?,?,?,?,?)""",
            [(component_id, *parameter, index) for index, parameter in enumerate(parameters)],
        )
        if parameters and not clean(item["specification"]):
            rows = connection.execute(
                """SELECT label,value FROM component_category_parameters WHERE component_id=?
                AND parameter_key<>'purchase_price' ORDER BY sort_order,parameter_key""",
                (component_id,),
            )
            fields["specification"] = "；".join(f"{row['label']}: {row['value']}" for row in rows)
        fields.update(jlc_matched=1, updated_at=utc_now())
        assignments = ", ".join(f"{field}=?" for field in fields)
        connection.execute(
            f"UPDATE components SET {assignments} WHERE id=?", (*fields.values(), component_id)
        )

    @staticmethod
    def _component(row: sqlite3.Row) -> dict[str, Any]:
        """把数据库行转换成 API 兼容字典。"""

        item = dict(row)
        try:
            item["aliases"] = json.loads(item["aliases"])
        except (json.JSONDecodeError, TypeError):
            item["aliases"] = []
        return item

    @log_operation("创建库存位置")
    def create_warehouse(self, data: WarehouseCreate) -> dict[str, Any]:
        """创建界面中的上级位置；方法名为兼容旧 API 保留。"""

        item = {
            "id": str(uuid.uuid4()),
            "name": data.name.strip(),
            "notes": clean(data.notes),
            "created_at": utc_now(),
        }
        try:
            with self.write() as connection:
                connection.execute(
                    "INSERT INTO warehouses(id,name,notes,created_at) VALUES(:id,:name,:notes,:created_at)",
                    item,
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "位置名称已存在") from error
        return item

    def list_warehouses(self) -> list[dict[str, Any]]:
        """列出所有上级位置。"""

        with self.read() as connection:
            return [
                dict(row) for row in connection.execute("SELECT * FROM warehouses ORDER BY name")
            ]

    @log_operation("修改库存位置")
    def update_warehouse(self, warehouse_id: str, data: WarehouseCreate) -> dict[str, Any]:
        """修改上级位置名称和备注；内部名称继续兼容旧 API。"""

        try:
            with self.write() as connection:
                self._require(connection, "warehouses", warehouse_id, "位置")
                connection.execute(
                    "UPDATE warehouses SET name=?,notes=? WHERE id=?",
                    (data.name.strip(), clean(data.notes), warehouse_id),
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "位置名称已存在") from error
        return next(item for item in self.list_warehouses() if item["id"] == warehouse_id)

    @log_operation("删除库存位置")
    def delete_warehouse(self, warehouse_id: str) -> dict[str, Any]:
        """删除没有下级仓库的上级位置。"""

        with self.write() as connection:
            self._require(connection, "warehouses", warehouse_id, "位置")
            if connection.execute(
                "SELECT 1 FROM locations WHERE warehouse_id=? LIMIT 1", (warehouse_id,)
            ).fetchone():
                raise DomainError("CONFLICT", "该位置下仍有仓库，请先删除下级仓库")
            connection.execute("DELETE FROM warehouses WHERE id=?", (warehouse_id,))
        return {"id": warehouse_id, "deleted": True}

    @log_operation("创建仓库")
    def create_location(self, data: LocationCreate) -> dict[str, Any]:
        """在指定位置创建具体仓库；方法名为兼容旧 API 保留。"""

        item = {
            "id": str(uuid.uuid4()),
            "warehouse_id": data.warehouse_id,
            "name": data.name.strip(),
            "notes": clean(data.notes),
            "created_at": utc_now(),
        }
        try:
            with self.write() as connection:
                used = {row[0] for row in connection.execute("SELECT color FROM locations")}
                item["color"] = self._next_location_color(used)
                connection.execute(
                    """INSERT INTO locations(id,warehouse_id,name,notes,created_at,color)
                    VALUES(:id,:warehouse_id,:name,:notes,:created_at,:color)""",
                    item,
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "仓库名称重复，或上级位置不存在") from error
        return item

    @log_operation("更改仓库颜色")
    def set_location_color(self, location_id: str, color: str) -> None:
        """保存具体仓库的显示颜色。"""

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise DomainError("VALIDATION_ERROR", "颜色应为六位十六进制 RGB 值")
        with self.write() as connection:
            self._require(connection, "locations", location_id, "仓库")
            connection.execute(
                "UPDATE locations SET color=? WHERE id=?", (color.lower(), location_id)
            )

    def list_locations(self) -> list[dict[str, Any]]:
        """列出全部具体仓库。"""

        with self.read() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM locations ORDER BY warehouse_id,name")
            ]

    @log_operation("修改仓库")
    def update_location(self, location_id: str, data: LocationCreate) -> dict[str, Any]:
        """修改具体仓库的所属位置、名称和备注。"""

        try:
            with self.write() as connection:
                self._require(connection, "locations", location_id, "仓库")
                self._require(connection, "warehouses", data.warehouse_id, "位置")
                connection.execute(
                    "UPDATE locations SET warehouse_id=?,name=?,notes=? WHERE id=?",
                    (data.warehouse_id, data.name.strip(), clean(data.notes), location_id),
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "同一位置内仓库名称不能重复") from error
        return next(item for item in self.list_locations() if item["id"] == location_id)

    @log_operation("删除仓库")
    def delete_location(self, location_id: str) -> dict[str, Any]:
        """删除没有批次或流水引用的具体仓库。"""

        with self.write() as connection:
            self._require(connection, "locations", location_id, "仓库")
            for table in ("stock_lots", "inventory_transactions"):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE location_id=? LIMIT 1", (location_id,)
                ).fetchone():
                    raise DomainError("CONFLICT", "该仓库已有库存历史，不能删除")
            connection.execute(
                "DELETE FROM storage_category_priorities WHERE location_id=?", (location_id,)
            )
            connection.execute(
                "DELETE FROM storage_location_priorities WHERE location_id=?", (location_id,)
            )
            connection.execute("DELETE FROM locations WHERE id=?", (location_id,))
        return {"id": location_id, "deleted": True}

    def storage_priority_rows(self, category: str | None = None) -> list[dict[str, Any]]:
        """返回全部仓库及整体或指定类别的人工优先级，数值越小越优先。"""

        with self.read() as connection:
            if category:
                rows = connection.execute(
                    """SELECT l.id location_id,l.name location_name,w.name warehouse_name,l.color,
                    cp.priority,
                    (SELECT priority FROM storage_location_priorities gp
                     WHERE gp.location_id=l.id) global_priority
                    FROM locations l JOIN warehouses w ON w.id=l.warehouse_id
                    LEFT JOIN storage_category_priorities cp
                      ON cp.location_id=l.id AND cp.category=?
                    ORDER BY COALESCE(cp.priority,100000),
                             COALESCE(global_priority,100000),w.name,l.name""",
                    (category,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT l.id location_id,l.name location_name,w.name warehouse_name,l.color,
                    gp.priority,NULL global_priority
                    FROM locations l JOIN warehouses w ON w.id=l.warehouse_id
                    LEFT JOIN storage_location_priorities gp ON gp.location_id=l.id
                    ORDER BY COALESCE(gp.priority,100000),w.name,l.name"""
                ).fetchall()
        return [dict(row) for row in rows]

    @log_operation("保存仓库推荐优先级")
    def save_storage_priorities(
        self, category: str | None, priorities: dict[str, int]
    ) -> dict[str, Any]:
        """整体替换一个优先级配置；空类别表示全局优先级。

        零值在界面中表示“未设置”，因此不会写入数据库；负数属于无效输入。
        返回保存后的完整列表，方便桌面界面和外部 API 使用同一套语义。
        """

        if any(priority < 0 for priority in priorities.values()):
            raise DomainError("VALIDATION_ERROR", "仓库优先级不能为负数")
        normalized = {
            location_id: priority for location_id, priority in priorities.items() if priority > 0
        }

        table = "storage_category_priorities" if category else "storage_location_priorities"
        with self.write() as connection:
            if category:
                connection.execute(f"DELETE FROM {table} WHERE category=?", (category,))
            else:
                connection.execute(f"DELETE FROM {table}")
            for location_id, priority in normalized.items():
                self._require(connection, "locations", location_id, "仓库")
                if category:
                    connection.execute(
                        f"INSERT INTO {table}(category,location_id,priority) VALUES(?,?,?)",
                        (category, location_id, priority),
                    )
                else:
                    connection.execute(
                        f"INSERT INTO {table}(location_id,priority) VALUES(?,?)",
                        (location_id, priority),
                    )
        return {"category": category, "locations": self.storage_priority_rows(category)}

    def recommend_location(self, category: str | None) -> dict[str, Any] | None:
        """综合类别规则、位置/仓库名称和全局优先级推荐一个仓库。"""

        rows = self.storage_priority_rows(category)
        if not rows:
            return None
        explicit = [row for row in rows if row["priority"] is not None]
        if category and explicit:
            chosen = min(
                explicit,
                key=lambda row: (
                    int(row["priority"]),
                    int(row["global_priority"] or 100000),
                    row["warehouse_name"],
                    row["location_name"],
                ),
            )
            return {**chosen, "reason": f"类别“{category}”人工优先级"}
        tokens = _storage_tokens(category)
        matched = [
            row
            for row in rows
            if any(
                token.casefold() in f"{row['warehouse_name']} {row['location_name']}".casefold()
                for token in tokens
            )
        ]
        # 没有类别名称命中、也没有人工整体顺序时，优先回退到名称明确表示通用的库位。
        # 这样“电阻1/RLC”不会因为中文排序靠前而意外接收 LED、连接器等全部商品。
        global_configured = any(
            (row["global_priority"] if category else row["priority"]) is not None for row in rows
        )
        generic = [
            row
            for row in rows
            if any(
                token in f"{row['warehouse_name']} {row['location_name']}".casefold()
                for token in ("通用", "默认", "综合", "未分类", "general", "default")
            )
        ]
        candidates = matched or (rows if global_configured else generic or rows)
        reason = (
            "位置/仓库名称匹配"
            if matched
            else "整体优先级"
            if global_configured
            else "通用仓库回退"
            if generic
            else "默认顺序"
        )
        chosen = min(
            candidates,
            key=lambda row: (
                int(row["global_priority"] or row["priority"] or 100000),
                row["warehouse_name"],
                row["location_name"],
            ),
        )
        return {**chosen, "reason": reason}

    def list_stock(self) -> list[dict[str, Any]]:
        """按元件和仓库汇总当前库存。"""

        with self.read() as connection:
            rows = connection.execute(
                """SELECT c.id component_id,c.name component_name,w.id warehouse_id,
                w.name warehouse_name,l.id location_id,l.name location_name,l.color,
                COALESCE(SUM(s.remaining_quantity),0) quantity
                FROM stock_lots s JOIN components c ON c.id=s.component_id
                JOIN locations l ON l.id=s.location_id JOIN warehouses w ON w.id=l.warehouse_id
                GROUP BY c.id,l.id HAVING quantity>0 ORDER BY c.name,w.name,l.name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_inventory_lots(self) -> list[dict[str, Any]]:
        """按实际入库批次返回有余量的库存明细，供库存页展示和移库。"""

        with self.read() as connection:
            rows = connection.execute(
                """SELECT s.id,s.component_id,s.location_id,s.remaining_quantity quantity,
                s.unit_price,s.received_at,s.purchase_date,w.name warehouse_name,
                l.name location_name,l.color,b.order_number,b.custom_code
                FROM stock_lots s JOIN locations l ON l.id=s.location_id
                JOIN warehouses w ON w.id=l.warehouse_id
                LEFT JOIN inventory_batches b ON b.id=s.batch_id
                WHERE s.remaining_quantity>0 ORDER BY s.received_at DESC,s.rowid DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def inventory_export_rows(
        self, component_ids: Collection[str] | None = None, *, lot_ids: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        """返回适合表格导出的当前库存明细。

        :param component_ids: 限定元件集合；``None`` 表示全部活动元件，空集合表示无结果
        :param lot_ids: 指定筛选后保留的库存批次；``None`` 表示不限制批次
        :return: 每个活动元件、仓库一行；无库存元件仍保留一行空仓库记录

        价值优先使用剩余批次的实际单价，批次未记录单价时回退到元件当前单价。
        两者都缺失的数量计入 ``unpriced_quantity``，不会被静默当作已定价库存。
        """

        selected = tuple(dict.fromkeys(component_ids)) if component_ids is not None else None
        if selected == ():
            return []
        where = "c.archived_at IS NULL"
        parameters: list[Any] = []
        lot_filter = ""
        if lot_ids is not None:
            lot_filter = " AND s.id IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(list(lot_ids)))
        if selected is not None:
            placeholders = ",".join("?" for _ in selected)
            where += f" AND c.id IN ({placeholders})"
            parameters.extend(selected)
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT c.id component_id,c.name component_name,c.aliases,
                c.lcsc_part_number,c.manufacturer_part_number,c.custom_part_number,
                c.category,c.brand,c.package,c.specification,c.price component_price,
                c.purchase_date,c.product_url,c.datasheet_url,c.notes,
                w.name warehouse_name,l.name location_name,l.color,
                COALESCE(SUM(s.remaining_quantity),0) quantity,
                COALESCE(SUM(CASE WHEN COALESCE(s.unit_price,c.price) IS NOT NULL
                    THEN s.remaining_quantity*COALESCE(s.unit_price,c.price) ELSE 0 END),0) value,
                COALESCE(SUM(CASE WHEN COALESCE(s.unit_price,c.price) IS NULL
                    THEN s.remaining_quantity ELSE 0 END),0) unpriced_quantity
                FROM components c
                LEFT JOIN stock_lots s ON s.component_id=c.id AND s.remaining_quantity>0{lot_filter}
                LEFT JOIN locations l ON l.id=s.location_id
                LEFT JOIN warehouses w ON w.id=l.warehouse_id
                WHERE {where}
                GROUP BY c.id,l.id
                ORDER BY c.name COLLATE NOCASE,w.name COLLATE NOCASE,l.name COLLATE NOCASE""",
                parameters,
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["aliases"] = ", ".join(json.loads(item["aliases"] or "[]"))
            except (json.JSONDecodeError, TypeError):
                item["aliases"] = ""
            item["quantity"] = int(item["quantity"] or 0)
            item["unpriced_quantity"] = int(item["unpriced_quantity"] or 0)
            item["value"] = float(item["value"] or 0)
            output.append(item)
        return output

    def inventory_statistics(
        self, component_ids: Collection[str] | None = None, *, lot_ids: Collection[str] | None = None
    ) -> dict[str, Any]:
        """汇总元件种类、库存数量、仓库、类别和库存价值。"""

        rows = self.inventory_export_rows(component_ids, lot_ids=lot_ids)
        component_ids_seen = {str(row["component_id"]) for row in rows}
        total_quantity = sum(int(row["quantity"]) for row in rows)
        unpriced_quantity = sum(int(row["unpriced_quantity"]) for row in rows)

        def grouped(key: str, empty_label: str) -> list[dict[str, Any]]:
            groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                label = str(row.get(key) or empty_label)
                group = groups.setdefault(
                    label,
                    {
                        "name": label,
                        "component_ids": set(),
                        "quantity": 0,
                        "value": 0.0,
                        "unpriced_quantity": 0,
                    },
                )
                group["component_ids"].add(str(row["component_id"]))
                group["quantity"] += int(row["quantity"])
                group["value"] += float(row["value"])
                group["unpriced_quantity"] += int(row["unpriced_quantity"])
            return [
                {
                    "name": group["name"],
                    "component_types": len(group["component_ids"]),
                    "quantity": group["quantity"],
                    "value": group["value"],
                    "unpriced_quantity": group["unpriced_quantity"],
                }
                for group in sorted(groups.values(), key=lambda item: str(item["name"]).casefold())
            ]

        location_rows = [row for row in rows if row.get("location_name")]
        location_groups: dict[str, dict[str, Any]] = {}
        for row in location_rows:
            label = f"{row['warehouse_name']} / {row['location_name']}"
            group = location_groups.setdefault(
                label,
                {
                    "name": label,
                    "color": row["color"],
                    "component_ids": set(),
                    "quantity": 0,
                    "value": 0.0,
                    "unpriced_quantity": 0,
                },
            )
            group["component_ids"].add(str(row["component_id"]))
            group["quantity"] += int(row["quantity"])
            group["value"] += float(row["value"])
            group["unpriced_quantity"] += int(row["unpriced_quantity"])
        by_location = [
            {
                "name": group["name"],
                "color": group["color"],
                "component_types": len(group["component_ids"]),
                "quantity": group["quantity"],
                "value": group["value"],
                "unpriced_quantity": group["unpriced_quantity"],
            }
            for group in sorted(
                location_groups.values(), key=lambda item: str(item["name"]).casefold()
            )
        ]
        return {
            "component_types": len(component_ids_seen),
            "total_quantity": total_quantity,
            "priced_quantity": total_quantity - unpriced_quantity,
            "unpriced_quantity": unpriced_quantity,
            "total_value": sum(float(row["value"]) for row in rows),
            "by_location": by_location,
            "by_category": grouped("category", "未分类"),
        }

    @staticmethod
    def _get_or_create_batch(
        connection: sqlite3.Connection,
        component_id: str,
        *,
        batch_code: str | None = None,
        order_number: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """建立或复用批次；二维码和立创订单键在整个数据库中保持唯一。"""

        code = clean(batch_code)
        order = clean(order_number)
        if code and order:
            raise DomainError("VALIDATION_ERROR", "自定义批次码和立创订单号不能同时填写")
        component = connection.execute(
            "SELECT lcsc_part_number FROM components WHERE id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise DomainError("NOT_FOUND", "元件不存在")
        if code:
            code = code.upper()
            if not CUSTOM_BATCH_CODE.fullmatch(code):
                raise DomainError("VALIDATION_ERROR", "批次二维码必须为 CL 加十位数字")
            source_type = "CUSTOM"
            external_key = f"CUSTOM:{code}"
            lcsc = None
        elif order:
            lcsc = clean(component["lcsc_part_number"])
            if not lcsc:
                raise DomainError("VALIDATION_ERROR", "立创批次必须绑定带立创编号的元件")
            order = order.upper()
            lcsc = lcsc.upper()
            source_type = "JLC"
            external_key = f"JLC:{order}:{lcsc}"
            code = None
        else:
            return None
        existing = connection.execute(
            "SELECT id,component_id FROM inventory_batches WHERE external_key=?", (external_key,)
        ).fetchone()
        if existing:
            if existing["component_id"] != component_id:
                raise DomainError("CONFLICT", "该批次二维码已绑定到其他元件")
            if source_type == "CUSTOM":
                raise DomainError("CONFLICT", "该批次二维码已经绑定，不能重复入库")
            return str(existing["id"])
        batch_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO inventory_batches(
            id,component_id,source_type,external_key,order_number,lcsc_part_number,
            custom_code,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                batch_id,
                component_id,
                source_type,
                external_key,
                order,
                lcsc,
                code,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        return batch_id

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        """返回批次主数据及各仓库余量。"""

        with self.read() as connection:
            batch = connection.execute(
                """SELECT b.*,c.name component_name FROM inventory_batches b
                JOIN components c ON c.id=b.component_id WHERE b.id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise DomainError("NOT_FOUND", "批次不存在")
            locations = connection.execute(
                """SELECT l.id location_id,l.name location_name,w.id warehouse_id,
                w.name warehouse_name,l.color,SUM(s.remaining_quantity) remaining_quantity
                FROM stock_lots s JOIN locations l ON l.id=s.location_id
                JOIN warehouses w ON w.id=l.warehouse_id
                WHERE s.batch_id=? GROUP BY l.id ORDER BY w.name,l.name""",
                (batch_id,),
            ).fetchall()
            initial = connection.execute(
                """SELECT COALESCE(SUM(t.quantity_delta),0)
                FROM inventory_transactions t JOIN stock_lots s ON s.id=t.lot_id
                WHERE s.batch_id=? AND t.kind='RECEIVE' AND t.quantity_delta>0""",
                (batch_id,),
            ).fetchone()[0]
        result = dict(batch)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["locations"] = [dict(row) for row in locations]
        result["initial_quantity"] = int(initial or 0)
        result["remaining_quantity"] = sum(
            int(row["remaining_quantity"] or 0) for row in locations
        )
        return result

    def resolve_batch_code(self, raw_text: str) -> dict[str, Any]:
        """把立创袋贴或自定义二维码解析成唯一且仍有库存的批次。"""

        text = raw_text.strip()
        with self.read() as connection:
            if CUSTOM_BATCH_CODE.fullmatch(text.upper()):
                rows = connection.execute(
                    "SELECT id FROM inventory_batches WHERE custom_code=?", (text.upper(),)
                ).fetchall()
            else:
                from .label_parser import parse_qr_payload

                fields = parse_qr_payload(text)
                order = fields.get("order_number")
                lcsc = fields.get("lcsc_part_number")
                if not order or not lcsc:
                    raise DomainError("VALIDATION_ERROR", "二维码不是有效的立创袋贴或 CL 批次码")
                rows = connection.execute(
                    """SELECT id FROM inventory_batches
                    WHERE order_number=? AND lcsc_part_number=?""",
                    (order.normalized_value.upper(), lcsc.normalized_value.upper()),
                ).fetchall()
        if not rows:
            raise DomainError("NOT_FOUND", "没有找到对应的已入库批次")
        if len(rows) > 1:
            raise DomainError("CONFLICT", "二维码对应多个批次，请人工选择")
        result = self.get_batch(str(rows[0]["id"]))
        if result["remaining_quantity"] <= 0:
            raise DomainError("INSUFFICIENT_STOCK", "该批次已经没有剩余库存")
        return result

    def preview_order_import(self, lines: list[OrderImportLine]) -> list[dict[str, Any]]:
        """标记订单行将创建、匹配或跳过的状态，不修改数据库。"""

        with self.read() as connection:
            components = {
                str(row["lcsc_part_number"]): row
                for row in connection.execute(
                    "SELECT id,name,lcsc_part_number,jlc_matched FROM components "
                    "WHERE archived_at IS NULL AND lcsc_part_number IS NOT NULL"
                )
            }
            imported_keys = {
                (str(row["order_number"]), str(row["lcsc_part_number"]))
                for row in connection.execute(
                    "SELECT order_number,lcsc_part_number FROM order_import_items"
                )
            }
            output = []
            for line in lines:
                existing = components.get(line.lcsc_part_number)
                imported = (line.order_number, line.lcsc_part_number) in imported_keys
                output.append(
                    {
                        "line": line.model_dump(),
                        "status": "已导入，将跳过"
                        if imported
                        else "匹配已有元件"
                        if existing
                        else "创建新元件",
                        "component_id": existing["id"] if existing else None,
                        "component_name": existing["name"] if existing else None,
                        "component_jlc_matched": bool(existing and existing["jlc_matched"]),
                    }
                )
        return output

    def imported_order_keys(self) -> set[tuple[str, str]]:
        """返回已完成订单导入的幂等键，供同一文件分批续导。"""

        with self.read() as connection:
            rows = connection.execute(
                "SELECT order_number,lcsc_part_number FROM order_import_items"
            ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    def preview_order_supplement(self, lines: list[OrderImportLine]) -> list[dict[str, Any]]:
        """按立创编号预览已有元件可补齐的字段，优先使用较新的订单资料。"""

        values_by_part: dict[str, dict[str, str]] = {}
        for line in sorted(lines, key=lambda item: item.purchased_at or "", reverse=True):
            values = values_by_part.setdefault(line.lcsc_part_number, {})
            purchased_at = clean(line.purchased_at)
            for field, value in (
                ("manufacturer_part_number", clean(line.manufacturer_part_number)),
                ("category", clean(line.category)),
                ("brand", clean(line.brand)),
                ("package", clean(line.package)),
                ("product_url", clean(line.product_url)),
                ("datasheet_url", _datasheet_for_save(
                    line.datasheet_url, line.product_url, line.manufacturer_part_number
                )),
                ("notes", clean(line.description)),
                ("specification", _order_specification(line)),
                ("purchase_date", purchased_at[:10] if purchased_at else None),
                ("purchase_price", f"{line.purchase_price:.15g}" if line.purchase_price is not None else None),
            ):
                if value is not None:
                    values.setdefault(field, value)
        with self.read() as connection:
            components = connection.execute(
                """SELECT c.*,p.value purchase_price
                FROM components c LEFT JOIN component_category_parameters p
                ON c.id=p.component_id AND p.parameter_key='purchase_price'
                WHERE c.archived_at IS NULL AND c.lcsc_part_number IS NOT NULL
                ORDER BY c.name COLLATE NOCASE"""
            )
            return [
                {"id": item["id"], "name": item["name"], "lcsc_part_number": part, "fields": fields}
                for item in components
                if (part := item["lcsc_part_number"])
                if (fields := {
                    key: value for key, value in values_by_part.get(part.upper(), {}).items()
                    if not clean(item[key])
                })
            ]

    @log_operation("补充订单元件资料")
    def apply_order_supplement(self, changes: list[dict[str, Any]]) -> int:
        """写入用户确认的空白字段，保留预览后新增的元件资料。"""

        updated = 0
        timestamp = utc_now()
        with self.write() as connection:
            for change in changes:
                item = connection.execute(
                    """SELECT * FROM components
                    WHERE id=? AND lcsc_part_number=? AND archived_at IS NULL""",
                    (change["id"], change["lcsc_part_number"]),
                ).fetchone()
                if item is None:
                    continue
                fields = change["fields"]
                updates = {}
                for field, _ in ORDER_SUPPLEMENT_FIELDS:
                    if field == "purchase_price" or clean(item[field]):
                        continue
                    value = clean(fields.get(field))
                    if field == "datasheet_url":
                        value = normalize_datasheet_url(value)
                    if value is not None:
                        updates[field] = value
                purchase_price = clean(fields.get("purchase_price"))
                price_added = purchase_price is not None and self._save_order_purchase_price(
                    connection, item["id"], purchase_price, replace=False
                )
                if updates or price_added:
                    assignments = ", ".join(f"{field}=?" for field in updates)
                    if assignments:
                        assignments += ", "
                    connection.execute(
                        f"UPDATE components SET {assignments}updated_at=? WHERE id=?",
                        (*updates.values(), timestamp, item["id"]),
                    )
                    self._save_catalog_snapshot(connection, item["id"], "user")
                    updated += 1
        return updated

    @log_operation("订单商品入库")
    def import_order_lines(
        self,
        lines: list[OrderImportLine],
        location_id: str | None = None,
        location_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """在单个立即事务中导入订单商品、批次和库存流水。

        :param lines: 用户在预览页确认需要导入的订单商品行
        :param location_id: 可选，所有商品共用的默认目标库位
        :param location_overrides: 可选，以“订单号:立创编号”为键的逐商品库位覆盖
        :return: 事务组编号、导入数、跳过数和新建元件数
        :raises DomainError: 未选择库位或引用的库位不存在时抛出

        订单号与立创编号组成持久幂等键，重复选择同一文件只会跳过已经导入的商品。
        已有元件的用户资料不会被覆盖，只补齐空字段；价格和购买日期作为最新采购信息
        更新，并在批次中保留当次值。
        """

        if len(lines) > ORDER_IMPORT_BATCH_SIZE:
            raise DomainError(
                "VALIDATION_ERROR",
                f"单次最多导入 {ORDER_IMPORT_BATCH_SIZE} 条；完成后重新上传同一文件继续",
            )

        ### 初始化事务统计信息 ###
        group_id = str(uuid.uuid4())
        imported_count = 0
        skipped_count = 0
        created_count = 0
        timestamp = utc_now()
        ### 在同一个立即事务内处理每条订单商品 ###
        with self.write() as connection:
            for line in lines:
                import_key = f"{line.order_number}:{line.lcsc_part_number}"
                purchased_at = clean(line.purchased_at)
                purchase_date = purchased_at[:10] if purchased_at else None
                datasheet_url = _datasheet_for_save(
                    line.datasheet_url, line.product_url, line.manufacturer_part_number
                )
                selected_location = (location_overrides or {}).get(import_key, location_id)
                if not selected_location:
                    raise DomainError("VALIDATION_ERROR", f"{line.name} 尚未选择仓库")
                self._require(connection, "locations", selected_location, "仓库")
                duplicate = connection.execute(
                    "SELECT 1 FROM order_import_items WHERE order_number=? AND lcsc_part_number=?",
                    (line.order_number, line.lcsc_part_number),
                ).fetchone()
                if duplicate:
                    skipped_count += 1
                    continue
                component = connection.execute(
                    "SELECT id FROM components WHERE lcsc_part_number=?",
                    (line.lcsc_part_number,),
                ).fetchone()
                if component:
                    component_id = str(component["id"])
                    connection.execute(
                        """UPDATE components SET category=COALESCE(category,?),
                        manufacturer_part_number=COALESCE(manufacturer_part_number,?),
                        package=COALESCE(package,?),brand=COALESCE(brand,?),
                        product_url=COALESCE(product_url,?),datasheet_url=COALESCE(datasheet_url,?),
                        notes=COALESCE(notes,?),
                        specification=COALESCE(specification,?),price=COALESCE(?,price),
                        purchase_date=COALESCE(?,purchase_date),
                        jlc_matched=MAX(jlc_matched,?),
                        archived_at=NULL,updated_at=?
                        WHERE id=?""",
                        (
                            clean(line.category),
                            clean(line.manufacturer_part_number),
                            clean(line.package),
                            clean(line.brand),
                            clean(line.product_url),
                            datasheet_url,
                            clean(line.description),
                            _order_specification(line),
                            line.unit_price,
                            purchase_date,
                            int(line.jlc_matched),
                            timestamp,
                            component_id,
                        ),
                    )
                else:
                    component_id = str(uuid.uuid4())
                    connection.execute(
                        """INSERT INTO components(
                        id,name,aliases,lcsc_part_number,manufacturer_part_number,
                        custom_part_number,brand,package,category,specification,price,
                        purchase_date,product_url,datasheet_url,notes,jlc_matched,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            component_id,
                            line.name.strip(),
                            "[]",
                            line.lcsc_part_number,
                            clean(line.manufacturer_part_number),
                            None,
                            clean(line.brand),
                            clean(line.package),
                            clean(line.category),
                            _order_specification(line),
                            line.unit_price,
                            purchase_date,
                            clean(line.product_url),
                            datasheet_url,
                            clean(line.description) or f"由立创订单 {line.order_number} 离线导入",
                            int(line.jlc_matched),
                            timestamp,
                            timestamp,
                        ),
                    )
                    created_count += 1
                if line.parameter_groups:
                    connection.execute(
                        """DELETE FROM component_category_parameters
                        WHERE component_id=? AND parameter_key<>'purchase_price'""",
                        (component_id,),
                    )
                    parameter_rows = [
                        (parameter.key, parameter.label.strip(), parameter.value.strip(), group.name.strip())
                        for group in line.parameter_groups
                        for parameter in group.parameters
                        if parameter.key != "purchase_price"
                        and parameter.label.strip() and parameter.value.strip()
                    ]
                    for sort_order, (key, label, value, group_name) in enumerate(parameter_rows):
                        connection.execute(
                            """INSERT INTO component_category_parameters(
                            component_id,parameter_key,label,value,sort_order,group_name)
                            VALUES(?,?,?,?,?,?)""",
                            (component_id, key, label, value, sort_order, group_name or "类别参数"),
                        )
                    specification = "；".join(
                        f"{label}: {value}" for _key, label, value, _group in parameter_rows
                    )
                    connection.execute(
                        "UPDATE components SET specification=? WHERE id=?",
                        (specification or None, component_id),
                    )
                if line.purchase_price is not None:
                    self._save_order_purchase_price(
                        connection, component_id, f"{line.purchase_price:.15g}", replace=True
                    )
                lot_id = str(uuid.uuid4())
                batch_id = self._get_or_create_batch(
                    connection,
                    component_id,
                    order_number=line.order_number,
                    metadata={"source": "order_import"},
                )
                connection.execute(
                    """INSERT INTO stock_lots(id,component_id,location_id,initial_quantity,
                    remaining_quantity,source,unit_price,notes,received_at,purchase_date,batch_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lot_id,
                        component_id,
                        selected_location,
                        line.quantity,
                        line.quantity,
                        f"立创订单 {line.order_number}",
                        line.purchase_price if line.purchase_price is not None else line.unit_price,
                        "从用户选择的订单文件导入",
                        timestamp,
                        purchase_date,
                        batch_id,
                    ),
                )
                self._insert_transaction(
                    connection,
                    group_id,
                    "RECEIVE",
                    component_id,
                    lot_id,
                    selected_location,
                    line.quantity,
                    "离线订单导入",
                    None,
                )
                connection.execute(
                    """INSERT INTO order_import_items(
                    order_number,lcsc_part_number,component_id,transaction_group_id,imported_at)
                    VALUES(?,?,?,?,?)""",
                    (
                        line.order_number,
                        line.lcsc_part_number,
                        component_id,
                        group_id,
                        timestamp,
                    ),
                )
                imported_count += 1
                self._save_order_category_details(connection, component_id, line)
                if line.jlc_matched:
                    self._save_catalog_snapshot(connection, component_id, "marketplace", replace_user=True)
                elif connection.execute(
                    "SELECT 1 FROM user.catalog_components WHERE id=?", (component_id,)
                ).fetchone():
                    self._save_catalog_snapshot(connection, component_id, "user")
                else:
                    self._save_catalog_snapshot(connection, component_id, "marketplace")
        return {
            "transaction_group_id": group_id,
            "imported": imported_count,
            "skipped": skipped_count,
            "created_components": created_count,
        }

    @staticmethod
    def _save_order_purchase_price(
        connection: sqlite3.Connection, component_id: str, value: str, *, replace: bool
    ) -> bool:
        result = connection.execute(
            """INSERT INTO component_category_parameters(
            component_id,parameter_key,label,value,sort_order,group_name)
            VALUES(?,'purchase_price','购入单价',?,10000,'采购信息')
            ON CONFLICT(component_id,parameter_key) DO UPDATE SET value=excluded.value
            WHERE ? OR trim(value)=''""",
            (component_id, value, replace),
        )
        return result.rowcount > 0

    @staticmethod
    def _save_order_category_details(
        connection: sqlite3.Connection, component_id: str, line: OrderImportLine
    ) -> None:
        """把类别专属参数写入独立子表。"""

        if line.resistor:
            connection.execute(
                """INSERT INTO resistor_details(component_id,resistance,tolerance_percent,
                power_mw,technology,source_description) VALUES(?,?,?,?,?,?)
                ON CONFLICT(component_id) DO UPDATE SET
                resistance=excluded.resistance,tolerance_percent=excluded.tolerance_percent,
                power_mw=excluded.power_mw,technology=excluded.technology,
                source_description=excluded.source_description""",
                (
                    component_id,
                    clean(line.resistor.resistance),
                    line.resistor.tolerance_percent,
                    line.resistor.power_mw,
                    clean(line.resistor.technology),
                    clean(line.description),
                ),
            )
        if line.mosfet:
            connection.execute(
                """INSERT INTO mosfet_details(component_id,channel_type,mosfet_count,
                voltage_v,current_a,source_description) VALUES(?,?,?,?,?,?)
                ON CONFLICT(component_id) DO UPDATE SET
                channel_type=excluded.channel_type,mosfet_count=excluded.mosfet_count,
                voltage_v=excluded.voltage_v,current_a=excluded.current_a,
                source_description=excluded.source_description""",
                (
                    component_id,
                    clean(line.mosfet.channel_type),
                    line.mosfet.mosfet_count,
                    line.mosfet.voltage_v,
                    line.mosfet.current_a,
                    clean(line.description),
                ),
            )

    @log_operation("元件入库")
    def receive(
        self, data: ReceiveRequest, kind: str = "RECEIVE", *, platform: str | None = None
    ) -> dict[str, Any]:
        """创建或复用批次，新增库存批次并记录正向流水。

        :param data: 元件、库位、数量、采购信息及可选批次信息
        :param kind: 流水类型，默认 ``RECEIVE``；内部流程可传入更具体类型
        :return: 事务组编号、流水数量和批次编号
        :raises DomainError: 元件或库位不存在时抛出

        库存批次、流水和元件最新采购信息位于同一事务，任一步失败都会整体回滚。
        """

        ### 校验引用并创建库存批次 ###
        with self.write() as connection:
            self._require(connection, "components", data.component_id, "元件")
            self._require(connection, "locations", data.location_id, "仓库")
            group_id = str(uuid.uuid4())
            lot_id = str(uuid.uuid4())
            timestamp = utc_now()
            batch_id = self._get_or_create_batch(
                connection,
                data.component_id,
                batch_code=data.batch_code,
                order_number=(
                    field.normalized_value
                    if (field := parse_qr_payload(data.order_number or "").get("order_number"))
                    else data.order_number
                ),
                metadata=data.batch_metadata,
            )
            connection.execute(
                """INSERT INTO stock_lots(id,component_id,location_id,initial_quantity,
                remaining_quantity,source,unit_price,supplier_part_id,notes,received_at,purchase_date,
                batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lot_id,
                    data.component_id,
                    data.location_id,
                    data.quantity,
                    data.quantity,
                    clean(data.source),
                    data.unit_price,
                    data.supplier_part_id,
                    clean(data.notes),
                    timestamp,
                    clean(data.purchase_date),
                    batch_id,
                ),
            )
            ### 记录不可变流水并更新元件采购信息 ###
            self._insert_transaction(
                connection,
                group_id,
                kind,
                data.component_id,
                lot_id,
                data.location_id,
                data.quantity,
                data.source,
                data.notes,
                platform=platform,
            )
            connection.execute(
                """UPDATE components SET price=COALESCE(?,price),
                purchase_date=COALESCE(?,purchase_date),archived_at=NULL,updated_at=? WHERE id=?""",
                (data.unit_price, clean(data.purchase_date), timestamp, data.component_id),
            )
        return {
            "transaction_group_id": group_id,
            "affected_transactions": 1,
            "batch_id": batch_id,
        }

    def last_issue_location(self) -> str | None:
        """取最近一次明确选择的出库仓库，兼容升级前的库存流水。"""

        with self.read() as connection:
            preference = connection.execute(
                "SELECT location_id FROM issue_preferences WHERE id=1"
            ).fetchone()
            if preference:
                return str(preference[0]) if preference[0] is not None else None
            row = connection.execute(
                """SELECT location_id FROM inventory_transactions WHERE kind='ISSUE'
                ORDER BY created_at DESC,rowid DESC LIMIT 1"""
            ).fetchone()
            return str(row[0]) if row else None

    def remember_issue_location(self, location_id: str | None) -> None:
        with self.write() as connection:
            self._save_issue_location(connection, location_id)

    @staticmethod
    def _save_issue_location(connection: sqlite3.Connection, location_id: str | None) -> None:
        connection.execute(
            """INSERT INTO issue_preferences(id,location_id) VALUES(1,?)
            ON CONFLICT(id) DO UPDATE SET location_id=excluded.location_id""",
            (location_id,),
        )

    @log_operation("元件出库")
    def issue(
        self, data: IssueRequest, kind: str = "ISSUE", *, platform: str | None = None
    ) -> dict[str, Any]:
        """按接收时间顺序从库存批次扣减数量。

        :param data: 元件、可选库位/批次、出库数量和用途说明
        :param kind: 生成的流水类型，默认 ``ISSUE``
        :return: 事务组编号和实际生成的流水条数
        :raises DomainError: 元件不存在或可用库存不足时抛出

        注意：库存检查和扣减位于同一个立即事务中，避免并发出库造成负库存。
        """

        with self.write() as connection:
            self._require(connection, "components", data.component_id, "元件")
            group_id = str(uuid.uuid4())
            count = self._issue_from_lots(
                connection,
                group_id,
                data.component_id,
                data.location_id,
                data.quantity,
                data.source,
                data.notes,
                kind,
                data.batch_id,
                platform=platform,
            )
            if kind == "ISSUE":
                self._save_issue_location(connection, data.location_id)
        return {"transaction_group_id": group_id, "affected_transactions": count}

    @log_operation("移动库存仓库")
    def transfer(self, data: TransferRequest) -> dict[str, Any]:
        """在两个库位之间原子移动库存，并分别记录移出、移入流水。

        :param data: 元件、源库位、目标库位、数量及备注
        :return: 事务组编号和生成的流水条数
        :raises DomainError: 两个库位相同、库位不存在或源库存不足时抛出

        每个被消耗的源批次都会生成对应目标批次，并继承原批次编号和单价，保证
        移库后仍可追溯采购来源。
        """

        ### 校验源库位和目标库位 ###
        if data.from_location_id == data.to_location_id:
            raise DomainError("VALIDATION_ERROR", "源仓库和目标仓库不能相同")
        with self.write() as connection:
            self._require(connection, "locations", data.from_location_id, "源仓库")
            self._require(connection, "locations", data.to_location_id, "目标仓库")
            group_id = str(uuid.uuid4())
            ### 按批次消耗源库存并逐批创建目标库存 ###
            used = self._consume_lots(
                connection, data.component_id, data.from_location_id, data.quantity
            )
            for lot_id, quantity, _ in used:
                source_lot = connection.execute(
                    "SELECT * FROM stock_lots WHERE id=?", (lot_id,)
                ).fetchone()
                self._record_lot_transfer(
                    connection, group_id, source_lot, data.to_location_id, quantity, data.notes
                )
        return {"transaction_group_id": group_id, "affected_transactions": len(used) * 2}

    @log_operation("批量移动库存仓库")
    def transfer_lots(self, quantities: dict[str, int], target_id: str) -> dict[str, Any]:
        """原子移动所选批次的当前余量，库存与预览不符时回滚整组。"""

        group_id = str(uuid.uuid4())
        moved = 0
        with self.write() as connection:
            self._require(connection, "locations", target_id, "目标仓库")
            for lot_id, quantity in quantities.items():
                lot = connection.execute(
                    "SELECT * FROM stock_lots WHERE id=?", (lot_id,)
                ).fetchone()
                # 任一批次余量与预览不符时，回滚本组已经执行的移库。
                if not lot or quantity <= 0 or lot["remaining_quantity"] != quantity:
                    raise DomainError("CONFLICT", "所选批次库存已变动，请刷新后重试")
                if lot["location_id"] == target_id:
                    continue
                connection.execute(
                    "UPDATE stock_lots SET remaining_quantity=0 WHERE id=?", (lot_id,)
                )
                self._record_lot_transfer(connection, group_id, lot, target_id, quantity)
                moved += 1
            if not moved:
                raise DomainError("VALIDATION_ERROR", "请选择至少一个不在目标仓库的库存批次")
        return {"transaction_group_id": group_id, "moved_lots": moved}

    def _record_lot_transfer(
        self,
        connection: sqlite3.Connection,
        group_id: str,
        source: sqlite3.Row,
        target_id: str,
        quantity: float,
        notes: str | None = None,
    ) -> None:
        """创建目标库存，保留源批次采购信息并写入成对流水。"""

        target_lot_id = str(uuid.uuid4())
        component_id = source["component_id"]
        # 批次保留原入库时间供 FIFO 排序。实际换库时间由两条移库流水记录。
        connection.execute(
            """INSERT INTO stock_lots(id,component_id,location_id,initial_quantity,
            remaining_quantity,source,unit_price,supplier_part_id,notes,received_at,
            purchase_date,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target_lot_id, component_id, target_id, quantity, quantity, "移库",
                source["unit_price"], source["supplier_part_id"], clean(notes) or source["notes"],
                source["received_at"], source["purchase_date"], source["batch_id"],
            ),
        )
        self._insert_transaction(
            connection, group_id, "TRANSFER_OUT", component_id, source["id"],
            source["location_id"], -quantity, "移库", notes,
        )
        self._insert_transaction(
            connection, group_id, "TRANSFER_IN", component_id, target_lot_id,
            target_id, quantity, "移库", notes,
        )

    @log_operation("盘点调整库存")
    def adjust(self, data: AdjustRequest) -> dict[str, Any]:
        """把仓库库存调整到目标值，差额仍通过批次流水表达。

        :param data: 元件、库位、盘点目标数量和调整原因
        :return: 事务组编号和生成的调整流水条数
        :raises DomainError: 引用不存在、目标量未变化或减库存时库存不足时抛出

        当前数量的读取、差额计算和流水写入必须处于同一个立即事务中。若把读取
        放在事务外，另一线程可能在两步之间入库，最终数量便不再等于盘点目标。
        """

        ### 在事务内读取当前数量并计算差额 ###
        with self.write() as connection:
            self._require(connection, "components", data.component_id, "元件")
            self._require(connection, "locations", data.location_id, "仓库")
            current = float(
                connection.execute(
                    "SELECT COALESCE(SUM(remaining_quantity),0) FROM stock_lots WHERE component_id=? AND location_id=?",
                    (data.component_id, data.location_id),
                ).fetchone()[0]
            )
            difference = data.target_quantity - current
            if abs(difference) <= EPSILON:
                raise DomainError("CONFLICT", "目标数量与当前库存相同")

            group_id = str(uuid.uuid4())
            ### 根据差额方向创建入库或出库流水 ###
            if difference > 0:
                lot_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO stock_lots(id,component_id,location_id,initial_quantity,
                    remaining_quantity,source,notes,received_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        lot_id,
                        data.component_id,
                        data.location_id,
                        difference,
                        difference,
                        "盘点调整",
                        clean(data.reason),
                        utc_now(),
                    ),
                )
                self._insert_transaction(
                    connection,
                    group_id,
                    "ADJUST_IN",
                    data.component_id,
                    lot_id,
                    data.location_id,
                    difference,
                    "盘点调整",
                    data.reason,
                )
                affected = 1
            else:
                affected = self._issue_from_lots(
                    connection,
                    group_id,
                    data.component_id,
                    data.location_id,
                    -difference,
                    "盘点调整",
                    data.reason,
                    "ADJUST_OUT",
                )
        return {"transaction_group_id": group_id, "affected_transactions": affected}

    def list_transactions(self, *, include_part_numbers: bool = False) -> list[dict[str, Any]]:
        """返回带元件和位置名称的库存流水。"""

        part_columns = (
            "c.lcsc_part_number,c.manufacturer_part_number,c.custom_part_number,"
            if include_part_numbers else ""
        )
        with self.read() as connection:
            rows = connection.execute(
                f"""SELECT t.*,c.name component_name,{part_columns}
                w.name warehouse_name,l.name location_name,l.color,b.order_number,b.custom_code,
                s.unit_price,s.purchase_date,
                COALESCE(a.display_group_id,t.group_id) display_group_id
                FROM inventory_transactions t JOIN components c ON c.id=t.component_id
                JOIN locations l ON l.id=t.location_id JOIN warehouses w ON w.id=l.warehouse_id
                JOIN stock_lots s ON s.id=t.lot_id LEFT JOIN inventory_batches b ON b.id=s.batch_id
                LEFT JOIN transaction_group_associations a ON a.group_id=t.group_id
                ORDER BY t.created_at DESC,t.rowid DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    @log_operation("更改流水分组")
    def regroup_transactions(
        self, original_groups: list[list[str]], new_groups: list[list[str]]
    ) -> None:
        """改变显示分组关联，每个库存事务始终作为一个整体。"""

        original_ids = [group_id for group in original_groups for group_id in group]
        new_ids = [group_id for group in new_groups for group_id in group]
        if (
            not original_ids or len(set(original_ids)) != len(original_ids)
            or sorted(original_ids) != sorted(new_ids) or any(not group for group in new_groups)
        ):
            raise DomainError("VALIDATION_ERROR", "流水分组无效")
        with self.write() as connection:
            rows = connection.execute(
                """SELECT t.group_id,COALESCE(a.display_group_id,t.group_id) display_id
                FROM inventory_transactions t
                LEFT JOIN transaction_group_associations a ON a.group_id=t.group_id
                GROUP BY t.group_id ORDER BY max(t.created_at) DESC,max(t.rowid) DESC"""
            )
            actual: dict[str, list[str]] = {}
            display_ids: dict[str, str] = {}
            for row in rows:
                group_id, display_id = row
                display_ids[group_id] = display_id
                actual.setdefault(display_id, []).append(group_id)
            if any(
                group and actual.get(display_ids.get(group[0], "")) != group for group in original_groups
            ):
                raise DomainError("CONFLICT", "流水分组已变动，请刷新后重试")
            connection.executemany(
                """INSERT INTO transaction_group_associations(group_id,display_group_id,manual)
                VALUES(?,?,1) ON CONFLICT(group_id) DO UPDATE SET
                display_group_id=excluded.display_group_id,manual=1""",
                [(group_id, group[0]) for group in new_groups for group_id in group],
            )

    @log_operation("修改库存流水")
    def update_transaction(
        self, transaction_id: str, expected_delta: int | float, quantity: int | float, notes: str,
        *, order_number: str | None = None, expected_order_number: str | None = None,
        details: TransactionDetails | None = None, expected_location_id: str | None = None,
        component: ComponentCreate | None = None,
        parameter_groups: list[JlcParameterGroup] | None = None,
    ) -> None:
        """在同一事务中更正流水、库存批次和元件资料。"""

        try:
            with self.write() as connection:
                row = self._transaction_for_edit(connection, transaction_id)
                previous = row["quantity_delta"]
                if previous != expected_delta or (
                    expected_location_id is not None and row["location_id"] != expected_location_id
                ):
                    raise DomainError("CONFLICT", "该流水的数量或仓库已变动，请刷新后重试")
                if (order_number is not None or details is not None) and (
                    row["order_number"] != expected_order_number
                ):
                    raise DomainError("CONFLICT", "订单号已变动，请刷新后重试")
                if component is not None:
                    self._update_component_fields(connection, row["component_id"], component, parameter_groups)
                location = details.location_id if details is not None else row["location_id"]
                self._require(connection, "locations", location, "仓库")
                delta = quantity if previous > 0 else -quantity
                changed_stock = delta != previous or location != row["location_id"]
                if delta != previous and (not isinstance(quantity, int) or not 0 < quantity <= 2**63 - 1):
                    raise DomainError("VALIDATION_ERROR", "请输入有效的正整数数量")
                edited_ids = [transaction_id]
                if changed_stock:
                    if row["kind"] in {"REVERSAL", "BOM_PICK_ISSUE"} or (
                        row["kind"] == "BOM_ISSUE" and delta != previous
                    ):
                        raise DomainError("CONFLICT", "更改会与关联的原流水或 BOM 取货记录冲突")
                    edited_ids = self._edit_transaction_stock(connection, row, location, delta)
                source = clean(details.source) if details is not None else row["source"]
                connection.executemany(
                    "UPDATE inventory_transactions SET source=?,notes=? WHERE id=?",
                    [(source, clean(notes), entry_id) for entry_id in edited_ids],
                )
                if details is not None and row["kind"] in TRANSACTION_RECEIPT_KINDS:
                    self._edit_receipt_details(connection, row, details, source, clean(notes))
                elif order_number is not None:
                    self._rename_transaction_order(connection, row["batch_id"], order_number)
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "元件编号、批次码或订单号与已有记录冲突") from error

    @staticmethod
    def _transaction_for_edit(connection: sqlite3.Connection, transaction_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            """SELECT t.rowid entry_index,t.*,s.remaining_quantity,s.batch_id,s.unit_price,
            s.purchase_date,b.order_number,b.custom_code FROM inventory_transactions t
            JOIN stock_lots s ON s.id=t.lot_id LEFT JOIN inventory_batches b ON b.id=s.batch_id
            WHERE t.id=?""",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "该流水已删除，请刷新")
        return row

    def _edit_transaction_stock(
        self, connection: sqlite3.Connection, row: sqlite3.Row, location: str, delta: int | float
    ) -> list[str]:
        if row["kind"] in {"TRANSFER_IN", "TRANSFER_OUT"}:
            rows = connection.execute(
                "SELECT id,kind FROM inventory_transactions WHERE group_id=? ORDER BY rowid",
                (row["group_id"],),
            ).fetchall()
            index = next(index for index, item in enumerate(rows) if item["id"] == row["id"])
            pair_start = index - index % 2
            pair = rows[pair_start:pair_start + 2]
            if [item["kind"] for item in pair] != ["TRANSFER_OUT", "TRANSFER_IN"]:
                raise DomainError("CONFLICT", "移库流水缺少对应的移入或移出记录")
            outgoing, incoming = (self._transaction_for_edit(connection, item["id"]) for item in pair)
            origin = location if row["kind"] == "TRANSFER_OUT" else outgoing["location_id"]
            target = location if row["kind"] == "TRANSFER_IN" else incoming["location_id"]
            if origin == target:
                raise DomainError("CONFLICT", "移出和移入仓库不能相同")
            if outgoing["quantity_delta"] != -incoming["quantity_delta"]:
                raise DomainError("CONFLICT", "关联的移出和移入数量不一致")
            self._edit_lot_quantity(connection, outgoing, origin, -abs(delta))
            self._edit_lot_quantity(connection, incoming, target, abs(delta))
            return [row["id"]]
        return self._edit_lot_quantity(connection, row, location, delta)

    def _edit_lot_quantity(
        self, connection: sqlite3.Connection, row: sqlite3.Row, location: str, delta: int | float
    ) -> list[str]:
        previous = row["quantity_delta"]
        lot_id = row["lot_id"]
        moved = location != row["location_id"]
        if connection.execute(
            "SELECT 1 FROM inventory_transactions WHERE reversal_of=? LIMIT 1", (row["id"],)
        ).fetchone():
            raise DomainError("CONFLICT", "该流水已经冲销，请先处理关联流水")
        if previous > 0:
            if moved and connection.execute(
                "SELECT 1 FROM inventory_transactions WHERE lot_id=? AND id<>? LIMIT 1",
                (lot_id, row["id"]),
            ).fetchone():
                raise DomainError("CONFLICT", "该批次已有后续出入库流水，无法更改仓库")
            remaining = row["remaining_quantity"] + delta - previous
            if remaining < 0:
                raise DomainError("INSUFFICIENT_STOCK", "库存不足：该批次已使用的数量大于修改后的数量")
            connection.execute(
                """UPDATE stock_lots SET location_id=?,initial_quantity=?,remaining_quantity=?
                WHERE id=?""", (location, delta, remaining, lot_id)
            )
        elif moved:
            return self._reassign_outgoing_lot(connection, row, location, -delta)
        else:
            remaining = row["remaining_quantity"] + delta - previous
            if remaining < 0:
                raise DomainError("INSUFFICIENT_STOCK", "原批次的剩余库存不足以修改数量")
            connection.execute("UPDATE stock_lots SET remaining_quantity=? WHERE id=?", (remaining, lot_id))
        connection.execute(
            "UPDATE inventory_transactions SET quantity_delta=?,location_id=?,lot_id=? WHERE id=?",
            (delta, location, lot_id, row["id"]),
        )
        self._restore_component_with_stock(connection, row["component_id"])
        return [row["id"]]

    @staticmethod
    def _reassign_outgoing_lot(
        connection: sqlite3.Connection, row: sqlite3.Row, location: str, quantity: int | float
    ) -> list[str]:
        paired_transfer = row["kind"] == "TRANSFER_OUT"
        targets = connection.execute(
            """SELECT s.id,s.remaining_quantity FROM stock_lots s WHERE s.component_id=?
            AND s.location_id=? AND s.remaining_quantity>0 AND (s.batch_id IS ? OR ?)
            AND (NOT ? OR s.remaining_quantity>=?)
            AND EXISTS (SELECT 1 FROM inventory_transactions t WHERE t.lot_id=s.id
                AND t.quantity_delta>0 AND (t.created_at<? OR
                    (t.created_at=? AND t.rowid<?)))
            ORDER BY s.received_at,s.id""",
            # 只使用原操作发生时已经入库的批次。
            (row["component_id"], location, row["batch_id"],
             row["batch_id"] is None and not paired_transfer, paired_transfer, quantity,
             row["created_at"], row["created_at"], row["entry_index"]),
        ).fetchall()
        if sum(target["remaining_quantity"] for target in targets) < quantity:
            raise DomainError("INSUFFICIENT_STOCK", "目标仓库的可用批次库存不足")
        connection.execute(
            "UPDATE stock_lots SET remaining_quantity=remaining_quantity-? WHERE id=?",
            (row["quantity_delta"], row["lot_id"]),
        )
        entries: list[str] = []
        remaining = quantity
        for target in targets:
            used = min(remaining, target["remaining_quantity"])
            connection.execute(
                "UPDATE stock_lots SET remaining_quantity=remaining_quantity-? WHERE id=?",
                (used, target["id"]),
            )
            if not entries:
                entry_id = row["id"]
                connection.execute(
                    "UPDATE inventory_transactions SET quantity_delta=?,location_id=?,lot_id=? WHERE id=?",
                    (-used, location, target["id"], entry_id),
                )
            else:
                entry_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO inventory_transactions(
                    id,group_id,kind,component_id,lot_id,location_id,quantity_delta,created_at,operation_platform)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (entry_id, row["group_id"], row["kind"], row["component_id"], target["id"],
                     location, -used, row["created_at"], row["operation_platform"]),
                )
            entries.append(entry_id)
            remaining -= used
            if remaining <= EPSILON:
                break
        Database._restore_component_with_stock(connection, row["component_id"])
        return entries

    def _edit_receipt_details(
        self, connection: sqlite3.Connection, row: sqlite3.Row,
        details: TransactionDetails, source: str | None, notes: str | None,
    ) -> None:
        raw_order = details.order_number or ""
        field = parse_qr_payload(raw_order).get("order_number")
        order = (field.normalized_value if field else raw_order).strip().upper() or None
        code = (details.batch_code or "").strip().upper() or None
        if order and code:
            raise DomainError("VALIDATION_ERROR", "立创订单号与自定义批次码只能填写一项")
        if order and not re.fullmatch(r"[A-Z0-9-]+", order):
            raise DomainError("VALIDATION_ERROR", "请输入有效的立创订单号")
        if code and not CUSTOM_BATCH_CODE.fullmatch(code):
            raise DomainError("VALIDATION_ERROR", "批次二维码必须为 CL 加十位数字")

        batch_id = row["batch_id"]
        if order and row["order_number"] and order != row["order_number"]:
            self._rename_transaction_order(connection, batch_id, order)
        elif code and row["custom_code"] and code != row["custom_code"]:
            connection.execute(
                "UPDATE inventory_batches SET custom_code=?,external_key=? WHERE id=?",
                (code, f"CUSTOM:{code}", batch_id),
            )
        elif (order, code) != (row["order_number"], row["custom_code"]):
            if connection.execute(
                """SELECT 1 FROM order_import_items WHERE transaction_group_id=?
                AND component_id=? AND order_number=?""",
                (row["group_id"], row["component_id"], row["order_number"]),
            ).fetchone():
                raise DomainError("CONFLICT", "该入库来自立创订单导入，请保留立创订单号")
            if batch_id and connection.execute(
                "SELECT 1 FROM bom_pick_entries WHERE batch_id=? LIMIT 1", (batch_id,)
            ).fetchone():
                raise DomainError("CONFLICT", "该批次已关联 BOM 取货记录，无法更换批次类型")
            batch_id = self._get_or_create_batch(
                connection, row["component_id"], order_number=order, batch_code=code
            )

        date = clean(details.purchase_date)
        connection.execute(
            """WITH RECURSIVE related(id) AS (
                SELECT ? UNION
                SELECT incoming.lot_id FROM related r
                JOIN inventory_transactions outgoing ON outgoing.lot_id=r.id AND outgoing.kind='TRANSFER_OUT'
                JOIN inventory_transactions incoming ON incoming.rowid=(
                    SELECT min(t.rowid) FROM inventory_transactions t
                    WHERE t.group_id=outgoing.group_id AND t.rowid>outgoing.rowid
                ) AND incoming.kind='TRANSFER_IN'
            ) UPDATE stock_lots SET unit_price=?,purchase_date=?,batch_id=?
            WHERE id IN (SELECT id FROM related)""",
            (row["lot_id"], details.unit_price, date, batch_id),
        )
        connection.execute(
            "UPDATE stock_lots SET source=?,notes=? WHERE id=?", (source, notes, row["lot_id"])
        )
        if row["batch_id"] is not None and row["batch_id"] != batch_id:
            self._delete_empty_batch(connection, row["batch_id"])
        purchase_changed = details.unit_price != row["unit_price"] or date != row["purchase_date"]
        if purchase_changed and row["kind"] == "RECEIVE" and not connection.execute(
            """SELECT 1 FROM inventory_transactions WHERE component_id=? AND kind='RECEIVE'
            AND rowid>? LIMIT 1""", (row["component_id"], row["entry_index"])
        ).fetchone():
            connection.execute(
                """UPDATE components SET price=?,purchase_date=?,updated_at=? WHERE id=?""",
                (details.unit_price, date, utc_now(), row["component_id"]),
            )

    @staticmethod
    def _delete_empty_batch(connection: sqlite3.Connection, batch_id: str) -> None:
        connection.execute(
            """DELETE FROM inventory_batches WHERE id=?
            AND NOT EXISTS (SELECT 1 FROM stock_lots WHERE batch_id=?)
            AND NOT EXISTS (SELECT 1 FROM bom_pick_entries WHERE batch_id=?)""",
            (batch_id, batch_id, batch_id),
        )

    @staticmethod
    def _rename_transaction_order(
        connection: sqlite3.Connection, batch_id: str | None, order_number: str
    ) -> None:
        order = order_number.strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]+", order):
            raise DomainError("VALIDATION_ERROR", "请输入有效的立创订单号")
        batch = connection.execute(
            "SELECT * FROM inventory_batches WHERE id=? AND source_type='JLC'", (batch_id,)
        ).fetchone()
        if batch is None:
            raise DomainError("VALIDATION_ERROR", "该流水没有关联立创订单")
        previous = batch["order_number"]
        part = batch["lcsc_part_number"]
        if previous == order:
            return
        try:
            connection.execute(
                "UPDATE inventory_batches SET order_number=?,external_key=? WHERE id=?",
                (order, f"JLC:{order}:{part}", batch_id),
            )
            connection.execute(
                """UPDATE order_import_items SET order_number=?
                WHERE order_number=? AND upper(lcsc_part_number)=? AND component_id=?""",
                (order, previous, part, batch["component_id"]),
            )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "新订单号与已有立创批次冲突") from error
        connection.execute(
            "UPDATE stock_lots SET source=? WHERE batch_id=? AND source=?",
            (f"立创订单 {order}", batch_id, f"立创订单 {previous}"),
        )
        connection.execute(
            """UPDATE inventory_transactions SET source=?
            WHERE source=? AND lot_id IN (SELECT id FROM stock_lots WHERE batch_id=?)""",
            (f"立创订单 {order}", f"立创订单 {previous}", batch_id),
        )

    @log_operation("删除流水并回退库存")
    def delete_transaction_group(
        self, group_id: str, expected_rows: dict[str, int | float]
    ) -> None:
        """原子删除整个事务组，归还扣减库存并移除该组创建的无引用批次明细。"""

        with self.write() as connection:
            rows = connection.execute(
                "SELECT * FROM inventory_transactions WHERE group_id=?", (group_id,)
            ).fetchall()
            if not rows:
                raise DomainError("NOT_FOUND", "该事务组已删除，请刷新")
            if {row["id"]: row["quantity_delta"] for row in rows} != expected_rows:
                raise DomainError("CONFLICT", "该事务组已变动，请刷新后重试")
            if connection.execute(
                """SELECT 1 FROM inventory_transactions r
                JOIN inventory_transactions t ON t.id=r.reversal_of
                WHERE t.group_id=? LIMIT 1""",
                (group_id,),
            ).fetchone():
                raise DomainError("CONFLICT", "仍有关联的冲销流水，请先处理冲销流水")

            created_lots = {
                row["lot_id"] for row in rows
                if row["quantity_delta"] > 0 and row["reversal_of"] is None
            }
            if connection.execute(
                """SELECT 1 FROM inventory_transactions other
                JOIN inventory_transactions original ON original.lot_id=other.lot_id
                WHERE original.group_id=? AND original.quantity_delta>0
                AND original.reversal_of IS NULL AND other.group_id<>? LIMIT 1""",
                (group_id, group_id),
            ).fetchone():
                raise DomainError("CONFLICT", "部分入库批次已有后续流水，请先处理关联流水")

            changes: dict[str, int | float] = {}
            for row in rows:
                lot_id = row["lot_id"]
                changes[lot_id] = changes.get(lot_id, 0) - row["quantity_delta"]
            for lot_id, change in changes.items():
                lot = connection.execute(
                    "SELECT initial_quantity,remaining_quantity FROM stock_lots WHERE id=?",
                    (lot_id,),
                ).fetchone()
                remaining = lot["remaining_quantity"] + change
                if not 0 <= remaining <= lot["initial_quantity"]:
                    raise DomainError("CONFLICT", "删除后的批次数量超出范围，请先处理关联流水")
                if lot_id in created_lots and remaining != 0:
                    raise DomainError("CONFLICT", "该入库批次还有其他库存，无法删除")
                connection.execute(
                    "UPDATE stock_lots SET remaining_quantity=? WHERE id=?", (remaining, lot_id)
                )

            connection.execute("DELETE FROM inventory_transactions WHERE group_id=?", (group_id,))
            connection.execute(
                "DELETE FROM transaction_group_associations WHERE group_id=?", (group_id,)
            )
            connection.execute("DELETE FROM order_import_items WHERE transaction_group_id=?", (group_id,))
            for lot_id in created_lots:
                batch_id = connection.execute(
                    "SELECT batch_id FROM stock_lots WHERE id=?", (lot_id,)
                ).fetchone()[0]
                connection.execute("DELETE FROM stock_lots WHERE id=?", (lot_id,))
                if batch_id is not None:
                    self._delete_empty_batch(connection, batch_id)
            for component_id in {row["component_id"] for row in rows}:
                self._restore_component_with_stock(connection, component_id)

    @staticmethod
    def _restore_component_with_stock(connection: sqlite3.Connection, component_id: str) -> None:
        connection.execute(
            """UPDATE components SET archived_at=NULL,updated_at=? WHERE id=? AND archived_at IS NOT NULL
            AND EXISTS (SELECT 1 FROM stock_lots WHERE component_id=? AND remaining_quantity>0)""",
            (utc_now(), component_id, component_id),
        )

    @log_operation("冲销库存操作")
    def reverse_group(self, group_id: str, reason: str) -> dict[str, Any]:
        """用数值相反的新流水冲销整个事务组。

        :param group_id: 需要冲销的原事务组编号
        :param reason: 必填的冲销原因，写入审计流水
        :return: 新冲销事务组编号和受影响流水数
        :raises DomainError: 原组不存在、已冲销或相关入库数量已被使用时抛出

        注意：原流水不会被删除。正向入库的冲销必须先确认对应批次仍有足够余量。
        """

        ### 校验冲销请求和原事务组 ###
        if not reason.strip():
            raise DomainError("VALIDATION_ERROR", "必须填写冲销原因")
        with self.write() as connection:
            rows = connection.execute(
                "SELECT * FROM inventory_transactions WHERE group_id=? ORDER BY rowid DESC",
                (group_id,),
            ).fetchall()
            if not rows:
                raise DomainError("NOT_FOUND", "库存事务组不存在")
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            if connection.execute(
                f"SELECT 1 FROM inventory_transactions WHERE reversal_of IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone():
                raise DomainError("CONFLICT", "该事务组已经冲销")
            ### 验证正向流水仍可撤销 ###
            # 正向流水的冲销需要从原批次取回数量，因此必须先验证剩余量。
            for row in rows:
                if row["quantity_delta"] > 0:
                    remaining = float(
                        connection.execute(
                            "SELECT remaining_quantity FROM stock_lots WHERE id=?", (row["lot_id"],)
                        ).fetchone()[0]
                    )
                    if remaining + EPSILON < row["quantity_delta"]:
                        raise DomainError("INSUFFICIENT_STOCK", "部分已入库库存已经使用，无法冲销")
            ### 写入反向库存变化和审计流水 ###
            reversal_group = str(uuid.uuid4())
            for row in rows:
                connection.execute(
                    "UPDATE stock_lots SET remaining_quantity=remaining_quantity-? WHERE id=?",
                    (row["quantity_delta"], row["lot_id"]),
                )
                self._insert_transaction(
                    connection,
                    reversal_group,
                    "REVERSAL",
                    row["component_id"],
                    row["lot_id"],
                    row["location_id"],
                    -row["quantity_delta"],
                    "冲销",
                    reason,
                    row["id"],
                )
        return {"transaction_group_id": reversal_group, "affected_transactions": len(rows)}

    @log_operation("BOM 批量出库")
    def commit_bom(
        self, data: BomCommitRequest, *, platform: str | None = None
    ) -> dict[str, Any]:
        """验证全部 BOM 行后一次性创建 BOM 并扣减库存。

        :param data: 用户确认后的 BOM 名称、来源文件和逐行元件选择
        :return: BOM 编号、事务组编号和生成的出库流水数
        :raises DomainError: 元件不存在或任意累计需求库存不足时抛出

        注意：同一元件可能出现在多行中，必须先按元件和指定库位汇总需求；任一行
        缺料都会回滚 BOM、映射记录和所有库存变化。
        """

        ### 汇总并验证全部 BOM 需求 ###
        with self.write() as connection:
            # 同一元件可能在 BOM 中出现多行，必须按累计需求验证，不能逐行各自
            # 对比完整库存。指定仓库的需求还要额外验证该仓库自身的余量。
            total_demand: dict[str, int] = {}
            location_demand: dict[tuple[str, str], int] = {}
            for line in data.lines:
                self._require(connection, "components", line.component_id, "元件")
                total_demand[line.component_id] = (
                    total_demand.get(line.component_id, 0) + line.quantity
                )
                if line.location_id:
                    key = (line.component_id, line.location_id)
                    location_demand[key] = location_demand.get(key, 0) + line.quantity
            for component_id, required in total_demand.items():
                available = self._available(connection, component_id, None)
                if available + EPSILON < required:
                    raise DomainError(
                        "INSUFFICIENT_STOCK",
                        f"BOM 累计库存不足：需要 {required}，可用 {available}",
                    )
            for (component_id, location_id), required in location_demand.items():
                available = self._available(connection, component_id, location_id)
                if available + EPSILON < required:
                    raise DomainError(
                        "INSUFFICIENT_STOCK",
                        f"BOM 指定仓库库存不足：需要 {required}，可用 {available}",
                    )
            ### 保存 BOM、人工映射并逐行扣减库存 ###
            bom_id = str(uuid.uuid4())
            group_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO boms(id,name,source_filename,created_at) VALUES(?,?,?,?)",
                (bom_id, data.name.strip(), clean(data.source_filename), utc_now()),
            )
            affected = 0
            for line in data.lines:
                connection.execute(
                    "INSERT INTO bom_lines(id,bom_id,row_number,raw_json,component_id,quantity) VALUES(?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        bom_id,
                        line.row_number,
                        json.dumps(
                            {
                                "input": line.raw_input,
                                "mapping_key": line.mapping_key,
                                "match_reason": line.match_reason,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        line.component_id,
                        line.quantity,
                    ),
                )
                if line.remember_mapping and line.mapping_key:
                    connection.execute(
                        """INSERT INTO part_mappings(id,mapping_key,component_id,created_at)
                        VALUES(?,?,?,?) ON CONFLICT(mapping_key) DO UPDATE SET
                        component_id=excluded.component_id,created_at=excluded.created_at""",
                        (str(uuid.uuid4()), line.mapping_key, line.component_id, utc_now()),
                    )
                affected += self._issue_from_lots(
                    connection,
                    group_id,
                    line.component_id,
                    line.location_id,
                    line.quantity,
                    f"BOM: {data.name}",
                    None,
                    "BOM_ISSUE",
                    platform=platform,
                )
        return {
            "bom_id": bom_id,
            "transaction_group_id": group_id,
            "affected_transactions": affected,
        }

    def _available(
        self,
        connection: sqlite3.Connection,
        component_id: str,
        location_id: str | None,
        batch_id: str | None = None,
    ) -> float:
        """在当前事务快照内计算可用库存。"""

        sql = "SELECT COALESCE(SUM(remaining_quantity),0) FROM stock_lots WHERE component_id=?"
        parameters: list[Any] = [component_id]
        if location_id:
            sql += " AND location_id=?"
            parameters.append(location_id)
        if batch_id:
            sql += " AND batch_id=?"
            parameters.append(batch_id)
        row = connection.execute(sql, parameters).fetchone()
        return float(row[0])

    def _consume_lots(
        self,
        connection: sqlite3.Connection,
        component_id: str,
        location_id: str | None,
        quantity: float,
        batch_id: str | None = None,
    ) -> list[tuple[str, float, float | None]]:
        """按 FIFO 扣减批次，返回 ``(批次, 使用量, 单价)``。"""

        if batch_id:
            batch = connection.execute(
                "SELECT component_id FROM inventory_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise DomainError("NOT_FOUND", "批次不存在")
            if batch["component_id"] != component_id:
                raise DomainError("VALIDATION_ERROR", "批次与出库元件不一致")
        available = self._available(connection, component_id, location_id, batch_id)
        if available + EPSILON < quantity:
            raise DomainError("INSUFFICIENT_STOCK", f"库存不足：需要 {quantity}，可用 {available}")
        sql = "SELECT id,remaining_quantity,unit_price FROM stock_lots WHERE component_id=? AND remaining_quantity>0"
        parameters: list[Any] = [component_id]
        if location_id:
            sql += " AND location_id=?"
            parameters.append(location_id)
        if batch_id:
            sql += " AND batch_id=?"
            parameters.append(batch_id)
        sql += " ORDER BY received_at,id"
        remaining = quantity
        used: list[tuple[str, float, float | None]] = []
        for row in connection.execute(sql, parameters):
            if remaining <= EPSILON:
                break
            amount = min(remaining, float(row["remaining_quantity"]))
            connection.execute(
                "UPDATE stock_lots SET remaining_quantity=remaining_quantity-? WHERE id=?",
                (amount, row["id"]),
            )
            used.append((row["id"], amount, row["unit_price"]))
            remaining -= amount
        return used

    def _issue_from_lots(
        self,
        connection: sqlite3.Connection,
        group_id: str,
        component_id: str,
        location_id: str | None,
        quantity: float,
        source: str | None,
        notes: str | None,
        kind: str,
        batch_id: str | None = None,
        *,
        platform: str | None = None,
    ) -> int:
        """扣减批次并写入负向流水；调用方必须已经开启写事务。"""

        used = self._consume_lots(connection, component_id, location_id, quantity, batch_id)
        for lot_id, amount, _ in used:
            actual_location = connection.execute(
                "SELECT location_id FROM stock_lots WHERE id=?", (lot_id,)
            ).fetchone()[0]
            self._insert_transaction(
                connection,
                group_id,
                kind,
                component_id,
                lot_id,
                actual_location,
                -amount,
                source,
                notes,
                platform=platform,
            )
        return len(used)

    @staticmethod
    def _require(connection: sqlite3.Connection, table: str, item_id: str, label: str) -> None:
        """确认外键对象存在；表名只允许由内部常量传入。"""

        if table not in {"components", "locations", "warehouses"}:
            raise ValueError("不允许的表名")
        if connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone() is None:
            raise DomainError("NOT_FOUND", f"{label}不存在")

    @staticmethod
    def _insert_transaction(
        connection: sqlite3.Connection,
        group_id: str,
        kind: str,
        component_id: str,
        lot_id: str,
        location_id: str,
        quantity: float,
        source: str | None,
        notes: str | None,
        reversal_of: str | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        """插入库存流水；只有冲销记录允许填写 ``reversal_of``。"""

        timestamp = utc_now()
        previous = connection.execute(
            """SELECT t.group_id,t.kind,t.source,
            round((julianday(?) - julianday(t.created_at)) * 86400) elapsed,a.display_group_id,a.manual
            FROM inventory_transactions t
            LEFT JOIN transaction_group_associations a ON a.group_id=t.group_id
            ORDER BY t.rowid DESC LIMIT 1""", (timestamp,)
        ).fetchone()
        if (
            previous and previous["group_id"] != group_id and not previous["manual"]
            and previous["elapsed"] is not None and 0 <= previous["elapsed"] <= TRANSACTION_MERGE_SECONDS
            and _transaction_kind(kind, source) == _transaction_kind(previous["kind"], previous["source"])
        ):
            connection.execute(
                """INSERT OR IGNORE INTO transaction_group_associations(group_id,display_group_id)
                VALUES(?,?)""",
                (group_id, previous["display_group_id"] or previous["group_id"]),
            )
        connection.execute(
            """INSERT INTO inventory_transactions(id,group_id,kind,component_id,lot_id,
            location_id,quantity_delta,source,notes,reversal_of,created_at,operation_platform)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                group_id,
                kind,
                component_id,
                lot_id,
                location_id,
                quantity,
                clean(source),
                clean(notes),
                reversal_of,
                timestamp,
                platform,
            ),
        )

    @log_operation("创建数据库备份")
    def backup(self, directory: Path | None = None, *, automatic: bool = False) -> dict[str, str]:
        """使用 SQLite 在线备份 API 创建一致性快照和 SHA-256 文件。"""

        destination_dir = directory or self.path.parent / "backups"
        destination_dir.mkdir(parents=True, exist_ok=True)
        prefix = "component-library-auto" if automatic else "component-library"
        destination = destination_dir / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        with TemporaryDirectory(dir=destination_dir) as temporary, self.read() as source:
            snapshot = Path(temporary) / destination.name
            source.execute("BEGIN")
            source.execute("SELECT count(*) FROM components").fetchone()
            self._write_snapshot(source, snapshot)
            self._include_catalogs_in_backup(source, snapshot)
            with snapshot.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            checksum = snapshot.with_suffix(".db.sha256")
            checksum.write_text(digest + "\n", encoding="ascii")
            checksum.replace(destination.with_suffix(".db.sha256"))
            snapshot.replace(destination)
        return {"path": str(destination), "sha256": digest}

    @property
    def automatic_backup_directory(self) -> Path:
        return self.path.parent / "backups" / "automatic"

    def automatic_backups(self) -> list[Path]:
        """列出程序创建的自动快照，按时间从新到旧排序。"""

        return sorted(
            (path for path in self.automatic_backup_directory.glob("component-library-auto-*.db")
             if re.fullmatch(r"component-library-auto-\d{8}-\d{6}-\d{6}\.db", path.name)),
            reverse=True,
        )

    def automatic_backup(self, retain: int) -> dict[str, str]:
        """成功创建快照后轮换自动备份，手动备份存放在独立位置。"""

        if retain < 1:
            raise DomainError("VALIDATION_ERROR", "至少保留一份自动备份")
        result = self.backup(self.automatic_backup_directory, automatic=True)
        for old in self.automatic_backups()[retain:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".db.sha256").unlink(missing_ok=True)
        return result

    @staticmethod
    def _write_snapshot(source: sqlite3.Connection, destination: Path) -> None:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()

    @staticmethod
    def _include_catalogs_in_backup(source: sqlite3.Connection, destination: Path) -> None:
        with closing(sqlite3.connect(destination)) as target, target:
            Database._create_catalog_backup_table(target)
            for catalog in CATALOGS:
                target.executemany(
                    f"INSERT INTO {SNAPSHOT_TABLE}(kind,id,category,name,payload) VALUES(?,?,?,?,?)",
                    (
                        (catalog, *row) for row in source.execute(
                            f"SELECT id,category,name,payload FROM {catalog}.catalog_components"
                        )
                    ),
                )

    @staticmethod
    def _create_catalog_backup_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE}(
            kind TEXT NOT NULL,id TEXT NOT NULL,category TEXT,name TEXT NOT NULL,
            payload TEXT NOT NULL,PRIMARY KEY(kind,id))"""
        )

    @log_operation("安排重启后恢复备份")
    def prepare_restore(self, source: Path) -> dict[str, str]:
        """校验备份并暂存，真正替换在下次启动、数据库打开前进行。"""

        if not source.is_file():
            raise DomainError("NOT_FOUND", "备份文件不存在")
        checksum = source.with_suffix(".db.sha256")
        if checksum.is_file():
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            recorded = checksum.read_text(encoding="ascii").strip().split(maxsplit=1)
            if not recorded or digest != recorded[0].casefold():
                raise DomainError("VALIDATION_ERROR", "备份 SHA-256 校验失败")
        check = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        pending = self.path.with_suffix(".restore-pending.db")
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("VALIDATION_ERROR", "备份完整性检查失败")
            required = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('components','stock_lots','inventory_transactions')"
            ).fetchone()[0]
            if required != 3:
                raise DomainError("VALIDATION_ERROR", "文件不是有效的元件库备份")
            with TemporaryDirectory(dir=self.path.parent) as temporary:
                snapshot = Path(temporary) / "restore.db"
                self._write_snapshot(check, snapshot)
                with closing(sqlite3.connect(snapshot)) as target:
                    self._create_catalog_backup_table(target)
                snapshot.replace(pending)
        finally:
            check.close()
        return {"pending_path": str(pending), "message": "已校验，将在重启后恢复"}

    @log_operation("安排重启后清空数据")
    def prepare_reset(self) -> dict[str, str]:
        """暂存新空库，由下次启动统一替换当前库。"""

        with TemporaryDirectory(dir=self.path.parent) as temporary:
            fresh = Database(Path(temporary) / "empty.db")
            return self.prepare_restore(fresh.path)

    def idempotent(self, key: str, operation: str, work: Any) -> Any:
        """执行一次写操作并持久保存响应。

        查询旧响应和实际写入共用一个 ``BEGIN IMMEDIATE`` 事务。``work`` 必须接受
        当前连接；这样应用在写入业务数据后崩溃时，不会只保存其中一半。
        """

        if not key.strip():
            raise DomainError("VALIDATION_ERROR", "写接口必须提供 Idempotency-Key")
        with self.write() as connection:
            existing = connection.execute(
                "SELECT operation,response_json FROM idempotency_records WHERE key=?", (key,)
            ).fetchone()
            if existing:
                if existing["operation"] != operation:
                    raise DomainError("CONFLICT", "同一幂等键不能用于不同操作")
                return json.loads(existing["response_json"])
            result = work(connection)
            connection.execute(
                "INSERT INTO idempotency_records(key,operation,response_json,created_at) VALUES(?,?,?,?)",
                (key, operation, json.dumps(result, ensure_ascii=False), utc_now()),
            )
            return result

    def save_scan_session(
        self, kind: str, raw_text: str, confidence: float | None
    ) -> dict[str, Any]:
        """保存待确认扫描草稿，不修改库存。"""

        item = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "raw_text": raw_text,
            "confidence": confidence,
            "status": "DRAFT",
            "created_at": utc_now(),
        }
        with self.write() as connection:
            connection.execute(
                "INSERT INTO scan_sessions(id,kind,raw_text,confidence,status,created_at) VALUES(:id,:kind,:raw_text,:confidence,:status,:created_at)",
                item,
            )
        return item

    @log_operation("保存识别纠错样本")
    def save_correction(
        self,
        field_name: str,
        original_text: str,
        corrected_text: str,
        model_version: str,
        confidence: float,
        quality: dict[str, Any],
        image_path: str | None = None,
    ) -> dict[str, Any]:
        """保存用户明确授权的本地 OCR 纠错记录。"""

        item = {
            "id": str(uuid.uuid4()),
            "field_name": field_name,
            "original_text": original_text,
            "corrected_text": corrected_text,
            "model_version": model_version,
            "confidence": confidence,
            "quality_json": json.dumps(quality, ensure_ascii=False),
            "image_path": image_path,
            "created_at": utc_now(),
        }
        with self.write() as connection:
            connection.execute(
                """INSERT INTO ocr_corrections(id,field_name,original_text,corrected_text,
                model_version,confidence,quality_json,image_path,created_at)
                VALUES(:id,:field_name,:original_text,:corrected_text,:model_version,
                :confidence,:quality_json,:image_path,:created_at)""",
                item,
            )
        return item

    def list_corrections(self) -> list[dict[str, Any]]:
        """列出本机保存的 OCR 纠错样本元数据。"""

        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM ocr_corrections ORDER BY created_at DESC"
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["quality"] = json.loads(item.pop("quality_json"))
            output.append(item)
        return output

    @staticmethod
    @log_operation("应用待恢复数据库")
    def apply_pending_restore(path: Path) -> bool:
        """在数据库尚未打开时原子应用已暂存的恢复文件。"""

        pending = path.with_suffix(".restore-pending.db")
        if not pending.exists():
            return False
        previous = path.with_suffix(".before-restore.db")
        if path.exists():
            main_usable = True
            connection = sqlite3.connect(path)
            try:
                if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise DomainError("CONFLICT", "数据库正在使用，无法安全恢复")
            except sqlite3.DatabaseError as error:
                if getattr(error, "sqlite_errorcode", None) not in {
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB,
                } or path.with_name(path.name + "-wal").exists():
                    raise
                main_usable = False
            finally:
                connection.close()
            with TemporaryDirectory(dir=path.parent) as temporary:
                snapshot = Path(temporary) / previous.name
                shutil.copy2(path, snapshot)
                if main_usable and all(catalog_path(path, catalog).is_file() for catalog in CATALOGS):
                    with closing(sqlite3.connect(path)) as source:
                        Database._attach_catalogs(source, path)
                        Database._include_catalogs_in_backup(source, snapshot)
                snapshot.replace(previous)
        os.replace(pending, path)
        return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT OR IGNORE INTO schema_migrations VALUES(1,datetime('now'));
CREATE TABLE IF NOT EXISTS components(
 id TEXT PRIMARY KEY,name TEXT NOT NULL CHECK(trim(name)<>''),aliases TEXT NOT NULL DEFAULT '[]',
 lcsc_part_number TEXT UNIQUE,manufacturer_part_number TEXT,custom_part_number TEXT UNIQUE,
 brand TEXT,package TEXT,category TEXT,specification TEXT,price REAL,purchase_date TEXT,
 product_url TEXT,datasheet_url TEXT,notes TEXT,jlc_matched INTEGER NOT NULL DEFAULT 0,
 archived_at TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_components_name ON components(name);
CREATE INDEX IF NOT EXISTS idx_components_mpn ON components(manufacturer_part_number);
CREATE TABLE IF NOT EXISTS warehouses(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE CHECK(trim(name)<>''),notes TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locations(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),name TEXT NOT NULL CHECK(trim(name)<>''),notes TEXT,created_at TEXT NOT NULL,UNIQUE(warehouse_id,name));
CREATE TABLE IF NOT EXISTS supplier_parts(id TEXT PRIMARY KEY,component_id TEXT NOT NULL REFERENCES components(id),provider TEXT NOT NULL,provider_part_number TEXT NOT NULL,snapshot_json TEXT,last_fetched_at TEXT,UNIQUE(provider,provider_part_number));
CREATE TABLE IF NOT EXISTS inventory_batches(id TEXT PRIMARY KEY,component_id TEXT NOT NULL REFERENCES components(id),source_type TEXT NOT NULL CHECK(source_type IN ('JLC','CUSTOM')),external_key TEXT NOT NULL UNIQUE,order_number TEXT,lcsc_part_number TEXT,custom_code TEXT UNIQUE,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock_lots(id TEXT PRIMARY KEY,component_id TEXT NOT NULL REFERENCES components(id),location_id TEXT NOT NULL REFERENCES locations(id),initial_quantity INTEGER NOT NULL CHECK(initial_quantity>0),remaining_quantity INTEGER NOT NULL CHECK(remaining_quantity>=0),source TEXT,unit_price REAL,supplier_part_id TEXT REFERENCES supplier_parts(id),notes TEXT,received_at TEXT NOT NULL,purchase_date TEXT,batch_id TEXT REFERENCES inventory_batches(id));
CREATE INDEX IF NOT EXISTS idx_lots_fifo ON stock_lots(component_id,location_id,received_at);
CREATE TABLE IF NOT EXISTS inventory_transactions(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,kind TEXT NOT NULL,component_id TEXT NOT NULL REFERENCES components(id),lot_id TEXT NOT NULL REFERENCES stock_lots(id),location_id TEXT NOT NULL REFERENCES locations(id),quantity_delta INTEGER NOT NULL CHECK(quantity_delta<>0),source TEXT,notes TEXT,reversal_of TEXT REFERENCES inventory_transactions(id),created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_transactions_component ON inventory_transactions(component_id,created_at);
CREATE TABLE IF NOT EXISTS transaction_group_associations(
 group_id TEXT PRIMARY KEY, display_group_id TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS scan_sessions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,raw_text TEXT NOT NULL,confidence REAL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boms(id TEXT PRIMARY KEY,name TEXT NOT NULL,source_filename TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bom_lines(id TEXT PRIMARY KEY,bom_id TEXT NOT NULL REFERENCES boms(id),row_number INTEGER NOT NULL,raw_json TEXT NOT NULL,component_id TEXT REFERENCES components(id),quantity INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS part_mappings(id TEXT PRIMARY KEY,mapping_key TEXT NOT NULL UNIQUE,component_id TEXT NOT NULL REFERENCES components(id),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_records(key TEXT PRIMARY KEY,operation TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ocr_corrections(id TEXT PRIMARY KEY,field_name TEXT NOT NULL,original_text TEXT NOT NULL,corrected_text TEXT NOT NULL,model_version TEXT NOT NULL,confidence REAL NOT NULL,quality_json TEXT NOT NULL,image_path TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_host_state(host TEXT PRIMARY KEY,last_request_at TEXT,day_key TEXT,day_count INTEGER NOT NULL DEFAULT 0,blocked_until TEXT,robots_text TEXT,robots_expires_at TEXT);
CREATE TABLE IF NOT EXISTS web_page_cache(url TEXT PRIMARY KEY,metadata_json TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issue_preferences(
 id INTEGER PRIMARY KEY CHECK(id=1), location_id TEXT REFERENCES locations(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS order_import_items(
 order_number TEXT NOT NULL,lcsc_part_number TEXT NOT NULL,
 component_id TEXT NOT NULL REFERENCES components(id),transaction_group_id TEXT NOT NULL,
 imported_at TEXT NOT NULL,PRIMARY KEY(order_number,lcsc_part_number));
CREATE TABLE IF NOT EXISTS resistor_details(
 component_id TEXT PRIMARY KEY REFERENCES components(id),resistance TEXT,
 tolerance_percent REAL,power_mw REAL,technology TEXT,source_description TEXT);
CREATE TABLE IF NOT EXISTS mosfet_details(
 component_id TEXT PRIMARY KEY REFERENCES components(id),channel_type TEXT,
 mosfet_count INTEGER,voltage_v REAL,current_a REAL,source_description TEXT);
CREATE TABLE IF NOT EXISTS component_category_parameters(
 component_id TEXT NOT NULL REFERENCES components(id),parameter_key TEXT NOT NULL,
 label TEXT NOT NULL,value TEXT NOT NULL,sort_order INTEGER NOT NULL DEFAULT 0,
 group_name TEXT NOT NULL DEFAULT '类别参数',
 PRIMARY KEY(component_id,parameter_key));
CREATE TABLE IF NOT EXISTS storage_location_priorities(
 location_id TEXT PRIMARY KEY REFERENCES locations(id),priority INTEGER NOT NULL CHECK(priority>=0));
CREATE TABLE IF NOT EXISTS storage_category_priorities(
 category TEXT NOT NULL,location_id TEXT NOT NULL REFERENCES locations(id),
 priority INTEGER NOT NULL CHECK(priority>=0),PRIMARY KEY(category,location_id));
INSERT OR IGNORE INTO schema_migrations VALUES(2,datetime('now'));
INSERT OR IGNORE INTO schema_migrations VALUES(4,datetime('now'));
INSERT OR IGNORE INTO schema_migrations VALUES(5,datetime('now'));
INSERT OR IGNORE INTO schema_migrations VALUES(6,datetime('now'));
INSERT OR IGNORE INTO schema_migrations VALUES(9,datetime('now'));
"""

INTEGER_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS stock_lots_integer_insert BEFORE INSERT ON stock_lots
WHEN NEW.initial_quantity != CAST(NEW.initial_quantity AS INTEGER)
  OR NEW.remaining_quantity != CAST(NEW.remaining_quantity AS INTEGER)
BEGIN SELECT RAISE(ABORT,'库存数量必须为整数'); END;
CREATE TRIGGER IF NOT EXISTS stock_lots_integer_update BEFORE UPDATE OF initial_quantity,remaining_quantity ON stock_lots
WHEN NEW.initial_quantity != CAST(NEW.initial_quantity AS INTEGER)
  OR NEW.remaining_quantity != CAST(NEW.remaining_quantity AS INTEGER)
BEGIN SELECT RAISE(ABORT,'库存数量必须为整数'); END;
CREATE TRIGGER IF NOT EXISTS transactions_integer_insert BEFORE INSERT ON inventory_transactions
WHEN NEW.quantity_delta != CAST(NEW.quantity_delta AS INTEGER)
BEGIN SELECT RAISE(ABORT,'流水数量必须为整数'); END;
CREATE TRIGGER IF NOT EXISTS bom_lines_integer_insert BEFORE INSERT ON bom_lines
WHEN NEW.quantity != CAST(NEW.quantity AS INTEGER)
BEGIN SELECT RAISE(ABORT,'BOM 数量必须为整数'); END;
"""
