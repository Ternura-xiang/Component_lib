"""本地订单页面解析和原子入库测试。"""

from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import xlrd

from component_library.database import Database, DomainError
from component_library.models import (
    ComponentCreate,
    JlcParameter,
    JlcParameterGroup,
    LocationCreate,
    WarehouseCreate,
)
from component_library.order_import import parse_order_files, parse_saved_orders

HTML = """<!doctype html><html><body><ul><li class="mt-[10px] relative">
<section><section>2026-10-21 14:07:58 <span>订单编号：</span><a>SO26102118592</a></section></section>
<section class="flex justify-between pl-[10px]">
  <section><a href="https://item.szlcsc.com/29373045.html?private=session">
    <ul><li>LED数码管 / LED贴片数码管 编带</li><li>GS2821CR-G / SMD,15.2x13mm</li>
    <li>Guangcai Display(光彩显示)</li><li>编号： C27636670</li></ul></a></section>
  <ul><li>商品单价： ￥3.32</li><li>购买数量： 2个</li><li>商品金额： ￥6.64</li></ul>
</section></li></ul></body></html>"""

RESISTOR_HTML = """<!doctype html><html><body><ul><li class="mt-[10px] relative">
<section><section>2026-10-22 09:30:00 <span>订单编号：</span><a>SO26102200001</a></section></section>
<section class="flex justify-between pl-[10px]">
  <section><a href="https://item.szlcsc.com/123456.html">
    <ul><li>贴片电阻 / 厚膜电阻 91Ω ±1% 62.5mW 编带</li><li>FRC0402F91R0TS / 0402</li>
    <li>FOJAN(富捷电子)</li><li>编号： C12345678</li></ul></a></section>
  <ul><li>商品单价： ￥0.01</li><li>购买数量： 100个</li><li>商品金额： ￥1.00</li></ul>
</section></li></ul></body></html>"""


def saved_order_zip(tmp_path: Path) -> Path:
    """创建不含脚本的最小订单压缩包。"""

    path = tmp_path / "orders.zip"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("我的订单.mhtml", HTML)
        archive.writestr("files/ignored.js", "throw new Error('不得执行')")
    return path


def test_parse_saved_order_without_session_query(tmp_path: Path) -> None:
    lines = parse_saved_orders(saved_order_zip(tmp_path))
    assert len(lines) == 1
    line = lines[0]
    assert line.order_number == "SO26102118592"
    assert line.lcsc_part_number == "C27636670"
    assert line.quantity == 2
    assert line.unit_price == 3.32
    assert line.name == "GS2821CR-G"
    assert line.description == "LED贴片数码管"
    assert line.manufacturer_part_number == "GS2821CR-G"
    assert line.product_url == "https://item.szlcsc.com/29373045.html"


def test_statement_merges_by_order_and_part_and_saves_purchase_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "statement.xls"
    path.touch()
    values = [
        ["订单编号", "商品编号", "订购数量", "单价（人民币含税）", "订单状态", "收货人"],
        ["SO26102118592", "C27636670", 3.0, 1.23456789, "已发货", "收货人字段"],
        ["SO26102118593", "C27636670", "5.0", "", "已发货", "收货人字段"],
        ["SO26102118594", "C27636671", 1.0, 0.1, "已取消", "收货人字段"],
        ["", "", "", 1.0, "", ""],
    ]
    sheet = MagicMock(name="明细")
    sheet.name = "物料明细"
    sheet.nrows = len(values)
    sheet.row_values.side_effect = values.__getitem__
    sheet.cell.side_effect = lambda row, column: xlrd.sheet.Cell(
        xlrd.XL_CELL_TEXT, values[row][column]
    )
    workbook = MagicMock()
    workbook.__enter__.return_value = workbook
    workbook.sheets.return_value = [sheet]
    monkeypatch.setattr(xlrd, "open_workbook", lambda *args, **kwargs: workbook)

    statement = parse_order_files([path])
    assert len(statement) == 2
    assert statement[0].unit_price is None and statement[0].product_url is None
    assert "收货人字段" not in str(statement)

    warnings: list[str] = []
    lines = parse_order_files([saved_order_zip(tmp_path), path], warning=warnings.append)
    assert len(lines) == 2
    matched, unmatched = lines
    assert (matched.quantity, matched.unit_price, matched.purchase_price) == (3, 3.32, 1.23456789)
    assert matched.product_url == "https://item.szlcsc.com/29373045.html"
    assert unmatched.purchased_at is None and unmatched.purchase_price is None
    assert any("数量不一致" in warning for warning in warnings)

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A"))
    assert database.import_order_lines(lines, location["id"])["imported"] == 2
    assert database.import_order_lines(lines, location["id"])["skipped"] == 2
    lots = {row["order_number"]: row for row in database.list_inventory_lots()}
    assert lots[matched.order_number]["unit_price"] == matched.purchase_price
    assert lots[unmatched.order_number]["unit_price"] is None
    component = database.get_component(lots[matched.order_number]["component_id"])
    assert component["price"] == 3.32
    assert [(row["label"], row["value"]) for row in component["category_parameters"]] == [
        ("购入单价", "1.23456789"),
    ]

    supplement_db = Database(tmp_path / "supplement.db")
    existing = supplement_db.create_component(
        ComponentCreate(name="已有元件", lcsc_part_number=matched.lcsc_part_number)
    )
    changes = supplement_db.preview_order_supplement(lines)
    assert len(changes) == 1 and changes[0]["fields"]["purchase_price"] == "1.23456789"
    assert supplement_db.apply_order_supplement(changes) == 1
    assert supplement_db.apply_order_supplement(changes) == 0
    detail = supplement_db.get_component(existing["id"])
    assert detail["category_parameters"][0]["value"] == "1.23456789"
    assert detail["price"] is None


