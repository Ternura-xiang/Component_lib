"""BOM 文件和候选匹配测试。"""

from pathlib import Path

from component_library.bom import parse_bom, preview_bom
from component_library.database import Database
from component_library.models import (
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
