"""PySide6 主窗口无显示设备烟雾测试。"""

import csv
import logging
import os
import time
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from PySide6.QtCore import QDate, QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
)
from test_jlc_product import PRODUCT_HTML
from test_order_import import HTML as ORDER_HTML

import component_library.ui.main_window as main_window_module
from component_library import __version__
from component_library.connection import ConnectionManager
from component_library.database import Database
from component_library.debug_log import DebugLogHandler
from component_library.jlc_product import parse_jlc_product_page
from component_library.models import (
    BomInputRow,
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
    ComponentEditDialog,
    InlineEditor,
    InventoryPage,
    MainWindow,
    OrderFilesDialog,
    OrderImportPage,
    SettingsPage,
    WheelSafeComboBox,
    default_debug_mode,
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
    assert window.stack.count() == 11
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


def test_debug_default_differs_between_source_and_packaged(monkeypatch: Any) -> None:
    """源码便于诊断而默认开启，打包程序首次运行默认关闭。"""

    monkeypatch.delattr("sys.frozen", raising=False)
    assert default_debug_mode() is True
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert default_debug_mode() is False


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
    ] == ["连接信息", "备份与恢复", "资料匹配", "调试", "关于"]
    about_labels = {
        label.text() for label in window.settings_page.tabs.widget(4).findChildren(QLabel)
    }
    assert "Ternura_xiang" in about_labels
    assert __version__ in about_labels
    assert "第三方许可" not in about_labels
    assert isinstance(window.receive_page.category, WheelSafeComboBox)


def test_online_android_appears_as_camera_and_bom_rows_are_optional(
    qtbot: Any, tmp_path: Path
) -> None:
    """在线手机应进入摄像头列表；BOM 行可取消并按仓库计算红色缺料。"""

    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="测试电阻", category="电阻"))
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(
        LocationCreate(warehouse_id=position["id"], name="A01")
    )
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=2)
    )
    connection = ConnectionManager(database, tmp_path)
    code = connection.pairing_payload()["pairing_code"]
    paired = connection.pair(str(code), "测试手机", "ANDROID_PHONE", "1.0.3")
    connection.authenticate(paired["device_token"])
    window = MainWindow(database, RecognitionEngine(), "token", connection)
    qtbot.addWidget(window)
    window.issue_page._refresh_camera_choices()
    assert any(
        "安卓 · 测试手机" in window.issue_page.camera_choice.itemText(index)
        for index in range(window.issue_page.camera_choice.count())
    )
    remote = window.sessions.create(
        "ISSUE",
        {"camera_only": True, "target_device_id": paired["device_id"], "quantity": 1},
        "desktop",
    )
    window.issue_page._refresh_camera_choices()
    selector = window.issue_page.camera_choice
    busy_index = next(i for i in range(selector.count()) if "正在出库" in selector.itemText(i))
    assert not selector.model().flags(selector.model().index(busy_index, 0)) & main_window_module.Qt.ItemFlag.ItemIsEnabled
    window._show_main_page(2)
    window.issue_page.remote_session_id = remote["session_id"]
    window._show_main_page(0)
    assert window.sessions.get(remote["session_id"])["state"] == "CANCELLED"

    row = BomInputRow(row_number=1, name="测试电阻", quantity=3, raw_fields={})
    window.bom_page._parsed(([row],))
    window.bom_page._candidate_changed(0, component["id"])
    assert window.bom_page.table.cellWidget(0, 0).isChecked()
    assert window.bom_page.table.item(0, 9).text() == "1"
    assert window.bom_page.table.item(0, 9).background().color().name() == "#ffd6d6"
    assert window.bom_page.preview[0]["location_id"] is None
    assert "主柜 / A01" in window.bom_page.table.item(0, 6).text()
    window.bom_page.location.setCurrentIndex(
        window.bom_page.location.findData(location["id"])
    )
    window.bom_page._apply_overall_location()
    assert window.bom_page.preview[0]["location_id"] == location["id"]
    window.bom_page.table.cellWidget(0, 0).setChecked(False)
    assert window.bom_page.commit.isEnabled() is False


