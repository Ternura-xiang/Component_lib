"""PySide6 主窗口无显示设备烟雾测试。"""

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QMessageBox, QPushButton
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
from component_library.ui.jlc_browser import JlcBatchMergeDialog, JlcMergeDialog
from component_library.ui.main_window import (
    BatchCodeScanDialog,
    CollapsibleSection,
    InlineEditor,
    MainWindow,
    WheelSafeComboBox,
)


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
        "库存流水",
        "统计",
        "设置",
    ]
    window._show_main_page(1)
    window.receive_page.open_order_import.emit()
    assert window.stack.currentWidget() is window.order_page
    assert window.navigation.currentRow() == 1
    window.order_page.back_requested.emit()
    assert window.stack.currentWidget() is window.receive_page
    window.receive_page.open_scan.emit()
    assert window.stack.currentWidget() is window.scan_page
    assert window.navigation.currentRow() == 1
    window.scan_page.back_requested.emit()
    assert window.stack.currentWidget() is window.receive_page
    window._show_main_page(2)
    window.issue_page.open_bom.emit()
    assert window.stack.currentWidget() is window.bom_page
    assert window.navigation.currentRow() == 2
    window.bom_page.back_requested.emit()
    assert window.stack.currentWidget() is window.issue_page


def test_leaving_receive_scan_stops_camera(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """从扫描入库子页返回或切换主页时必须释放摄像头。"""

    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)
    stopped: list[bool] = []
    monkeypatch.setattr(window.scan_page, "stop_camera", lambda: stopped.append(True))
    window._show_main_page(1)
    window.receive_page.open_scan.emit()
    window._show_main_page(0)
    assert stopped == [True]


def test_receive_actions_and_settings_tabs(qtbot: Any, tmp_path: Path) -> None:
    """入库页只保留商城选择和扫描入口，设置包含备份与关于。"""

    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)
    button_texts = {button.text() for button in window.receive_page.findChildren(QPushButton)}
    assert "从立创商城选择" in button_texts
    assert "摄像头扫码" in button_texts
    assert "扫描 / 图片识别入库" in button_texts
    assert not any("匿名临时浏览器" in text for text in button_texts)
    assert not any("剪贴板" in text for text in button_texts)
    assert [
        window.settings_page.tabs.tabText(index)
        for index in range(window.settings_page.tabs.count())
    ] == ["连接信息", "备份与恢复", "关于"]
    about_labels = {
        label.text() for label in window.settings_page.tabs.widget(2).findChildren(QLabel)
    }
    assert "Ternura_xiang" in about_labels
    assert "0.9.0" in about_labels
    assert "第三方许可" not in about_labels
    assert isinstance(window.receive_page.category, WheelSafeComboBox)


def test_batch_code_scan_dialog_accepts_only_custom_code(qtbot: Any) -> None:
    """自定义批次扫码对话框只接受 CL 加十位数字。"""

    parent = QDialog()
    qtbot.addWidget(parent)
    dialog = BatchCodeScanDialog(RecognitionEngine(), parent)
    qtbot.addWidget(dialog)
    dialog._decoded("{on:SO123,pc:C123,qty:1}")
    assert dialog.code is None
    assert "不是 CL 批次码" in dialog.status.text()
    dialog._decoded("cl0000000042")
    assert dialog.code == "CL0000000042"
    assert dialog.result() == QDialog.DialogCode.Accepted


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


def test_manual_issue_clears_parameters_after_success(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="手工出库元件"))
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="A01"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=5)
    )
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    monkeypatch.setattr(issue, "show_info", lambda _message: None)
    issue.search.setText("手工出库元件")
    issue.quantity.setValue(2)
    issue.notes.setText("测试用途")
    issue.submit()

    assert issue.search.text() == ""
    assert issue.component.count() == 0
    assert issue.location.currentData() is None
    assert issue.location.currentText() == ""
    assert issue.quantity.value() == 1
    assert issue.notes.text() == ""


def test_issue_scan_prefills_batch_without_immediate_mutation(
    qtbot: Any, tmp_path: Path
) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="扫码二极管"))
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=warehouse["id"], name="B04"))
    database.receive(
        ReceiveRequest(
            component_id=component["id"], location_id=location["id"], quantity=7,
            batch_code="CL0000000099",
        )
    )
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    assert issue.resolve_scan("CL0000000099")
    assert not issue.scan_result.isHidden()
    assert issue.scan_quantity.value() == 7
    assert "识别成功" in issue.scan_status.text()
    assert "主柜 / B04：7" in issue.scan_stock_info.text()
    assert database.resolve_batch_code("CL0000000099")["remaining_quantity"] == 7
    issue.scan_quantity.setValue(3)
    issue.submit_scan()
    assert database.get_batch(issue.database.resolve_batch_code("CL0000000099")["id"])[
        "remaining_quantity"
    ] == 4
    assert "本次 3" in issue.scan_status.text()
    assert "批次剩余 4" in issue.scan_status.text()
    assert issue.selected_batch is None
    assert issue.scan_text.text() == ""
    assert not issue.scan_quantity.isEnabled()
    assert not issue.scan_confirm.isEnabled()


