"""元件库主窗口及各功能页面。

界面层只负责收集输入、展示结果和发起后台任务。库存规则全部位于 Database/BOM/OCR
服务中，避免按钮事件绕过事务校验。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import (
    QCamera,
    QMediaCaptureSession,
    QMediaDevices,
    QVideoFrame,
    QVideoSink,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..bom import ALIASES, parse_bom, preview_bom
from ..database import Database, DomainError
from ..export import export_inventory_csv, export_inventory_xlsx
from ..label_parser import CUSTOM_BATCH_PATTERN, parse_qr_payload
from ..models import (
    ComponentCreate,
    IssueRequest,
    JlcParameter,
    JlcParameterGroup,
    JlcProductDraft,
    LocationCreate,
    OrderImportLine,
    ReceiveRequest,
    WarehouseCreate,
)
from ..order_import import parse_saved_orders
from ..recognition import RecognitionEngine
from .jlc_browser import (
    JlcBatchBrowserDialog,
    JlcBatchMergeDialog,
    JlcBrowserDialog,
    JlcMergeDialog,
)
from .workers import Worker


def qimage_to_png(image: QImage) -> bytes:
    """把 Qt 图像编码为内存 PNG，兼容 PySide6 6.12 的运行时参数。"""

    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("无法创建摄像头帧内存缓冲区")
    if not image.save(buffer, "PNG"):  # type: ignore[call-overload]
        raise RuntimeError("无法将摄像头帧编码为 PNG")
    return bytes(data.data())


def make_table(headers: list[str]) -> QTableWidget:
    """创建统一外观的只读表格。"""

    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setSortingEnabled(True)
    return table


def fill_table(table: QTableWidget, rows: list[list[Any]]) -> None:
    """整体替换表格内容，避免旧行残留。"""

    sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem()
            if isinstance(value, (int, float)):
                item.setData(Qt.ItemDataRole.DisplayRole, value)
            else:
                item.setText("" if value is None else str(value))
            table.setItem(row_index, column, item)
    table.setSortingEnabled(sorting)


def close_inline_editor(table: QTableWidget, editor: QLineEdit, row: int, column: int) -> None:
    """立即移除原位编辑器，避免延迟销毁期间仍覆盖文字或链接。"""

    editor.blockSignals(True)
    table.removeCellWidget(row, column)
    editor.hide()
    editor.deleteLater()


class WheelSafeSpinBox(QSpinBox):
    """忽略鼠标滚轮，防止滚动页面时意外改变整数数量。"""

    def wheelEvent(self, event: Any) -> None:
        """拒绝滚轮事件，让父级滚动区域继续处理页面滚动。"""

        event.ignore()


class WheelSafeComboBox(QComboBox):
    """忽略滚轮切换选项，防止滚动页面时误改类别或仓库。"""

    def wheelEvent(self, event: Any) -> None:
        event.ignore()


class CollapsibleSection(QWidget):
    """使用三角箭头展开/收起内容，不占用复选框的视觉语义。"""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.content: QWidget | None = None
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.toggled.connect(self._toggled)
        self.body = QWidget()
        self.body.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.body)

    def setContentWidget(self, widget: QWidget) -> None:
        self.content = widget
        widget.hide()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(widget)

    def _toggled(self, checked: bool) -> None:
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.body.setVisible(checked)
        if self.content is not None:
            self.content.setVisible(checked)

    def setChecked(self, checked: bool) -> None:
        self.toggle.setChecked(checked)

    def isChecked(self) -> bool:
        return self.toggle.isChecked()


class WheelSafeDoubleSpinBox(QDoubleSpinBox):
    """忽略鼠标滚轮，防止滚动页面时意外改变单价。"""

    def wheelEvent(self, event: Any) -> None:
        """拒绝滚轮事件，避免未聚焦时误改浮点数值。"""

        event.ignore()


class InlineEditor(QLineEdit):
    """支持 Escape 取消的单元格原位编辑器。

    点击其他位置仍由 ``editingFinished`` 自动保存；按 Escape 时先标记取消，再通知
    所属页面重建表格，从而恢复双击编辑前的显示和数据库状态。
    """

    cancelled = Signal()
    focus_left = Signal()

    def cancel_edit(self) -> None:
        """取消当前修改；该方法也供无显示设备测试直接调用。"""

        self.setProperty("cancelled", True)
        self.cancelled.emit()

    def keyPressEvent(self, event: Any) -> None:
        """拦截 Escape 作为取消，其余按键交还 QLineEdit 默认实现。"""

        if event.key() == Qt.Key.Key_Escape:
            self.cancel_edit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        """显式报告失焦；即使平台未发 editingFinished 也能结束编辑模式。"""

        super().focusOutEvent(event)
        if not self.property("cancelled") and not self.property("saving"):
            self.focus_left.emit()


CATEGORY_PARAMETER_GROUPS: tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...] = (
    (
        ("电阻",),
        (("resistance", "阻值"), ("tolerance", "精度"), ("power", "功率"), ("technology", "工艺")),
    ),
    (
        ("MOS", "场效应"),
        (("channel", "P/N 沟道"), ("count", "MOS 数量"), ("voltage", "耐压"), ("current", "电流")),
    ),
    (
        ("电容",),
        (
            ("capacitance", "容值"),
            ("tolerance", "精度"),
            ("voltage", "耐压"),
            ("dielectric", "介质"),
        ),
    ),
    (
        ("电感", "磁珠"),
        (
            ("inductance", "感值/阻抗"),
            ("tolerance", "精度"),
            ("current", "额定电流"),
            ("dcr", "直流电阻"),
        ),
    ),
    (
        ("二极管", "LED"),
        (
            ("forward_voltage", "正向压降"),
            ("current", "额定电流"),
            ("color", "颜色"),
            ("wavelength", "波长"),
        ),
    ),
)
DEFAULT_CATEGORY_PARAMETERS = (
    ("rated_voltage", "额定电压"),
    ("rated_current", "额定电流"),
    ("power", "功率"),
    ("temperature_range", "温度范围"),
)
CATEGORY_NAMES = ("贴片电阻", "电容", "电感", "MOSFET", "二极管", "LED", "集成电路")


def category_parameter_definitions(category: str) -> tuple[tuple[str, str], ...]:
    """返回类别对应的结构化参数定义。"""

    normalized = category.casefold()
    for markers, fields in CATEGORY_PARAMETER_GROUPS:
        if any(marker.casefold() in normalized for marker in markers):
            return fields
    return DEFAULT_CATEGORY_PARAMETERS


def category_parameter_values(detail: dict[str, Any]) -> dict[str, str]:
    """统一读取手工参数表以及订单导入生成的电阻/MOS 子表。"""

    values = {
        parameter["parameter_key"]: parameter["value"]
        for parameter in detail.get("category_parameters", [])
    }
    resistor = detail.get("resistor_details")
    if not values and resistor:
        values = {
            "resistance": str(resistor.get("resistance") or ""),
            "tolerance": f"±{resistor['tolerance_percent']:g}%"
            if resistor.get("tolerance_percent") is not None
            else "",
            "power": f"{resistor['power_mw']:g}mW" if resistor.get("power_mw") is not None else "",
            "technology": str(resistor.get("technology") or ""),
        }
    mosfet = detail.get("mosfet_details")
    if not values and mosfet:
        values = {
            "channel": str(mosfet.get("channel_type") or ""),
            "count": str(mosfet.get("mosfet_count") or ""),
            "voltage": f"{mosfet['voltage_v']:g}V" if mosfet.get("voltage_v") is not None else "",
            "current": f"{mosfet['current_a']:g}A" if mosfet.get("current_a") is not None else "",
        }
    return values


def category_parameter_entries(detail: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """按数据库顺序返回参数键、标签、值和分组，兼容旧库的无分组数据。"""

    return [
        (
            str(parameter["parameter_key"]),
            str(parameter["label"]),
            str(parameter["value"]),
            str(parameter.get("group_name") or "类别参数"),
        )
        for parameter in detail.get("category_parameters", [])
    ]


class ComponentEditDialog(QDialog):
    """双击库存行时编辑元件主数据和类别参数。"""

    def __init__(self, database: Database, component_id: str, parent: QWidget) -> None:
        """读取 ``component_id`` 的完整详情并构造可编辑表单。

        参数字段会保留数据库中的稳定键与分组；对话框确认前不写入数据。
        """

        super().__init__(parent)
        self.database = database
        self.component_id = component_id
        self.detail = database.get_component(component_id)
        self.parameter_fields: dict[str, tuple[str, QLineEdit]] = {}
        self.parameter_groups: dict[str, str] = {}
        self.setWindowTitle("修改元件")
        self.resize(620, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(str(self.detail.get("name") or ""))
        self.aliases = QLineEdit(", ".join(self.detail.get("aliases") or []))
        self.lcsc = QLineEdit(str(self.detail.get("lcsc_part_number") or ""))
        self.mpn = QLineEdit(str(self.detail.get("manufacturer_part_number") or ""))
        self.custom = QLineEdit(str(self.detail.get("custom_part_number") or ""))
        self.brand = QLineEdit(str(self.detail.get("brand") or ""))
        self.package = QLineEdit(str(self.detail.get("package") or ""))
        self.category = WheelSafeComboBox()
        self.category.setEditable(True)
        self.category.addItems(CATEGORY_NAMES)
        self.category.setCurrentText(str(self.detail.get("category") or ""))
        self.category.currentTextChanged.connect(self._rebuild_parameters)
        self.price = WheelSafeDoubleSpinBox()
        self.price.setRange(0, 100_000_000)
        self.price.setDecimals(6)
        self.price.setValue(float(self.detail.get("price") or 0))
        self.purchase_date = QLineEdit(str(self.detail.get("purchase_date") or ""))
        self.product_url = QLineEdit(str(self.detail.get("product_url") or ""))
        self.datasheet_url = QLineEdit(str(self.detail.get("datasheet_url") or ""))
        self.notes = QLineEdit(str(self.detail.get("notes") or ""))
        for label, main_widget in (
            ("名称", self.name),
            ("别名（逗号分隔）", self.aliases),
            ("立创编号", self.lcsc),
            ("制造商料号", self.mpn),
            ("自定义编号", self.custom),
            ("品牌", self.brand),
            ("封装", self.package),
            ("类别", self.category),
        ):
            form.addRow(label, main_widget)
        layout.addLayout(form)
        self.parameter_group = CollapsibleSection("类别参数")
        parameter_content = QWidget()
        self.parameter_form = QFormLayout(parameter_content)
        self.parameter_group.setContentWidget(parameter_content)
        layout.addWidget(self.parameter_group)
        trailing = QFormLayout()
        for label, trailing_widget in (
            ("单价", self.price),
            ("购买日期", self.purchase_date),
            ("商品链接", self.product_url),
            ("数据手册", self.datasheet_url),
            ("备注", self.notes),
        ):
            trailing.addRow(label, trailing_widget)
        layout.addLayout(trailing)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        entries = category_parameter_entries(self.detail)
        self._rebuild_parameters(
            self.category.currentText(),
            {key: value for key, _label, value, _group in entries}
            or category_parameter_values(self.detail),
            [(key, label, group) for key, label, _value, group in entries] or None,
        )

    def _rebuild_parameters(
        self,
        _category: str = "",
        values: dict[str, str] | None = None,
        definitions: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """重建类别参数输入框，同时尽量保留用户尚未保存的输入。

        ``definitions`` 用于恢复持久化的键、标签和分组；为空时按当前类别使用默认
        定义。动态删除的旧控件通过 ``deleteLater`` 交给 Qt 安全回收。
        """

        preserved = {key: field.text() for key, (_, field) in self.parameter_fields.items()}
        if values:
            preserved.update(values)
        while self.parameter_form.count():
            item = self.parameter_form.takeAt(0)
            child = item.widget() if item else None
            if child:
                child.deleteLater()
        self.parameter_fields.clear()
        self.parameter_groups.clear()
        rows = definitions or [
            (key, label, "类别参数")
            for key, label in category_parameter_definitions(self.category.currentText())
        ]
        last_group = ""
        for key, label, group_name in rows:
            if group_name != last_group:
                heading = QLabel(group_name)
                heading.setStyleSheet("font-weight: 600; color: #425466;")
                self.parameter_form.addRow(heading)
                last_group = group_name
            field = QLineEdit(preserved.get(key, ""))
            self.parameter_form.addRow(label, field)
            self.parameter_fields[key] = (label, field)
            self.parameter_groups[key] = group_name

    def save(self) -> None:
        """验证并保存双击编辑结果。"""

        try:
            aliases = [value.strip() for value in self.aliases.text().split(",") if value.strip()]
            self.database.update_component(
                self.component_id,
                ComponentCreate(
                    name=self.name.text(),
                    aliases=aliases,
                    lcsc_part_number=self.lcsc.text() or None,
                    manufacturer_part_number=self.mpn.text() or None,
                    custom_part_number=self.custom.text() or None,
                    brand=self.brand.text() or None,
                    package=self.package.text() or None,
                    category=self.category.currentText() or None,
                    price=self.price.value(),
                    purchase_date=self.purchase_date.text() or None,
                    product_url=self.product_url.text() or None,
                    datasheet_url=self.datasheet_url.text() or None,
                    notes=self.notes.text() or None,
                ),
            )
            grouped: dict[str, list[JlcParameter]] = {}
            for key, (label, field) in self.parameter_fields.items():
                if field.text().strip():
                    grouped.setdefault(self.parameter_groups.get(key, "类别参数"), []).append(
                        JlcParameter(key=key, label=label, value=field.text().strip())
                    )
            self.database.save_component_parameter_groups(
                self.component_id,
                [
                    JlcParameterGroup(name=name, parameters=parameters)
                    for name, parameters in grouped.items()
                ],
            )
            self.accept()
        except Exception as error:
            QMessageBox.critical(self, "保存失败", str(error))


class OrderLineEditDialog(QDialog):
    """编辑订单导入草稿的一行，不直接修改库存。"""

    def __init__(self, line: OrderImportLine, parent: QWidget) -> None:
        """从订单草稿行填充控件；保存结果写入 ``edited_line`` 而非数据库。"""

        super().__init__(parent)
        self.original = line
        self.edited_line = line
        self.setWindowTitle("修改订单商品")
        form = QFormLayout(self)
        self.category = QLineEdit(line.category or "")
        self.name = QLineEdit(line.name)
        self.description = QLineEdit(line.description or "")
        self.brand = QLineEdit(line.brand or "")
        self.package = QLineEdit(line.package or "")
        self.price = WheelSafeDoubleSpinBox()
        self.price.setRange(0, 100_000_000)
        self.price.setDecimals(6)
        self.price.setValue(line.unit_price)
        self.quantity = WheelSafeSpinBox()
        self.quantity.setRange(1, 1_000_000_000)
        self.quantity.setValue(line.quantity)
        self.product_url = QLineEdit(line.product_url or "")
        for label, widget in (
            ("类别", self.category),
            ("名称", self.name),
            ("商品描述", self.description),
            ("品牌", self.brand),
            ("封装", self.package),
            ("单价", self.price),
            ("数量", self.quantity),
            ("商品链接", self.product_url),
        ):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self) -> None:
        """校验必填名称并生成不可变草稿的更新副本。"""

        if not self.name.text().strip():
            QMessageBox.critical(self, "保存失败", "元件名称不能为空")
            return
        self.edited_line = self.original.model_copy(
            update={
                "category": self.category.text() or None,
                "name": self.name.text(),
                "description": self.description.text() or None,
                "brand": self.brand.text() or None,
                "package": self.package.text() or None,
                "unit_price": self.price.value(),
                "quantity": self.quantity.value(),
                "product_url": self.product_url.text() or None,
            }
        )
        self.accept()


class StorageEditDialog(QDialog):
    """修改具体仓库的所属位置、名称和备注。"""

    def __init__(self, database: Database, item: dict[str, Any], parent: QWidget) -> None:
        """用库位记录初始化所属位置、名称和备注编辑控件。"""

        super().__init__(parent)
        self.setWindowTitle("修改仓库")
        form = QFormLayout(self)
        self.position = WheelSafeComboBox()
        for position in database.list_warehouses():
            self.position.addItem(position["name"], position["id"])
        self.position.setCurrentIndex(self.position.findData(item["warehouse_id"]))
        self.name = QLineEdit(str(item["name"]))
        self.notes = QLineEdit(str(item.get("notes") or ""))
        form.addRow("位置", self.position)
        form.addRow("仓库名称", self.name)
        form.addRow("备注", self.notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class BasePage(QWidget):
    """所有页面共享的提示和线程池入口。"""

    changed = Signal()

    def __init__(self, database: Database) -> None:
        """保存共享数据库引用并复用全局 Qt 线程池。"""

        super().__init__()
        self.database = database
        self.pool = QThreadPool.globalInstance()

    def show_error(self, message: str) -> None:
        """显示业务或运行时错误。"""

        QMessageBox.critical(self, "操作失败", message)

    def show_info(self, message: str) -> None:
        """显示成功信息。"""

        QMessageBox.information(self, "元件库", message)


class InventoryPage(BasePage):
    """元件搜索和分仓库库存完整信息总览。"""

    def __init__(self, database: Database) -> None:
        """构建库存筛选、表格、原位编辑和右键菜单，并加载首次快照。"""

        super().__init__(database)
        self.visible_component_ids: list[str] = []
        layout = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("名称、立创编号、制造商料号、封装……")
        self.category_filter = WheelSafeComboBox()
        self.brand_filter = WheelSafeComboBox()
        self.package_filter = WheelSafeComboBox()
        for combo, placeholder in (
            (self.category_filter, "全部类别"),
            (self.brand_filter, "全部品牌"),
            (self.package_filter, "全部封装"),
        ):
            combo.addItem(placeholder, "")
            combo.currentIndexChanged.connect(self.refresh)
        button = QPushButton("搜索 / 刷新")
        button.clicked.connect(self.refresh)
        delete_button = QPushButton("删除所选元件")
        delete_button.clicked.connect(self.delete_selected)
        self.query.returnPressed.connect(self.refresh)
        search_row.addWidget(self.query)
        search_row.addWidget(self.category_filter)
        search_row.addWidget(self.brand_filter)
        search_row.addWidget(self.package_filter)
        search_row.addWidget(button)
        search_row.addWidget(delete_button)
        layout.addLayout(search_row)
        export_row = QHBoxLayout()
        export_note = QLabel("导出范围：当前搜索和筛选结果；未筛选时导出全部活动元件。")
        export_excel = QPushButton("导出 Excel")
        export_excel.clicked.connect(self.export_excel)
        export_csv = QPushButton("导出 CSV")
        export_csv.clicked.connect(self.export_csv)
        export_row.addWidget(export_note)
        export_row.addStretch()
        export_row.addWidget(export_excel)
        export_row.addWidget(export_csv)
        layout.addLayout(export_row)
        self.table = make_table(
            [
                "元件",
                "别名",
                "立创编号",
                "制造商料号",
                "自定义编号",
                "类别",
                "品牌",
                "封装",
                "类别参数",
                "库存",
                "单价",
                "购买日期",
                "订单号",
                "建立日期",
                "入库日期",
                "最后更改",
                "位置 / 仓库",
                "商品",
                "数据手册",
                "备注",
                "资料匹配",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(
            (
                180,
                160,
                110,
                150,
                120,
                110,
                120,
                100,
                220,
                90,
                80,
                90,
                105,
                170,
                150,
                150,
                150,
                260,
                70,
                80,
                220,
            )
        ):
            self.table.setColumnWidth(column, width)
        self.table.cellDoubleClicked.connect(self.edit_component)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_context_menu)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        """重新读取元件和库存汇总。"""

        components = self.database.list_components(self.query.text())
        self._refresh_filters(components)
        category = str(self.category_filter.currentData() or "")
        brand = str(self.brand_filter.currentData() or "")
        package = str(self.package_filter.currentData() or "")
        components = [
            item
            for item in components
            if (not category or item.get("category") == category)
            and (not brand or item.get("brand") == brand)
            and (not package or item.get("package") == package)
        ]
        self.visible_component_ids = [str(item["id"]) for item in components]
        stock = self.database.list_stock()
        rows = []
        for component in components:
            locations = [item for item in stock if item["component_id"] == component["id"]]
            total = sum(float(item["quantity"]) for item in locations)
            position = "; ".join(
                f"{item['warehouse_name']}/{item['location_name']}: {int(item['quantity'])}"
                for item in locations
            )
            rows.append(
                [
                    component["name"],
                    ", ".join(component.get("aliases") or []) or "—",
                    component.get("lcsc_part_number") or "—",
                    component.get("manufacturer_part_number") or "—",
                    component.get("custom_part_number") or "—",
                    component.get("category") or "—",
                    component.get("brand") or "—",
                    component.get("package") or "—",
                    component.get("specification") or "—",
                    int(total),
                    component.get("price"),
                    component.get("purchase_date") or "—",
                    component.get("order_numbers") or "—",
                    component.get("created_at") or "—",
                    component.get("last_received_at") or "—",
                    component.get("updated_at") or "—",
                    position or "—",
                    "打开" if component.get("product_url") else "—",
                    "打开" if component.get("datasheet_url") else "—",
                    component.get("notes") or "—",
                    "已匹配" if component.get("jlc_matched") else "未匹配",
                ]
            )
        self.table.setSortingEnabled(False)
        fill_table(self.table, rows)
        for row, component in enumerate(components):
            name_item = self.table.item(row, 0)
            if name_item is not None:
                name_item.setData(Qt.ItemDataRole.UserRole, component["id"])
            for column, key in ((17, "product_url"), (18, "datasheet_url")):
                url = component.get(key)
                if url:
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setData(Qt.ItemDataRole.UserRole, url)
                        item.setForeground(QColor("#1769aa"))
                        item.setToolTip("双击在默认浏览器中打开")
        self.table.setSortingEnabled(True)

    def export_excel(self) -> None:
        """选择目标文件并导出当前筛选结果及配套统计工作表。"""

        filename, _ = QFileDialog.getSaveFileName(
            self, "导出库存 Excel", "component-library-inventory.xlsx", "Excel (*.xlsx)"
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.casefold() != ".xlsx":
            destination = destination.with_suffix(".xlsx")
        try:
            export_inventory_xlsx(self.database, destination, self.visible_component_ids)
            self.show_info(f"Excel 已导出：\n{destination}")
        except Exception as error:
            self.show_error(str(error))

    def export_csv(self) -> None:
        """选择目标文件并把当前筛选后的库存明细导出为 CSV。"""

        filename, _ = QFileDialog.getSaveFileName(
            self, "导出库存 CSV", "component-library-inventory.csv", "CSV (*.csv)"
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.casefold() != ".csv":
            destination = destination.with_suffix(".csv")
        try:
            export_inventory_csv(self.database, destination, self.visible_component_ids)
            self.show_info(f"CSV 已导出：\n{destination}")
        except Exception as error:
            self.show_error(str(error))

    def delete_selected(self) -> None:
        """删除当前选中元件；有库存时由业务层拒绝并说明原因。"""

        self.delete_component_at_row(self.table.currentRow())

    def delete_component_at_row(self, row: int) -> None:
        """从指定表格行删除元件，供顶部按钮和右键菜单共用。"""

        item = self.table.item(row, 0)
        if item is None:
            self.show_error("请先选择要删除的元件")
            return
        component_id = item.data(Qt.ItemDataRole.UserRole)
        if not component_id:
            self.show_error("所选行缺少元件标识")
            return
        answer = QMessageBox.question(
            self,
            "删除元件",
            f"确定删除“{item.text()}”吗？已有历史且库存为零时将安全归档。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.database.delete_component(str(component_id))
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def _refresh_filters(self, components: list[dict[str, Any]]) -> None:
        """保留当前选择并补齐类别、品牌和封装过滤项。"""

        for combo, key, placeholder in (
            (self.category_filter, "category", "全部类别"),
            (self.brand_filter, "brand", "全部品牌"),
            (self.package_filter, "package", "全部封装"),
        ):
            selected = combo.currentData()
            values = sorted({str(item[key]) for item in components if item.get(key)})
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, "")
            for value in values:
                combo.addItem(value, value)
            index = combo.findData(selected)
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)

    def edit_component(self, row: int, column: int) -> None:
        """双击可编辑格时原位输入；派生库存和系统时间保持只读。"""

        field_by_column = {
            0: "name",
            1: "aliases",
            2: "lcsc_part_number",
            3: "manufacturer_part_number",
            4: "custom_part_number",
            5: "category",
            6: "brand",
            7: "package",
            10: "price",
            11: "purchase_date",
            17: "product_url",
            18: "datasheet_url",
            19: "notes",
        }
        field = field_by_column.get(column)
        identity = self.table.item(row, 0)
        component_id = identity.data(Qt.ItemDataRole.UserRole) if identity else None
        if not field or not component_id:
            return
        detail = self.database.get_component(str(component_id))
        value = detail.get(field)
        if field == "aliases":
            value = ", ".join(detail.get("aliases") or [])
        editor = InlineEditor("" if value is None else str(value), self.table)
        editor.setFrame(False)
        self.table.setCellWidget(row, column, editor)
        editor.setFocus()
        editor.selectAll()
        editor.cancelled.connect(
            lambda current=editor, item_row=row, item_column=column: self._cancel_inline_component(
                current, item_row, item_column
            )
        )

        def save() -> None:
            """把当前库存单元格编辑委托给统一校验与刷新流程。"""

            self._save_inline_component(editor, str(component_id), field, row, column)

        editor.editingFinished.connect(save)
        editor.focus_left.connect(save)

    def _cancel_inline_component(self, editor: QLineEdit, row: int, column: int) -> None:
        """移除库存单元格编辑器，并从数据库恢复原始显示。"""

        close_inline_editor(self.table, editor, row, column)
        self.refresh()

    def _save_inline_component(
        self, editor: QLineEdit, component_id: str, field: str, row: int, column: int
    ) -> None:
        """失焦时保存库存单元格，并用完整模型维持其他字段不变。"""

        if editor.property("saving") or editor.property("cancelled"):
            return
        editor.setProperty("saving", True)
        close_inline_editor(self.table, editor, row, column)
        try:
            detail = self.database.get_component(component_id)
            values = {
                "name": detail["name"],
                "aliases": detail.get("aliases") or [],
                "lcsc_part_number": detail.get("lcsc_part_number"),
                "manufacturer_part_number": detail.get("manufacturer_part_number"),
                "custom_part_number": detail.get("custom_part_number"),
                "brand": detail.get("brand"),
                "package": detail.get("package"),
                "category": detail.get("category"),
                "specification": detail.get("specification"),
                "price": detail.get("price"),
                "purchase_date": detail.get("purchase_date"),
                "product_url": detail.get("product_url"),
                "datasheet_url": detail.get("datasheet_url"),
                "notes": detail.get("notes"),
            }
            text = editor.text().strip()
            if field == "aliases":
                values[field] = [part.strip() for part in text.split(",") if part.strip()]
            elif field == "price":
                values[field] = float(text) if text else None
            elif field == "name":
                values[field] = text
            else:
                values[field] = text or None
            self.database.update_component(component_id, ComponentCreate.model_validate(values))
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.refresh()
            self.show_error(str(error))

    def open_full_component_editor(self, row: int) -> None:
        """从右键菜单打开包含类别参数的完整编辑对话框。"""

        item = self.table.item(row, 0)
        component_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if component_id and ComponentEditDialog(self.database, str(component_id), self).exec():
            self.refresh()
            self.changed.emit()

    def open_context_menu(self, position: Any) -> None:
        """库存右键菜单提供完整编辑和外部链接入口。"""

        row = self.table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        edit = menu.addAction("完整编辑…")
        enrich = menu.addAction("从立创商城补充资料…")
        delete = menu.addAction("删除元件")
        menu.addSeparator()
        product = menu.addAction("打开商品链接")
        datasheet = menu.addAction("打开数据手册")
        product_item = self.table.item(row, 17)
        datasheet_item = self.table.item(row, 18)
        product.setEnabled(bool(product_item and product_item.data(Qt.ItemDataRole.UserRole)))
        datasheet.setEnabled(bool(datasheet_item and datasheet_item.data(Qt.ItemDataRole.UserRole)))
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected == edit:
            self.open_full_component_editor(row)
            return
        if selected == enrich:
            self.enrich_from_jlc(row)
            return
        if selected == delete:
            self.delete_component_at_row(row)
            return
        column = 17 if selected == product else 18 if selected == datasheet else -1
        if column >= 0:
            link_item = self.table.item(row, column)
            url = link_item.data(Qt.ItemDataRole.UserRole) if link_item else None
            if url:
                QDesktopServices.openUrl(QUrl(str(url)))

    @staticmethod
    def _jlc_product_url(component: dict[str, Any]) -> str | None:
        """返回可直接打开的商品页；订单未保存链接时按立创编号构造公开详情页。"""

        if component.get("product_url"):
            return str(component["product_url"])
        lcsc = str(component.get("lcsc_part_number") or "").strip().upper()
        return f"https://item.szlcsc.com/product/{lcsc}.html" if lcsc else None

    def enrich_from_jlc(self, row: int) -> None:
        """按用户选择的范围遍历商品页，并在写库前集中确认逐字段差异。"""

        item = self.table.item(row, 0)
        selected_id = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        choices = ("仅补充未匹配元件", "替换当前元件资料", "全部重新匹配并替换")
        choice, accepted = QInputDialog.getItem(
            self, "自动补充范围", "请选择匹配范围：", choices, 0, False
        )
        if not accepted:
            return
        components = self.database.list_components()
        if choice == choices[0]:
            candidates = [component for component in components if not component.get("jlc_matched")]
        elif choice == choices[1]:
            candidates = [component for component in components if component["id"] == selected_id]
        else:
            candidates = components
        candidates = [component for component in candidates if self._jlc_product_url(component)]
        if not candidates:
            self.show_info("所选范围内没有可通过立创编号或商品链接补充的元件")
            return
        targets = [
            (str(component["id"]), str(component["name"]), self._jlc_product_url(component) or "")
            for component in candidates
        ]
        browser = JlcBatchBrowserDialog(targets, self)
        QTimer.singleShot(0, browser.start)
        if browser.exec() != QDialog.DialogCode.Accepted or not browser.drafts:
            return
        changes = []
        for component in candidates:
            key = str(component["id"])
            draft = browser.drafts.get(key)
            if not draft:
                continue
            detail = self.database.get_component(key)
            changes.append(
                (
                    key,
                    str(detail["name"]),
                    draft,
                    self._component_text_fields(detail),
                    list(detail.get("category_parameters") or []),
                )
            )
        confirmation = JlcBatchMergeDialog(changes, self)
        if confirmation.exec() != QDialog.DialogCode.Accepted:
            return
        applied = self._apply_jlc_component_changes(confirmation.result_data())
        self.refresh()
        self.changed.emit()
        failures = len(browser.errors)
        self.show_info(f"已更新 {applied} 个元件；商品页读取失败 {failures} 个。")

    @staticmethod
    def _component_text_fields(detail: dict[str, Any]) -> dict[str, str]:
        return {
            key: str(detail.get(key) or "")
            for key in (
                "name",
                "lcsc_part_number",
                "manufacturer_part_number",
                "brand",
                "package",
                "category",
                "product_url",
                "datasheet_url",
                "notes",
            )
        }

    def _apply_jlc_component_changes(self, results: dict[str, Any]) -> int:
        """只写入确认对话框返回的元件和字段，并保存完整参数集合。"""

        applied = 0
        for component_id, result in results.items():
            detail = self.database.get_component(component_id)
            values = {
                key: detail.get(key)
                for key in (
                    "name",
                    "aliases",
                    "lcsc_part_number",
                    "manufacturer_part_number",
                    "custom_part_number",
                    "brand",
                    "package",
                    "category",
                    "specification",
                    "price",
                    "purchase_date",
                    "product_url",
                    "datasheet_url",
                    "notes",
                )
            }
            for key in self._component_text_fields(detail):
                value = getattr(result, key)
                if value is not None:
                    values[key] = value
            values["jlc_matched"] = True
            self.database.update_component(component_id, ComponentCreate.model_validate(values))
            self.database.save_component_parameter_groups(component_id, result.parameter_groups)
            applied += 1
        return applied


class BatchCodeScanDialog(QDialog):
    """使用摄像头扫描非立创物料的 ``CL`` 批次二维码。"""

    def __init__(self, recognition: RecognitionEngine, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("扫描自定义批次二维码")
        self.resize(720, 560)
        self.recognition = recognition
        self.code: str | None = None
        self.camera: QCamera | None = None
        self.session: QMediaCaptureSession | None = None
        self.sink: QVideoSink | None = None
        self.worker: Worker | None = None
        self.busy = False
        self.last_frame_at = 0.0
        self.pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        self.status = QLabel("正在准备摄像头…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.preview = QLabel("摄像头未启动")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(360)
        self.preview.setStyleSheet("background:#17202a;color:#ccd6dd;border-radius:8px")
        layout.addWidget(self.preview)
        controls = QHBoxLayout()
        self.camera_choice = WheelSafeComboBox()
        self._refresh_cameras()
        restart = QPushButton("重新打开摄像头")
        restart.clicked.connect(self.start_camera)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        controls.addWidget(self.camera_choice)
        controls.addWidget(restart)
        controls.addWidget(cancel)
        layout.addLayout(controls)

    def _refresh_cameras(self) -> None:
        selected = self.camera_choice.currentText() if self.camera_choice.count() else ""
        self.camera_choice.clear()
        devices = QMediaDevices.videoInputs()
        for device in devices:
            self.camera_choice.addItem(device.description())
        if not devices:
            self.camera_choice.addItem("未检测到摄像头")
            self.camera_choice.setEnabled(False)
            return
        self.camera_choice.setEnabled(True)
        previous = self.camera_choice.findText(selected)
        if previous >= 0:
            self.camera_choice.setCurrentIndex(previous)

    def start_camera(self) -> None:
        """打开所选摄像头并自动扫描。"""

        self._refresh_cameras()
        devices = QMediaDevices.videoInputs()
        if not devices:
            self.status.setText("未检测到摄像头，请检查系统权限")
            return
        self.stop_camera()
        index = max(0, min(self.camera_choice.currentIndex(), len(devices) - 1))
        device = devices[index]
        self.camera = QCamera(device)
        formats = [
            item
            for item in device.videoFormats()
            if item.resolution().width() * item.resolution().height() <= 1280 * 720
            and item.maxFrameRate() >= 15
        ]
        if formats:
            self.camera.setCameraFormat(
                max(
                    formats,
                    key=lambda item: (
                        item.resolution().width() * item.resolution().height(),
                        item.maxFrameRate(),
                    ),
                )
            )
        if self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAutoNear):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAutoNear)
        elif self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAuto):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAuto)
        self.camera.errorOccurred.connect(
            lambda _error, message: self.status.setText(f"摄像头异常：{message}")
        )
        self.session = QMediaCaptureSession()
        self.sink = QVideoSink()
        self.session.setCamera(self.camera)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.status.setText("请将 CL 加十位数字的二维码放入画面中央")
        self.camera.start()

    def stop_camera(self) -> None:
        if self.camera:
            self.camera.stop()
        self.camera = None
        self.session = None
        self.sink = None

    def _frame(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        self.preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        now = time.monotonic()
        if self.busy or now - self.last_frame_at < 0.35:
            return
        self.last_frame_at = now
        try:
            image_bytes = qimage_to_png(image.copy())
        except Exception as error:
            self.status.setText(f"图像处理异常：{error}")
            return
        self.busy = True
        self.worker = Worker(lambda: self.recognition.decode_barcode(image_bytes))
        self.worker.signals.succeeded.connect(self._decoded)
        self.worker.signals.failed.connect(
            lambda message: self.status.setText(f"二维码解码异常：{message}")
        )
        self.worker.signals.finished.connect(self._finished)
        self.pool.start(self.worker)

    def _finished(self) -> None:
        self.busy = False
        self.worker = None

    def _decoded(self, raw_text: str | None) -> None:
        text = raw_text.strip().upper() if raw_text else ""
        if not text:
            return
        if not CUSTOM_BATCH_PATTERN.fullmatch(text):
            self.status.setText(f"已识别二维码，但不是 CL 批次码：{text}")
            return
        self.code = text
        self.stop_camera()
        self.accept()

    def reject(self) -> None:
        self.stop_camera()
        super().reject()

    def closeEvent(self, event: Any) -> None:
        self.stop_camera()
        super().closeEvent(event)


class ReceivePage(BasePage):
    """使用可输入下拉框匹配已有元件，未匹配内容作为新元件入库。"""

    open_order_import = Signal()
    open_scan = Signal()

    def __init__(self, database: Database, recognition: RecognitionEngine) -> None:
        """构建手工入库表单、商城导入入口和动态类别参数区域。"""

        super().__init__(database)
        self.recognition = recognition
        self._creating_new = True
        self._selected_component_id: str | None = None
        self._jlc_draft_active = False
        self._scan_raw_barcode: str | None = None
        self._components: dict[str, dict[str, Any]] = {}
        self.parameter_fields: dict[str, tuple[str, QLineEdit]] = {}
        self.parameter_groups: dict[str, str] = {}
        page_layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        layout = QVBoxLayout(self.scroll_content)
        self.scroll_area.setWidget(self.scroll_content)
        page_layout.addWidget(self.scroll_area)
        action_row = QHBoxLayout()
        open_orders = QPushButton("订单导入")
        open_orders.clicked.connect(self.open_order_import.emit)
        open_scan = QPushButton("扫描 / 图片识别入库")
        open_scan.clicked.connect(self.open_scan.emit)
        action_row.addWidget(open_orders)
        action_row.addWidget(open_scan)
        layout.addLayout(action_row)
        form = QFormLayout()
        self.name = WheelSafeComboBox()
        self.name.setEditable(True)
        self.name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        name_editor = self.name.lineEdit()
        if name_editor:
            name_editor.setPlaceholderText("输入后自动展开并过滤已有元件")
            name_editor.textEdited.connect(self._name_edited)
        self.name.activated.connect(self._load_selected_component)
        self.lcsc = QLineEdit()
        self.mpn = QLineEdit()
        self.brand = QLineEdit()
        self.package = QLineEdit()
        self.category = WheelSafeComboBox()
        self.category.setEditable(True)
        self.category.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.category.addItems(self._available_categories())
        self.category.setCurrentIndex(-1)
        category_editor = self.category.lineEdit()
        if category_editor:
            category_editor.textEdited.connect(self._category_edited)
        self.category.activated.connect(self._category_selected)
        self.parameter_group = CollapsibleSection("类别参数")
        self.parameter_content = QWidget()
        self.parameter_form = QFormLayout(self.parameter_content)
        self.parameter_group.setContentWidget(self.parameter_content)
        self.parameter_group.hide()
        self.price = WheelSafeDoubleSpinBox()
        self.price.setRange(0, 100_000_000)
        self.price.setDecimals(6)
        self.purchase_date = QLineEdit()
        self.purchase_date.setPlaceholderText("可选，格式 YYYY-MM-DD")
        self.product_url = QLineEdit()
        self.datasheet_url = QLineEdit()
        self.component_notes = QLineEdit()
        self.position = WheelSafeComboBox()
        self.position.setToolTip("先选择位置")
        self.position.currentIndexChanged.connect(self._position_changed)
        self.location = WheelSafeComboBox()
        self.location.setToolTip("再选择该位置下的仓库")
        storage_row = QWidget()
        storage_layout = QHBoxLayout(storage_row)
        storage_layout.setContentsMargins(0, 0, 0, 0)
        storage_layout.addWidget(self.position)
        storage_layout.addWidget(self.location)
        self.quantity = WheelSafeSpinBox()
        self.quantity.setRange(1, 1_000_000_000)
        self.batch_code = QLineEdit()
        self.batch_code.setPlaceholderText("可选：CL 加十位数字")
        batch_code_row = QWidget()
        batch_code_layout = QHBoxLayout(batch_code_row)
        batch_code_layout.setContentsMargins(0, 0, 0, 0)
        batch_code_layout.addWidget(self.batch_code)
        scan_batch = QPushButton("摄像头扫码")
        scan_batch.clicked.connect(self.scan_batch_code)
        batch_code_layout.addWidget(scan_batch)
        self.order_number = QLineEdit()
        self.order_number.setPlaceholderText("扫描立创袋贴后自动填写")
        self.notes = QLineEdit()
        for label, main_widget in (
            ("元件名称", self.name),
            ("立创编号", self.lcsc),
            ("制造商料号", self.mpn),
            ("品牌", self.brand),
            ("封装", self.package),
            ("元件类别", self.category),
        ):
            form.addRow(label, main_widget)
        form.addRow(self.parameter_group)
        for label, detail_widget in (
            ("购买单价", self.price),
            ("购买日期", self.purchase_date),
            ("嘉立创商品链接", self.product_url),
            ("嘉立创数据手册", self.datasheet_url),
            ("元件备注", self.component_notes),
            ("位置 / 仓库", storage_row),
            ("数量", self.quantity),
            ("自定义批次二维码", batch_code_row),
            ("立创订单号", self.order_number),
            ("备注", self.notes),
        ):
            form.addRow(label, detail_widget)
        layout.addLayout(form)
        search_jlc = QPushButton("从立创商城选择")
        search_jlc.clicked.connect(self.open_jlc_browser)
        layout.addWidget(search_jlc)
        submit = QPushButton("确认入库")
        submit.clicked.connect(self.submit)
        layout.addWidget(submit)
        layout.addStretch()
        self.reload()

    def _available_categories(self) -> list[str]:
        """合并内置类别和本机已有类别，避免商城目录只能依靠重复手输。"""

        return list(dict.fromkeys((*CATEGORY_NAMES, *self.database.list_component_categories())))

    def reload(self) -> None:
        """刷新同行显示的位置和仓库下拉框，并保留当前选择。"""

        selected_position = self.position.currentData()
        selected_location = self.location.currentData()
        self.position.blockSignals(True)
        self.position.clear()
        for item in self.database.list_warehouses():
            self.position.addItem(item["name"], item["id"])
        position_index = self.position.findData(selected_position)
        if position_index >= 0:
            self.position.setCurrentIndex(position_index)
        self.position.blockSignals(False)
        self._reload_storage_options(selected_location)

    def _position_changed(self, _index: int) -> None:
        """切换位置时，只列出该位置实际包含的仓库。"""

        self._reload_storage_options()

    def _reload_storage_options(self, preferred_location_id: object | None = None) -> None:
        """重建仓库选择；数据库中的 location 对应界面中的“仓库”。"""

        position_id = self.position.currentData()
        self.location.clear()
        for item in self.database.list_locations():
            if item["warehouse_id"] == position_id:
                self.location.addItem(item["name"], item["id"])
        location_index = self.location.findData(preferred_location_id)
        if location_index >= 0:
            self.location.setCurrentIndex(location_index)

    def _name_edited(self, text: str) -> None:
        """按输入内容重建同一个下拉框，并立即展开过滤结果。"""

        self._jlc_draft_active = False
        self._scan_raw_barcode = None
        if not self._creating_new:
            self._clear_matched_fields()
        self._creating_new = True
        self._selected_component_id = None
        matches = self.database.list_components(text) if text.strip() else []
        self._components = {item["id"]: item for item in matches}
        stock_by_component: dict[str, list[str]] = {}
        for stock_item in self.database.list_stock():
            quantity = int(stock_item["quantity"])
            if quantity <= 0:
                continue
            stock_by_component.setdefault(str(stock_item["component_id"]), []).append(
                f"{stock_item['warehouse_name']}/{stock_item['location_name']}({quantity})"
            )
        self.name.blockSignals(True)
        self.name.clear()
        for item in matches:
            stock_text = "；".join(stock_by_component.get(str(item["id"]), [])) or "无库存"
            self.name.addItem(
                f"{item['name']} · {item.get('brand') or '无品牌'} · "
                f"{item.get('package') or '无封装'} · {stock_text}",
                item["id"],
            )
        self.name.setCurrentIndex(-1)
        self.name.setEditText(text)
        self.name.blockSignals(False)
        if matches:
            self.name.showPopup()

    def _load_selected_component(self, index: int) -> None:
        """点击名称下拉项后完成匹配，并自动填写元件资料。"""

        component_id = self.name.itemData(index)
        item = self._components.get(str(component_id)) if component_id else None
        if not item:
            return
        self._apply_existing_component(item)

    def _apply_existing_component(self, item: dict[str, Any]) -> None:
        """把一个已存在元件装入表单，供名称下拉和商城精确匹配共用。"""

        self._jlc_draft_active = False
        component_id = str(item["id"])
        self._creating_new = False
        self._selected_component_id = component_id
        self.name.blockSignals(True)
        self.name.setEditText(str(item["name"]))
        self.name.blockSignals(False)
        self.lcsc.setText(str(item.get("lcsc_part_number") or ""))
        self.mpn.setText(str(item.get("manufacturer_part_number") or ""))
        self.brand.setText(str(item.get("brand") or ""))
        self.package.setText(str(item.get("package") or ""))
        self.category.setCurrentText(str(item.get("category") or ""))
        self.price.setValue(float(item.get("price") or 0))
        self.purchase_date.clear()
        self.product_url.setText(str(item.get("product_url") or ""))
        self.datasheet_url.setText(str(item.get("datasheet_url") or ""))
        self.component_notes.setText(str(item.get("notes") or ""))
        detail = self.database.get_component(component_id)
        entries = category_parameter_entries(detail)
        self._rebuild_category_fields(
            {key: value for key, _label, value, _group in entries}
            or category_parameter_values(detail),
            [(key, label, group) for key, label, _value, group in entries] or None,
        )

    def _clear_matched_fields(self) -> None:
        """用户重新输入名称时清除上一个匹配元件带入的资料。"""

        for field in (
            self.lcsc,
            self.mpn,
            self.brand,
            self.package,
            self.product_url,
            self.datasheet_url,
            self.component_notes,
        ):
            field.clear()
        self.category.blockSignals(True)
        self.category.setCurrentIndex(-1)
        self.category.setEditText("")
        self.category.blockSignals(False)
        self.price.setValue(0)
        self.purchase_date.clear()
        self._rebuild_category_fields({})

    def _category_edited(self, text: str) -> None:
        """像元件名称一样过滤类别，并在输入时直接展开下拉项。"""

        matches = [
            name for name in self._available_categories() if text.casefold() in name.casefold()
        ]
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItems(matches)
        self.category.setCurrentIndex(-1)
        self.category.setEditText(text)
        self.category.blockSignals(False)
        self.parameter_group.hide()
        if matches:
            self.category.showPopup()

    def _category_selected(self, index: int) -> None:
        """点击类别下拉项后自动显示对应参数组。"""

        if index >= 0:
            selected = self.category.itemText(index)
            self.category.setEditText(selected)
            self._rebuild_category_fields()

    def _rebuild_category_fields(
        self,
        values: dict[str, str] | None = None,
        definitions: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """根据类别模板和商城字段重建入库参数表单。

        已保存的同类参数定义优先，``definitions`` 中的新字段追加；按稳定键去重后
        保留当前输入，防止切换类别或合并草稿时丢失用户内容。
        """

        has_category = bool(self.category.currentText().strip() or definitions)
        self.parameter_group.setVisible(has_category)
        preserved = {key: field.text() for key, (_, field) in self.parameter_fields.items()}
        if values:
            preserved.update(values)
        while self.parameter_form.count():
            item = self.parameter_form.takeAt(0)
            child = item.widget() if item else None
            if child:
                child.deleteLater()
        self.parameter_fields.clear()
        self.parameter_groups.clear()
        category = self.category.currentText().strip()
        shared = [
            (item["parameter_key"], item["label"], item["group_name"])
            for item in self.database.list_category_parameter_definitions(category)
        ]
        incoming = definitions or []
        if not shared and not incoming:
            incoming = [
                (key, label, "类别参数")
                for key, label in (category_parameter_definitions(category) if has_category else ())
            ]
        # 已保存的类别模板优先保持稳定顺序；当前商城页出现的新字段追加到模板末尾。
        # 按稳定 key 去重后，同类别元件始终显示相同的参数输入列表。
        rows: list[tuple[str, str, str]] = []
        seen_keys: set[str] = set()
        for key, label, group_name in (*shared, *incoming):
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append((key, label, group_name))
        last_group = ""
        for key, label, group_name in rows:
            if group_name != last_group:
                heading = QLabel(group_name)
                heading.setStyleSheet("font-weight: 600; color: #425466;")
                self.parameter_form.addRow(heading)
                last_group = group_name
            field = QLineEdit(preserved.get(key, ""))
            self.parameter_form.addRow(label, field)
            self.parameter_fields[key] = (label, field)
            self.parameter_groups[key] = group_name

    def _parameters(self) -> dict[str, tuple[str, str]]:
        """返回当前参数键到“显示标签、原始输入值”的映射。"""

        return {key: (label, field.text()) for key, (label, field) in self.parameter_fields.items()}

    def _parameter_group_models(self) -> list[JlcParameterGroup]:
        """把当前动态表单转换为带分组的持久化模型。"""

        grouped: dict[str, list[JlcParameter]] = {}
        for key, (label, field) in self.parameter_fields.items():
            if field.text().strip():
                grouped.setdefault(self.parameter_groups.get(key, "类别参数"), []).append(
                    JlcParameter(key=key, label=label, value=field.text().strip())
                )
        return [
            JlcParameterGroup(name=name, parameters=parameters)
            for name, parameters in grouped.items()
        ]

    def _component_form(self) -> ComponentCreate:
        """把主数据控件转换为经 Pydantic 校验的元件写入模型。"""

        return ComponentCreate(
            name=self.name.currentText(),
            lcsc_part_number=self.lcsc.text() or None,
            manufacturer_part_number=self.mpn.text() or None,
            brand=self.brand.text() or None,
            package=self.package.text() or None,
            category=self.category.currentText() or None,
            price=self.price.value(),
            purchase_date=self.purchase_date.text().strip() or None,
            product_url=self.product_url.text() or None,
            datasheet_url=self.datasheet_url.text() or None,
            notes=self.component_notes.text() or None,
            jlc_matched=True if self._jlc_draft_active else None,
        )

    def open_jlc_browser(self) -> None:
        """打开立创商城选择器，并把用户确认的商品资料应用到当前草稿。"""

        browser = JlcBrowserDialog(self)
        if browser.exec() != QDialog.DialogCode.Accepted or not browser.draft:
            return
        self.apply_jlc_draft(browser.draft)

    def scan_batch_code(self) -> None:
        """打开专用摄像头对话框，并回填符合规则的自定义批次码。"""

        dialog = BatchCodeScanDialog(self.recognition, self)
        QTimer.singleShot(0, dialog.start_camera)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.code:
            self.batch_code.setText(dialog.code)

    def apply_jlc_draft(self, draft: JlcProductDraft) -> None:
        """将立创商品草稿与当前入库表单进行逐字段合并。

        :param draft: 内嵌浏览器解析并经用户选中的商品草稿
        :return: 无返回值

        首先按立创编号精确查找已有元件，再打开合并对话框；用户取消时不改变表单，
        用户确认后也只更新草稿控件，不会在此步骤写数据库或增加库存。
        """

        ### 查找已有元件并建立合并基线 ###
        exact = next(
            (
                item
                for item in self.database.list_components(draft.lcsc_part_number)
                if item.get("lcsc_part_number") == draft.lcsc_part_number
            ),
            None,
        )
        if exact:
            exact_detail = self.database.get_component(str(exact["id"]))
            current_parameters = list(exact_detail.get("category_parameters", []))
            current_fields = {
                "name": str(exact_detail.get("name") or ""),
                "lcsc_part_number": str(exact_detail.get("lcsc_part_number") or ""),
                "manufacturer_part_number": str(exact_detail.get("manufacturer_part_number") or ""),
                "brand": str(exact_detail.get("brand") or ""),
                "package": str(exact_detail.get("package") or ""),
                "category": str(exact_detail.get("category") or ""),
                "product_url": str(exact_detail.get("product_url") or ""),
                "datasheet_url": str(exact_detail.get("datasheet_url") or ""),
                "notes": str(exact_detail.get("notes") or ""),
            }
        elif self._jlc_draft_active:
            # 再次从商城选型代表用户要替换上一份尚未入库的商城草稿。若继续把
            # 上一份草稿当作“现有人工值”，合并窗口会默认全部不勾选，看起来便
            # 无法覆盖。这里只清空合并基线，不提前改动当前表单，用户取消仍保留原稿。
            current_parameters = []
            current_fields = {}
        else:
            current_parameters = [
                {
                    "parameter_key": key,
                    "label": label,
                    "value": field.text(),
                    "group_name": self.parameter_groups.get(key, "类别参数"),
                }
                for key, (label, field) in self.parameter_fields.items()
                if field.text().strip()
            ]
            current_fields = {
                "name": self.name.currentText(),
                "lcsc_part_number": self.lcsc.text(),
                "manufacturer_part_number": self.mpn.text(),
                "brand": self.brand.text(),
                "package": self.package.text(),
                "category": self.category.currentText(),
                "product_url": self.product_url.text(),
                "datasheet_url": self.datasheet_url.text(),
                "notes": self.component_notes.text(),
            }
        ### 等待用户逐字段确认 ###
        merge = JlcMergeDialog(draft, current_fields, current_parameters, self)
        if merge.exec() != QDialog.DialogCode.Accepted:
            return
        ### 将确认结果写回当前入库草稿 ###
        result = merge.result_data()
        if exact:
            self._apply_existing_component(exact)
        else:
            self._creating_new = True
            self._selected_component_id = None
        field_widgets = {
            "lcsc_part_number": self.lcsc,
            "manufacturer_part_number": self.mpn,
            "brand": self.brand,
            "package": self.package,
            "product_url": self.product_url,
            "datasheet_url": self.datasheet_url,
            "notes": self.component_notes,
        }
        if result.name is not None:
            self.name.blockSignals(True)
            self.name.setEditText(result.name)
            self.name.blockSignals(False)
        for field_name, widget in field_widgets.items():
            value = getattr(result, field_name)
            if value is not None:
                widget.setText(value)
        if result.category is not None:
            self.category.setCurrentText(result.category)
        if result.parameter_groups:
            values = {
                parameter.key: parameter.value
                for group in result.parameter_groups
                for parameter in group.parameters
            }
            definitions = [
                (parameter.key, parameter.label, group.name)
                for group in result.parameter_groups
                for parameter in group.parameters
            ]
            self._rebuild_category_fields(values, definitions)
        self._jlc_draft_active = True

    def _reset_after_receive(self) -> None:
        """入库成功后清空元件和批次输入，保留位置/仓库方便连续入库。"""

        self._creating_new = True
        self._selected_component_id = None
        self._jlc_draft_active = False
        self._scan_raw_barcode = None
        self._components.clear()
        self.name.blockSignals(True)
        self.name.clear()
        self.name.setEditText("")
        self.name.blockSignals(False)
        for field in (
            self.lcsc,
            self.mpn,
            self.brand,
            self.package,
            self.purchase_date,
            self.product_url,
            self.datasheet_url,
            self.component_notes,
            self.batch_code,
            self.order_number,
            self.notes,
        ):
            field.clear()
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItems(self._available_categories())
        self.category.setCurrentIndex(-1)
        self.category.setEditText("")
        self.category.blockSignals(False)
        self.price.setValue(0)
        self.quantity.setValue(1)
        self._rebuild_category_fields({})

    def apply_scan_draft(self, draft: Any) -> None:
        """把 OCR/条码草稿映射到手工入库控件。

        :param draft: ``RecognitionEngine`` 返回的 ``LabelDraft`` 或兼容对象
        :return: 无返回值

        注意：原始条码会保存在批次元数据中以便追溯；本方法只填充控件，不创建元件
        或修改库存，最终数据仍需用户检查并点击确认。
        """

        ### 保存追溯信息并提取规范值 ###

        self._scan_raw_barcode = draft.raw_barcode
        values = {key: field.normalized_value for key, field in draft.fields.items()}
        name = values.get("description") or values.get("mpn") or ""
        self._name_edited(name)
        self.name.setEditText(name)
        self.lcsc.setText(values.get("lcsc_part_number") or "")
        self.mpn.setText(values.get("manufacturer_part_number") or values.get("mpn") or "")
        self.brand.setText(values.get("brand") or "")
        self.package.setText(values.get("package") or "")
        self.batch_code.setText(values.get("batch_code") or "")
        self.order_number.setText(values.get("order_number") or "")
        quantity = values.get("quantity")
        if quantity and quantity.isdigit():
            self.quantity.setValue(int(quantity))

    def submit(self) -> None:
        """保存元件及参数后执行入库事务，并在全部成功后重置表单。

        :return: 无返回值

        已选已有元件时同步保存表单修改；任一步异常都会显示错误且不会伪装为完成。
        """

        ### 创建或更新元件主数据 ###
        try:
            if self._creating_new:
                component_id = self.database.create_component(self._component_form())["id"]
            else:
                component_id = self._selected_component_id or ""
                # 既然用户已选中已有元件并确认入库，表单中的编辑一并保存。
                # 不再设置额外开关，避免界面显示一套数据而数据库仍保留旧值。
                if component_id:
                    self.database.update_component(component_id, self._component_form())
            if not component_id or not self.location.currentData():
                raise ValueError("请选择元件和仓库")
            ### 保存动态参数并执行库存入库 ###
            self.database.save_component_parameter_groups(
                component_id, self._parameter_group_models()
            )
            self.database.receive(
                ReceiveRequest(
                    component_id=component_id,
                    location_id=str(self.location.currentData()),
                    quantity=self.quantity.value(),
                    source="手工入库",
                    unit_price=self.price.value(),
                    purchase_date=self.purchase_date.text().strip() or None,
                    batch_code=self.batch_code.text().strip() or None,
                    order_number=self.order_number.text().strip() or None,
                    batch_metadata={"raw_barcode": draft_raw}
                    if (draft_raw := getattr(self, "_scan_raw_barcode", None))
                    else {},
                    notes=self.notes.text() or None,
                )
            )
            ### 全部成功后重置草稿并通知其他页面刷新 ###
            self.show_info("入库完成")
            self._reset_after_receive()
            self.reload()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))


class StoragePriorityDialog(QDialog):
    """配置全局或单个元件类别的仓库优先级。"""

    def __init__(self, database: Database, category: str | None, parent: QWidget) -> None:
        """列出所有库位及当前显式优先级；``category=None`` 表示全局配置。"""

        super().__init__(parent)
        self.database = database
        self.category = category
        self.controls: dict[str, QSpinBox] = {}
        self.setWindowTitle(f"仓库优先级：{category or '整体'}")
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        hint = QLabel("数字越小越优先；类别优先级高于名称匹配和整体优先级。")
        layout.addWidget(hint)
        self.table = make_table(["位置 / 仓库", "优先级"])
        self.table.setSortingEnabled(False)
        rows = database.storage_priority_rows(category)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            self.table.setItem(
                row_index,
                0,
                QTableWidgetItem(f"{row['warehouse_name']} / {row['location_name']}"),
            )
            priority = WheelSafeSpinBox()
            priority.setRange(0, 100_000)
            priority.setSpecialValueText("未设置")
            priority.setValue(int(row["priority"] or 0))
            self.table.setCellWidget(row_index, 1, priority)
            self.controls[str(row["location_id"])] = priority
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        """只保存大于零的显式优先级，零表示未设置。"""

        priorities = {
            location_id: control.value()
            for location_id, control in self.controls.items()
            if control.value() > 0
        }
        self.database.save_storage_priorities(self.category, priorities)
        self.accept()


class OrderImportPage(BasePage):
    """逐元件确认仓库并原子导入用户保存的立创商城订单页面。"""

    back_requested = Signal()

    def __init__(self, database: Database) -> None:
        """构建订单文件选择、异步预览、逐行编辑和提交控件。"""

        super().__init__(database)
        self.lines: list[Any] = []
        self.location_boxes: dict[str, QComboBox] = {}
        self.preview_generation = 0
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "订单号与购买日期已隐藏。每行默认加入，可按推荐仓库或手工选择后一次性提交。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        controls = QHBoxLayout()
        back = QPushButton("返回入库")
        back.clicked.connect(self.back_requested.emit)
        choose = QPushButton("选择订单压缩包或页面")
        choose.clicked.connect(self.choose_file)
        self.match_mode = WheelSafeComboBox()
        self.match_mode.addItems(("仅匹配未匹配项", "匹配已勾选项", "全部重新匹配"))
        enrich = QPushButton("自动匹配并补充资料")
        enrich.clicked.connect(self.enrich_order_lines)
        cancel = QPushButton("取消导入")
        cancel.clicked.connect(self.cancel_import)
        confirm = QPushButton("确认勾选项入库")
        confirm.clicked.connect(self.commit)
        controls.addWidget(back)
        controls.addWidget(choose)
        controls.addWidget(self.match_mode)
        controls.addWidget(enrich)
        controls.addWidget(cancel)
        controls.addStretch()
        controls.addWidget(confirm)
        layout.addLayout(controls)
        self.summary = QLabel("尚未选择订单文件")
        layout.addWidget(self.summary)
        self.table = make_table(
            [
                "加入",
                "立创编号",
                "元件",
                "数量",
                "单价",
                "资料匹配",
                "位置 / 仓库",
                "详细信息",
            ]
        )
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(2, 180)
        self.table.setColumnWidth(6, 260)
        self.table.setColumnWidth(7, 320)
        self.table.cellDoubleClicked.connect(self.edit_line)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_context_menu)
        layout.addWidget(self.table)

    def reload(self) -> None:
        """仓库变化后重新计算订单草稿的推荐位置。"""

        if self.lines:
            self._show_preview(self.lines)

    def choose_file(self) -> None:
        """选择本地订单文件，并在工作线程中完成安全解析。"""

        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择保存的订单页面",
            "",
            "订单页面 (*.zip *.html *.htm *.mhtml *.mht)",
        )
        if not filename:
            return
        self.preview_generation += 1
        generation = self.preview_generation
        self.summary.setText("正在离线解析订单……")
        worker = Worker(lambda: parse_saved_orders(Path(filename)))
        worker.signals.succeeded.connect(
            lambda result, current=generation: self._show_preview_if_current(result, current)
        )
        worker.signals.failed.connect(
            lambda message, current=generation: self._show_parse_error_if_current(message, current)
        )
        self.pool.start(worker)

    def _show_preview_if_current(self, lines: Any, generation: int) -> None:
        """忽略用户点击取消后才返回的后台解析结果。"""

        if generation == self.preview_generation:
            self._show_preview(lines)

    def _show_parse_error_if_current(self, message: str, generation: int) -> None:
        """取消后不再弹出已经失效的解析任务错误。"""

        if generation == self.preview_generation:
            self.show_error(message)

    def cancel_import(self) -> None:
        """取消当前预览并使仍在运行的离线解析结果失效。"""

        self.preview_generation += 1
        self.lines.clear()
        self.location_boxes.clear()
        self.table.setRowCount(0)
        self.summary.setText("已取消订单导入")

    @staticmethod
    def _detail_summary(line: Any) -> str:
        """把独立类别参数压缩为预览文字。"""

        if line.resistor:
            values = [
                line.resistor.resistance,
                f"±{line.resistor.tolerance_percent:g}%"
                if line.resistor.tolerance_percent is not None
                else None,
                f"{line.resistor.power_mw:g}mW" if line.resistor.power_mw is not None else None,
            ]
            return " ".join(value for value in values if value)
        if line.mosfet:
            values = [
                f"{line.mosfet.channel_type}沟道" if line.mosfet.channel_type else None,
                f"{line.mosfet.mosfet_count}个" if line.mosfet.mosfet_count else None,
                f"{line.mosfet.voltage_v:g}V" if line.mosfet.voltage_v is not None else None,
                f"{line.mosfet.current_a:g}A" if line.mosfet.current_a is not None else None,
            ]
            return " ".join(value for value in values if value)
        return "—"

    def _show_preview(self, lines: Any) -> None:
        """以紧凑摘要显示订单，次要字段默认收进三角形详情区域。"""

        self.lines = list(lines)
        preview = self.database.preview_order_import(self.lines)
        locations = self.database.storage_priority_rows()
        self.location_boxes.clear()
        self.table.setRowCount(len(self.lines))
        for row, (line, _item) in enumerate(zip(self.lines, preview, strict=True)):
            import_key = f"{line.order_number}:{line.lcsc_part_number}"
            include = QTableWidgetItem()
            include.setFlags(include.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            include.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, include)
            values = [
                line.lcsc_part_number,
                line.name,
                line.quantity,
                line.unit_price,
                "已匹配" if line.jlc_matched else "未匹配",
            ]
            for column, value in enumerate(values, start=1):
                cell = QTableWidgetItem()
                if isinstance(value, (int, float)):
                    cell.setData(Qt.ItemDataRole.DisplayRole, value)
                else:
                    cell.setText(str(value))
                self.table.setItem(row, column, cell)
            recommendation = self.database.recommend_location(line.category)
            location = WheelSafeComboBox()
            for candidate in locations:
                location.addItem(
                    f"{candidate['warehouse_name']} / {candidate['location_name']}",
                    candidate["location_id"],
                )
            if recommendation:
                index = location.findData(recommendation["location_id"])
                location.setCurrentIndex(max(index, 0))
                location.setToolTip(f"推荐原因：{recommendation['reason']}")
                status = self.table.item(row, 5)
                if status:
                    status.setToolTip(f"仓库推荐：{recommendation['reason']}")
            self.table.setCellWidget(row, 6, location)
            self.location_boxes[import_key] = location
            details = CollapsibleSection("展开")
            detail_text = QLabel(
                "\n".join(
                    (
                        f"类别：{line.category or '—'}    品牌：{line.brand or '—'}",
                        f"封装：{line.package or '—'}    制造商料号：{line.manufacturer_part_number or '—'}",
                        f"描述：{line.description or '—'}",
                        f"类别参数：{self._detail_summary(line)}",
                        f"商品链接：{line.product_url or '—'}",
                    )
                )
            )
            detail_text.setWordWrap(True)
            details.setContentWidget(detail_text)
            details.toggle.toggled.connect(lambda _checked: self.table.resizeRowsToContents())
            self.table.setCellWidget(row, 7, details)
        orders = len({line.order_number for line in self.lines})
        self.summary.setText(
            f"解析到 {orders} 个订单、{len(self.lines)} 条商品；可逐行决定是否加入及目标仓库。"
        )

    def _preview_state(self) -> tuple[list[bool], dict[str, Any]]:
        """暂存勾选和仓库选择，供原位或完整编辑后重建预览。"""

        checked = []
        for index in range(len(self.lines)):
            item = self.table.item(index, 0)
            checked.append(bool(item and item.checkState() == Qt.CheckState.Checked))
        locations = {key: box.currentData() for key, box in self.location_boxes.items()}
        return checked, locations

    def _restore_preview_state(self, checked: list[bool], locations: dict[str, Any]) -> None:
        """重建预览后恢复用户已经作出的导入选择。"""

        for index, selected in enumerate(checked):
            item = self.table.item(index, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        for key, location_id in locations.items():
            box = self.location_boxes.get(key)
            if box and location_id:
                choice = box.findData(location_id)
                if choice >= 0:
                    box.setCurrentIndex(choice)

    def edit_line(self, row: int, column: int) -> None:
        """双击可编辑格时直接换成输入框，失焦后保存订单草稿。"""

        field_by_column = {
            2: "name",
            3: "quantity",
            4: "unit_price",
        }
        if row < 0 or row >= len(self.lines) or column not in field_by_column:
            return
        field = field_by_column[column]
        value = getattr(self.lines[row], field)
        editor = InlineEditor("" if value is None else str(value), self.table)
        editor.setFrame(False)
        self.table.setCellWidget(row, column, editor)
        editor.setFocus()
        editor.selectAll()
        checked, locations = self._preview_state()
        editor.cancelled.connect(
            lambda current=editor, item_row=row, item_column=column, saved_checked=checked, saved_locations=locations: (
                self._cancel_inline_order_edit(
                    current, item_row, item_column, saved_checked, saved_locations
                )
            )
        )

        def save() -> None:
            """把订单单元格的新值交给统一校验函数并重建预览。"""

            self._save_inline_order_line(editor, row, column, field)

        editor.editingFinished.connect(save)
        editor.focus_left.connect(save)

    def _save_inline_order_line(self, editor: QLineEdit, row: int, column: int, field: str) -> None:
        """点击其他位置时校验并保存订单草稿中的单个字段。"""

        if editor.property("saving") or editor.property("cancelled"):
            return
        editor.setProperty("saving", True)
        close_inline_editor(self.table, editor, row, column)
        if row < 0 or row >= len(self.lines):
            return
        checked, locations = self._preview_state()
        try:
            text = editor.text().strip()
            if field == "name" and not text:
                raise ValueError("元件名称不能为空")
            if field == "unit_price":
                value: Any = float(text)
                if value < 0:
                    raise ValueError("单价不能为负数")
            elif field == "quantity":
                value = int(text)
                if value <= 0:
                    raise ValueError("数量必须是正整数")
            else:
                value = text or None
            self.lines[row] = self.lines[row].model_copy(update={field: value})
            self._show_preview(self.lines)
            self._restore_preview_state(checked, locations)
        except Exception as error:
            self._show_preview(self.lines)
            self._restore_preview_state(checked, locations)
            self.show_error(str(error))

    def _cancel_inline_order_edit(
        self,
        editor: QLineEdit,
        row: int,
        column: int,
        checked: list[bool],
        locations: dict[str, Any],
    ) -> None:
        """取消订单单元格编辑并恢复勾选、仓库选择及原始文字。"""

        close_inline_editor(self.table, editor, row, column)
        self._show_preview(self.lines)
        self._restore_preview_state(checked, locations)

    def open_full_line_editor(self, row: int) -> None:
        """从右键菜单打开原有的整行订单编辑对话框。"""

        if row < 0 or row >= len(self.lines):
            return
        checked, locations = self._preview_state()
        dialog = OrderLineEditDialog(self.lines[row], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.lines[row] = dialog.edited_line
        self._show_preview(self.lines)
        self._restore_preview_state(checked, locations)

    def open_context_menu(self, position: Any) -> None:
        """订单右键菜单提供完整编辑对话框和商品链接。"""

        row = self.table.rowAt(position.y())
        if row < 0:
            return
        url = self.lines[row].product_url if 0 <= row < len(self.lines) else None
        menu = QMenu(self)
        edit = menu.addAction("完整编辑…")
        menu.addSeparator()
        open_link = menu.addAction("打开商品链接")
        open_link.setEnabled(bool(url))
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected == edit:
            self.open_full_line_editor(row)
        elif selected == open_link and url:
            QDesktopServices.openUrl(QUrl(str(url)))

    def enrich_order_lines(self) -> None:
        """遍历订单商品页，集中展示差异并把确认结果写回订单草稿。"""

        if not self.lines:
            self.show_error("请先选择并解析订单文件")
            return
        mode = self.match_mode.currentIndex()
        indexes = []
        for index, line in enumerate(self.lines):
            include = self.table.item(index, 0)
            if mode == 0 and line.jlc_matched:
                continue
            if mode == 1 and (include is None or include.checkState() != Qt.CheckState.Checked):
                continue
            indexes.append(index)
        targets = []
        for index in indexes:
            line = self.lines[index]
            url = line.product_url or (
                f"https://item.szlcsc.com/product/{line.lcsc_part_number}.html"
            )
            targets.append((str(index), line.name, url))
        if not targets:
            self.show_info("当前范围内没有需要匹配的订单元件")
            return
        checked, locations = self._preview_state()
        browser = JlcBatchBrowserDialog(targets, self)
        QTimer.singleShot(0, browser.start)
        if browser.exec() != QDialog.DialogCode.Accepted or not browser.drafts:
            return
        changes = []
        for index in indexes:
            key = str(index)
            draft = browser.drafts.get(key)
            if not draft:
                continue
            line = self.lines[index]
            current_fields = {
                "name": line.name,
                "lcsc_part_number": line.lcsc_part_number,
                "manufacturer_part_number": line.manufacturer_part_number or "",
                "brand": line.brand or "",
                "package": line.package or "",
                "category": line.category or "",
                "product_url": line.product_url or "",
                "datasheet_url": line.datasheet_url or "",
                "notes": line.description or "",
            }
            current_parameters = [
                {
                    "parameter_key": parameter.key,
                    "label": parameter.label,
                    "value": parameter.value,
                    "group_name": group.name,
                }
                for group in line.parameter_groups
                for parameter in group.parameters
            ]
            changes.append((key, line.name, draft, current_fields, current_parameters))
        confirmation = JlcBatchMergeDialog(changes, self)
        if confirmation.exec() != QDialog.DialogCode.Accepted:
            return
        for key, result in confirmation.result_data().items():
            index = int(key)
            line = self.lines[index]
            updates: dict[str, Any] = {"jlc_matched": True}
            for field in (
                "name",
                "lcsc_part_number",
                "manufacturer_part_number",
                "brand",
                "package",
                "category",
                "product_url",
                "datasheet_url",
            ):
                value = getattr(result, field)
                if value is not None:
                    updates[field] = value
            if result.notes is not None:
                updates["description"] = result.notes
            updates["parameter_groups"] = result.parameter_groups
            self.lines[index] = line.model_copy(update=updates)
        self._show_preview(self.lines)
        self._restore_preview_state(checked, locations)
        self.summary.setText(
            f"自动匹配完成：已读取 {len(browser.drafts)} 项，失败 {len(browser.errors)} 项；"
            "请核对勾选项后再入库。"
        )

    def commit(self) -> None:
        """只提交勾选行，并为每行使用其独立选择的仓库。"""

        selected = []
        overrides: dict[str, str] = {}
        for row, line in enumerate(self.lines):
            include = self.table.item(row, 0)
            if include is None or include.checkState() != Qt.CheckState.Checked:
                continue
            import_key = f"{line.order_number}:{line.lcsc_part_number}"
            location_id = self.location_boxes[import_key].currentData()
            if not location_id:
                self.show_error(f"{line.name} 尚未选择仓库")
                return
            selected.append(line)
            overrides[import_key] = str(location_id)
        if not selected:
            self.show_error("至少勾选一个需要加入库存的元件")
            return
        try:
            result = self.database.import_order_lines(selected, location_overrides=overrides)
            self.show_info(
                f"订单导入完成：入库 {result['imported']} 条，"
                f"新建元件 {result['created_components']} 个，跳过重复 {result['skipped']} 条。"
            )
            self._show_preview(self.lines)
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))


class IssuePage(BasePage):
    """按元件和可选仓库进行 FIFO 出库。"""

    open_bom = Signal()

    def __init__(self, database: Database, recognition: RecognitionEngine | None = None) -> None:
        """构建手工/FIFO 出库与实时扫码出库两条流程。

        可注入 ``recognition`` 便于共享模型会话和测试；缺省时创建延迟加载实例。
        """

        super().__init__(database)
        self.recognition = recognition or RecognitionEngine()
        self.selected_batch: dict[str, Any] | None = None
        self.camera: QCamera | None = None
        self.session: QMediaCaptureSession | None = None
        self.sink: QVideoSink | None = None
        self.scan_busy = False
        self.last_scan_at = 0.0
        self.last_camera_code: str | None = None
        self.scan_attempts = 0
        self.scan_received_frames = 0
        self.scan_worker: Worker | None = None
        layout = QVBoxLayout(self)
        open_bom = QPushButton("BOM 出库")
        open_bom.clicked.connect(self.open_bom.emit)
        layout.addWidget(open_bom)
        scan_group = QGroupBox("扫码出库")
        scan_layout = QVBoxLayout(scan_group)
        self.scan_preview = QLabel("摄像头未启动")
        self.scan_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scan_preview.setMinimumHeight(180)
        self.scan_preview.setStyleSheet("background:#17202a;color:#ccd6dd;border-radius:8px")
        scan_layout.addWidget(self.scan_preview)
        scan_row = QHBoxLayout()
        self.scan_text = QLineEdit()
        self.scan_text.setPlaceholderText("也可粘贴立创二维码原文或 CL 批次码")
        self.camera_choice = WheelSafeComboBox()
        self.camera_choice.setMinimumWidth(150)
        self._refresh_camera_choices()
        resolve = QPushButton("解析二维码")
        resolve.clicked.connect(self.resolve_manual_scan)
        start = QPushButton("打开摄像头")
        start.clicked.connect(self.start_camera)
        stop = QPushButton("关闭摄像头")
        stop.clicked.connect(self.stop_camera)
        scan_row.addWidget(self.scan_text)
        scan_row.addWidget(self.camera_choice)
        scan_row.addWidget(resolve)
        scan_row.addWidget(start)
        scan_row.addWidget(stop)
        scan_layout.addLayout(scan_row)
        self.scan_result = QGroupBox("扫码出库确认")
        result_form = QFormLayout(self.scan_result)
        self.scan_status = QLabel()
        self.scan_status.setWordWrap(True)
        self.scan_code_info = QLabel()
        self.scan_code_info.setWordWrap(True)
        self.scan_component_info = QLabel()
        self.scan_component_info.setWordWrap(True)
        self.scan_batch_info = QLabel()
        self.scan_batch_info.setWordWrap(True)
        self.scan_stock_info = QLabel()
        self.scan_stock_info.setWordWrap(True)
        self.scan_location = WheelSafeComboBox()
        self.scan_location.currentIndexChanged.connect(self._scan_location_changed)
        self.scan_quantity = WheelSafeSpinBox()
        self.scan_quantity.setRange(1, 1)
        self.scan_quantity.setEnabled(False)
        self.scan_notes = QLineEdit()
        self.scan_notes.setPlaceholderText("可选，填写用途或异常说明")
        self.scan_confirm = QPushButton("确认扫码出库")
        self.scan_confirm.setEnabled(False)
        self.scan_confirm.clicked.connect(self.submit_scan)
        result_form.addRow("识别状态", self.scan_status)
        result_form.addRow("二维码", self.scan_code_info)
        result_form.addRow("元件", self.scan_component_info)
        result_form.addRow("批次", self.scan_batch_info)
        result_form.addRow("库存分布", self.scan_stock_info)
        result_form.addRow("出库仓库", self.scan_location)
        result_form.addRow("出库数量", self.scan_quantity)
        result_form.addRow("用途 / 异常备注", self.scan_notes)
        result_form.addRow(self.scan_confirm)
        self.scan_result.hide()
        scan_layout.addWidget(self.scan_result)
        layout.addWidget(scan_group)
        form = QFormLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入名称、型号、品牌、封装或立创编号")
        self.search.textChanged.connect(self._reload_components)
        self.component = WheelSafeComboBox()
        self.component.hide()
        self.component_label = QLabel("匹配结果")
        self.component_label.hide()
        self.component.currentIndexChanged.connect(self._reload_locations)
        self.location = WheelSafeComboBox()
        self.quantity = WheelSafeSpinBox()
        self.quantity.setRange(1, 1_000_000_000)
        self.notes = QLineEdit()
        button = QPushButton("确认出库")
        button.clicked.connect(self.submit)
        form.addRow("搜索元件", self.search)
        form.addRow(self.component_label, self.component)
        form.addRow("有库存的仓库", self.location)
        form.addRow("数量", self.quantity)
        form.addRow("用途 / 备注", self.notes)
        form.addRow(button)
        layout.addLayout(form)
        layout.addStretch()
        self.reload()

    def start_camera(self) -> None:
        """打开默认摄像头并立即识别，识别成功后自动停止。"""

        self._refresh_camera_choices()
        devices = QMediaDevices.videoInputs()
        if not devices:
            self.scan_result.show()
            self.scan_code_info.setText("等待识别")
            self._show_scan_error("没有发现摄像头，请检查权限或粘贴二维码内容")
            self.show_error("没有发现摄像头，可粘贴二维码内容后解析")
            return
        self.stop_camera()
        # 每次用户主动打开摄像头都是新会话；同一会话内的连续帧只触发一次。
        self.last_camera_code = None
        self.scan_busy = False
        self.last_scan_at = 0.0
        self.scan_attempts = 0
        self.scan_received_frames = 0
        self.scan_worker = None
        self.scan_preview.setPixmap(QPixmap())
        self.scan_preview.setText("摄像头已打开，正在自动识别二维码…")
        self.selected_batch = None
        self.scan_result.show()
        self.scan_status.setText("正在自动识别，请将完整二维码放入画面中央并保持清晰")
        self.scan_status.setStyleSheet("color:#c58b00;font-weight:600")
        self.scan_code_info.setText("等待识别…")
        self.scan_component_info.setText("-")
        self.scan_batch_info.setText("-")
        self.scan_stock_info.setText("-")
        self.scan_location.clear()
        self.scan_quantity.setEnabled(False)
        self.scan_confirm.setEnabled(False)
        device_index = max(0, min(self.camera_choice.currentIndex(), len(devices) - 1))
        device = devices[device_index]
        self.camera = QCamera(device)
        formats = list(device.videoFormats())
        if formats:
            suitable = [
                item
                for item in formats
                if item.resolution().width() * item.resolution().height() <= 1920 * 1080
            ]
            candidates = suitable or formats
            best = max(
                candidates,
                key=lambda item: (
                    item.resolution().width() * item.resolution().height(),
                    item.maxFrameRate(),
                ),
            )
            self.camera.setCameraFormat(best)
        if self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAutoNear):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAutoNear)
        elif self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAuto):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAuto)
        self.camera.errorOccurred.connect(self._camera_failed)
        self.session = QMediaCaptureSession()
        self.sink = QVideoSink()
        self.session.setCamera(self.camera)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._scan_frame)
        self.camera.start()

    def _refresh_camera_choices(self) -> None:
        """刷新摄像头选择，避免多摄像头设备时固定打开错误镜头。"""

        selected = self.camera_choice.currentText() if self.camera_choice.count() else ""
        self.camera_choice.clear()
        devices = QMediaDevices.videoInputs()
        for device in devices:
            self.camera_choice.addItem(device.description())
        if not devices:
            self.camera_choice.addItem("未检测到摄像头")
            self.camera_choice.setEnabled(False)
            return
        self.camera_choice.setEnabled(True)
        previous = self.camera_choice.findText(selected)
        if previous >= 0:
            self.camera_choice.setCurrentIndex(previous)

    def _camera_failed(self, _error: Any, message: str) -> None:
        """在页面中显示摄像头驱动或权限错误。"""

        self.selected_batch = None
        self.scan_result.show()
        self.scan_code_info.setText("未获取")
        self._show_scan_error(f"摄像头异常：{message or '无法读取视频帧'}")

    def stop_camera(self) -> None:
        """停止摄像头并释放会话与帧接收器引用；重复调用是安全的。"""

        if self.camera:
            self.camera.stop()
        self.camera = None
        self.session = None
        self.sink = None

    def _scan_frame(self, frame: QVideoFrame) -> None:
        """显示实时帧，在主线程固化图像后交给后台解码。"""

        image = frame.toImage()
        if image.isNull():
            return
        self.scan_received_frames += 1
        pixmap = QPixmap.fromImage(image).scaled(
            self.scan_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.scan_preview.setPixmap(pixmap)
        now = time.monotonic()
        if self.scan_busy:
            if self.scan_received_frames % 10 == 0:
                self.scan_status.setText(
                    f"摄像头已接收 {self.scan_received_frames} 帧，"
                    f"正在处理第 {self.scan_attempts} 次识别"
                )
            return
        if now - self.last_scan_at < 0.35:
            return
        self.last_scan_at = now
        # QVideoFrame.toImage() 返回的 QImage 可能仍引用视频缓冲区，且 Qt 图像
        # 编码器不应在工作线程中读取该对象。先在主线程生成独立 PNG
        # 字节，避免帧回调返回后底层缓冲区失效导致始终识别不到。
        try:
            image_bytes = ScanPage._image_bytes(image.copy())
        except Exception as error:
            self._barcode_decode_failed(str(error))
            return
        self.scan_attempts += 1
        self.scan_status.setText(
            f"正在自动识别（已检查 {self.scan_attempts} 帧），"
            "请将完整二维码放入画面中央"
        )
        self.scan_status.setStyleSheet("color:#c58b00;font-weight:600")
        self.scan_busy = True
        self.scan_worker = Worker(lambda: self.recognition.decode_barcode(image_bytes))
        self.scan_worker.signals.succeeded.connect(self._barcode_decoded)
        self.scan_worker.signals.failed.connect(self._barcode_decode_failed)
        self.scan_worker.signals.finished.connect(self._scan_decode_finished)
        self.pool.start(self.scan_worker)

    def _scan_decode_finished(self) -> None:
        """释放实时解码忙碌状态，使后续摄像头帧能继续进入识别。"""

        self.scan_busy = False
        self.scan_worker = None

    def _barcode_decode_failed(self, message: str) -> None:
        """展示解码依赖或图像处理异常，避免静默失败。"""

        self.scan_result.show()
        self.scan_status.setText(f"二维码解码异常：{message}")
        self.scan_status.setStyleSheet("color:#b42318;font-weight:600")

    def _barcode_decoded(self, raw_barcode: str | None) -> None:
        """去重有效条码、解析批次并在首次识别后关闭摄像头。"""

        normalized = raw_barcode.strip() if raw_barcode else ""
        if not normalized:
            self.scan_status.setText(
                f"摄像头工作正常，已接收 {self.scan_received_frames} 帧，"
                f"已检查 {self.scan_attempts} 帧，"
                "尚未检测到二维码"
            )
            self.scan_status.setStyleSheet("color:#c58b00;font-weight:600")
            return
        if normalized == self.last_camera_code:
            return
        # 先记录再解析，避免无效码在每一帧中反复触发异常。
        self.last_camera_code = normalized
        self.scan_text.setText(normalized)
        self.resolve_scan(normalized)
        # 解码成功即停止摄像头；批次未入库属于库存匹配问题，
        # 不应继续对同一二维码反复解码。
        self.stop_camera()

    def resolve_manual_scan(self) -> None:
        """解析粘贴的二维码文本，空输入不发起无效查询。"""

        raw_text = self.scan_text.text().strip()
        if not raw_text:
            self.scan_result.show()
            self.scan_status.setText("请先粘贴二维码原文，或打开摄像头自动识别")
            self.scan_status.setStyleSheet("color:#c58b00;font-weight:600")
            self.scan_code_info.setText("未输入二维码内容")
            self.scan_component_info.setText("-")
            self.scan_batch_info.setText("-")
            self.scan_stock_info.setText("-")
            self.scan_location.clear()
            self.scan_quantity.setEnabled(False)
            self.scan_confirm.setEnabled(False)
            return
        self.resolve_scan(raw_text)

    def resolve_scan(self, raw_text: str) -> bool:
        """解析二维码并在扫码确认区展示对应库存批次。

        :param raw_text: 摄像头或外部扫码器提供的二维码原文/内部批次码
        :return: 找到可出库批次时返回 ``True``，否则返回 ``False``

        注意：这里只解析和查询，不扣减库存；所有失败信息都会显示在确认区，用户仍需
        选择库位、数量并点击确认出库。
        """

        ### 解析二维码中的可读字段 ###
        raw_text = raw_text.strip()
        self.scan_result.show()
        parsed_fields = parse_qr_payload(raw_text)
        details = []
        for key, label in (
            ("order_number", "订单号"),
            ("lcsc_part_number", "立创编号"),
            ("manufacturer_part_number", "型号"),
            ("quantity", "标签数量"),
        ):
            if field := parsed_fields.get(key):
                details.append(f"{label}：{field.normalized_value}")
        self.scan_code_info.setText(
            "\n".join([raw_text or "（空）", *details])
        )
        try:
            batch = self.database.resolve_batch_code(raw_text)
            self.selected_batch = batch
            self._show_scan_batch(batch)
            return True
        except DomainError as error:
            self.selected_batch = None
            recognized = bool(parsed_fields) or bool(CUSTOM_BATCH_PATTERN.fullmatch(raw_text))
            if recognized and error.code in {"NOT_FOUND", "CONFLICT", "INSUFFICIENT_STOCK"}:
                self._show_scan_inventory_error(parsed_fields, str(error))
            else:
                self._show_scan_error(str(error))
            return False
        except Exception as error:
            self.selected_batch = None
            self._show_scan_error(str(error))
            return False

    def _show_scan_inventory_error(
        self, parsed_fields: dict[str, Any], message: str
    ) -> None:
        """区分“二维码已识别”和“库存批次不可出库”。"""

        self.scan_status.setText(f"二维码识别成功，但无法出库：{message}")
        self.scan_status.setStyleSheet("color:#c58b00;font-weight:600")
        part = parsed_fields.get("lcsc_part_number")
        model = parsed_fields.get("manufacturer_part_number")
        self.scan_component_info.setText(
            "；".join(
                value
                for value in (
                    f"立创编号：{part.normalized_value}" if part else "",
                    f"型号：{model.normalized_value}" if model else "",
                )
                if value
            )
            or "自定义批次码已识别"
        )
        order = parsed_fields.get("order_number")
        quantity = parsed_fields.get("quantity")
        self.scan_batch_info.setText(
            "；".join(
                value
                for value in (
                    f"订单号：{order.normalized_value}" if order else "",
                    f"标签数量：{quantity.normalized_value}" if quantity else "",
                )
                if value
            )
            or "未找到批次资料"
        )
        self.scan_stock_info.setText("未关联到可出库库存，请先完成该批次入库")
        self.scan_location.clear()
        self.scan_quantity.setRange(1, 1)
        self.scan_quantity.setValue(1)
        self.scan_quantity.setEnabled(False)
        self.scan_confirm.setEnabled(False)

    def _show_scan_batch(self, batch: dict[str, Any], message: str | None = None) -> None:
        """在扫码区持续展示批次详情和可扣减的仓库余量。"""

        remaining = int(batch["remaining_quantity"])
        self.scan_status.setText(message or "识别成功，请选择仓库和出库数量后确认")
        self.scan_status.setStyleSheet("color:#18794e;font-weight:600")
        self.scan_component_info.setText(
            f"{batch['component_name']}（ID：{batch['component_id']}）"
        )
        source_names = {"jlc": "立创", "custom": "自定义"}
        source = source_names.get(str(batch.get("source_type")), str(batch.get("source_type") or "未知"))
        label = batch.get("custom_code") or (
            f"订单 {batch.get('order_number') or '-'} / 立创编号 "
            f"{batch.get('lcsc_part_number') or '-'}"
        )
        self.scan_batch_info.setText(
            f"{label}；来源：{source}；原始数量：{int(batch['initial_quantity'])}；"
            f"剩余数量：{remaining}"
        )
        active_locations = [
            item for item in batch["locations"] if int(item["remaining_quantity"]) > 0
        ]
        self.scan_stock_info.setText(
            "；".join(
                f"{item['warehouse_name']} / {item['location_name']}："
                f"{int(item['remaining_quantity'])}"
                for item in active_locations
            )
            or "无可用库存"
        )
        self.scan_location.blockSignals(True)
        self.scan_location.clear()
        self.scan_location.addItem(f"全部仓库（批次总余量 {remaining}）", None)
        for item in active_locations:
            self.scan_location.addItem(
                f"{item['warehouse_name']} / {item['location_name']}"
                f"（{int(item['remaining_quantity'])}）",
                item["location_id"],
            )
        self.scan_location.blockSignals(False)
        self.scan_quantity.setEnabled(remaining > 0)
        self.scan_quantity.setRange(1, max(1, remaining))
        self.scan_quantity.setValue(max(1, remaining))
        self.scan_confirm.setEnabled(remaining > 0)

    def _show_scan_error(self, message: str) -> None:
        """将识别或库存异常保留在扫码区，便于用户处理。"""

        self.scan_status.setText(f"识别异常：{message}")
        self.scan_status.setStyleSheet("color:#b42318;font-weight:600")
        self.scan_component_info.setText("-")
        self.scan_batch_info.setText("-")
        self.scan_stock_info.setText("-")
        self.scan_location.clear()
        self.scan_quantity.setRange(1, 1)
        self.scan_quantity.setValue(1)
        self.scan_quantity.setEnabled(False)
        self.scan_confirm.setEnabled(False)

    def _scan_location_changed(self) -> None:
        """切换仓库时按该仓库的批次余量限制数量。"""

        if not self.selected_batch:
            return
        location_id = self.scan_location.currentData()
        available = int(self.selected_batch["remaining_quantity"])
        if location_id is not None:
            available = next(
                (
                    int(item["remaining_quantity"])
                    for item in self.selected_batch["locations"]
                    if item["location_id"] == location_id
                ),
                0,
            )
        self.scan_quantity.setRange(1, max(1, available))
        self.scan_quantity.setValue(max(1, available))
        self.scan_quantity.setEnabled(available > 0)
        self.scan_confirm.setEnabled(available > 0)

    def reload(self) -> None:
        """刷新选择数据。"""

        self._reload_components()

    def _reload_components(self) -> None:
        """根据输入内容即时搜索匹配元件。"""

        query = self.search.text().strip()
        selected = self.component.currentData()
        self.component.blockSignals(True)
        self.component.clear()
        for item in self.database.list_components(query) if query else []:
            self.component.addItem(
                f"{item['name']} · {item.get('brand') or '无品牌'} · {item.get('package') or '无封装'}",
                item["id"],
            )
        index = self.component.findData(selected)
        self.component.setCurrentIndex(index if index >= 0 else 0)
        self.component.blockSignals(False)
        show_matches = bool(query and self.component.count())
        self.component.setVisible(show_matches)
        self.component_label.setVisible(show_matches)
        self._reload_locations()

    def _reload_locations(self) -> None:
        """只展示所选元件实际有库存的仓库，并自动选择 FIFO 建议。"""

        component_id = self.component.currentData()
        stock = [
            item
            for item in self.database.list_stock()
            if item["component_id"] == component_id and int(item["quantity"]) > 0
        ]
        self.location.clear()
        total = sum(int(item["quantity"]) for item in stock)
        # 留空表示自动跨仓库 FIFO，避免默认值看起来像一个真实仓库。
        self.location.addItem("", None)
        self.location.setToolTip(f"留空时自动跨仓库 FIFO（共 {total}）")
        for item in stock:
            self.location.addItem(
                f"{item['warehouse_name']} / {item['location_name']}（{int(item['quantity'])}）",
                item["location_id"],
            )

    def submit(self) -> None:
        """提交手工出库并显示库存不足等业务错误。"""

        try:
            self.database.issue(
                IssueRequest(
                    component_id=str(self.component.currentData()),
                    quantity=self.quantity.value(),
                    location_id=self.location.currentData(),
                    batch_id=None,
                    source="手工出库",
                    notes=self.notes.text() or None,
                )
            )
            self._reset_manual_issue()
            self.show_info("出库完成")
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def _reset_manual_issue(self) -> None:
        """手工出库成功后清除元件、仓库、数量和备注。"""

        self.search.clear()
        self.component.clear()
        self.component.hide()
        self.component_label.hide()
        self.location.clear()
        self.quantity.setValue(1)
        self.notes.clear()

    def _reset_scan_issue(self, message: str) -> None:
        """扫码出库成功后清除可重复提交的批次参数。"""

        self.selected_batch = None
        self.scan_text.clear()
        self.scan_code_info.setText("-")
        self.scan_component_info.setText("-")
        self.scan_batch_info.setText("-")
        self.scan_stock_info.setText("-")
        self.scan_location.clear()
        self.scan_quantity.setRange(1, 1)
        self.scan_quantity.setValue(1)
        self.scan_quantity.setEnabled(False)
        self.scan_notes.clear()
        self.scan_confirm.setEnabled(False)
        self.scan_status.setText(message)
        self.scan_status.setStyleSheet("color:#18794e;font-weight:600")

    def submit_scan(self) -> None:
        """经用户确认后只扣减当前扫描到的批次。"""

        if not self.selected_batch:
            self._show_scan_error("请先扫描并识别有效的批次二维码")
            return
        batch_id = str(self.selected_batch["id"])
        quantity = self.scan_quantity.value()
        try:
            self.database.issue(
                IssueRequest(
                    component_id=str(self.selected_batch["component_id"]),
                    quantity=quantity,
                    location_id=self.scan_location.currentData(),
                    batch_id=batch_id,
                    source="扫码出库",
                    notes=self.scan_notes.text() or None,
                )
            )
            updated = self.database.get_batch(batch_id)
            remaining = int(updated["remaining_quantity"])
            self._reset_scan_issue(
                f"出库完成：本次 {quantity}，"
                + (f"批次剩余 {remaining}" if remaining else "该批次已全部出库")
            )
            self.changed.emit()
        except Exception as error:
            self.scan_status.setText(f"出库失败：{error}")
            self.scan_status.setStyleSheet("color:#b42318;font-weight:600")


class LocationsPage(BasePage):
    """维护上级位置和其中的具体仓库，并集中配置推荐优先级。"""

    def __init__(self, database: Database) -> None:
        """构建位置、仓库新增区和可原位编辑的层级列表。"""

        super().__init__(database)
        layout = QVBoxLayout(self)
        warehouse_row = QHBoxLayout()
        self.warehouse_name = QLineEdit()
        self.warehouse_name.setPlaceholderText("新位置名称")
        warehouse_button = QPushButton("添加位置")
        warehouse_button.clicked.connect(self.add_warehouse)
        warehouse_row.addWidget(self.warehouse_name)
        warehouse_row.addWidget(warehouse_button)
        layout.addLayout(warehouse_row)
        location_row = QHBoxLayout()
        self.warehouse = WheelSafeComboBox()
        self.location_name = QLineEdit()
        self.location_name.setPlaceholderText("新仓库名称")
        location_button = QPushButton("添加仓库")
        location_button.clicked.connect(self.add_location)
        location_row.addWidget(self.warehouse)
        location_row.addWidget(self.location_name)
        location_row.addWidget(location_button)
        layout.addLayout(location_row)
        priority = QPushButton("配置整体仓库优先级")
        priority.clicked.connect(lambda: self.open_priority_dialog(None))
        layout.addWidget(priority)
        self.table = make_table(["位置", "仓库", "备注"])
        self.table.cellDoubleClicked.connect(self.edit_item)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_context_menu)
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        """刷新位置、仓库和选择框。"""

        warehouses = self.database.list_warehouses()
        names = {item["id"]: item["name"] for item in warehouses}
        self.warehouse.clear()
        for item in warehouses:
            self.warehouse.addItem(item["name"], item["id"])
        locations = self.database.list_locations()
        self.table.setSortingEnabled(False)
        fill_table(
            self.table,
            [
                [names.get(item["warehouse_id"], "?"), item["name"], item.get("notes") or ""]
                for item in locations
            ],
        )
        for row, item in enumerate(locations):
            position_cell = self.table.item(row, 0)
            storage_cell = self.table.item(row, 1)
            if position_cell:
                position_cell.setData(Qt.ItemDataRole.UserRole, item["warehouse_id"])
            if storage_cell:
                storage_cell.setData(Qt.ItemDataRole.UserRole, item["id"])
        self.table.setSortingEnabled(True)

    def add_warehouse(self) -> None:
        """创建上级位置。"""

        try:
            self.database.create_warehouse(WarehouseCreate(name=self.warehouse_name.text()))
            self.warehouse_name.clear()
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def add_location(self) -> None:
        """在当前上级位置中创建具体仓库。"""

        try:
            self.database.create_location(
                LocationCreate(
                    warehouse_id=str(self.warehouse.currentData()), name=self.location_name.text()
                )
            )
            self.location_name.clear()
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def delete_warehouse(self, position_id: str | None = None) -> None:
        """通过右键删除位置；仍有下级仓库时业务层会拒绝。"""

        position_id = position_id or self.warehouse.currentData()
        if not position_id:
            self.show_error("请先选择位置")
            return
        if (
            QMessageBox.question(self, "删除位置", "确定删除当前所选位置吗？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.database.delete_warehouse(str(position_id))
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def delete_location(self, location_id: str | None = None, name: str = "") -> None:
        """通过右键删除仓库；已有库存历史时拒绝。"""

        if not location_id:
            self.show_error("请先在表格中选择仓库")
            return
        if (
            QMessageBox.question(self, "删除仓库", f"确定删除“{name or '所选仓库'}”吗？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.database.delete_location(str(location_id))
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))

    def edit_item(self, row: int, column: int) -> None:
        """双击位置、仓库或备注格后原位编辑，失焦时自动保存。"""

        if row < 0 or column not in (0, 1, 2):
            return
        cell = self.table.item(row, column)
        if cell is None:
            return
        editor = InlineEditor(cell.text(), self.table)
        editor.setFrame(False)
        self.table.setCellWidget(row, column, editor)
        editor.setFocus()
        editor.selectAll()
        editor.cancelled.connect(
            lambda current=editor, item_row=row, item_column=column: self._cancel_inline_storage(
                current, item_row, item_column
            )
        )

        def save() -> None:
            """把位置/仓库单元格的新值交给统一保存流程。"""

            self._save_inline_storage(editor, row, column)

        editor.editingFinished.connect(save)
        editor.focus_left.connect(save)

    def _save_inline_storage(self, editor: QLineEdit, row: int, column: int) -> None:
        """保存位置或仓库的单格修改，并保持所属关系与其他字段不变。"""

        if editor.property("saving") or editor.property("cancelled"):
            return
        editor.setProperty("saving", True)
        close_inline_editor(self.table, editor, row, column)
        position_cell = self.table.item(row, 0)
        storage_cell = self.table.item(row, 1)
        position_id = position_cell.data(Qt.ItemDataRole.UserRole) if position_cell else None
        location_id = storage_cell.data(Qt.ItemDataRole.UserRole) if storage_cell else None
        try:
            text = editor.text().strip()
            if column in (0, 1) and not text:
                raise ValueError("名称不能为空")
            if column == 0 and position_id:
                current = next(
                    item for item in self.database.list_warehouses() if item["id"] == position_id
                )
                self.database.update_warehouse(
                    str(position_id), WarehouseCreate(name=text, notes=current.get("notes"))
                )
            elif location_id:
                current = next(
                    item for item in self.database.list_locations() if item["id"] == location_id
                )
                self.database.update_location(
                    str(location_id),
                    LocationCreate(
                        warehouse_id=str(current["warehouse_id"]),
                        name=text if column == 1 else str(current["name"]),
                        notes=(text or None) if column == 2 else current.get("notes"),
                    ),
                )
            self.refresh()
            self.changed.emit()
        except Exception as error:
            self.refresh()
            self.show_error(str(error))

    def _cancel_inline_storage(self, editor: QLineEdit, row: int, column: int) -> None:
        """移除仓库单元格编辑器，并恢复数据库中的原始文字。"""

        close_inline_editor(self.table, editor, row, column)
        self.refresh()

    def open_full_item_editor(self, row: int, column: int) -> None:
        """从右键菜单打开原有的位置或仓库完整编辑对话框。"""

        if column == 0:
            cell = self.table.item(row, 0)
            position_id = str(cell.data(Qt.ItemDataRole.UserRole)) if cell else ""
            current = next(
                (item for item in self.database.list_warehouses() if item["id"] == position_id),
                None,
            )
            if not current:
                return
            name, accepted = QInputDialog.getText(
                self, "修改位置", "位置名称：", text=str(current["name"])
            )
            if accepted and name.strip():
                try:
                    self.database.update_warehouse(
                        position_id,
                        WarehouseCreate(name=name.strip(), notes=current.get("notes")),
                    )
                    self.refresh()
                    self.changed.emit()
                except Exception as error:
                    self.show_error(str(error))
            return
        cell = self.table.item(row, 1)
        location_id = str(cell.data(Qt.ItemDataRole.UserRole)) if cell else ""
        current = next(
            (item for item in self.database.list_locations() if item["id"] == location_id), None
        )
        if not current:
            return
        dialog = StorageEditDialog(self.database, current, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.database.update_location(
                    location_id,
                    LocationCreate(
                        warehouse_id=str(dialog.position.currentData()),
                        name=dialog.name.text(),
                        notes=dialog.notes.text() or None,
                    ),
                )
                self.refresh()
                self.changed.emit()
            except Exception as error:
                self.show_error(str(error))

    def open_context_menu(self, position: Any) -> None:
        """右键提供完整编辑、删除及元件类别优先级配置。"""

        row = self.table.rowAt(position.y())
        if row >= 0:
            self.table.selectRow(row)
        menu = QMenu(self)
        edit_position = menu.addAction("完整编辑位置…")
        edit_storage = menu.addAction("完整编辑仓库…")
        menu.addSeparator()
        action = menu.addAction("配置元件类别仓库优先级…")
        menu.addSeparator()
        delete_storage = menu.addAction("删除仓库")
        delete_position = menu.addAction("删除位置")
        storage_cell = self.table.item(row, 1) if row >= 0 else None
        position_cell = self.table.item(row, 0) if row >= 0 else None
        edit_position.setEnabled(position_cell is not None)
        edit_storage.setEnabled(storage_cell is not None)
        delete_storage.setEnabled(storage_cell is not None)
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected == edit_position and row >= 0:
            self.open_full_item_editor(row, 0)
        elif selected == edit_storage and row >= 0:
            self.open_full_item_editor(row, 1)
        elif selected == action:
            category, accepted = QInputDialog.getText(self, "类别优先级", "元件类别名称：")
            if accepted and category.strip():
                self.open_priority_dialog(category.strip())
        elif selected == delete_storage and storage_cell:
            self.delete_location(
                str(storage_cell.data(Qt.ItemDataRole.UserRole)), storage_cell.text()
            )
        elif selected == delete_position:
            position_id = (
                str(position_cell.data(Qt.ItemDataRole.UserRole))
                if position_cell
                else str(self.warehouse.currentData() or "")
            )
            self.delete_warehouse(position_id)

    def open_priority_dialog(self, category: str | None) -> None:
        """打开整体或类别仓库优先级配置。"""

        StoragePriorityDialog(self.database, category, self).exec()


class BomFieldMappingDialog(QDialog):
    """当立创 EDA 自定义了导出标题时，让用户明确映射原始列。"""

    FIELDS = (
        ("quantity", "数量"),
        ("name", "名称 / Comment"),
        ("specification", "规格 / Value"),
        ("designators", "位号 / Designator"),
        ("footprint", "封装 / Footprint"),
        ("lcsc_part_number", "立创编号 / Supplier Part"),
        ("manufacturer_part_number", "制造商料号"),
        ("custom_part_number", "自定义编号"),
        ("manufacturer", "制造商 / 品牌"),
    )

    def __init__(self, headers: list[str], parent: QWidget) -> None:
        """为每个目标字段创建原始表头选择器，并预选可识别别名。"""

        super().__init__(parent)
        self.setWindowTitle("确认 BOM 字段映射")
        self.selectors: dict[str, QComboBox] = {}
        form = QFormLayout(self)
        for field, label in self.FIELDS:
            selector = WheelSafeComboBox()
            selector.addItem("不映射", None)
            for header in headers:
                selector.addItem(header, header)
            detected = next(
                (
                    header
                    for header in headers
                    if ALIASES.get(header.strip().casefold()) == field
                ),
                None,
            )
            if detected:
                selector.setCurrentIndex(selector.findData(detected))
            self.selectors[field] = selector
            form.addRow(label, selector)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def mapping(self) -> dict[str, str]:
        """返回“原始表头到内部字段名”的非空映射。"""

        return {
            str(selector.currentData()): field
            for field, selector in self.selectors.items()
            if selector.currentData()
        }


class BomPage(BasePage):
    """BOM 文件解析、候选预览与原子出库页面。"""

    back_requested = Signal()

    def __init__(self, database: Database) -> None:
        """构建 BOM 选择、匹配候选、缺料预览和原子提交区域。"""

        super().__init__(database)
        self.rows: list[Any] = []
        self.preview: list[dict[str, Any]] = []
        self.source: Path | None = None
        self.sheet_name: str | None = None
        self._mapping_attempted = False
        layout = QVBoxLayout(self)
        back = QPushButton("返回出库")
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back)
        choose = QPushButton("选择 CSV / XLSX")
        choose.clicked.connect(self.choose)
        layout.addWidget(choose)
        self.table = make_table(
            ["行", "物料", "需求", "匹配候选", "置信度 / 原因", "可用", "缺料", "记住匹配"]
        )
        layout.addWidget(self.table)
        self.commit = QPushButton("确认 BOM 出库")
        self.commit.setEnabled(False)
        self.commit.clicked.connect(self.commit_bom)
        layout.addWidget(self.commit)

    def choose(self) -> None:
        """选择并解析 BOM；文件读取在线程池中执行。"""

        filename, _ = QFileDialog.getOpenFileName(self, "选择 BOM", "", "BOM (*.csv *.xlsx)")
        if not filename:
            return
        source = Path(filename)
        self.source = source
        self._mapping_attempted = False
        sheet_name: str | None = None
        if source.suffix.casefold() == ".xlsx":
            from openpyxl import load_workbook

            workbook = load_workbook(source, read_only=True, data_only=True)
            if len(workbook.sheetnames) > 1:
                selected, accepted = QInputDialog.getItem(
                    self, "选择工作表", "工作表", workbook.sheetnames, 0, False
                )
                if not accepted:
                    return
                sheet_name = selected
        self.sheet_name = sheet_name
        worker = Worker(lambda: (parse_bom(source, sheet_name),))
        worker.signals.succeeded.connect(self._parsed)
        worker.signals.failed.connect(self.show_error)
        self.pool.start(worker)

    def _parsed(self, result: Any) -> None:
        """显示解析和匹配结果。"""

        self.rows = result[0]
        if (
            self.rows
            and not self._mapping_attempted
            and all(row.errors for row in self.rows)
            and self.source is not None
        ):
            dialog = BomFieldMappingDialog(list(self.rows[0].raw_fields), self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._mapping_attempted = True
                source = self.source
                mapping = dialog.mapping()
                worker = Worker(
                    lambda: (
                        parse_bom(source, self.sheet_name, mapping),
                    )
                )
                worker.signals.succeeded.connect(self._parsed)
                worker.signals.failed.connect(self.show_error)
                self.pool.start(worker)
                return
        self.preview = preview_bom(self.database, self.rows)
        display = []
        for item in self.preview:
            source = item["input"]
            name = (
                source.get("name")
                or source.get("lcsc_part_number")
                or source.get("manufacturer_part_number")
            )
            display.append(
                [
                    source["row_number"],
                    name,
                    item["required_quantity"],
                    "",
                    f"{item['confidence']} · {item['reason']}",
                    int(item["available_quantity"]),
                    int(item["shortage"]),
                    "",
                ]
            )
        fill_table(self.table, display)
        all_components = self.database.list_components()
        for row_index, item in enumerate(self.preview):
            selector = WheelSafeComboBox()
            selector.setEditable(True)
            selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            selector.addItem("请选择元件", None)
            candidate_ids = {
                str(candidate["component"]["id"]) for candidate in item["candidates"]
            }
            ordered = [candidate["component"] for candidate in item["candidates"]]
            ordered.extend(
                component for component in all_components if str(component["id"]) not in candidate_ids
            )
            for component in ordered:
                selector.addItem(
                    f"{component['name']} · {component.get('package') or '无封装'}",
                    str(component["id"]),
                )
            selected_index = selector.findData(item["selected_component_id"])
            selector.setCurrentIndex(selected_index if selected_index >= 0 else 0)
            selector.currentIndexChanged.connect(
                lambda _index, row=row_index, widget=selector: self._candidate_changed(
                    row, widget.currentData()
                )
            )
            self.table.setCellWidget(row_index, 3, selector)
            remember = QCheckBox()
            remember.setChecked(item["match_status"] != "auto")
            remember.setEnabled(not bool(item["input"].get("errors")))
            self.table.setCellWidget(row_index, 7, remember)
        self._refresh_bom_ready()

    def _candidate_changed(self, row: int, component_id: str | None) -> None:
        """接受人工候选选择并按所有行的累计需求重新计算缺料。"""

        item = self.preview[row]
        item["selected_component_id"] = component_id
        item["match_status"] = "manual" if component_id else "unmatched"
        item["reason"] = "人工选择" if component_id else "尚未选择"
        if reason_cell := self.table.item(row, 4):
            reason_cell.setText(item["reason"])
        self._refresh_bom_ready()

    def _refresh_bom_ready(self) -> None:
        """按同一元件的累计需求重算缺料，并仅在所有行可提交时启用按钮。"""

        totals: dict[str, int] = {}
        for stock in self.database.list_stock():
            component_id = str(stock["component_id"])
            totals[component_id] = totals.get(component_id, 0) + int(stock["quantity"])
        demand: dict[str, int] = {}
        for item in self.preview:
            if selected := item.get("selected_component_id"):
                demand[selected] = demand.get(selected, 0) + int(item["required_quantity"])
        ready = bool(self.preview)
        for row, item in enumerate(self.preview):
            selected = item.get("selected_component_id")
            available = totals.get(selected, 0) if selected else 0
            shortage = max(0, demand.get(selected, 0) - available) if selected else item["required_quantity"]
            item["available_quantity"] = available
            item["shortage"] = shortage
            if available_cell := self.table.item(row, 5):
                available_cell.setText(str(available))
            if shortage_cell := self.table.item(row, 6):
                shortage_cell.setText(str(shortage))
            ready = ready and bool(selected) and not item["input"].get("errors") and shortage <= 0
        self.commit.setEnabled(ready)

    def commit_bom(self) -> None:
        """提交全部已唯一匹配且无缺料的 BOM 行。"""

        from ..models import BomCommitLine, BomCommitRequest

        try:
            request = BomCommitRequest(
                name=self.source.stem if self.source else "BOM 出库",
                source_filename=self.source.name if self.source else None,
                lines=[
                    BomCommitLine(
                        row_number=item["input"]["row_number"],
                        component_id=item["selected_component_id"],
                        quantity=item["required_quantity"],
                        raw_input=item["input"],
                        mapping_key=item["mapping_key"],
                        match_reason=item["reason"],
                        remember_mapping=bool(
                            checkbox.isChecked()
                            if isinstance(
                                (checkbox := self.table.cellWidget(row, 7)), QCheckBox
                            )
                            else False
                        ),
                    )
                    for row, item in enumerate(self.preview)
                ],
            )
            self.database.commit_bom(request)
            self.show_info("BOM 已原子出库")
            self.commit.setEnabled(False)
            self.changed.emit()
        except Exception as error:
            self.show_error(str(error))


class ScanPage(BasePage):
    """摄像头/图片文字识别与字段确认页面。"""

    use_for_receive = Signal(object)
    back_requested = Signal()

    def __init__(self, database: Database, recognition: RecognitionEngine) -> None:
        """构建摄像头/图片识别界面，并复用应用级离线识别器。"""

        super().__init__(database)
        self.recognition = recognition
        self.camera: QCamera | None = None
        self.session: QMediaCaptureSession | None = None
        self.sink: QVideoSink | None = None
        self.last_frame_at = 0.0
        self.busy = False
        self.last_draft: Any | None = None
        self.recognition_worker: Worker | None = None
        layout = QVBoxLayout(self)
        back = QPushButton("返回入库")
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back)
        self.status = QLabel("● 等待图像")
        self.status.setStyleSheet("color:#657786;font-weight:600")
        layout.addWidget(self.status)
        self.preview_label = QLabel("摄像头未启动")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(280)
        self.preview_label.setStyleSheet("background:#17202a;color:#ccd6dd;border-radius:8px")
        layout.addWidget(self.preview_label)
        self.recognition_preview = QLabel("识别完成后在这里显示文字检测框")
        self.recognition_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recognition_preview.setMinimumHeight(220)
        self.recognition_preview.setStyleSheet(
            "background:#ffffff;color:#657786;border:2px dashed #b8c4cc;border-radius:8px"
        )
        layout.addWidget(self.recognition_preview)
        controls = QHBoxLayout()
        self.camera_choice = WheelSafeComboBox()
        self.camera_choice.setMinimumWidth(150)
        self._refresh_camera_choices()
        start = QPushButton("打开摄像头")
        start.clicked.connect(self.start_camera)
        stop = QPushButton("关闭摄像头")
        stop.clicked.connect(self.stop_camera)
        load = QPushButton("导入照片")
        load.clicked.connect(self.choose_image)
        controls.addWidget(self.camera_choice)
        controls.addWidget(start)
        controls.addWidget(stop)
        controls.addWidget(load)
        layout.addLayout(controls)
        use_draft = QPushButton("使用识别结果创建入库草稿")
        use_draft.clicked.connect(self._use_draft)
        layout.addWidget(use_draft)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result)

    def start_camera(self) -> None:
        """打开默认摄像头并在 Python 中接收视频帧。"""

        self._refresh_camera_choices()
        devices = QMediaDevices.videoInputs()
        if not devices:
            self.show_error("没有发现摄像头，请使用图片导入")
            return
        self.stop_camera()
        device_index = max(0, min(self.camera_choice.currentIndex(), len(devices) - 1))
        device = devices[device_index]
        self.camera = QCamera(device)
        formats = list(device.videoFormats())
        if formats:
            suitable = [
                item
                for item in formats
                if item.resolution().width() * item.resolution().height() <= 1280 * 720
                and item.maxFrameRate() >= 15
            ]
            if suitable:
                best = max(
                    suitable,
                    key=lambda item: (
                        item.resolution().width() * item.resolution().height(),
                        item.maxFrameRate(),
                    ),
                )
                self.camera.setCameraFormat(best)
        if self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAutoNear):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAutoNear)
        elif self.camera.isFocusModeSupported(QCamera.FocusMode.FocusModeAuto):
            self.camera.setFocusMode(QCamera.FocusMode.FocusModeAuto)
        self.camera.errorOccurred.connect(self._camera_failed)
        self.session = QMediaCaptureSession()
        self.sink = QVideoSink()
        self.session.setCamera(self.camera)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.camera.start()

    def _refresh_camera_choices(self) -> None:
        """刷新入库识别摄像头列表。"""

        selected = self.camera_choice.currentText() if self.camera_choice.count() else ""
        self.camera_choice.clear()
        devices = QMediaDevices.videoInputs()
        for device in devices:
            self.camera_choice.addItem(device.description())
        if not devices:
            self.camera_choice.addItem("未检测到摄像头")
            self.camera_choice.setEnabled(False)
            return
        self.camera_choice.setEnabled(True)
        previous = self.camera_choice.findText(selected)
        if previous >= 0:
            self.camera_choice.setCurrentIndex(previous)

    def _camera_failed(self, _error: Any, message: str) -> None:
        """展示入库识别摄像头异常。"""

        self.status.setText(f"● 摄像头异常：{message or '无法读取视频帧'}")
        self.status.setStyleSheet("color:#b42318;font-weight:700")

    def stop_camera(self) -> None:
        """停止并释放摄像头。"""

        if self.camera:
            self.camera.stop()
        self.camera = None
        self.session = None
        self.sink = None

    def _frame(self, frame: QVideoFrame) -> None:
        """显示摄像头帧，并以最多每秒一次的频率触发识别。"""

        image = frame.toImage()
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        preview = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 实时取景框只用于帮助用户把完整白色标签放在画面中央；真正 OCR 文字框显示在
        # 下方矫正图中，避免将透视变换后的坐标错误叠加到原始相机帧。
        painter = QPainter(preview)
        guide_color = QColor("#e0a800" if self.busy else "#3bb273")
        painter.setPen(QPen(guide_color, 4))
        guide = preview.rect().adjusted(
            preview.width() // 10,
            preview.height() // 8,
            -preview.width() // 10,
            -preview.height() // 8,
        )
        painter.drawRect(guide)
        painter.drawText(guide.adjusted(8, 8, -8, -8), "将完整标签放入框内")
        painter.end()
        self.preview_label.setPixmap(preview)
        now = time.monotonic()
        if not self.busy and now - self.last_frame_at >= 1.0:
            self.last_frame_at = now
            try:
                image_bytes = self._image_bytes(image.copy())
            except Exception as error:
                self._recognition_failed(str(error))
                return
            self._analyze(image_bytes)

    @staticmethod
    def _image_bytes(image: QImage) -> bytes:
        """把 Qt 图像编码为内存 PNG，不创建临时文件。"""

        return qimage_to_png(image)

    def choose_image(self) -> None:
        """导入照片并启动后台识别。"""

        filename, _ = QFileDialog.getOpenFileName(
            self, "选择元件袋照片", "", "图片 (*.png *.jpg *.jpeg *.bmp)"
        )
        if filename:
            self._analyze(Path(filename).read_bytes())

    def _analyze(self, image: bytes) -> None:
        """在线程池中启动一次 OCR 识别。

        :param image: 已编码为 PNG/JPEG 的图像字节
        :return: 无返回值；结果通过 Worker 信号传给 ``_show_draft``

        注意：识别期间设置 ``busy``，摄像头帧处理会据此跳过重复任务，避免模型推理
        堆积并阻塞界面。
        """

        ### 更新忙碌状态和视觉提示 ###
        self.busy = True
        self.status.setText("● 正在识别印刷文字……")
        self.status.setStyleSheet("color:#c58b00;font-weight:700")
        self.recognition_preview.setStyleSheet(
            "background:#fffdf5;color:#657786;border:3px solid #e0a800;border-radius:8px"
        )
        ### 在线程池执行识别并回到主线程更新界面 ###
        self.recognition_worker = Worker(lambda: self.recognition.analyze(image))
        self.recognition_worker.signals.succeeded.connect(self._show_draft)
        self.recognition_worker.signals.failed.connect(self._recognition_failed)
        self.recognition_worker.signals.finished.connect(self._recognition_finished)
        self.pool.start(self.recognition_worker)

    def _recognition_finished(self) -> None:
        """释放入库 OCR 忙碌状态，允许后续摄像头帧继续识别。"""

        self.busy = False
        self.recognition_worker = None

    def _show_draft(self, draft: Any) -> None:
        """显示识别草稿中的标注图、字段、置信度和警告。

        :param draft: 后台识别成功后返回的 ``LabelDraft``
        :return: 无返回值

        本方法由 Qt 信号在主线程调用，可以安全更新 QWidget；结果只作为草稿展示，
        不会自动写入数据库。
        """

        ### 显示带文字框的识别图 ###
        self.last_draft = draft
        if draft.annotated_image_png:
            pixmap = QPixmap()
            pixmap.loadFromData(draft.annotated_image_png, "PNG")
            self.recognition_preview.setPixmap(
                pixmap.scaled(
                    self.recognition_preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        recognized = bool(draft.ocr_lines)
        self.status.setText(
            f"● {'识别完成' if recognized else '未识别到文字'} · "
            f"{len(draft.ocr_lines)} 个文字框 · {len(draft.fields)} 个字段"
        )
        color = "#16833b" if recognized else "#b42318"
        self.status.setStyleSheet(f"color:{color};font-weight:700")
        self.recognition_preview.setStyleSheet(
            f"background:#ffffff;border:3px solid {color};border-radius:8px"
        )
        ### 输出结构化字段和人工核对警告 ###
        lines = [f"模型：{draft.model_version}", f"预处理：{draft.preprocessing}", ""]
        for name, field in draft.fields.items():
            lines.append(
                f"{name}: {field.normalized_value}  [{field.source}, {field.confidence:.1%}]"
            )
        if draft.warnings:
            lines.extend(["", "警告：", *[f"- {warning}" for warning in draft.warnings]])
        lines.extend(["", "结果仅为草稿，必须人工确认后再入库。"])
        self.result.setPlainText("\n".join(lines))

    def _recognition_failed(self, message: str) -> None:
        """把识别异常同时反映到状态边框和错误对话框。"""

        self.status.setText("● 识别失败")
        self.status.setStyleSheet("color:#b42318;font-weight:700")
        self.recognition_preview.setStyleSheet(
            "background:#fff5f5;border:3px solid #b42318;border-radius:8px"
        )
        self.show_error(message)

    def _use_draft(self) -> None:
        """把最近草稿交给手工入库页，绝不直接修改库存。"""

        if self.last_draft is None:
            self.show_error("请先完成一次识别")
            return
        self.use_for_receive.emit(self.last_draft)


class TransactionsPage(BasePage):
    """库存流水查看页面。"""

    def __init__(self, database: Database) -> None:
        """构建只读库存流水表并立即加载当前记录。"""

        super().__init__(database)
        layout = QVBoxLayout(self)
        button = QPushButton("刷新")
        button.clicked.connect(self.refresh)
        layout.addWidget(button)
        self.table = make_table(["时间", "类型", "元件", "数量", "位置 / 仓库", "来源"])
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        """重新读取不可变流水。"""

        fill_table(
            self.table,
            [
                [
                    item["created_at"],
                    item["kind"],
                    item["component_name"],
                    int(item["quantity_delta"]),
                    f"{item['warehouse_name']} / {item['location_name']}",
                    item.get("source") or "",
                ]
                for item in self.database.list_transactions()
            ],
        )


class StatisticsPage(BasePage):
    """展示全库库存数量、价值、分库和分类统计。"""

    def __init__(self, database: Database) -> None:
        """构建摘要指标和两个可排序的统计表。"""

        super().__init__(database)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        refresh = QPushButton("刷新统计")
        refresh.clicked.connect(self.refresh)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        toolbar.addWidget(refresh)
        toolbar.addWidget(self.summary, 1)
        layout.addLayout(toolbar)
        note = QLabel(
            "库存估值优先使用剩余批次单价，缺失时回退到元件当前单价；"
            "仍无价格的数量单独列为未定价库存。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs = QTabWidget()
        self.location_table = make_table(
            ["位置 / 仓库", "元件种类数", "库存件数", "库存估值", "未定价件数"]
        )
        self.category_table = make_table(
            ["元件类别", "元件种类数", "库存件数", "库存估值", "未定价件数"]
        )
        self.tabs.addTab(self.location_table, "分库库存")
        self.tabs.addTab(self.category_table, "分类库存")
        layout.addWidget(self.tabs)
        self.refresh()

    def refresh(self) -> None:
        """从同一数据库快照口径重新计算并展示全部统计。"""

        statistics = self.database.inventory_statistics()
        self.summary.setText(
            f"元件种类：{statistics['component_types']}　"
            f"库存总量：{statistics['total_quantity']}　"
            f"库存估值：{statistics['total_value']:.4f}　"
            f"未定价库存：{statistics['unpriced_quantity']}"
        )
        for table, key in (
            (self.location_table, "by_location"),
            (self.category_table, "by_category"),
        ):
            fill_table(
                table,
                [
                    [
                        item["name"],
                        item["component_types"],
                        item["quantity"],
                        round(float(item["value"]), 4),
                        item["unpriced_quantity"],
                    ]
                    for item in statistics[key]
                ],
            )


class SettingsPage(BasePage):
    """集中管理连接信息、备份恢复与关于信息。"""

    def __init__(self, database: Database, token: str) -> None:
        """构建设置选项卡。"""

        super().__init__(database)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        connection_tab = QWidget()
        connection_form = QFormLayout(connection_tab)
        api = QLineEdit("http://127.0.0.1:17635")
        api.setReadOnly(True)
        token_field = QLineEdit(token)
        token_field.setEchoMode(QLineEdit.EchoMode.Password)
        connection_form.addRow("本机 API", api)
        connection_form.addRow("Bearer Token", token_field)
        self.tabs.addTab(connection_tab, "连接信息")

        backup_tab = QWidget()
        backup_layout = QVBoxLayout(backup_tab)
        backup_tip = QLabel(
            "备份使用 SQLite 在线快照；恢复文件会先校验，并在下次启动时生效。"
        )
        backup_tip.setWordWrap(True)
        backup_layout.addWidget(backup_tip)
        backup = QPushButton("创建备份")
        backup.clicked.connect(self.backup)
        restore = QPushButton("选择备份并在重启后恢复")
        restore.clicked.connect(self.restore)
        backup_layout.addWidget(backup)
        backup_layout.addWidget(restore)
        backup_layout.addStretch()
        self.tabs.addTab(backup_tab, "备份与恢复")

        about_tab = QWidget()
        about_form = QFormLayout(about_tab)
        about_form.addRow("应用", QLabel("元件库"))
        about_form.addRow("版本", QLabel(__version__))
        about_form.addRow("作者", QLabel("Ternura_xiang"))
        about_form.addRow("许可证", QLabel("MIT"))
        description = QLabel("本地优先的电子元件库存、批次、BOM 与扫码管理工具。")
        description.setWordWrap(True)
        about_form.addRow("简介", description)
        self.tabs.addTab(about_tab, "关于")

    def backup(self) -> None:
        """让用户选择目录并创建数据库快照。"""

        directory = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if directory:
            try:
                result = self.database.backup(Path(directory))
                self.show_info(f"备份已创建：\n{result['path']}")
            except Exception as error:
                self.show_error(str(error))

    def restore(self) -> None:
        """校验备份并暂存到下次启动。"""

        filename, _ = QFileDialog.getOpenFileName(self, "选择数据库备份", "", "SQLite (*.db)")
        if filename:
            try:
                self.database.prepare_restore(Path(filename))
                self.show_info("备份已校验，将在重启应用后恢复")
            except Exception as error:
                self.show_error(str(error))


class MainWindow(QMainWindow):
    """组织所有功能页并同步数据刷新。"""

    def __init__(self, database: Database, recognition: RecognitionEngine, token: str) -> None:
        """实例化所有主页面和子页面，连接跨页信号并应用全局样式。"""

        super().__init__()
        self.setWindowTitle("元件库")
        self.resize(1280, 800)
        root = QWidget()
        layout = QHBoxLayout(root)
        self.navigation = QListWidget()
        self.navigation.setFixedWidth(180)
        self.stack = QStackedWidget()
        self.inventory_page = InventoryPage(database)
        self.receive_page = ReceivePage(database, recognition)
        self.issue_page = IssuePage(database, recognition)
        self.locations_page = LocationsPage(database)
        self.scan_page = ScanPage(database, recognition)
        self.order_page = OrderImportPage(database)
        self.bom_page = BomPage(database)
        self.transactions_page = TransactionsPage(database)
        self.statistics_page = StatisticsPage(database)
        self.settings_page = SettingsPage(database, token)
        pages: list[tuple[str, QWidget]] = [
            ("元件库存", self.inventory_page),
            ("入库", self.receive_page),
            ("出库", self.issue_page),
            ("位置与仓库", self.locations_page),
            ("库存流水", self.transactions_page),
            ("统计", self.statistics_page),
            ("设置", self.settings_page),
        ]
        for name, page in pages:
            self.navigation.addItem(name)
            self.stack.addWidget(page)
            if isinstance(page, BasePage):
                page.changed.connect(self.refresh_pages)
        for page in (self.scan_page, self.order_page, self.bom_page):
            self.stack.addWidget(page)
            page.changed.connect(self.refresh_pages)
        self.navigation.currentRowChanged.connect(self._show_main_page)
        self.receive_page.open_order_import.connect(lambda: self._show_subpage(self.order_page))
        self.receive_page.open_scan.connect(lambda: self._show_subpage(self.scan_page))
        self.issue_page.open_bom.connect(lambda: self._show_subpage(self.bom_page))
        self.scan_page.back_requested.connect(lambda: self._show_main_page(1))
        self.order_page.back_requested.connect(lambda: self._show_main_page(1))
        self.bom_page.back_requested.connect(lambda: self._show_main_page(2))
        self.scan_page.use_for_receive.connect(self._open_receive_draft)
        self.navigation.setCurrentRow(0)
        layout.addWidget(self.navigation)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

    def _open_receive_draft(self, draft: Any) -> None:
        """将扫描结果带到手工入库页，等待用户编辑并最终确认。"""

        self.scan_page.stop_camera()
        self.receive_page.apply_scan_draft(draft)
        self._show_main_page(1)

    def _show_main_page(self, index: int) -> None:
        """显示侧栏主页面；返回时即使侧栏行未变化也会切回。"""

        if index < 0 or index >= self.navigation.count():
            return
        if self.stack.currentWidget() is self.scan_page:
            self.scan_page.stop_camera()
        self.navigation.blockSignals(True)
        self.navigation.setCurrentRow(index)
        self.navigation.blockSignals(False)
        self.stack.setCurrentIndex(index)

    def _show_subpage(self, page: QWidget) -> None:
        """显示订单或 BOM 子页面，同时保留其所属主页面的侧栏状态。"""

        # QListWidget 清空选中项后可能自动回落到第 0 行，并再次触发页面刷新。
        # 子页面只需切换右侧堆栈；保留“入库”或“出库”选中项可避免 UI 叠加。
        self.stack.setCurrentWidget(page)

    def refresh_pages(self) -> None:
        """库存变化后刷新需要共享选择数据的页面。"""

        for index in range(self.stack.count()):
            page = self.stack.widget(index)
            for method_name in ("refresh", "reload"):
                method = getattr(page, method_name, None)
                if callable(method):
                    method()
                    break

    def closeEvent(self, event: Any) -> None:
        """关闭窗口前释放摄像头。"""

        for index in range(self.stack.count()):
            page = self.stack.widget(index)
            if isinstance(page, ScanPage):
                page.stop_camera()
        super().closeEvent(event)


STYLE = """
QMainWindow, QWidget { background: #f4f6f8; color: #17202a; font-size: 14px; }
QListWidget { background: #16212b; color: #dce6ee; border: 0; outline: none; padding: 12px; }
QListWidget::item { padding: 12px 10px; border-radius: 6px; }
QListWidget::item:selected { background: #2b7a78; color: white; }
QLineEdit, QComboBox, QDoubleSpinBox, QTextEdit { background: white; border: 1px solid #ccd6dd; border-radius: 5px; padding: 7px; }
QPushButton { background: #2b7a78; color: white; border: 0; border-radius: 5px; padding: 8px 14px; }
QPushButton:disabled { background: #9eacad; }
QTableWidget { background: white; border: 1px solid #dce3e8; gridline-color: #edf1f3; }
QHeaderView::section { background: #e8eef1; padding: 7px; border: 0; }
QCheckBox::indicator, QGroupBox::indicator, QAbstractItemView::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid #111111;
    border-radius: 2px;
    background: white;
}
QCheckBox::indicator:checked, QGroupBox::indicator:checked, QAbstractItemView::indicator:checked {
    border: 2px solid #111111;
    background: white;
    image: url("__CHECKMARK_ICON__");
}
QCheckBox::indicator:disabled, QGroupBox::indicator:disabled, QAbstractItemView::indicator:disabled {
    border-color: #555555;
    background: #eeeeee;
}
""".replace(
    "__CHECKMARK_ICON__",
    (Path(__file__).resolve().parents[1] / "assets" / "checkmark.svg").as_posix(),
)