def test_bom_typed_candidate_and_project_name_reach_transaction(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(
        name="测试电阻", package="0603", manufacturer_part_number="PART-101",
    ))
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    database.receive(ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=2))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    page = window.bom_page
    window._show_subpage(page)
    window.show()
    monkeypatch.setattr(page, "show_info", lambda _message: None)
    page.source = tmp_path / "BOM_控制板.csv"
    page._parsed(([BomInputRow(row_number=1, name="未知物料", quantity=1, raw_fields={})],))
    selector = page.table.cellWidget(0, 4)
    editor = selector.lineEdit()
    editor.selectAll()
    qtbot.keyClicks(editor, "PART-101")
    assert editor.text() == "PART-101"
    assert selector.count() == 1
    completer = selector.completer()
    popup = completer.popup()
    index = popup.model().index(0, 0)
    assert index.isValid()
    qtbot.mouseClick(popup.viewport(), main_window_module.Qt.MouseButton.LeftButton,
                     pos=popup.visualRect(index).center())
    assert page.preview[0]["selected_component_id"] == component["id"]
    assert page.table.item(0, 2).text() == "0603"
    assert not page.commit.isEnabled()
    warehouse = page.table.cellWidget(0, 5)
    warehouse.setCurrentIndex(warehouse.findData(location["id"]))
    assert page.commit.isEnabled()

    with qtbot.waitSignal(page.open_pick) as opened:
        page.commit_bom()
    session_id = opened.args[0]
    snapshot = window.sessions.get(session_id)["snapshot"]
    assert snapshot["name"] == "控制板"
    item_id = snapshot["groups"][0]["items"][0]["id"]
    window.sessions.command(session_id, "pick", 0, "CHECK_ITEM", {"item_id": item_id}, "phone")
    window.sessions.command(session_id, "submit", 1, "COMMIT", {}, "phone")
    window.transactions_page.refresh()
    assert window.transactions_page.table.item(0, 3).text() == "控制板"
    assert window.transactions_page.table.item(0, 6).text() == "安卓BOM 出库"


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
    assert issue.scan_quantity.value() == 1
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


def test_phone_scan_opens_desktop_issue_and_synchronizes_confirmation(
    qtbot: Any, tmp_path: Path
) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="手机扫码元件"))
    position = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    database.receive(ReceiveRequest(
        component_id=component["id"], location_id=location["id"], quantity=5, batch_code="CL0000000103",
    ))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    session_id = window.sessions.create("ISSUE", {"camera_stream": True}, "phone")["session_id"]
    window.sessions.command(session_id, "scan", 0, "SCAN_CODE", {"raw_text": "CL0000000103"}, "phone")
    window._follow_mobile_inventory()
    assert window.stack.currentWidget() is window.issue_page
    assert window.issue_page.scan_quantity.value() == 1
    assert not window.issue_page.camera_choice.isEnabled()
    window.sessions.command(session_id, "confirm", 1, "UPDATE_AND_CONFIRM", {"quantity": 2}, "phone")
    window.issue_page._poll_remote_camera()
    assert "出库成功" in window.issue_page.scan_status.text()
    assert window.transactions_page.table.item(0, 6).text() == "安卓扫码出库"
    assert database.resolve_batch_code("CL0000000103")["remaining_quantity"] == 3
    window._show_main_page(0)
    assert window.sessions.get(session_id)["state"] == "CANCELLED"


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


