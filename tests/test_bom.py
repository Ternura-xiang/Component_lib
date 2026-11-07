"""BOM 文件和候选匹配测试。"""

from pathlib import Path

from component_library.bom import normalize_specification, parse_bom, preview_bom
from component_library.database import Database
from component_library.models import (
    BomCommitLine,
    BomCommitRequest,
    ComponentCreate,
    LocationCreate,
    ReceiveRequest,
    WarehouseCreate,
)


def test_csv_and_unique_match(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(
        ComponentCreate(name="10k 电阻", lcsc_part_number="C25804")
    )
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=50)
    )
    path = tmp_path / "bom.csv"
    path.write_text("立创编号,数量\nC25804,12\n", encoding="utf-8")
    rows = parse_bom(path)
    preview = preview_bom(database, rows)
    assert preview[0]["selected_component_id"] == component["id"]
    assert preview[0]["shortage"] == 0


def test_lceda_utf16_tab_csv_and_designator_quantity(tmp_path: Path) -> None:
    path = tmp_path / "lceda.csv"
    path.write_text(
        "No.\tQuantity\tComment\tDesignator\tFootprint\tManufacturer Part\tSupplier Part\n"
        "1\t2\t10kΩ\tR1,R3\tR0603\t0603WAF1002T5E\tC25804\n",
        encoding="utf-16",
    )
    rows = parse_bom(path)
    assert len(rows) == 1
    assert rows[0].quantity == 2
    assert rows[0].footprint == "R0603"
    assert rows[0].lcsc_part_number == "C25804"


def test_invalid_bom_row_is_reported_instead_of_skipped(tmp_path: Path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text("Comment,Quantity\n10k,abc\n", encoding="utf-8")
    rows = parse_bom(path)
    assert len(rows) == 1
    assert rows[0].quantity == 0
    assert rows[0].errors == ["数量缺失或不是正整数"]


def test_custom_header_mapping(tmp_path: Path) -> None:
    path = tmp_path / "custom.csv"
    path.write_text("我的型号,装机数\nC25804,3\n", encoding="utf-8")
    rows = parse_bom(
        path,
        field_mapping={"我的型号": "lcsc_part_number", "装机数": "quantity"},
    )
    assert rows[0].lcsc_part_number == "C25804"
    assert rows[0].quantity == 3
    assert rows[0].errors == []


def test_semantic_units_and_cumulative_shortage(tmp_path: Path) -> None:
    assert normalize_specification("10K") == normalize_specification("10000R")
    assert normalize_specification("0.1uF") == normalize_specification("100nF")
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(
        ComponentCreate(name="10k 电阻", package="0603", specification="10K")
    )
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=10)
    )
    path = tmp_path / "bom.csv"
    path.write_text(
        "Comment,Quantity,Footprint\n10k 电阻,6,R0603\n10k 电阻,6,R0603\n",
        encoding="utf-8",
    )
    preview = preview_bom(database, parse_bom(path))
    assert all(item["selected_component_id"] == component["id"] for item in preview)
    assert [item["shortage"] for item in preview] == [2, 2]


def test_confirmed_mapping_is_reused_and_audited(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="库存中的名称"))
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=5)
    )
    path = tmp_path / "bom.csv"
    path.write_text("Comment,Quantity\n完全不同的名称,1\n", encoding="utf-8")
    row = parse_bom(path)[0]
    first = preview_bom(database, [row])[0]
    assert first["selected_component_id"] is None
    database.commit_bom(
        BomCommitRequest(
            name="学习映射",
            lines=[
                BomCommitLine(
                    row_number=row.row_number,
                    component_id=component["id"],
                    quantity=1,
                    raw_input=row.model_dump(),
                    mapping_key=first["mapping_key"],
                    match_reason="人工选择",
                    remember_mapping=True,
                )
            ],
        )
    )
    second = preview_bom(database, [row])[0]
    assert second["selected_component_id"] == component["id"]
    assert second["reason"] == "已保存的人工映射"
    with database.read() as connection:
        raw = connection.execute("SELECT raw_json FROM bom_lines").fetchone()[0]
    assert "完全不同的名称" in raw
    assert "人工选择" in raw