def test_unrendered_order_page_explains_how_to_save_details(tmp_path: Path) -> None:
    path = tmp_path / "orders.html"
    path.write_text(
        "<html><head><title>我的订单-立创商城</title></head>"
        "<body><main><span>元器件订单 (0)</span></main></body></html>",
        encoding="utf-8",
    )

    with pytest.raises(DomainError, match="页面不含订单明细.*等订单和商品明细显示"):
        parse_saved_orders(path)


def test_cancelled_order_is_excluded_but_cancel_button_is_not_status(tmp_path: Path) -> None:
    path = tmp_path / "mixed-orders.html"
    cancelled = HTML.replace("</a></section></section>", "</a></section><span>已取消</span></section>", 1)
    active = (
        HTML.replace("SO26102118592", "SO26102118593")
        .replace("</a></section></section>", "</a></section><button>取消订单</button></section>", 1)
    )
    path.write_text(cancelled + active, encoding="utf-8")

    assert [line.order_number for line in parse_saved_orders(path)] == ["SO26102118593"]

    path.write_text(cancelled, encoding="utf-8")
    with pytest.raises(DomainError, match="已剔除取消或关闭的订单"):
        parse_saved_orders(path)


def test_parse_resistor_category_details(tmp_path: Path) -> None:
    path = tmp_path / "resistor.html"
    path.write_text(RESISTOR_HTML, encoding="utf-8")
    line = parse_saved_orders(path)[0]
    assert line.name == "FRC0402F91R0TS"
    assert line.category == "贴片电阻"
    assert line.resistor is not None
    assert line.resistor.resistance == "91Ω"
    assert line.resistor.tolerance_percent == 1
    assert line.resistor.power_mw == 62.5
    assert line.resistor.technology == "厚膜"


def test_parse_reports_current_date_and_part(tmp_path: Path) -> None:
    """离线解析应报告非敏感进度。"""

    second = (
        HTML.replace("SO26102118592", "SO26102118593")
        .replace("C27636670", "C27636671")
        .replace("GS2821CR-G", "GS2821CR-H")
    )
    path = tmp_path / "two-orders.html"
    path.write_text(HTML + second, encoding="utf-8")
    progress: list[str] = []
    lines = parse_saved_orders(path, progress.append)
    assert len(lines) == 2
    assert "2026-10-21" in progress[0]
    assert "GS2821CR-G" in progress[0]


def test_collapsed_view_all_section_still_imports_hidden_products(tmp_path: Path) -> None:
    """保存页面中的折叠只影响 CSS 展示，解析器必须读取其隐藏商品节点。"""

    hidden_product = RESISTOR_HTML.split("<section class=\"flex justify-between pl-[10px]\">")[
        1
    ].split("</section></li>", 1)[0]
    page = HTML.replace(
        "</section></li></ul>",
        "<button>查看全部商品</button>"
        '<section style="display:none" class="flex justify-between pl-[10px]">'
        + hidden_product
        + "</section></section></li></ul>",
    )
    # 隐藏商品属于同一订单，需换成立创编号以避免正常去重。
    page = page.replace("C12345678", "C12345679")
    path = tmp_path / "collapsed.html"
    path.write_text(page, encoding="utf-8")
    warnings: list[str] = []
    assert {line.lcsc_part_number for line in parse_saved_orders(path, warning=warnings.append)} == {
        "C27636670",
        "C12345679",
    }
    assert "未展开订单" in warnings[0]


