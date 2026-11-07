"""库存事务与迁移回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from component_library.database import Database, DomainError
from component_library.models import (
    BomCommitLine,
    BomCommitRequest,
    ComponentCreate,
    IssueRequest,
    LocationCreate,
    ReceiveRequest,
    TransferRequest,
    WarehouseCreate,
)


@pytest.fixture
def inventory(tmp_path: Path) -> tuple[Database, dict, dict, dict]:
    """创建一个包含元件、仓库和库位的最小数据库。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(
        ComponentCreate(name="10k 电阻", lcsc_part_number="C25804")
    )
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    return database, component, warehouse, location


def total(database: Database, component_id: str) -> float:
    """汇总一个元件的全部库存。"""

    return sum(
        float(item["quantity"])
        for item in database.list_stock()
        if item["component_id"] == component_id
    )


def test_v9_migration_adds_batch_column_before_index(tmp_path: Path) -> None:
    """旧库没有 batch_id 时，迁移不能在加列之前创建索引。"""

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE stock_lots(
        id TEXT PRIMARY KEY,component_id TEXT NOT NULL,location_id TEXT NOT NULL,
        initial_quantity INTEGER NOT NULL,remaining_quantity INTEGER NOT NULL,
        source TEXT,unit_price REAL,supplier_part_id TEXT,notes TEXT,
        received_at TEXT NOT NULL,purchase_date TEXT)"""
    )
    connection.close()

    database = Database(path)
    with database.read() as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(stock_lots)")}
        indexes = {row["name"] for row in migrated.execute("PRAGMA index_list(stock_lots)")}
    assert "batch_id" in columns
    assert "idx_lots_batch" in indexes


def test_receive_and_prevent_negative_stock(inventory: tuple[Database, dict, dict, dict]) -> None:
    database, component, _, location = inventory
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=10)
    )
    assert total(database, component["id"]) == 10
    with pytest.raises(DomainError, match="库存不足"):
        database.issue(IssueRequest(component_id=component["id"], quantity=11))
    assert total(database, component["id"]) == 10


def test_custom_batch_scan_issues_only_bound_lot(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    first = database.receive(
        ReceiveRequest(
            component_id=component["id"],
            location_id=location["id"],
            quantity=8,
            batch_code="CL0000000001",
        )
    )
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=20)
    )
    batch = database.resolve_batch_code("cl0000000001")
    assert batch["id"] == first["batch_id"]
    assert batch["remaining_quantity"] == 8
    database.issue(
        IssueRequest(component_id=component["id"], quantity=5, batch_id=batch["id"])
    )
    assert database.get_batch(batch["id"])["remaining_quantity"] == 3
    assert total(database, component["id"]) == 23


def test_transfer_preserves_batch_identity(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, warehouse, source = inventory
    target = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A02"))
    received = database.receive(
        ReceiveRequest(
            component_id=component["id"], location_id=source["id"], quantity=6,
            batch_code="CL0000000003",
        )
    )
    database.transfer(
        TransferRequest(
            component_id=component["id"], from_location_id=source["id"],
            to_location_id=target["id"], quantity=2,
        )
    )
    batch = database.get_batch(received["batch_id"])
    assert batch["initial_quantity"] == 6
    assert batch["remaining_quantity"] == 6
    assert {item["location_name"] for item in batch["locations"]} == {"A01", "A02"}


def test_batch_code_validation_and_component_conflict(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    with pytest.raises(DomainError, match="CL 加十位数字"):
        database.receive(
            ReceiveRequest(
                component_id=component["id"], location_id=location["id"], quantity=1,
                batch_code="CL123",
            )
        )
    database.receive(
        ReceiveRequest(
            component_id=component["id"], location_id=location["id"], quantity=1,
            batch_code="CL0000000002",
        )
    )
    with pytest.raises(DomainError, match="不能重复入库"):
        database.receive(
            ReceiveRequest(
                component_id=component["id"], location_id=location["id"], quantity=1,
                batch_code="CL0000000002",
            )
        )
    second = database.create_component(ComponentCreate(name="第二个元件"))
    with pytest.raises(DomainError, match="其他元件"):
        database.receive(
            ReceiveRequest(
                component_id=second["id"], location_id=location["id"], quantity=1,
                batch_code="CL0000000002",
            )
        )


def test_jlc_batch_resolves_from_order_and_part(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    database.receive(
        ReceiveRequest(
            component_id=component["id"], location_id=location["id"], quantity=10,
            order_number="SO26090420076",
        )
    )
    batch = database.resolve_batch_code(
        "{on:SO26090420076,pc:C25804,pm:TEST,qty:10,pdi:123}"
    )
    assert batch["component_id"] == component["id"]
    assert batch["order_number"] == "SO26090420076"


def test_transfer_preserves_total(inventory: tuple[Database, dict, dict, dict]) -> None:
    database, component, warehouse, source = inventory
    target = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A02"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=source["id"], quantity=10)
    )
    result = database.transfer(
        TransferRequest(
            component_id=component["id"],
            from_location_id=source["id"],
            to_location_id=target["id"],
            quantity=4,
        )
    )
    assert result["affected_transactions"] == 2
    assert total(database, component["id"]) == 10
    target_stock = next(
        item for item in database.list_stock() if item["location_id"] == target["id"]
    )
    assert target_stock["quantity"] == 4


def test_bom_rolls_back_when_any_line_is_short(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    second = database.create_component(ComponentCreate(name="100nF 电容"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=10)
    )
    database.receive(
        ReceiveRequest(component_id=second["id"], location_id=location["id"], quantity=1)
    )
    request = BomCommitRequest(
        name="测试",
        lines=[
            BomCommitLine(row_number=1, component_id=component["id"], quantity=5),
            BomCommitLine(row_number=2, component_id=second["id"], quantity=2),
        ],
    )
    with pytest.raises(DomainError):
        database.commit_bom(request)
    assert total(database, component["id"]) == 10
    assert total(database, second["id"]) == 1


def test_reverse_once(inventory: tuple[Database, dict, dict, dict]) -> None:
    database, component, _, location = inventory
    operation = database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=3)
    )
    database.reverse_group(operation["transaction_group_id"], "误入库")
    assert total(database, component["id"]) == 0
    with pytest.raises(DomainError, match="已经冲销"):
        database.reverse_group(operation["transaction_group_id"], "重复")


def test_backup_is_valid(inventory: tuple[Database, dict, dict, dict], tmp_path: Path) -> None:
    database, _, _, _ = inventory
    result = database.backup(tmp_path / "exports")
    assert Path(result["path"]).is_file()
    assert len(result["sha256"]) == 64


def test_component_procurement_fields_and_integer_quantity(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    """采购元数据应保存正确，且小数数量在进入数据库前就被拒绝。"""

    database, component, _, location = inventory
    updated = database.update_component(
        component["id"],
        ComponentCreate(
            name="10k 电阻",
            lcsc_part_number="C25804",
            brand="UNI-ROYAL",
            package="0603",
            category="电阻",
            price=0.01,
            purchase_date="2026-11-03",
            product_url="https://example.com/product",
            datasheet_url="https://example.com/datasheet.pdf",
        ),
    )
    assert updated["brand"] == "UNI-ROYAL"
    assert updated["package"] == "0603"
    assert updated["category"] == "电阻"
    assert updated["product_url"].endswith("/product")
    database.receive(
        ReceiveRequest(
            component_id=component["id"],
            location_id=location["id"],
            quantity=7,
            unit_price=0.02,
            purchase_date="2026-11-04",
        )
    )
    listed = next(item for item in database.list_components() if item["id"] == component["id"])
    assert listed["price"] == 0.02
    assert listed["purchase_date"] == "2026-11-04"
    assert listed["last_received_at"]
    with pytest.raises(ValidationError):
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=1.5)


def test_structured_category_parameters_generate_summary(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="91R", category="贴片电阻"))
    database.save_component_parameters(
        component["id"],
        {
            "resistance": ("阻值", "91Ω"),
            "tolerance": ("精度", "±1%"),
            "power": ("功率", "62.5mW"),
            "empty": ("空字段", ""),
        },
    )
    detail = database.get_component(component["id"])
    assert detail["specification"] == "阻值: 91Ω；精度: ±1%；功率: 62.5mW"
    assert [item["parameter_key"] for item in detail["category_parameters"]] == [
        "resistance",
        "tolerance",
        "power",
    ]


def test_safe_delete_component_and_storage(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    unused = database.create_component(ComponentCreate(name="误建元件"))
    assert database.delete_component(unused["id"])["mode"] == "deleted"
    assert database.list_components() == []

    position = database.create_warehouse(WarehouseCreate(name="实验桌"))
    storage = database.create_location(LocationCreate(warehouse_id=position["id"], name="抽屉1"))
    updated_position = database.update_warehouse(
        position["id"], WarehouseCreate(name="实验台", notes="北侧")
    )
    assert updated_position["name"] == "实验台"
    updated_storage = database.update_location(
        storage["id"],
        LocationCreate(warehouse_id=position["id"], name="抽屉A", notes="电阻"),
    )
    assert updated_storage["name"] == "抽屉A"
    assert updated_storage["notes"] == "电阻"
    database.delete_location(storage["id"])
    database.delete_warehouse(position["id"])
    assert database.list_locations() == []
    assert database.list_warehouses() == []


def test_component_with_stock_cannot_be_deleted(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=1)
    )
    with pytest.raises(DomainError, match="仍有库存"):
        database.delete_component(component["id"])


def test_zero_stock_component_with_history_is_archived(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=1)
    )
    database.issue(IssueRequest(component_id=component["id"], quantity=1))
    assert database.delete_component(component["id"])["mode"] == "archived"
    assert all(item["id"] != component["id"] for item in database.list_components())
    assert database.get_component(component["id"])["archived_at"] is not None
