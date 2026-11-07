"""SQLite 持久化与库存事务。

本模块是库存一致性的唯一写入口。每个写操作都显式使用 ``BEGIN IMMEDIATE``，
这样库存检查和扣减位于同一个事务中，不会出现两个并发请求都看到旧库存的情况。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    AdjustRequest,
    BomCommitRequest,
    ComponentCreate,
    IssueRequest,
    JlcParameterGroup,
    LocationCreate,
    OrderImportLine,
    ReceiveRequest,
    TransferRequest,
    WarehouseCreate,
)

EPSILON = 1e-9
CUSTOM_BATCH_CODE = re.compile(r"^CL\d{10}$")


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
            line.resistor.resistance,
            f"±{line.resistor.tolerance_percent:g}%"
            if line.resistor.tolerance_percent is not None
            else None,
            f"{line.resistor.power_mw:g}mW" if line.resistor.power_mw is not None else None,
        ]
        return " ".join(value for value in values if value) or None
    if line.mosfet:
        values = [
            f"{line.mosfet.channel_type}沟道" if line.mosfet.channel_type else None,
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
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        """创建配置一致的新连接，调用方负责关闭。"""

        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def migrate(self) -> None:
        """创建或升级数据库；所有迁移均设计为可重复执行。"""

        with self.write() as connection:
            connection.executescript(SCHEMA)
            self._migrate_v3(connection)
            self._migrate_v6(connection)
            self._migrate_v8(connection)
            self._migrate_v9(connection)

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

    def integrity_check(self) -> None:
        """验证数据库物理完整性，不通过时抛出业务错误。"""

        with self.read() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise DomainError("DATABASE_CORRUPT", f"数据库完整性检查失败：{result}")

    def create_component(self, data: ComponentCreate) -> dict[str, Any]:
        """创建元件并返回完整记录。"""

        component_id = str(uuid.uuid4())
        timestamp = utc_now()
        values = (
            component_id,
            data.name.strip(),
            json.dumps(data.aliases, ensure_ascii=False),
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
            clean(data.datasheet_url),
            clean(data.notes),
            timestamp,
        )
        try:
            with self.write() as connection:
                connection.execute(
                    """INSERT INTO components(
                    id,name,aliases,lcsc_part_number,manufacturer_part_number,
                    custom_part_number,brand,package,category,specification,price,
                    purchase_date,product_url,datasheet_url,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, timestamp),
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
        return [
            {"parameter_key": key, "label": label, "value": value} for key, label, value in cleaned
        ]

    def save_component_parameter_groups(
        self, component_id: str, groups: list[JlcParameterGroup]
    ) -> list[dict[str, str]]:
        """保存带页面分组的动态参数，并同步派生规格摘要。

        调用方必须传入用户最终确认后的完整参数集合；本方法与旧接口一样采用整体替换，
        从而避免已删除的表单字段残留在数据库中。
        """

        cleaned = [
            (parameter.key, parameter.label.strip(), parameter.value.strip(), group.name.strip())
            for group in groups
            for parameter in group.parameters
            if parameter.label.strip() and parameter.value.strip()
        ]
        with self.write() as connection:
            self._require(connection, "components", component_id, "元件")
            connection.execute(
                "DELETE FROM component_category_parameters WHERE component_id=?", (component_id,)
            )
            for order, (key, label, value, group_name) in enumerate(cleaned):
                connection.execute(
                    """INSERT INTO component_category_parameters(
                    component_id,parameter_key,label,value,sort_order,group_name)
                    VALUES(?,?,?,?,?,?)""",
                    (component_id, key, label, value, order, group_name or "类别参数"),
                )
            summary = "；".join(f"{label}: {value}" for _, label, value, _ in cleaned) or None
            connection.execute(
                "UPDATE components SET specification=?,updated_at=? WHERE id=?",
                (summary, utc_now(), component_id),
            )
        return [
            {
                "parameter_key": key,
                "label": label,
                "value": value,
                "group_name": group_name or "类别参数",
            }
            for key, label, value, group_name in cleaned
        ]

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
            return {"id": component_id, "deleted": True, "mode": "deleted"}

    def update_component(self, component_id: str, data: ComponentCreate) -> dict[str, Any]:
        """保存用户确认后的元件主数据，并刷新最后更改时间。"""

        try:
            with self.write() as connection:
                self._require(connection, "components", component_id, "元件")
                connection.execute(
                    """UPDATE components SET name=?,aliases=?,lcsc_part_number=?,
                    manufacturer_part_number=?,custom_part_number=?,brand=?,package=?,category=?,
                    specification=?,price=?,purchase_date=?,product_url=?,datasheet_url=?,notes=?,
                    updated_at=? WHERE id=?""",
                    (
                        data.name.strip(),
                        json.dumps(data.aliases, ensure_ascii=False),
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
                        clean(data.datasheet_url),
                        clean(data.notes),
                        utc_now(),
                        component_id,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "元件名称或编号与现有记录冲突") from error
        return self.get_component(component_id)

    def list_components(self, query: str = "") -> list[dict[str, Any]]:
        """按元件资料搜索，并附加由订单导入记录汇总出的订单号。"""

        needle = query.strip()
        with self.read() as connection:
            rows = connection.execute(
                """SELECT c.*,
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

    @staticmethod
    def _component(row: sqlite3.Row) -> dict[str, Any]:
        """把数据库行转换成 API 兼容字典。"""

        item = dict(row)
        try:
            item["aliases"] = json.loads(item["aliases"])
        except (json.JSONDecodeError, TypeError):
            item["aliases"] = []
        return item

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
                connection.execute(
                    "INSERT INTO locations(id,warehouse_id,name,notes,created_at) VALUES(:id,:warehouse_id,:name,:notes,:created_at)",
                    item,
                )
        except sqlite3.IntegrityError as error:
            raise DomainError("CONFLICT", "仓库名称重复，或上级位置不存在") from error
        return item

    def list_locations(self) -> list[dict[str, Any]]:
        """列出全部具体仓库。"""

        with self.read() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM locations ORDER BY warehouse_id,name")
            ]

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
                    """SELECT l.id location_id,l.name location_name,w.name warehouse_name,
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
                    """SELECT l.id location_id,l.name location_name,w.name warehouse_name,
                    gp.priority,NULL global_priority
                    FROM locations l JOIN warehouses w ON w.id=l.warehouse_id
                    LEFT JOIN storage_location_priorities gp ON gp.location_id=l.id
                    ORDER BY COALESCE(gp.priority,100000),w.name,l.name"""
                ).fetchall()
        return [dict(row) for row in rows]

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
                w.name warehouse_name,l.id location_id,l.name location_name,
                COALESCE(SUM(s.remaining_quantity),0) quantity
                FROM stock_lots s JOIN components c ON c.id=s.component_id
                JOIN locations l ON l.id=s.location_id JOIN warehouses w ON w.id=l.warehouse_id
                GROUP BY c.id,l.id HAVING quantity>0 ORDER BY c.name,w.name,l.name"""
            ).fetchall()
        return [dict(row) for row in rows]

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
                w.name warehouse_name,SUM(s.remaining_quantity) remaining_quantity
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
            output = []
            for line in lines:
                existing = connection.execute(
                    "SELECT id,name FROM components WHERE lcsc_part_number=?",
                    (line.lcsc_part_number,),
                ).fetchone()
                imported = connection.execute(
                    "SELECT 1 FROM order_import_items WHERE order_number=? AND lcsc_part_number=?",
                    (line.order_number, line.lcsc_part_number),
                ).fetchone()
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
                    }
                )
        return output

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
                        product_url=COALESCE(product_url,?),notes=COALESCE(notes,?),
                        specification=COALESCE(specification,?),price=?,purchase_date=?,
                        archived_at=NULL,updated_at=?
                        WHERE id=?""",
                        (
                            clean(line.category),
                            clean(line.manufacturer_part_number),
                            clean(line.package),
                            clean(line.brand),
                            clean(line.product_url),
                            clean(line.description),
                            _order_specification(line),
                            line.unit_price,
                            line.purchased_at[:10],
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
                        purchase_date,product_url,datasheet_url,notes,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            component_id,
                            line.name,
                            "[]",
                            line.lcsc_part_number,
                            clean(line.manufacturer_part_number),
                            None,
                            clean(line.brand),
                            clean(line.package),
                            clean(line.category),
                            _order_specification(line),
                            line.unit_price,
                            line.purchased_at[:10],
                            clean(line.product_url),
                            None,
                            clean(line.description) or f"由立创订单 {line.order_number} 离线导入",
                            timestamp,
                            timestamp,
                        ),
                    )
                    created_count += 1
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
                        line.unit_price,
                        "从用户保存的订单页面导入",
                        timestamp,
                        line.purchased_at[:10],
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
                    f"立创订单 {line.order_number}",
                    "离线订单导入",
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
        return {
            "transaction_group_id": group_id,
            "imported": imported_count,
            "skipped": skipped_count,
            "created_components": created_count,
        }

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

    def receive(self, data: ReceiveRequest, kind: str = "RECEIVE") -> dict[str, Any]:
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
                order_number=data.order_number,
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

    def issue(self, data: IssueRequest, kind: str = "ISSUE") -> dict[str, Any]:
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
            )
        return {"transaction_group_id": group_id, "affected_transactions": count}

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
            affected = 0
            for source_lot, quantity, unit_price in used:
                source_batch = connection.execute(
                    "SELECT batch_id FROM stock_lots WHERE id=?", (source_lot,)
                ).fetchone()[0]
                self._insert_transaction(
                    connection,
                    group_id,
                    "TRANSFER_OUT",
                    data.component_id,
                    source_lot,
                    data.from_location_id,
                    -quantity,
                    "移库",
                    data.notes,
                )
                target_lot = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO stock_lots(id,component_id,location_id,initial_quantity,
                    remaining_quantity,source,unit_price,notes,received_at,batch_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        target_lot,
                        data.component_id,
                        data.to_location_id,
                        quantity,
                        quantity,
                        "移库",
                        unit_price,
                        clean(data.notes),
                        utc_now(),
                        source_batch,
                    ),
                )
                self._insert_transaction(
                    connection,
                    group_id,
                    "TRANSFER_IN",
                    data.component_id,
                    target_lot,
                    data.to_location_id,
                    quantity,
                    "移库",
                    data.notes,
                )
                affected += 2
        return {"transaction_group_id": group_id, "affected_transactions": affected}

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

    def list_transactions(self) -> list[dict[str, Any]]:
        """返回带元件和位置名称的库存流水。"""

        with self.read() as connection:
            rows = connection.execute(
                """SELECT t.*,c.name component_name,w.name warehouse_name,l.name location_name
                FROM inventory_transactions t JOIN components c ON c.id=t.component_id
                JOIN locations l ON l.id=t.location_id JOIN warehouses w ON w.id=l.warehouse_id
                ORDER BY t.created_at DESC,t.rowid DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

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

    def commit_bom(self, data: BomCommitRequest) -> dict[str, Any]:
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
    ) -> None:
        """插入不可变流水；只有冲销记录允许填写 ``reversal_of``。"""

        connection.execute(
            """INSERT INTO inventory_transactions(id,group_id,kind,component_id,lot_id,
            location_id,quantity_delta,source,notes,reversal_of,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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
                utc_now(),
            ),
        )

    def backup(self, directory: Path | None = None) -> dict[str, str]:
        """使用 SQLite 在线备份 API 创建一致性快照和 SHA-256 文件。"""

        destination_dir = directory or self.path.parent / "backups"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"component-library-{datetime.now():%Y%m%d-%H%M%S}.db"
        with self.read() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        destination.with_suffix(".db.sha256").write_text(digest + "\n", encoding="ascii")
        return {"path": str(destination), "sha256": digest}

    def prepare_restore(self, source: Path) -> dict[str, str]:
        """校验备份并暂存，真正替换在下次启动、数据库打开前进行。"""

        if not source.is_file():
            raise DomainError("NOT_FOUND", "备份文件不存在")
        check = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("VALIDATION_ERROR", "备份完整性检查失败")
            required = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('components','stock_lots','inventory_transactions')"
            ).fetchone()[0]
            if required != 3:
                raise DomainError("VALIDATION_ERROR", "文件不是有效的元件库备份")
        finally:
            check.close()
        pending = self.path.with_suffix(".restore-pending.db")
        shutil.copy2(source, pending)
        return {"pending_path": str(pending), "message": "已校验，将在重启后恢复"}

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
    def apply_pending_restore(path: Path) -> bool:
        """在数据库尚未打开时原子应用已暂存的恢复文件。"""

        pending = path.with_suffix(".restore-pending.db")
        if not pending.exists():
            return False
        previous = path.with_suffix(".before-restore.db")
        if previous.exists():
            previous.unlink()
        if path.exists():
            os.replace(path, previous)
        try:
            os.replace(pending, path)
        except Exception:
            if previous.exists():
                os.replace(previous, path)
            raise
        return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT OR IGNORE INTO schema_migrations VALUES(1,datetime('now'));