@pytest.mark.parametrize("entry", ["button", "shortcut", "menu"])
def test_inventory_shows_separate_lots_and_moves_ctrl_selected_rows(
    qtbot: Any, tmp_path: Path, monkeypatch: Any, entry: str
) -> None:
    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    source = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    target_position = database.create_warehouse(WarehouseCreate(name="目标位置"))
    target = database.create_location(LocationCreate(warehouse_id=target_position["id"], name="B01"))
    component = database.create_component(ComponentCreate(name="分批元件", lcsc_part_number="C7654321"))
    for index in (1, 2):
        database.receive(
            ReceiveRequest(
                component_id=component["id"], location_id=source["id"], quantity=index,
                order_number=f"SO2611100000{index}",
            )
        )
    page = InventoryPage(database)
    qtbot.addWidget(page)
    page.resize(1200, 500)
    page.show()
    page.activateWindow()
    page.table.setFocus()
    qtbot.waitUntil(page.table.hasFocus)
    assert page.table.rowCount() == 2
    assert {page.table.item(row, 12).text() for row in (0, 1)} == {
        "SO26111000001", "SO26111000002",
    }
    assert [page.table.item(row, 21).text() for row in (0, 1)] == ["3", "3"]
    assert not page.table.item(0, 16).icon().isNull()
    assert page.table.horizontalHeader().visualIndex(16) + 1 == (
        page.table.horizontalHeader().visualIndex(9)
    )
    for row, modifier in ((0, Qt.KeyboardModifier.NoModifier), (1, Qt.KeyboardModifier.ControlModifier)):
        item = page.table.item(row, 0)
        qtbot.mouseClick(
            page.table.viewport(), Qt.MouseButton.LeftButton, modifier,
            page.table.visualItemRect(item).center(),
        )
    assert len(page.table.selectionModel().selectedRows()) == 2

    def confirm(dialog: QDialog) -> QDialog.DialogCode:
        position_selector, location = dialog.findChildren(WheelSafeComboBox)
        assert not location.isEnabled()
        position_selector.setCurrentIndex(position_selector.findData(target_position["id"]))
        assert location.findData(source["id"]) == -1
        location.setCurrentIndex(location.findData(target["id"]))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", confirm)
    monkeypatch.setattr(page, "show_error", lambda message: pytest.fail(message))
    if entry == "shortcut":
        qtbot.keyClick(page.table, Qt.Key.Key_M, Qt.KeyboardModifier.ControlModifier)
    elif entry == "menu":
        class SelectTransferMenu(QMenu):
            def exec(self, *_args: Any) -> Any:
                return next(action for action in self.actions() if action.text() == "变更仓库…")

        monkeypatch.setattr(main_window_module, "QMenu", SelectTransferMenu)
        page.open_context_menu(page.table.visualItemRect(page.table.item(0, 0)).center())
    else:
        next(button for button in page.findChildren(QPushButton) if button.text() == "变更仓库").click()
    assert page.table.rowCount() == 2
    assert {lot["location_id"] for lot in database.list_inventory_lots()} == {target["id"]}


def test_component_dialog_scrolls_parameters_and_recovers_on_collapse(
    qtbot: Any, tmp_path: Path
) -> None:
    database = Database(tmp_path / "inventory.db")
    component = database.create_component(ComponentCreate(name="多参数元件"))
    database.save_component_parameter_groups(
        component["id"],
        [JlcParameterGroup(
            name="商品参数",
            parameters=[JlcParameter(key=f"param_{i}", label=f"参数 {i}", value="1") for i in range(35)],
        )],
    )
    parent = InventoryPage(database)
    qtbot.addWidget(parent)
    dialog = ComponentEditDialog(database, component["id"], parent)
    qtbot.addWidget(dialog)
    dialog.resize(460, 320)
    dialog.show()
    scroll = dialog.findChild(QScrollArea)
    buttons = dialog.findChild(QDialogButtonBox)
    assert scroll is not None and buttons is not None
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)
    collapsed_maximum = scroll.verticalScrollBar().maximum()
    size = dialog.size()
    dialog.parameter_group.setChecked(True)
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > collapsed_maximum)
    assert dialog.size() == size
    assert dialog.rect().contains(buttons.geometry())
    dialog.parameter_group.setChecked(False)
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() == collapsed_maximum)

    details = ComponentEditDialog(database, component["id"], parent, read_only=True)
    qtbot.addWidget(details)
    assert details.name.isReadOnly()
    assert all(field.isReadOnly() for _, field in details.parameter_fields.values())
    assert details.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save) is None