def test_issue_scan_missing_batch_stays_visible_without_mutating_stock(
    qtbot: Any, tmp_path: Path
) -> None:
    """二维码可识别但批次未入库时，应明确区分于识别失败。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    assert not issue.resolve_scan("CL0000009999")
    assert not issue.scan_result.isHidden()
    assert "二维码识别成功" in issue.scan_status.text()
    assert "没有找到" in issue.scan_status.text()
    assert "CL0000009999" in issue.scan_code_info.text()
    assert not issue.scan_confirm.isEnabled()
    assert not issue.scan_quantity.isEnabled()


def test_jlc_qr_details_are_shown_when_batch_is_missing(qtbot: Any, tmp_path: Path) -> None:
    """实际袋贴二维码解码成功后，即使未入库也展示扫描字段。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    payload = (
        "{on:SO26090420180,pc:C22356447,pm:RCA03120KFLF,"
        "qty:100,mc:,cc:1,pdi:234896327,hp:11}"
    )

    assert not issue.resolve_scan(payload)
    assert "二维码识别成功" in issue.scan_status.text()
    assert "订单号：SO26090420180" in issue.scan_code_info.text()
    assert "立创编号：C22356447" in issue.scan_component_info.text()
    assert "型号：RCA03120KFLF" in issue.scan_component_info.text()
    assert "标签数量：100" in issue.scan_batch_info.text()
    assert "请先完成该批次入库" in issue.scan_stock_info.text()


def test_manual_scan_empty_input_is_not_reported_as_invalid_qr(
    qtbot: Any, tmp_path: Path
) -> None:
    """未粘贴任何内容时只给出操作提示，不误报二维码无效。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page

    issue.scan_text.clear()
    issue.resolve_manual_scan()

    assert "请先粘贴" in issue.scan_status.text()
    assert "二维码不是有效" not in issue.scan_status.text()
    assert not issue.scan_confirm.isEnabled()


def test_camera_scan_only_triggers_once_for_same_qr(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """摄像头连续帧识别到同一二维码时只解析一次。"""

    database = Database(tmp_path / "inventory.db")
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    parsed: list[str] = []
    stopped: list[bool] = []

    def record_scan(code: str) -> bool:
        parsed.append(code)
        return False

    monkeypatch.setattr(issue, "resolve_scan", record_scan)
    monkeypatch.setattr(issue, "stop_camera", lambda: stopped.append(True))

    issue._barcode_decoded(" CL0000000001 ")
    issue._barcode_decoded("CL0000000001")
    issue._barcode_decoded("CL0000000002")

    assert parsed == ["CL0000000001", "CL0000000002"]
    assert stopped == [True, True]
    assert issue.scan_text.text() == "CL0000000002"


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
    receive.batch_code.setText("CL0000000123")
    receive.notes.setText("入库备注")
    receive._scan_raw_barcode = "raw-test"
    receive.submit()

    assert database.get_component(component["id"])["brand"] == "新品牌"
    stock = database.list_stock()
    assert len(stock) == 1
    assert stock[0]["location_id"] == location["id"]
    assert stock[0]["quantity"] == 3
    assert receive.name.currentText() == ""
    assert receive.brand.text() == ""
    assert receive.batch_code.text() == ""
    assert receive.notes.text() == ""
    assert receive.quantity.value() == 1
    assert receive._scan_raw_barcode is None


def test_inventory_context_delete_path_uses_safe_delete(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    database = Database(tmp_path / "inventory.db")
    database.create_component(ComponentCreate(name="可删除元件"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.inventory_page.delete_component_at_row(0)

    assert database.list_components() == []


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
    assert page.table.horizontalHeaderItem(7).text() == "详细信息"
    details = page.table.cellWidget(0, 7)
    assert isinstance(details, CollapsibleSection)
    assert not details.isChecked()
    page.cancel_import()
    assert page.table.rowCount() == 0
    assert page.lines == []


def test_batch_merge_supports_component_and_field_selection(qtbot: Any) -> None:
    """批量变更窗口应同时支持元件整体和单独字段的对号选择。"""

    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    dialog = JlcBatchMergeDialog(
        [("component-1", "测试元件", draft, {"brand": "旧品牌"}, [])]
    )
    qtbot.addWidget(dialog)
    result = dialog.result_data()["component-1"]
    assert result.brand == draft.brand
    dialog.masters["component-1"].setChecked(False)
    assert dialog.result_data() == {}


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
