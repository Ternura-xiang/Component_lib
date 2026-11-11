"""匿名立创选型浏览器和逐字段合并确认窗口。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..jlc_product import (
    is_blocked_jlc_navigation,
    is_jlc_product_url,
    parse_jlc_product_page,
)
from ..models import (
    JlcMergeResult,
    JlcParameter,
    JlcParameterGroup,
    JlcProductDraft,
)

JLC_HOME = "https://www.szlcsc.com/"


class SafeJlcPage(QWebEnginePage):
    """只允许匿名商品选型所需页面，拒绝账户和交易相关导航。"""

    navigation_blocked = Signal(str)

    def createWindow(self, _window_type: QWebEnginePage.WebWindowType) -> QWebEnginePage:
        """把商城的新标签页请求复用到当前页面。

        立创搜索结果通常使用 ``target=_blank`` 或 ``window.open`` 打开商品页。
        嵌入式浏览器没有标签栏，如果不接管该请求，Qt 会创建一个没有视图的页面，
        用户看到的当前页面便会一直停留在搜索结果。返回当前页面后，Qt 会把目标
        URL 导航到现有视图，后续仍会经过 ``acceptNavigationRequest`` 的安全检查。
        """

        return self

    def acceptNavigationRequest(
        self,
        url: QUrl | str,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        """允许公开商城导航，并在主框架请求被拒绝时通知界面。

        ``navigation_type`` 由 Qt API 要求但当前策略不区分点击、重定向或脚本导航；
        所有类型都经过相同的域名和敏感路径检查。
        """

        raw = url.toString() if isinstance(url, QUrl) else url
        if raw == "about:blank":
            return True
        allowed = not is_blocked_jlc_navigation(raw)
        if not allowed and is_main_frame:
            self.navigation_blocked.emit(raw)
        return allowed


class JlcBrowserDialog(QDialog):
    """由用户手动浏览并明确确认当前商品，不持久化任何浏览状态。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建一次性匿名浏览器并连接导航、加载和确认回调。"""

        super().__init__(parent)
        self.draft: JlcProductDraft | None = None
        self._closed = False
        self.setWindowTitle("从立创商城选择商品")
        self.resize(1180, 820)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        back = QPushButton("返回")
        forward = QPushButton("前进")
        home = QPushButton("主页")
        reload_page = QPushButton("刷新")
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.use_current = QPushButton("使用当前商品")
        self.use_current.setEnabled(False)
        cancel = QPushButton("取消")
        toolbar.addWidget(back)
        toolbar.addWidget(forward)
        toolbar.addWidget(home)
        toolbar.addWidget(reload_page)
        toolbar.addWidget(self.address, 1)
        toolbar.addWidget(self.use_current)
        toolbar.addWidget(cancel)
        layout.addLayout(toolbar)
        self.status = QLabel("匿名模式：请搜索并打开商品详情页")
        layout.addWidget(self.status)

        # 不命名的 profile 是 Qt WebEngine 的 Off-the-Record 配置，不读取系统浏览器
        # Cookie，也不会把本窗口的登录态、历史或 HTTP 缓存持久化到用户目录。
        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.downloadRequested.connect(self._cancel_download)
        self.page = SafeJlcPage(self.profile, self)
        self.page.navigation_blocked.connect(self._show_blocked_navigation)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view, 1)

        back.clicked.connect(self.view.back)
        forward.clicked.connect(self.view.forward)
        home.clicked.connect(lambda: self.view.setUrl(QUrl(JLC_HOME)))
        reload_page.clicked.connect(self.view.reload)
        cancel.clicked.connect(self.reject)
        self.use_current.clicked.connect(self.capture_current_product)
        self.view.urlChanged.connect(self._url_changed)
        self.view.loadProgress.connect(
            lambda progress: self.status.setText(f"页面加载中：{progress}%")
        )
        self.view.loadFinished.connect(self._load_finished)
        self.view.setUrl(QUrl(JLC_HOME))

    @staticmethod
    def _cancel_download(download: Any) -> None:
        """商品选择流程只保留链接，任何下载请求都立即取消。"""

        download.cancel()

    def _show_blocked_navigation(self, _url: str) -> None:
        """在状态栏提示导航已被安全策略拦截；不展示可能含敏感信息的 URL。"""

        self.status.setText("已阻止登录、账户、购物车、订单或站外页面")

    def _url_changed(self, url: QUrl) -> None:
        """同步只读地址栏，并仅在商品详情页启用确认按钮。"""

        raw = url.toString()
        self.address.setText(raw)
        self.use_current.setEnabled(is_jlc_product_url(raw))

    def _load_finished(self, ok: bool) -> None:
        """根据加载结果和页面类型更新下一步操作提示。"""

        if not ok:
            self.status.setText("页面加载失败；可取消并使用剪贴板导入")
        elif self.use_current.isEnabled():
            self.status.setText("已进入商品详情页，请点击“使用当前商品”")
        else:
            self.status.setText("请搜索并打开一个商品详情页")

    def capture_current_product(self) -> None:
        """异步读取并解析用户当前打开的商品详情页。

        :return: 无返回值；解析成功后设置 ``draft`` 并接受对话框

        注意：只有公开商品详情页能够触发读取；HTML 仅传给一次回调，不写临时文件，
        读取期间确认按钮会禁用以避免重复提交。
        """

        ### 确认当前 URL 是商品详情页 ###
        url = self.view.url().toString()
        if not is_jlc_product_url(url):
            QMessageBox.warning(self, "尚未选择商品", "请先打开一个立创商品详情页")
            return
        self.use_current.setEnabled(False)
        self.status.setText("正在读取并解析当前商品页……")
        ### 异步读取渲染后的页面内容 ###
        self.page.toHtml(lambda html, source=url: self._parse_current_html(source, html))

    def _parse_current_html(self, url: str, html: str) -> None:
        """解析浏览器返回的 HTML，并只保留结构化商品草稿。

        :param url: 发起读取时固定的页面 URL，避免回调期间导航造成错配
        :param html: Qt WebEngine 返回的渲染后 HTML
        :return: 无返回值

        对话框已关闭时直接忽略迟到回调；无论成功或失败都解除本地 HTML 引用。
        """

        if self._closed:
            return
        try:
            self.draft = parse_jlc_product_page(url, html)
            html = ""  # 明确解除本地引用；未创建任何临时文件。
            self.accept()
        except Exception as error:
            html = ""
            self.status.setText("页面解析失败，请核对页面或使用剪贴板导入")
            self.use_current.setEnabled(True)
            QMessageBox.critical(self, "商品解析失败", str(error))

    def done(self, result: int) -> None:
        """关闭窗口时清除临时 Cookie 和内存 HTTP 缓存。"""

        self._closed = True
        self.view.stop()
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        super().done(result)


class JlcBatchBrowserDialog(QDialog):
    """依次打开指定商品详情页，并收集可供用户确认的结构化草稿。"""

    def __init__(
        self, targets: list[tuple[str, str, str]], parent: QWidget | None = None
    ) -> None:
        """创建批量补全窗口。

        :param targets: ``(内部键, 显示名称, 商品页 URL)`` 列表
        :param parent: 父窗口
        """

        super().__init__(parent)
        self.targets = targets
        self.drafts: dict[str, JlcProductDraft] = {}
        self.errors: dict[str, str] = {}
        self._index = -1
        self._closed = False
        self.setWindowTitle("自动补充立创商品资料")
        self.resize(1120, 760)
        layout = QVBoxLayout(self)
        self.status = QLabel("准备打开商品详情页……")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
        )
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.downloadRequested.connect(JlcBrowserDialog._cancel_download)
        self.page = SafeJlcPage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view, 1)
        controls = QHBoxLayout()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        controls.addStretch()
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        self.view.loadProgress.connect(self._show_progress)
        self.view.loadFinished.connect(self._loaded)

    def start(self) -> None:
        """从第一项开始遍历；空任务直接完成。"""

        if not self.targets:
            self.accept()
            return
        self._load_next()

    def _show_progress(self, progress: int) -> None:
        if 0 <= self._index < len(self.targets):
            _key, name, _url = self.targets[self._index]
            self.status.setText(
                f"正在读取 {self._index + 1}/{len(self.targets)}：{name}（{progress}%）"
            )

    def _load_next(self) -> None:
        self._index += 1
        if self._index >= len(self.targets):
            self.status.setText(
                f"读取完成：成功 {len(self.drafts)} 项，失败 {len(self.errors)} 项"
            )
            self.accept()
            return
        _key, _name, url = self.targets[self._index]
        self.view.setUrl(QUrl(url))

    def _loaded(self, ok: bool) -> None:
        if self._closed or not (0 <= self._index < len(self.targets)):
            return
        key, name, url = self.targets[self._index]
        if not ok:
            self.errors[key] = f"{name}：页面加载失败"
            self._load_next()
            return
        self.page.toHtml(lambda html, item_key=key, item_name=name, source=url: self._parsed(
            item_key, item_name, source, html
        ))

    def _parsed(self, key: str, name: str, url: str, html: str) -> None:
        if self._closed:
            return
        try:
            self.drafts[key] = parse_jlc_product_page(url, html)
        except Exception as error:
            self.errors[key] = f"{name}：{error}"
        self._load_next()

    def done(self, result: int) -> None:
        self._closed = True
        self.view.stop()
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        super().done(result)


class JlcMergeDialog(QDialog):
    """显示现有值、商城值和最终值，由用户逐项授权应用。"""

    FIELD_LABELS = (
        ("name", "元件名称"),
        ("lcsc_part_number", "立创编号"),
        ("manufacturer_part_number", "制造商料号"),
        ("brand", "品牌"),
        ("package", "封装"),
        ("category", "元件类别"),
        ("product_url", "商品链接"),
        ("datasheet_url", "数据手册"),
        ("notes", "商品描述 / 备注"),
    )

    def __init__(
        self,
        draft: JlcProductDraft,
        current_fields: dict[str, str],
        current_parameters: list[dict[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        """构建逐字段合并界面。

        ``draft`` 是网页解析结果；``current_fields`` 和 ``current_parameters`` 是现有
        表单快照。对话框只生成合并结果，不直接写数据库或修改调用方控件。
        """

        super().__init__(parent)
        self.draft = draft
        self.current_fields = current_fields
        self.current_parameters = current_parameters
        self.field_rows: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        self.parameter_rows: list[tuple[JlcParameter, str, QCheckBox, QLineEdit]] = []
        self.setWindowTitle("确认立创商品资料")
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        if draft.warnings:
            warning = QLabel("；".join(draft.warnings))
            warning.setStyleSheet("color: #a15c00;")
            warning.setWordWrap(True)
            layout.addWidget(warning)
        layout.addWidget(QLabel("商城资料默认全部选中；如需保留现有值，请取消勾选对应字段。"))
        self.fields_table = self._make_table(["选择", "字段", "现有值", "商城值", "最终值"])
        layout.addWidget(self.fields_table)
        self._fill_fields()
        layout.addWidget(QLabel("商品参数（保留商城分组和顺序）"))
        self.parameters_table = self._make_table(
            ["选择", "分组", "参数", "现有值", "商城值", "最终值"]
        )
        layout.addWidget(self.parameters_table, 1)
        self._fill_parameters()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("确认资料")
        # Qt 将标准 Apply 按钮归为 ApplyRole，它不会触发 QDialogButtonBox.accepted。
        # 因此必须直接连接 clicked，否则界面看似可点，实际对话框不会返回 Accepted，
        # 入库页也就永远收不到用户确认后的合并结果。
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        """创建统一样式的空表格；调用方随后设置行数和单元格控件。"""

        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        return table

    def _draft_fields(self) -> dict[str, str]:
        """把强类型商城草稿转换为表格所需的字符串字段映射。"""

        return {
            "name": self.draft.name,
            "lcsc_part_number": self.draft.lcsc_part_number,
            "manufacturer_part_number": self.draft.manufacturer_part_number or "",
            "brand": self.draft.brand or "",
            "package": self.draft.package or "",
            "category": self.draft.category or "",
            "product_url": self.draft.product_url,
            "datasheet_url": self.draft.datasheet_url or "",
            "notes": self.draft.description or "",
        }

    def _fill_fields(self) -> None:
        """填充非空基础字段，并用底色标出商城值与现有值的冲突。"""

        values = self._draft_fields()
        available = [(key, label, values[key]) for key, label in self.FIELD_LABELS if values[key]]
        self.fields_table.setRowCount(len(available))
        for row, (key, label, website_value) in enumerate(available):
            current = self.current_fields.get(key, "")
            check = QCheckBox()
            # 基础字段与商品参数使用同一导入规则：商城有值就默认应用。
            # 这样封装、品牌等已有值的字段不会因默认未勾选而漏导入。
            check.setChecked(True)
            self.fields_table.setCellWidget(row, 0, check)
            self.fields_table.setItem(row, 1, QTableWidgetItem(label))
            self.fields_table.setItem(row, 2, QTableWidgetItem(current))
            self.fields_table.setItem(row, 3, QTableWidgetItem(website_value))
            final = QLineEdit(website_value)
            self.fields_table.setCellWidget(row, 4, final)
            if current and current != website_value:
                for column in (2, 3):
                    item = self.fields_table.item(row, column)
                    if item:
                        item.setBackground(QColor("#fff1cc"))
            self.field_rows[key] = (check, final)

    def _fill_parameters(self) -> None:
        """按商城分组顺序填充动态参数；所有商城参数默认勾选应用。"""

        existing = {item["parameter_key"]: item for item in self.current_parameters}
        parameters = [
            (group.name, parameter)
            for group in self.draft.parameter_groups
            for parameter in group.parameters
        ]
        self.parameters_table.setRowCount(len(parameters))
        for row, (group_name, parameter) in enumerate(parameters):
            old = existing.get(parameter.key, {})
            current = str(old.get("value") or "")
            check = QCheckBox()
            check.setChecked(True)
            self.parameters_table.setCellWidget(row, 0, check)
            for column, value in (
                (1, group_name),
                (2, parameter.label),
                (3, current),
                (4, parameter.value),
            ):
                self.parameters_table.setItem(row, column, QTableWidgetItem(value))
            final = QLineEdit(parameter.value)
            self.parameters_table.setCellWidget(row, 5, final)
            if current and current != parameter.value:
                for column in (3, 4):
                    item = self.parameters_table.item(row, column)
                    if item:
                        item.setBackground(QColor("#fff1cc"))
            self.parameter_rows.append((parameter, group_name, check, final))

    def result_data(self) -> JlcMergeResult:
        """返回最终勾选的字段和“现有参数＋已选商城参数”的完整集合。"""

        selected_fields = {
            key: editor.text().strip() or None
            for key, (check, editor) in self.field_rows.items()
            if check.isChecked()
        }
        merged: dict[str, tuple[str, JlcParameter]] = {
            item["parameter_key"]: (
                item.get("group_name") or "类别参数",
                JlcParameter(key=item["parameter_key"], label=item["label"], value=item["value"]),
            )
            for item in self.current_parameters
            if item.get("value")
        }
        for parameter, group_name, check, editor in self.parameter_rows:
            if check.isChecked() and editor.text().strip():
                merged[parameter.key] = (
                    group_name,
                    parameter.model_copy(update={"value": editor.text().strip()}),
                )
        groups: dict[str, list[JlcParameter]] = {}
        for group_name, parameter in merged.values():
            groups.setdefault(group_name, []).append(parameter)
        return JlcMergeResult(
            **selected_fields,
            parameter_groups=[
                JlcParameterGroup(name=name, parameters=parameters)
                for name, parameters in groups.items()
            ],
        )


class JlcBatchMergeDialog(QDialog):
    """集中展示批量补全差异，并允许按元件及单独字段授权覆盖。"""

    def __init__(
        self,
        changes: list[
            tuple[str, str, JlcProductDraft, dict[str, str], list[dict[str, str]]]
        ],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.changes = changes
        self.rows: dict[str, list[tuple[str, str, QCheckBox, str, str]]] = {}
        self.masters: dict[str, QCheckBox] = {}
        self.ranges: dict[str, tuple[int, int]] = {}
        self.setWindowTitle("确认自动补充的资料变更")
        self.resize(1080, 760)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "对号用于选择是否采用；三角形只控制详情显示。折叠状态不影响已选字段。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["元件 / 字段", "现有值", "商城值", "类型"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 360)
        self.table.setColumnWidth(1, 280)
        self.table.setColumnWidth(2, 280)
        self.table.setColumnWidth(3, 120)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        layout.addWidget(self.table, 1)
        for key, name, draft, current_fields, current_parameters in changes:
            self._append_component(key, name, draft, current_fields, current_parameters)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.setText("确认所选变更")
        apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _draft_fields(draft: JlcProductDraft) -> dict[str, str]:
        return {
            "name": draft.name,
            "lcsc_part_number": draft.lcsc_part_number,
            "manufacturer_part_number": draft.manufacturer_part_number or "",
            "brand": draft.brand or "",
            "package": draft.package or "",
            "category": draft.category or "",
            "product_url": draft.product_url,
            "datasheet_url": draft.datasheet_url or "",
            "notes": draft.description or "",
        }

    def _append_component(
        self,
        key: str,
        name: str,
        draft: JlcProductDraft,
        current_fields: dict[str, str],
        current_parameters: list[dict[str, str]],
    ) -> None:
        header = self.table.rowCount()
        self.table.insertRow(header)
        master = QCheckBox()
        master.setChecked(True)
        toggle = QToolButton()
        toggle.setText(name)
        toggle.setCheckable(True)
        toggle.setAutoRaise(True)
        toggle.setStyleSheet(
            "QToolButton{border:0;background:transparent;text-align:left;padding:2px;}"
            "QToolButton:hover,QToolButton:pressed{border:0;background:transparent;}"
        )
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        header_widget = QWidget()
        header_widget.setStyleSheet("background:#e6eaed")
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(6, 1, 4, 1)
        header_layout.setSpacing(4)
        header_layout.addWidget(master)
        header_layout.addWidget(toggle, 1)
        self.table.setCellWidget(header, 0, header_widget)
        for column, value in ((1, ""), (2, ""), (3, "元件")):
            item = QTableWidgetItem(value)
            item.setBackground(QColor("#e6eaed"))
            self.table.setItem(header, column, item)
        rows: list[tuple[str, str, QCheckBox, str, str]] = []
        values = self._draft_fields(draft)
        for field, _label in JlcMergeDialog.FIELD_LABELS:
            website = values.get(field, "")
            if website:
                rows.append(("field", field, QCheckBox(), current_fields.get(field, ""), website))
        existing_parameters = {
            item["parameter_key"]: item for item in current_parameters if item.get("parameter_key")
        }
        for parameter_group in draft.parameter_groups:
            for parameter in parameter_group.parameters:
                old = existing_parameters.get(parameter.key, {})
                rows.append(
                    (
                        "parameter",
                        f"{parameter_group.name}\x1f{parameter.key}\x1f{parameter.label}",
                        QCheckBox(),
                        str(old.get("value") or ""),
                        parameter.value,
                    )
                )
        start = self.table.rowCount()
        for kind, field, check, current, website in rows:
            check.setChecked(True)
            row = self.table.rowCount()
            self.table.insertRow(row)
            label = field
            if kind == "field":
                label = dict(JlcMergeDialog.FIELD_LABELS).get(field, field)
            else:
                group_name, _parameter_key, parameter_label = field.split("\x1f", 2)
                label = f"{group_name} / {parameter_label}"
            field_widget = QWidget()
            field_widget.setStyleSheet("background:#f4f5f6")
            field_layout = QHBoxLayout(field_widget)
            field_layout.setContentsMargins(28, 1, 4, 1)
            field_layout.setSpacing(4)
            field_layout.addWidget(check)
            field_layout.addWidget(QLabel(label), 1)
            self.table.setCellWidget(row, 0, field_widget)
            for column, value in (
                (1, current),
                (2, website),
                (3, "基础字段" if kind == "field" else "商品参数"),
            ):
                item = QTableWidgetItem(value)
                item.setBackground(QColor("#f4f5f6"))
                self.table.setItem(row, column, item)
            self.table.setRowHidden(row, True)
        end = self.table.rowCount()
        self.rows[key] = rows
        self.masters[key] = master
        self.ranges[key] = (start, end)
        master.toggled.connect(
            lambda checked, item_rows=rows: [check.setChecked(checked) for *_rest, check, _a, _b in item_rows]
        )
        toggle.toggled.connect(
            lambda expanded, item_key=key, button=toggle: self._toggle_rows(
                item_key, button, expanded
            )
        )

    def _toggle_rows(self, key: str, button: QToolButton, expanded: bool) -> None:
        button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        start, end = self.ranges[key]
        for row in range(start, end):
            self.table.setRowHidden(row, not expanded)

    def result_data(self) -> dict[str, JlcMergeResult]:
        output: dict[str, JlcMergeResult] = {}
        change_by_key = {item[0]: item for item in self.changes}
        for key, master in self.masters.items():
            if not master.isChecked():
                continue
            _key, _name, _draft, _fields, current_parameters = change_by_key[key]
            selected_fields: dict[str, str | None] = {}
            merged_parameters: dict[str, tuple[str, JlcParameter]] = {
                item["parameter_key"]: (
                    item.get("group_name") or "类别参数",
                    JlcParameter(
                        key=item["parameter_key"], label=item["label"], value=item["value"]
                    ),
                )
                for item in current_parameters
                if item.get("parameter_key") and item.get("value")
            }
            for kind, field, check, _current, website in self.rows[key]:
                if not check.isChecked():
                    continue
                if kind == "field":
                    selected_fields[field] = website or None
                else:
                    group, parameter_key, label = field.split("\x1f", 2)
                    merged_parameters[parameter_key] = (
                        group,
                        JlcParameter(key=parameter_key, label=label, value=website),
                    )
            groups: dict[str, list[JlcParameter]] = {}
            for group_name, parameter in merged_parameters.values():
                groups.setdefault(group_name, []).append(parameter)
            output[key] = JlcMergeResult(
                **selected_fields,
                parameter_groups=[
                    JlcParameterGroup(name=group_name, parameters=parameters)
                    for group_name, parameters in groups.items()
                ],
            )
        return output