def test_warehouse_color_change_refreshes_inventory_and_receive(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    component = database.create_component(ComponentCreate(name="有库存元件"))
    database.receive(
        ReceiveRequest(component_id=component["id"], location_id=location["id"], quantity=2)
    )
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    monkeypatch.setattr(QColorDialog, "getColor", lambda *_args: QColor("#ab328c"))
    page = window.locations_page
    page.change_storage_color(page.table.item(0, 1))
    icons = (
        page.table.item(0, 1).icon(),
        window.inventory_page.table.item(0, 16).icon(),
        window.receive_page.location.itemIcon(window.receive_page.location.findData(location["id"])),
    )
    assert [icon.pixmap(12, 12).toImage().pixelColor(5, 5).name() for icon in icons] == [
        "#ab328c", "#ab328c", "#ab328c",
    ]


def test_order_file_preview_runs_on_ui_thread(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """通过文件选择入口验证订单 HTML 直接生成界面预览。"""

    path = tmp_path / "orders.html"
    path.write_text(ORDER_HTML, encoding="utf-8")
    page = OrderImportPage(Database(tmp_path / "inventory.db"))
    qtbot.addWidget(page)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))

    def select_web(dialog: OrderFilesDialog) -> QDialog.DialogCode:
        browse = next(button for button in dialog.findChildren(QPushButton) if button.text() == "浏览…")
        browse.click()
        assert dialog.paths() == [path]
        dialog.accept()
        return QDialog.DialogCode(dialog.result())

    monkeypatch.setattr(OrderFilesDialog, "exec", select_web)
    preview = page._show_preview
    called_on_ui_thread: list[bool] = []

    def show_preview(*args: Any) -> None:
        on_ui_thread = QThread.currentThread() == page.thread()
        called_on_ui_thread.append(on_ui_thread)
        if on_ui_thread:
            preview(*args)

    monkeypatch.setattr(page, "_show_preview", show_preview)
    page.choose_file()
    qtbot.waitUntil(lambda: bool(called_on_ui_thread))

    assert called_on_ui_thread == [True]
    assert page.table.rowCount() == 1
    assert page.confirm.isEnabled()
    assert page.lines[0].lcsc_part_number == "C27636670"
    assert page.lines[0].quantity == 2
    assert page.lines[0].unit_price == 3.32
    assert not page.lines[0].jlc_matched


