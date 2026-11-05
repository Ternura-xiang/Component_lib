"""PySide6 主窗口无显示设备烟雾测试。"""

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QDialog, QLineEdit
from test_jlc_product import PRODUCT_HTML

from component_library.database import Database
from component_library.jlc_product import parse_jlc_product_page
from component_library.models import (
    ComponentCreate,
    JlcParameter,
    JlcParameterGroup,
    LocationCreate,
    OrderImportLine,
    ReceiveRequest,
    WarehouseCreate,
)
from component_library.recognition import RecognitionEngine
from component_library.ui.jlc_browser import JlcMergeDialog
from component_library.ui.main_window import InlineEditor, MainWindow


def test_main_window_builds(qtbot: Any, tmp_path: Path) -> None:
    """验证所有页面在空数据库上可构造，不启动真实摄像头。"""

    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)
    assert window.windowTitle() == "元件库"
    assert "outline: none" in window.styleSheet()
    assert "QCheckBox::indicator" in window.styleSheet()
    assert "QGroupBox::indicator" in window.styleSheet()
    assert "border: 2px solid #111111" in window.styleSheet()
    assert "checkmark.svg" in window.styleSheet()
    assert window.stack.count() == 10
    assert [window.navigation.item(index).text() for index in range(window.navigation.count())] == [
        "元件库存",
        "入库",
        "出库",
        "位置与仓库",
        "扫描入库",
        "库存流水",
        "OCR 样本",
        "设置与备份",
    ]
    window._show_main_page(1)
    window.receive_page.open_order_import.emit()
    assert window.stack.currentWidget() is window.order_page
    assert window.navigation.currentRow() == 1
    window.order_page.back_requested.emit()
    assert window.stack.currentWidget() is window.receive_page
    window._show_main_page(2)
    window.issue_page.open_bom.emit()
    assert window.stack.currentWidget() is window.bom_page
    assert window.navigation.currentRow() == 2
    window.bom_page.back_requested.emit()
    assert window.stack.currentWidget() is window.issue_page


def test_issue_search_generates_stock_locations(qtbot: Any, tmp_path: Path) -> None:
    """手工出库搜索元件后只生成该元件实际有库存的库位。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="测试二极管", package="SOD-323"))
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B04"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=20)
    )
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue_page = window.issue_page
    assert issue_page.component.isHidden()
    assert issue_page.component_label.isHidden()
    issue_page.search.setText("SOD-323")
    assert not issue_page.component.isHidden()
    assert not issue_page.component_label.isHidden()
    assert issue_page.component.currentData() == component["id"]
    assert issue_page.location.currentText() == ""
    assert issue_page.location.itemText(1) == "主柜 / B04（20）"


def test_receive_name_matching_and_category_group(qtbot: Any, tmp_path: Path) -> None:
    """名称候选选择后应回填资料；无匹配时自动切换为新建。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(
        ComponentCreate(
            name="测试二极管",
            brand="品牌A",
            package="SOD-323",
            category="二极管",
            purchase_date="2026-11-01",
        )
    )
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="B04"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=5)
    )
    database.save_component_parameters(component["id"], {"forward_voltage": ("正向压降", "1.2V")})
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    receive = window.receive_page
    assert not hasattr(receive, "save_changes")
    receive._name_edited("测试")
    receive.name.setEditText("测试")
    assert receive.name.count() == 1
    assert "主柜/B04(5)" in receive.name.itemText(0)
    assert receive.position.currentData() == position["id"]
    assert receive.location.currentData() == location["id"]
    receive.name.setCurrentIndex(0)
    receive.name.activated.emit(0)
    assert receive.brand.text() == "品牌A"
    assert receive.package.text() == "SOD-323"
    assert receive.purchase_date.text() == ""
    assert receive.parameter_fields["forward_voltage"][1].text() == "1.2V"

    receive._name_edited("全新元件")
    receive.name.setEditText("全新元件")
    assert receive.name.count() == 0
    assert receive._creating_new is True
    assert receive.brand.text() == ""
    receive._category_edited("电阻")
    assert receive.category.count() == 1
    receive.category.setCurrentIndex(0)
    receive.category.activated.emit(0)
    assert not receive.parameter_group.isHidden()
    assert "resistance" in receive.parameter_fields
    assert receive.scroll_area.widgetResizable()
    receive.parameter_group.setChecked(False)
    assert receive.parameter_content.isHidden()
    receive.parameter_group.setChecked(True)
    assert not receive.parameter_content.isHidden()


