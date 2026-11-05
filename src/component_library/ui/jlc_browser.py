"""匿名立创选型浏览器和逐字段合并确认窗口。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
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
        super().__init__(parent)
        self.draft: JlcProductDraft | None = None
        self._closed = False
        self.setWindowTitle("从立创商城选择商品（匿名临时浏览器）")
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
        self.status.setText("已阻止登录、账户、购物车、订单或站外页面")

    def _url_changed(self, url: QUrl) -> None:
        raw = url.toString()
        self.address.setText(raw)
        self.use_current.setEnabled(is_jlc_product_url(raw))

    def _load_finished(self, ok: bool) -> None:
        if not ok:
            self.status.setText("页面加载失败；可取消并使用剪贴板导入")
        elif self.use_current.isEnabled():
            self.status.setText("已进入商品详情页，请点击“使用当前商品”")
        else:
            self.status.setText("请搜索并打开一个商品详情页")

    def capture_current_product(self) -> None:
        """异步读取当前渲染 HTML；HTML 只传给一次解析回调。"""

        url = self.view.url().toString()
        if not is_jlc_product_url(url):
            QMessageBox.warning(self, "尚未选择商品", "请先打开一个立创商品详情页")
            return
        self.use_current.setEnabled(False)
        self.status.setText("正在读取并解析当前商品页……")
        self.page.toHtml(lambda html, source=url: self._parse_current_html(source, html))

    def _parse_current_html(self, url: str, html: str) -> None:
        """解析完成后不保存 HTML，只保留结构化草稿。"""

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
        layout.addWidget(QLabel("商城资料默认全部应用；如需保留现有值，请取消勾选对应字段。"))
        self.fields_table = self._make_table(["应用", "字段", "现有值", "商城值", "最终值"])
        layout.addWidget(self.fields_table)
        self._fill_fields()
        layout.addWidget(QLabel("商品参数（保留商城分组和顺序）"))
        self.parameters_table = self._make_table(
            ["应用", "分组", "参数", "现有值", "商城值", "最终值"]
        )
        layout.addWidget(self.parameters_table, 1)
        self._fill_parameters()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("应用到入库表单")
        # Qt 将标准 Apply 按钮归为 ApplyRole，它不会触发 QDialogButtonBox.accepted。
        # 因此必须直接连接 clicked，否则界面看似可点，实际对话框不会返回 Accepted，
        # 入库页也就永远收不到用户确认后的合并结果。
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        return table

    def _draft_fields(self) -> dict[str, str]:
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