def test_order_import_fixed_batch_limit_requires_reupload(tmp_path: Path) -> None:
    """业务层固定每批一千条，避免 UI 或 API 绕过批次限制。"""

    database = Database(tmp_path / "inventory.db")
    source = parse_saved_orders(saved_order_zip(tmp_path))[0]
    lines = [
        source.model_copy(
            update={
                "order_number": f"SO261110{index:05d}",
                "lcsc_part_number": f"C{index + 10000}",
            }
        )
        for index in range(1_001)
    ]
    with pytest.raises(DomainError, match="单次最多导入 1000 条"):
        database.import_order_lines(lines)


def test_order_import_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    lines = parse_saved_orders(saved_order_zip(tmp_path))
    first = database.import_order_lines(lines, location["id"])
    second = database.import_order_lines(lines, location["id"])
    assert first == {
        "transaction_group_id": first["transaction_group_id"],
        "imported": 1,
        "skipped": 0,
        "created_components": 1,
    }
    assert second["imported"] == 0
    assert second["skipped"] == 1
    assert database.list_stock()[0]["quantity"] == 2


def test_enriched_order_persists_match_state_datasheet_and_parameters(tmp_path: Path) -> None:
    """订单自动补全结果应随原子入库保存，不能只停留在预览界面。"""

    database = Database(tmp_path / "inventory.db")
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    source = parse_saved_orders(saved_order_zip(tmp_path))[0]
    enriched = source.model_copy(
        update={
            "datasheet_url": "https://example.com/datasheet.pdf",
            "jlc_matched": True,
            "parameter_groups": [
                JlcParameterGroup(
                    name="基础参数",
                    parameters=[JlcParameter(key="color", label="发光颜色", value="红色")],
                )
            ],
        }
    )
    database.import_order_lines([enriched], location["id"])
    detail = database.list_components(enriched.lcsc_part_number)[0]
    assert detail["jlc_matched"] == 1
    assert detail["datasheet_url"] == "https://example.com/datasheet.pdf"
    assert database.get_component(detail["id"])["category_parameters"][0]["value"] == "红色"


def test_name_matching_and_category_priority_recommendation(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    generic = database.create_warehouse(WarehouseCreate(name="通用柜"))
    resistor = database.create_warehouse(WarehouseCreate(name="电阻1/RLC"))
    generic_location = database.create_location(
        LocationCreate(warehouse_id=generic["id"], name="A01")
    )
    resistor_location = database.create_location(
        LocationCreate(warehouse_id=resistor["id"], name="R01")
    )
    database.save_storage_priorities(None, {generic_location["id"]: 1, resistor_location["id"]: 10})
    automatic = database.recommend_location("贴片电阻")
    assert automatic is not None
    assert automatic["location_id"] == resistor_location["id"]
    assert automatic["reason"] == "位置/仓库名称匹配"
    generic_fallback = database.recommend_location("LED数码管")
    assert generic_fallback is not None
    assert generic_fallback["location_id"] == generic_location["id"]
    assert generic_fallback["reason"] == "整体优先级"

    database.save_storage_priorities("贴片电阻", {generic_location["id"]: 1})
    configured = database.recommend_location("贴片电阻")
    assert configured is not None
    assert configured["location_id"] == generic_location["id"]
    assert "人工优先级" in configured["reason"]


def test_each_order_line_can_use_an_independent_location(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location_a = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    location_b = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B01"))
    led = parse_saved_orders(saved_order_zip(tmp_path))[0]
    resistor_path = tmp_path / "resistor.html"
    resistor_path.write_text(RESISTOR_HTML, encoding="utf-8")
    resistor = parse_saved_orders(resistor_path)[0]
    overrides = {
        f"{led.order_number}:{led.lcsc_part_number}": location_a["id"],
        f"{resistor.order_number}:{resistor.lcsc_part_number}": location_b["id"],
    }
    result = database.import_order_lines([led, resistor], location_overrides=overrides)
    assert result["imported"] == 2
    stock = {item["location_name"]: item["quantity"] for item in database.list_stock()}
    assert stock == {"A01": 2, "B01": 100}
    component = next(
        item for item in database.list_components() if item["name"] == "FRC0402F91R0TS"
    )
    assert component["order_numbers"] == resistor.order_number
    assert database.list_components(resistor.order_number)[0]["id"] == component["id"]
    detail = database.get_component(component["id"])
    assert detail["resistor_details"]["resistance"] == "91Ω"
    assert detail["resistor_details"]["source_description"].startswith("厚膜电阻")
