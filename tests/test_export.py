"""库存 Excel/CSV 导出回归测试。"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

from component_library.database import Database
from component_library.export import export_inventory_csv, export_inventory_xlsx
from component_library.models import ComponentCreate, LocationCreate, ReceiveRequest, WarehouseCreate


def populated_database(tmp_path: Path) -> tuple[Database, dict[str, object]]:
    """创建包含已定价库存的最小导出样本。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(
        LocationCreate(warehouse_id=position["id"], name="A01")
    )
    component = database.create_component(
        ComponentCreate(name="10k 电阻", category="电阻", price=0.1)
    )
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=10)
    )
    return database, component


def test_export_inventory_csv_uses_utf8_and_filter(tmp_path: Path) -> None:
    database, component = populated_database(tmp_path)
    ignored = database.create_component(ComponentCreate(name="不应导出"))
    target = tmp_path / "inventory.csv"

    export_inventory_csv(database, target, [str(component["id"])])

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["元件"] == "10k 电阻"
    assert rows[0]["库存数量"] == "10"
    assert str(ignored["id"]) not in target.read_text(encoding="utf-8-sig")


def test_export_inventory_xlsx_contains_summary_and_detail(tmp_path: Path) -> None:
    database, component = populated_database(tmp_path)
    target = tmp_path / "inventory.xlsx"

    export_inventory_xlsx(database, target, [str(component["id"])])

    workbook = load_workbook(target, data_only=True)
    assert workbook.sheetnames == ["概览", "库存明细", "分库统计", "分类统计"]
    assert workbook["概览"]["B2"].value == 1
    assert workbook["概览"]["B3"].value == 10
    assert workbook["概览"]["B6"].value == 1
    assert workbook["库存明细"]["A2"].value == "10k 电阻"
    assert workbook["库存明细"]["J2"].value == 10
    assert workbook["库存明细"].freeze_panes == "A2"