def test_order_import_reuses_matched_component_for_new_batch(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A"))
    existing = database.create_component(
        ComponentCreate(name="已有元件", lcsc_part_number="C27636670", jlc_matched=True)
    )
    line = OrderImportLine(
        name="新订单行", lcsc_part_number="C27636670", order_number="SO26102118592", quantity=3
    )
    page = OrderImportPage(database)
    qtbot.addWidget(page)
    page._show_preview([line])
    assert page.table.item(0, 5).text() == "已匹配"
    notices: list[str] = []
    monkeypatch.setattr(page, "show_info", notices.append)
    page.enrich_order_lines()
    assert notices == ["当前范围内没有需要匹配的订单元件"]
    assert database.import_order_lines([line], location["id"])["imported"] == 1
    assert database.get_component(existing["id"])["jlc_matched"] == 1
    assert database.list_inventory_lots()[0]["component_id"] == existing["id"]


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
    assert dialog.table.horizontalHeaderItem(0).text() == "元件 / 字段"
    assert all(
        dialog.table.isRowHidden(row)
        for row in range(*dialog.ranges["component-1"])
    )
    dialog.masters["component-1"].setChecked(False)
    assert dialog.result_data() == {}


def test_order_preview_never_imports_without_explicit_confirmation(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """解析、折叠和资料预览都不得写库存，确认后已导入行应自动排除。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    page = window.order_page
    page._show_preview(
        [
            OrderImportLine(
                order_number="SO26111000001",
                purchased_at="2026-11-10 12:00:00",
                lcsc_part_number="C7654321",
                name="SAFE-PREVIEW",
                quantity=2,
                unit_price=0.1,
            )
        ]
    )
    assert database.list_components() == []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )
    page.commit()
    assert database.list_components() == []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(page, "show_info", lambda _message: None)
    page.commit()
    assert len(database.list_components()) == 1
    assert page.table.rowCount() == 0
    assert "均已导入" in page.parse_progress.text()


def test_reupload_same_order_file_continues_next_batch(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """完成当前批次后，重新上传同一行集合只显示尚未导入部分。"""

    monkeypatch.setattr(main_window_module, "ORDER_IMPORT_BATCH_SIZE", 2)
    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    page = window.order_page
    lines = [
        OrderImportLine(
            order_number=f"SO261110000{index}",
            purchased_at="2026-11-10 12:00:00",
            lcsc_part_number=f"C765432{index}",
            name=f"续导元件{index}",
            quantity=1,
            unit_price=0.1,
        )
        for index in (1, 2, 3)
    ]
    page._show_preview(lines)
    assert page.table.rowCount() == 2
    assert "另有 1 条" in page.parse_progress.text()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(page, "show_info", lambda _message: None)
    page.commit()
    page._show_preview(lines)
    assert page.table.rowCount() == 1
    assert page.lines[0].name == "续导元件3"


def test_transactions_merge_group_and_expand_details(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一原子操作只显示一条摘要，展开后仍可查看原始流水。"""

    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A01"))
    first = database.create_component(ComponentCreate(name="元件一"))
    database.receive(ReceiveRequest(component_id=first["id"], location_id=location["id"], quantity=1))
    # BOM/订单等批量操作共用 group_id；这里直接通过订单导入构造一组两条流水。
    lines = [
        OrderImportLine(
            order_number="SO26111000002",
            purchased_at="2026-11-10 12:00:00",
            lcsc_part_number=f"C900000{index}",
            name=f"批量元件{index}",
            quantity=index,
            unit_price=0.1,
        )
        for index in (1, 2)
    ]
    database.import_order_lines(lines, location["id"])
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    table = window.transactions_page.table
    # 两个事务组：一条普通入库摘要 + 一组订单摘要和其两条隐藏详情。
    assert table.rowCount() == 4
    grouped_summary = next(
        row
        for row in range(table.rowCount())
        if isinstance(table.cellWidget(row, 0), QToolButton)
        and not table.cellWidget(row, 0).isHidden()
    )
    assert "展开" not in table.item(grouped_summary, 3).text()
    assert table.item(grouped_summary, 6).text() == "离线订单导入"
    assert "SO26111000002" in table.item(grouped_summary, 7).text()
    toggle = table.cellWidget(grouped_summary, 0)
    assert isinstance(toggle, QToolButton)
    assert table.isRowHidden(grouped_summary + 1)
    toggle.setChecked(True)
    assert not table.isRowHidden(grouped_summary + 1)
    single_summary = next(
        row for row in range(table.rowCount()) if table.item(row, 3).text() == "元件一"
    )
    assert table.cellWidget(single_summary, 0) is None

    page = window.transactions_page
    page.query.setText("C9000002")
    qtbot.waitUntil(lambda: table.rowCount() == 1)
    assert table.item(0, 3).text() == "批量元件2"
    assert page.filtered_rows[0]["quantity_delta"] == 2
    destination = tmp_path / "流水.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), "CSV"))
    monkeypatch.setattr(page, "show_info", lambda _message: None)
    page.export_filtered()
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["立创编号"], row["数量变动"]) for row in rows] == [("C9000002", "2")]


def test_inventory_facets_and_purchase_dates_filter_batches(qtbot: Any, tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    position = database.create_warehouse(WarehouseCreate(name="主位置"))
    location = database.create_location(LocationCreate(warehouse_id=position["id"], name="A"))
    component = database.create_component(
        ComponentCreate(name="电阻", category="电阻", brand="品牌 A", package="0603")
    )
    database.create_component(
        ComponentCreate(name="电容", category="电容", brand="品牌 B", package="0805")
    )
    for quantity, date in ((1, "2026-10-01"), (2, "2026-11-01")):
        database.receive(ReceiveRequest(
            component_id=component["id"], location_id=location["id"],
            quantity=quantity, purchase_date=date,
        ))
    page = InventoryPage(database)
    qtbot.addWidget(page)
    page.brand_filter.setCurrentIndex(page.brand_filter.findData("品牌 A"))
    assert page.category_filter.findData("电容") == -1
    assert page.package_filter.findData("0805") == -1
    assert page.table.rowCount() == 2
    page.date_field.setCurrentIndex(page.date_field.findData("purchase_date"))
    page.date_range.start.setDate(QDate(2026, 11, 1))
    page.date_range.end.setDate(QDate(2026, 11, 1))
    page.date_range.enabled.setChecked(True)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 9).text() == "2"
    assert page.table.item(0, 21).text() == "3"
    rows = database.inventory_export_rows(page.visible_component_ids, lot_ids=page.visible_lot_ids)
    assert [row["quantity"] for row in rows] == [2]