CREATE TABLE IF NOT EXISTS components(
 id TEXT PRIMARY KEY,name TEXT NOT NULL CHECK(trim(name)<>''),aliases TEXT NOT NULL DEFAULT '[]',
 lcsc_part_number TEXT UNIQUE,manufacturer_part_number TEXT,custom_part_number TEXT UNIQUE,
 brand TEXT,package TEXT,category TEXT,specification TEXT,price REAL,purchase_date TEXT,
 product_url TEXT,datasheet_url TEXT,notes TEXT,archived_at TEXT,
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
CREATE TABLE IF NOT EXISTS scan_sessions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,raw_text TEXT NOT NULL,confidence REAL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boms(id TEXT PRIMARY KEY,name TEXT NOT NULL,source_filename TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bom_lines(id TEXT PRIMARY KEY,bom_id TEXT NOT NULL REFERENCES boms(id),row_number INTEGER NOT NULL,raw_json TEXT NOT NULL,component_id TEXT REFERENCES components(id),quantity INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS part_mappings(id TEXT PRIMARY KEY,mapping_key TEXT NOT NULL UNIQUE,component_id TEXT NOT NULL REFERENCES components(id),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_records(key TEXT PRIMARY KEY,operation TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ocr_corrections(id TEXT PRIMARY KEY,field_name TEXT NOT NULL,original_text TEXT NOT NULL,corrected_text TEXT NOT NULL,model_version TEXT NOT NULL,confidence REAL NOT NULL,quality_json TEXT NOT NULL,image_path TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_host_state(host TEXT PRIMARY KEY,last_request_at TEXT,day_key TEXT,day_count INTEGER NOT NULL DEFAULT 0,blocked_until TEXT,robots_text TEXT,robots_expires_at TEXT);
CREATE TABLE IF NOT EXISTS web_page_cache(url TEXT PRIMARY KEY,metadata_json TEXT NOT NULL,expires_at TEXT NOT NULL);
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
