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
    OrderImportLine,
    ReceiveRequest,
    TransactionDetails,
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


def test_jlc_match_state_is_persistent_and_manual_edit_preserves_it(tmp_path: Path) -> None:
    """详情页确认状态应持久化，后续普通编辑不得误清除。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(
        ComponentCreate(name="已补全元件", lcsc_part_number="C12345", jlc_matched=True)
    )
    assert database.get_component(component["id"])["jlc_matched"] == 1
    database.update_component(
        component["id"], ComponentCreate(name="手工改名", lcsc_part_number="C12345")
    )
    assert database.get_component(component["id"])["jlc_matched"] == 1


def test_component_save_and_legacy_normalization(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    product_url = "https://item.szlcsc.com/6097543.html?fromZone=search"
    component = database.create_component(
        ComponentCreate(
            name="  蓝牙模块  ",
            manufacturer_part_number=" E83-2G4M03S ",
            product_url=product_url,
            aliases=[" \t模块 A ", " "],
        )
    )
    component_id = component["id"]
    assert component["name"] == "蓝牙模块"
    assert component["aliases"] == ["模块 A"]
    assert component["datasheet_url"] == (
        "https://item.szlcsc.com/datasheet/E83-2G4M03S/6097543.html"
    )

    pdf = "https://atta.szlcsc.com/upload/public/pdf/source/example.PDF"
    updated = database.update_component(
        component_id, ComponentCreate(name=" 模块 ", datasheet_url=f" {pdf}?download=1#page=2 ")
    )
    assert updated["datasheet_url"] == pdf
    with database.write() as connection:
        connection.execute(
            "UPDATE components SET notes=?,aliases=?,datasheet_url=? WHERE id=?",
            ("\n 首行\n  第二行 \t", '[" 模块 A ", " "]', f"{pdf}?download=1", component_id),
        )
        connection.execute(
            """INSERT INTO component_category_parameters
            (component_id,parameter_key,label,value,sort_order,group_name) VALUES(?,?,?,?,?,?)""",
            (component_id, "voltage", " 电压 ", " 3.3 V ", 0, " 电气特性 "),
        )
    assert database.normalize_component_data() == 1
    detail = database.get_component(component_id)
    assert detail["datasheet_url"] == pdf
    assert detail["notes"] == "首行\n  第二行"
    assert detail["aliases"] == ["模块 A"]
    assert detail["category_parameters"] == [
        {"parameter_key": "voltage", "label": "电压", "value": "3.3 V", "group_name": "电气特性"}
    ]
    assert database.normalize_component_data() == 0


def test_normalization_fills_empty_datasheets_and_keeps_catalog_variants(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    item = database.create_component(ComponentCreate(
        name="商城电源", product_url="https://item.szlcsc.com/mro/5748338.html",
        manufacturer_part_number="LRS-75-36", jlc_matched=True,
    ))
    database.update_component(item["id"], ComponentCreate(
        name="用户电源", product_url="https://item.szlcsc.com/mro/5748338.html",
        manufacturer_part_number="LRS-75-36",
    ))
    with database.write() as connection:
        connection.execute("UPDATE components SET datasheet_url=' ' WHERE id=?", (item["id"],))
        for catalog in ("marketplace", "user"):
            connection.execute(
                f"""UPDATE {catalog}.catalog_components SET
                payload=json_set(payload,'$.component.datasheet_url',NULL) WHERE id=?""", (item["id"],)
            )
    assert database.normalize_component_data() == 1
    expected = "https://item.szlcsc.com/datasheet/LRS-75-36/5748338.html"
    assert database.get_component(item["id"])["datasheet_url"] == expected
    assert database.get_catalog_component("marketplace", item["id"])["name"] == "商城电源"
    assert database.get_catalog_component("user", item["id"])["name"] == "用户电源"
    for catalog in ("marketplace", "user"):
        assert database.get_catalog_component(catalog, item["id"])["datasheet_url"] == expected
    assert database.normalize_component_data() == 0


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


def test_bulk_transfer_moves_exact_lots_atomically(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, warehouse, source = inventory
    target = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A02"))
    for index in (1, 2):
        database.receive(
            ReceiveRequest(
                component_id=component["id"], location_id=source["id"], quantity=index,
                purchase_date=f"2026-10-0{index}", unit_price=index / 10,
                order_number=f"SO2610010000{index}",
            )
        )
    lots = database.list_inventory_lots()
    quantities = {lot["id"]: lot["quantity"] for lot in lots}
    stale = dict(quantities)
    stale[lots[-1]["id"]] += 1

    with pytest.raises(DomainError, match="库存已变动"):
        database.transfer_lots(stale, target["id"])
    assert database.list_inventory_lots() == lots

    result = database.transfer_lots(quantities, target["id"])
    moved = database.list_inventory_lots()
    assert {lot["location_id"] for lot in moved} == {target["id"]}
    preserved = ("quantity", "purchase_date", "received_at", "order_number", "unit_price")
    assert sorted(tuple(lot[key] for key in preserved) for lot in moved) == sorted(
        tuple(lot[key] for key in preserved) for lot in lots
    )
    assert result["moved_lots"] == 2
    transactions = [
        row for row in database.list_transactions()
        if row["group_id"] == result["transaction_group_id"]
    ]
    assert sorted(row["quantity_delta"] for row in transactions) == [-2, -1, 1, 2]


def test_warehouse_colors_backfill_existing_database_and_persist(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, _, warehouse, first = inventory
    second = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A02"))
    with database.write() as connection:
        connection.execute("ALTER TABLE locations DROP COLUMN color")

    database = Database(database.path)
    colors = {item["id"]: item["color"] for item in database.list_locations()}
    assert len(set(colors.values())) == 2
    assert all(color.startswith("#") and len(color) == 7 for color in colors.values())
    database.set_location_color(first["id"], "#ABC123")
    colors[first["id"]] = "#abc123"
    assert {
        item["id"]: item["color"] for item in Database(database.path).list_locations()
    } == colors
    assert second["id"] in colors


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


def test_edit_and_delete_inventory_transactions(inventory: tuple[Database, dict, dict, dict]) -> None:
    database, component, _, location = inventory
    component_id = component["id"]
    location_id = location["id"]
    received = database.receive(
        ReceiveRequest(component_id=component_id, location_id=location_id, quantity=10)
    )
    receive_row = database.list_transactions()[0]
    issued = database.issue(IssueRequest(component_id=component_id, location_id=location_id, quantity=4))
    issue_row = database.list_transactions()[0]

    with pytest.raises(DomainError, match="库存不足"):
        database.update_transaction(receive_row["id"], 10, 3, "数量错误")
    with pytest.raises(DomainError, match="已有后续流水"):
        database.delete_transaction_group(received["transaction_group_id"], {receive_row["id"]: 10})
    assert total(database, component_id) == 6

    database.update_transaction(receive_row["id"], 10, 9, " 更正入库 ")
    database.update_transaction(issue_row["id"], -4, 3, "更正出库")
    assert total(database, component_id) == 6
    assert [row["notes"] for row in database.list_transactions()] == ["更正出库", "更正入库"]
    with pytest.raises(DomainError, match="已变动"):
        database.delete_transaction_group(issued["transaction_group_id"], {issue_row["id"]: -4})
    database.delete_transaction_group(issued["transaction_group_id"], {issue_row["id"]: -3})
    assert total(database, component_id) == 9
    database.delete_transaction_group(received["transaction_group_id"], {receive_row["id"]: 9})
    assert database.list_transactions() == []
    assert total(database, component_id) == 0


def test_edit_receipt_moves_stock_binds_order_and_rolls_back_conflicts(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, warehouse, location = inventory
    new_location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B01"))
    component_id = component["id"]
    database.receive(ReceiveRequest(component_id=component_id, location_id=location["id"], quantity=10))
    receipt = database.list_transactions()[0]
    details = TransactionDetails(
        location_id=new_location["id"], source="采购", unit_price=2.5, purchase_date="2026-11-14",
        order_number="{on:SO26111400001,pc:C12345}",
    )
    database.update_transaction(
        receipt["id"], 10, 8, "自定义备注", details=details,
        expected_location_id=location["id"],
        component=ComponentCreate(name="新名称", lcsc_part_number="C12345"),
    )
    row = database.list_transactions()[0]
    assert row["location_id"] == new_location["id"]
    assert (row["quantity_delta"], row["source"], row["notes"]) == (8, "采购", "自定义备注")
    assert (row["unit_price"], row["purchase_date"]) == (2.5, "2026-11-14")
    assert database.resolve_batch_code("{on:SO26111400001,pc:C12345}")["remaining_quantity"] == 8

    database.issue(IssueRequest(component_id=component_id, location_id=new_location["id"], quantity=7))
    with pytest.raises(DomainError, match="已使用"):
        database.update_transaction(
            receipt["id"], 8, 6, "不应保存", details=details,
            expected_order_number=row["order_number"],
            component=ComponentCreate(name="不应保存", lcsc_part_number="C12346"),
        )
    assert database.get_component(component_id)["name"] == "新名称"
    assert database.resolve_batch_code("{on:SO26111400001,pc:C12345}")["remaining_quantity"] == 1
    with pytest.raises(DomainError, match="后续"):
        database.update_transaction(
            receipt["id"], 8, 8, "不应保存", expected_order_number=row["order_number"],
            details=details.model_copy(update={"location_id": location["id"]}),
        )


def test_transfer_edit_updates_both_ends_and_keeps_material_balance(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, warehouse, origin = inventory
    first_location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B01"))
    next_location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="C01"))
    component_id = component["id"]
    database.receive(ReceiveRequest(component_id=component_id, location_id=origin["id"], quantity=10))
    database.transfer(TransferRequest(
        component_id=component_id, from_location_id=origin["id"],
        to_location_id=first_location["id"], quantity=5,
    ))
    entry = next(row for row in database.list_transactions() if row["kind"] == "TRANSFER_IN")
    database.update_transaction(
        entry["id"], 5, 6, "", details=TransactionDetails(location_id=next_location["id"])
    )
    assert {item["location_id"]: item["quantity"] for item in database.list_stock()} == {
        origin["id"]: 4, next_location["id"]: 6,
    }
    assert [row["quantity_delta"] for row in database.list_transactions()][:2] == [6, -6]


def test_edit_issue_moves_deduction_across_available_lots(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, warehouse, origin = inventory
    target = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B01"))
    component_id = component["id"]
    for location, quantity in ((origin, 10), (target, 2), (target, 3)):
        database.receive(ReceiveRequest(component_id=component_id, location_id=location["id"], quantity=quantity))
    database.issue(IssueRequest(component_id=component_id, location_id=origin["id"], quantity=4))
    issue = database.list_transactions()[0]
    database.update_transaction(
        issue["id"], -4, 4, "修订后的备注",
        details=TransactionDetails(location_id=target["id"], source="测试出库"),
    )
    assert {row["location_id"]: row["quantity"] for row in database.list_stock()} == {
        origin["id"]: 10, target["id"]: 1,
    }
    issues = [row for row in database.list_transactions() if row["kind"] == "ISSUE"]
    assert len(issues) == 2 and sum(row["quantity_delta"] for row in issues) == -4
    assert {row["notes"] for row in issues} == {"修订后的备注"}
    assert {row["group_id"] for row in issues} == {issue["group_id"]}


def test_delete_transfer_restores_source_batch(inventory: tuple[Database, dict, dict, dict]) -> None:
    database, component, warehouse, location = inventory
    target = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B01"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=8)
    )
    transfer = database.transfer(TransferRequest(
        component_id=component["id"], from_location_id=location["id"],
        to_location_id=target["id"], quantity=5,
    ))
    group_id = transfer["transaction_group_id"]
    rows = {row["id"]: row["quantity_delta"] for row in database.list_transactions()
            if row["group_id"] == group_id}
    database.delete_transaction_group(group_id, rows)
    assert [(lot["location_id"], lot["quantity"]) for lot in database.list_inventory_lots()] == [
        (location["id"], 8)
    ]
    assert [row["kind"] for row in database.list_transactions()] == ["RECEIVE"]


def test_regroup_preserves_original_transaction_groups(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    request = ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=2)
    operations = [database.receive(request)["transaction_group_id"] for _ in range(5)][::-1]
    rows = database.list_transactions()
    assert [row["group_id"] for row in rows] == operations
    assert len({row["display_group_id"] for row in rows}) == 1
    database.regroup_transactions([operations], [operations[:2], operations[2:3], operations[3:]])
    rows = database.list_transactions()
    assert [row["group_id"] for row in rows] == operations
    assert [row["display_group_id"] for row in rows] == [
        operations[0], operations[0], operations[2], operations[3], operations[3],
    ]
    assert total(database, component["id"]) == 10
    database.regroup_transactions([operations[:2], operations[2:3]], [operations[:3]])
    assert len({row["display_group_id"] for row in database.list_transactions()}) == 2


def test_transaction_order_change_updates_qr_and_order_import_keys(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    part = component["lcsc_part_number"]
    old_order, new_order = "SO26111400001", "SO26111400002"
    product = OrderImportLine(
        name=component["name"], lcsc_part_number=part, order_number=old_order,
        quantity=2, unit_price=0, purchase_price=0,
    )
    database.import_order_lines([product], location["id"])
    row = database.list_transactions()[0]
    assert row["notes"] is None and row["order_number"] == old_order
    assert database.list_inventory_lots()[0]["unit_price"] is None
    database.update_transaction(
        row["id"], 2, 2, "手工备注", order_number=new_order, expected_order_number=old_order
    )
    batch = database.resolve_batch_code(f"{{on:{new_order},pc:{part}}}")
    assert batch["remaining_quantity"] == 2
    assert database.imported_order_keys() == {(new_order, part)}
    assert database.list_transactions()[0]["notes"] == "手工备注"
    with pytest.raises(DomainError):
        database.resolve_batch_code(f"{{on:{old_order},pc:{part}}}")
    database.receive(ReceiveRequest(
        component_id=component["id"], location_id=location["id"], quantity=1,
        order_number=f"{{on:{new_order},pc:{part}}}",
    ))
    assert database.get_batch(batch["id"])["remaining_quantity"] == 3


def test_separate_component_catalogs_import_export_and_restore(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="原元件", lcsc_part_number="C12345"))
    component_id = component["id"]
    assert [item["name"] for item in database.list_catalog("user")] == ["原元件"]
    database.update_component(component_id, ComponentCreate(
        name="商城名称", category="电源", lcsc_part_number="C12345", jlc_matched=True
    ))
    assert database.list_catalog("user") == []
    database.update_component(component_id, ComponentCreate(
        name="自定义名称", category="电源", lcsc_part_number="C12345"
    ))
    assert [item["name"] for item in database.list_catalog("marketplace")] == ["商城名称"]
    assert database.get_component(component_id)["name"] == "自定义名称"
    destination = Database(tmp_path / "another.db")
    for catalog in ("user", "marketplace"):
        export = tmp_path / f"{catalog}.db"
        assert database.export_catalog(catalog, export) == 1
        assert destination.import_catalog(catalog, export) == 1
    assert destination.get_component(component_id)["name"] == "自定义名称"
    backup = Path(database.backup()["path"])
    other = Database(tmp_path / "restored.db")
    other.prepare_restore(backup)
    assert Database.apply_pending_restore(other.path)
    restored = Database(other.path)
    assert restored.list_catalog("user") == database.list_catalog("user")
    assert restored.list_catalog("marketplace") == database.list_catalog("marketplace")


def test_backup_is_valid(inventory: tuple[Database, dict, dict, dict], tmp_path: Path) -> None:
    database, _, _, _ = inventory
    result = database.backup(tmp_path / "exports")
    assert Path(result["path"]).is_file()
    assert len(result["sha256"]) == 64


def test_automatic_backup_retention_and_reset_recovery(
    inventory: tuple[Database, dict, dict, dict],
) -> None:
    database, component, _, location = inventory
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=3)
    )
    first = Path(database.automatic_backup(1)["path"])
    manual = Path(database.backup(database.automatic_backup_directory)["path"])
    second = Path(database.automatic_backup(1)["path"])
    assert not first.exists() and not first.with_suffix(".db.sha256").exists()
    assert database.automatic_backups() == [second]
    assert manual.exists()

    database.prepare_reset()
    assert total(database, component["id"]) == 3
    assert Database.apply_pending_restore(database.path)
    restored = Database(database.path)
    assert restored.list_components() == []
    assert restored.list_transactions() == []
    previous = Database(database.path.with_suffix(".before-restore.db"))
    assert total(previous, component["id"]) == 3

    checksum = second.with_suffix(".db.sha256")
    contents = checksum.read_bytes()
    checksum.write_text("bad", encoding="ascii")
    with pytest.raises(DomainError, match="SHA-256"):
        restored.prepare_restore(second)
    checksum.write_bytes(contents)
    restored.prepare_restore(second)
    assert Database.apply_pending_restore(restored.path)
    assert total(Database(restored.path), component["id"]) == 3


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


def test_inventory_statistics_tracks_value_and_unpriced_stock(tmp_path: Path) -> None:
    """统计价值应使用批次/元件价格，并单独报告没有价格的库存。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    first_location = database.create_location(
        LocationCreate(warehouse_id=position["id"], name="A01")
    )
    second_location = database.create_location(
        LocationCreate(warehouse_id=position["id"], name="B01")
    )
    priced = database.create_component(ComponentCreate(name="已定价", category="电阻", price=2))
    unpriced = database.create_component(ComponentCreate(name="未定价", category="电容"))
    database.receive(
        ReceiveRequest(component_id=priced["id"], location_id=first_location["id"], quantity=3)
    )
    database.receive(
        ReceiveRequest(component_id=unpriced["id"], location_id=second_location["id"], quantity=2)
    )

    statistics = database.inventory_statistics()
    assert statistics["component_types"] == 2
    assert statistics["total_quantity"] == 5
    assert statistics["priced_quantity"] == 3
    assert statistics["unpriced_quantity"] == 2
    assert statistics["total_value"] == 6
    assert {item["name"] for item in statistics["by_location"]} == {
        "主柜 / A01",
        "主柜 / B01",
    }
    categories = {item["name"]: item for item in statistics["by_category"]}
    assert categories["电阻"]["value"] == 6
    assert categories["电容"]["unpriced_quantity"] == 2