def test_same_category_reuses_parameter_definition_union(qtbot: Any, tmp_path: Path) -> None:
    """同类别元件应显示公共参数列表，当前元件没有的字段保持为空。"""

    database = Database(tmp_path / "inventory.db")
    first = database.create_component(ComponentCreate(name="MCU-A", category="单片机"))
    second = database.create_component(ComponentCreate(name="MCU-B", category="单片机"))
    database.save_component_parameter_groups(
        first["id"],
        [
            JlcParameterGroup(
                name="商品参数", parameters=[JlcParameter(key="core", label="CPU内核", value="M7")]
            )
        ],
    )
    database.save_component_parameter_groups(
        second["id"],
        [
            JlcParameterGroup(
                name="商品参数",
                parameters=[JlcParameter(key="frequency", label="主频", value="528MHz")],
            )
        ],
    )
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    window.receive_page.category.setCurrentText("单片机")
    window.receive_page._rebuild_category_fields()
    assert list(window.receive_page.parameter_fields) == ["core", "frequency"]
    assert "单片机" in window.receive_page._available_categories()


def test_receive_existing_component_saves_form_edits_without_extra_checkbox(
    qtbot: Any, tmp_path: Path
) -> None:
    """已有元件确认入库时直接保存表单修改，不再依赖额外开关。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="已有元件", brand="旧品牌"))
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    receive = window.receive_page
    receive.show_info = lambda _message: None
    receive._apply_existing_component(database.get_component(component["id"]))
    receive.brand.setText("新品牌")
    receive.quantity.setValue(3)
    receive.submit()

    assert database.get_component(component["id"])["brand"] == "新品牌"
    stock = database.list_stock()
    assert len(stock) == 1
    assert stock[0]["location_id"] == location["id"]
    assert stock[0]["quantity"] == 3


def test_order_import_cancel_clears_preview(qtbot: Any, tmp_path: Path) -> None:
    """取消订单导入应清空草稿并使当前预览失效。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="通用位置"))
    database.create_location(LocationCreate(warehouse_id=position["id"], name="仓库A"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    page = window.order_page
    page._show_preview(
        [
            OrderImportLine(
                order_number="SO26110400001",
                purchased_at="2026-11-04 12:00:00",
                lcsc_part_number="C123456",
                name="TEST-1",
                description="测试商品",
                quantity=1,
                unit_price=0.1,
            )
        ]
    )
    assert page.table.rowCount() == 1
    page.cancel_import()
    assert page.table.rowCount() == 0
    assert page.lines == []


def test_inline_cell_editing_saves_on_finish(qtbot: Any, tmp_path: Path) -> None:
    """库存、订单和仓库双击后应使用单元格输入框并在失焦信号时保存。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="旧名称"))
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="旧仓库"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)

    inventory = window.inventory_page
    inventory.edit_component(0, 0)
    component_editor = inventory.table.cellWidget(0, 0)
    assert isinstance(component_editor, QLineEdit)
    component_editor.setText("新名称")
    component_editor.editingFinished.emit()
    assert database.get_component(component["id"])["name"] == "新名称"
    assert inventory.table.cellWidget(0, 0) is None

    locations = window.locations_page
    locations.edit_item(0, 1)
    storage_editor = locations.table.cellWidget(0, 1)
    assert isinstance(storage_editor, QLineEdit)
    storage_editor.setText("新仓库")
    storage_editor.editingFinished.emit()
    assert (
        next(item for item in database.list_locations() if item["id"] == location["id"])["name"]
        == "新仓库"
    )
    assert locations.table.cellWidget(0, 1) is None

    order = window.order_page
    order._show_preview(
        [
            OrderImportLine(
                order_number="SO26110400002",
                purchased_at="2026-11-04 12:00:00",
                lcsc_part_number="C654321",
                name="OLD-PART",
                quantity=1,
                unit_price=0.2,
            )
        ]
    )
    order.edit_line(0, 2)
    order_editor = order.table.cellWidget(0, 2)
    assert isinstance(order_editor, QLineEdit)
    order_editor.setText("NEW-PART")
    order_editor.editingFinished.emit()
    assert order.lines[0].name == "NEW-PART"
    assert order.table.cellWidget(0, 2) is None


def test_inline_cell_editing_cancel_restores_original(qtbot: Any, tmp_path: Path) -> None:
    """原位编辑按 Escape 取消后不得写数据库，并应恢复普通单元格。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="原名称"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    inventory = window.inventory_page
    inventory.edit_component(0, 0)
    editor = inventory.table.cellWidget(0, 0)
    assert isinstance(editor, InlineEditor)
    editor.setText("不应保存")
    editor.cancel_edit()
    assert database.get_component(component["id"])["name"] == "原名称"
    assert inventory.table.cellWidget(0, 0) is None


def test_inline_cell_focus_loss_closes_editor(qtbot: Any, tmp_path: Path) -> None:
    """编辑器失焦时必须保存并立即恢复文字单元格，不能继续覆盖为输入框。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="失焦前"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    inventory = window.inventory_page
    inventory.edit_component(0, 0)
    editor = inventory.table.cellWidget(0, 0)
    assert isinstance(editor, InlineEditor)
    editor.setText("失焦后")
    editor.focus_left.emit()
    assert database.get_component(component["id"])["name"] == "失焦后"
    assert inventory.table.cellWidget(0, 0) is None
    assert inventory.table.item(0, 0).text() == "失焦后"


def test_jlc_draft_only_updates_receive_form_until_submit(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """商城草稿默认补空字段和动态参数，但在确认入库前不得创建元件。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    monkeypatch.setattr(JlcMergeDialog, "exec", lambda _self: JlcMergeDialog.DialogCode.Accepted)
    window.receive_page.apply_jlc_draft(draft)
    assert database.list_components() == []
    assert window.receive_page.name.currentText() == "FRC0402F91R0TS"
    assert window.receive_page.lcsc.text() == "C270654"
    assert window.receive_page.price.value() == 0
    assert window.receive_page.parameter_fields["resistance"][1].text() == "91Ω"
    assert window.receive_page.parameter_groups["resistance"] == "基础参数"


def test_second_jlc_draft_replaces_unsaved_first_draft(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """再次商城选型应默认覆盖上一份未入库草稿，而不是把它当作人工值锁住。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    first = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    second = first.model_copy(
        update={
            "lcsc_part_number": "C123456",
            "name": "SECOND-PART",
            "manufacturer_part_number": "SECOND-PART",
            "brand": "新品牌",
        }
    )
    monkeypatch.setattr(JlcMergeDialog, "exec", lambda _self: JlcMergeDialog.DialogCode.Accepted)
    window.receive_page.apply_jlc_draft(first)
    window.receive_page.apply_jlc_draft(second)
    assert window.receive_page.name.currentText() == "SECOND-PART"
    assert window.receive_page.lcsc.text() == "C123456"
    assert window.receive_page.brand.text() == "新品牌"


def test_receive_success_clears_form(qtbot: Any, tmp_path: Path, monkeypatch: Any) -> None:
    """确认入库成功后清空元件与批次输入，但保留当前位置和仓库选择。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="位置A"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="仓库A"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    receive = window.receive_page
    monkeypatch.setattr(receive, "show_info", lambda _message: None)
    receive.name.setEditText("待入库元件")
    receive.brand.setText("测试品牌")
    receive.category.setCurrentText("电阻")
    receive._rebuild_category_fields()
    receive.quantity.setValue(12)
    receive.notes.setText("批次备注")
    receive.submit()
    assert receive.name.currentText() == ""
    assert receive.brand.text() == ""
    assert receive.category.currentText() == ""
    assert receive.parameter_group.isHidden()
    assert receive.quantity.value() == 1
    assert receive.notes.text() == ""
    assert receive.location.currentData() == location["id"]


def test_jlc_merge_defaults_select_all_website_values(qtbot: Any) -> None:
    """商城基础字段和参数默认全选，同时保留商城页未包含的旧参数。"""

    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    dialog = JlcMergeDialog(
        draft,
        {"brand": "人工品牌", "package": "", "name": "已有名称"},
        [
            {
                "parameter_key": "custom_parameter",
                "label": "自定义参数",
                "value": "保留值",
                "group_name": "人工参数",
            }
        ],
    )
    qtbot.addWidget(dialog)
    assert dialog.field_rows["brand"][0].isChecked()
    assert dialog.field_rows["package"][0].isChecked()
    assert all(check.isChecked() for _, _, check, _ in dialog.parameter_rows)
    result = dialog.result_data()
    assert result.brand == draft.brand
    assert result.package == "0402"
    assert any(
        parameter.key == "custom_parameter"
        for group in result.parameter_groups
        for parameter in group.parameters
    )

    dialog.field_rows["brand"][0].setChecked(False)
    assert dialog.result_data().brand is None


def test_jlc_merge_apply_button_accepts_dialog(qtbot: Any) -> None:
    """ApplyRole 不会自动发出 accepted，按钮必须显式结束确认对话框。"""

    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    dialog = JlcMergeDialog(draft, {}, [])
    qtbot.addWidget(dialog)
    dialog.apply_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