def test_inventory_supplement_respects_request_limit(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "inventory.db")
    for index in range(3):
        database.create_component(ComponentCreate(name=f"元件 {index}", lcsc_part_number=f"C1234{index}"))
    page = InventoryPage(database)
    qtbot.addWidget(page)
    requested: list[tuple[str, str, str]] = []

    def confirm(dialog: QDialog) -> QDialog.DialogCode:
        labels = {label.text(): label for label in dialog.findChildren(QLabel)}
        assert labels["自动补充范围"].alignment() == Qt.AlignmentFlag.AlignCenter
        assert "为避免封IP风险请勿一次进行过多请求。" in labels
        limit = dialog.findChild(QSpinBox)
        assert limit is not None
        limit.setValue(2)
        return QDialog.DialogCode.Accepted

    class Browser:
        def __init__(self, targets: list[tuple[str, str, str]], _parent: InventoryPage) -> None:
            requested.extend(targets)

        def start(self) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", confirm)
    monkeypatch.setattr(main_window_module, "JlcBatchBrowserDialog", Browser)
    menu = page.enrich_button.menu()
    assert menu is not None
    next(action for action in menu.actions() if action.text() == "从立创商城…").trigger()
    assert len(requested) == 2


def test_inventory_supplements_existing_component_from_file(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "orders.html"
    path.write_text(ORDER_HTML, encoding="utf-8")
    database = Database(tmp_path / "inventory.db")
    item = database.create_component(
        ComponentCreate(name="自定义名称", lcsc_part_number="C27636670", brand="已有品牌")
    )
    page = InventoryPage(database)
    qtbot.addWidget(page)
    notices: list[str] = []
    monkeypatch.setattr(page, "show_info", notices.append)
    monkeypatch.setattr(page, "show_error", pytest.fail)

    def confirm(dialog: QDialog) -> QDialog.DialogCode:
        if isinstance(dialog, OrderFilesDialog):
            dialog.web.setText(str(path))
            dialog.accept()
            return QDialog.DialogCode(dialog.result())
        assert dialog.windowTitle() == "确认补充资料"
        assert database.get_component(item["id"])["package"] is None
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", confirm)
    menu = page.enrich_button.menu()
    assert menu is not None
    next(action for action in menu.actions() if action.text() == "从文件…").trigger()
    qtbot.waitUntil(lambda: bool(notices))
    detail = database.get_component(item["id"])
    assert detail["manufacturer_part_number"] == "GS2821CR-G"
    assert detail["package"] == "SMD,15.2x13mm"
    assert detail["name"] == "自定义名称" and detail["brand"] == "已有品牌"
    assert len(database.list_components()) == 1
    assert database.list_inventory_lots() == []
    assert database.list_transactions() == []


def test_frame_counts_only_appear_in_debug_mode(qtbot: Any, tmp_path: Path) -> None:
    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page
    issue.scan_received_frames = 12
    issue.scan_attempts = 3
    issue.set_debug_mode(False)
    issue._barcode_decoded(None)
    assert "帧" not in issue.scan_status.text()
    issue.set_debug_mode(True)
    issue._barcode_decoded(None)
    assert "12 帧" in issue.scan_status.text()
    window.set_debug_mode(True)
    window._show_main_page(2)
    window._refresh_debug_overlay()
    output = window.debug_overlay.text()
    assert "frames=12" in output and "[" not in output


def test_debug_log_appends_and_follows_only_at_end(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    preferences.setValue("debug_mode", True)
    monkeypatch.setattr(main_window_module, "QSettings", lambda *_args: preferences)
    path = tmp_path / "test.log"
    path.write_text("2026-11-12 08:00:00,000 INFO test.legacy: 旧格式事件\n", encoding="utf-8")
    handler = DebugLogHandler(path)
    logger = logging.Logger("test.debug", logging.DEBUG)
    logger.addHandler(handler)
    try:
        for index in range(80):
            logger.info("历史事件 %s", index)
        page = SettingsPage(Database(tmp_path / "inventory.db"), "token", log_handler=handler)
        qtbot.addWidget(page)
        page.resize(700, 420)
        page.tabs.setCurrentIndex(3)
        page.show()
        scrollbar = page.debug_output.verticalScrollBar()
        qtbot.waitUntil(lambda: scrollbar.maximum() > 5)
        scrollbar.setValue(5)
        initial = page.debug_output.toPlainText()
        assert "[2026-11-12 08:00:00,000][INFO][test.legacy] 旧格式事件" in initial
        assert initial.count("[INFO][运行环境]") == 1
        assert f"版本: {__version__}" in initial and "线程池活动数" in initial
        worker = Thread(target=logger.info, args=("工作线程事件",))
        worker.start()
        worker.join()
        qtbot.waitUntil(lambda: "工作线程事件" in page.debug_output.toPlainText())
        assert page.debug_output.toPlainText().startswith(initial)
        assert scrollbar.value() == 5

        scrollbar.setValue(scrollbar.maximum())
        handler.doRollover()
        logger.warning("轮换后的事件")
        qtbot.waitUntil(lambda: "[WARNING][test.debug] 轮换后的事件" in page.debug_output.toPlainText())
        assert scrollbar.value() == scrollbar.maximum()
        assert page.debug_output.toPlainText().count("工作线程事件") == 1

        page.debug_toggle.setChecked(False)
        logger.info("重新启用前的事件")
        page.debug_toggle.setChecked(True)
        logger.info("重新启用后的事件")
        qtbot.waitUntil(lambda: "重新启用后的事件" in page.debug_output.toPlainText())
        assert page.debug_output.toPlainText().count("重新启用前的事件") == 1
        assert path.with_suffix(".log.1").read_text(encoding="utf-8").count("[INFO][运行环境]") == 1
        assert "[INFO][运行环境]" not in path.read_text(encoding="utf-8")

        menu = page.debug_level_button.menu()
        next(action for action in menu.actions() if action.text() == "WARNING").trigger()
        logger.info("低于最低等级")
        logger.error("错误首行\n错误续行")
        qtbot.waitUntil(lambda: "[ERROR][test.debug] 错误续行" in page.debug_output.toPlainText())
        assert "重新启用后的事件" not in page.debug_output.toPlainText()
        assert "低于最低等级" not in path.read_text(encoding="utf-8")
        assert preferences.value("debug/log_level") == "WARNING"
        next(action for action in menu.actions() if action.text() == "DEBUG").trigger()
        logger.debug("调试级事件")
        qtbot.waitUntil(lambda: "[DEBUG][test.debug] 调试级事件" in page.debug_output.toPlainText())
    finally:
        handler.close()


def test_maintenance_settings_match_backup_and_require_exact_reset_confirmation(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_window_module, "QSettings", lambda *_args: preferences)
    database = Database(tmp_path / "inventory.db")
    item = database.create_component(ComponentCreate(name="电阻", lcsc_part_number="C270654"))
    database.save_component_parameter_groups(item["id"], [
        JlcParameterGroup(name="采购信息", parameters=[
            JlcParameter(key="purchase_price", label="购入单价", value="0.125"),
        ]),
    ])
    window = MainWindow(database, RecognitionEngine(), "token")
    qtbot.addWidget(window)
    page = window.settings_page
    inventory_page = window.inventory_page
    class FakeBrowser(QObject):
        succeeded = Signal(object)
        failed = Signal(str, bool)

        def __init__(self, part_number: str, _url: str, owner: QObject) -> None:
            super().__init__(owner)
            self.part_number = part_number
            self.cancelled = False
            browsers.append(self)

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            self.cancelled = True

    browsers: list[FakeBrowser] = []
    monkeypatch.setattr(main_window_module, "JlcBackgroundBrowser", FakeBrowser)
    price_column = next(
        column for column in range(inventory_page.table.columnCount())
        if inventory_page.table.horizontalHeaderItem(column).text() == "购入价格"
    )
    assert inventory_page.table.item(0, price_column).data(Qt.ItemDataRole.DisplayRole) == 0.125
    assert not page.match_timer.isActive()
    page.auto_match.setChecked(True)
    assert 1_000 <= page.match_timer.interval() <= 60_000
    assert page.match_status.text() == ""
    monkeypatch.setattr(main_window_module.random, "getrandbits", lambda _bits: 0)
    page._match_tick()
    assert database.get_component(item["id"])["jlc_matched"] == 0
    assert page.match_timer.interval() == 60_000
    monkeypatch.setattr(main_window_module.random, "getrandbits", lambda _bits: 1)
    page._match_tick()
    assert len(browsers) == 1 and browsers[0].part_number == "C270654"
    page._match_tick()
    assert len(browsers) == 1
    browsers[0].succeeded.emit(parse_jlc_product_page("https://item.szlcsc.com/12345.html", PRODUCT_HTML))
    assert database.get_component(item["id"])["jlc_matched"] == 1
    page._match_tick()
    assert page.match_stats.text() == "商城自动匹配次数 1　成功次数 1"
    assert preferences.value("matching/browser_attempts", type=int) == 1
    assert preferences.value("matching/browser_successes", type=int) == 1

    database.create_component(ComponentCreate(name="另一元件", lcsc_part_number="C270655"))
    page._match_tick()
    assert len(browsers) == 2 and browsers[1].part_number == "C270655"
    page.jlc_updates.setChecked(False)
    assert browsers[1].cancelled and not page.match_timer.isActive() and page.auto_match.isHidden()
    assert page.match_stats.isHidden() and page.match_status.isHidden()
    for table in (inventory_page.table, window.order_page.table):
        column = next(
            index for index in range(table.columnCount())
            if table.horizontalHeaderItem(index).text() == "资料匹配"
        )
        assert table.isColumnHidden(column)
    assert not preferences.value("matching/jlc_updates", type=bool)
    assert all(control.isHidden() for control in window.receive_page.jlc_update_controls)
    assert all(control.isHidden() for control in window.order_page.jlc_update_controls)
    menu = inventory_page.enrich_button.menu()
    assert [action.text() for action in menu.actions() if action.isVisible()] == ["从文件…"]
    page.jlc_updates.setChecked(True)
    assert page.match_timer.isActive() and not page.auto_match.isHidden()
    assert not page.match_stats.isHidden() and page.match_status.text() == ""
    assert not inventory_page.table.isColumnHidden(20)
    assert not window.order_page.table.isColumnHidden(5)
    page._match_tick()
    browsers[-1].failed.emit("商城要求登录", True)
    assert not page.auto_match.isChecked() and page.match_status.text() == "商城要求登录"
    page.auto_match.setChecked(False)
    assert not page.match_timer.isActive()

    page.backup_interval.setValue(10)
    page.backup_retain.setValue(2)
    page.auto_backup.setChecked(True)
    qtbot.waitUntil(lambda: not page._backup_running)
    assert len(database.automatic_backups()) == 1
    first = database.automatic_backups()[0]
    page._check_automatic_backup()
    assert not page._backup_running
    assert 0 < page.backup_timer.remainingTime() <= 600_000
    ten_minutes_ago = time.time() - 601
    os.utime(first, (ten_minutes_ago, ten_minutes_ago))
    page._check_automatic_backup()
    qtbot.waitUntil(lambda: not page._backup_running)
    assert len(database.automatic_backups()) == 2
    assert preferences.value("backup/interval_minutes", type=int) == 10
    assert preferences.value("backup/retain", type=int) == 2
    page.auto_backup.setChecked(False)

    notices: list[str] = []
    monkeypatch.setattr(page, "show_info", notices.append)
    monkeypatch.setattr(page, "show_error", notices.append)
    pending = database.path.with_suffix(".restore-pending.db")
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("yes", True))
    page.reset_data()
    assert not pending.exists()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("YES", True))
    page.reset_data()
    assert pending.exists()
    assert database.get_component(item["id"])["name"] == "电阻"


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
