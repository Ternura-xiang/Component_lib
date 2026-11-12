"""离线解析立创商城订单网页和 XLS 物料明细对账单。

解析器绝不执行 HTML 中的脚本、不读取 Cookie，也不访问页面中的网络链接。压缩包只在
内存中读取一个 HTML/MHTML 文件，并限制文件数和解压后大小以避免恶意压缩包。
页面中的收货人、联系人、物流信息和 URL 查询参数不会进入返回结果。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

import xlrd
from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from .database import DomainError
from .models import MosfetDetails, OrderImportLine, ResistorDetails

MAX_FILES = 500
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ORDER_FILE_ITEMS = 10_000
ORDER_NUMBER = re.compile(r"SO\d{8,16}")
ORDER_DATE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
CANCELLED_ORDER = re.compile(
    r"(?:订单状态\s*[:：]\s*)?(?:"
    r"(?:订单\s*)?(?:已取消|已撤销|已关闭)|(?:交易|订单)\s*关闭|交易已关闭)"
)
LCSC_NUMBER = re.compile(r"C\d{4,10}", re.IGNORECASE)
PRICE = re.compile(r"￥\s*(\d+(?:\.\d+)?)")
QUANTITY = re.compile(r"(\d+)\s*个")
RESISTANCE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?\s*[kKmM]?[ΩΩ])")
TOLERANCE = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%")
POWER = re.compile(r"(\d+(?:\.\d+)?)\s*(mW|W)\b", re.IGNORECASE)
VOLTAGE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*V\b", re.IGNORECASE)
CURRENT = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*A\b", re.IGNORECASE)


def _read_html(path: Path) -> str:
    """安全读取 ZIP 中的单个页面，或直接读取 HTML/MHTML 文件。"""

    if path.suffix.casefold() not in {".zip", ".html", ".htm", ".mhtml", ".mht"}:
        raise DomainError("VALIDATION_ERROR", "只支持 ZIP、HTML 或 MHTML 订单页面")
    if path.suffix.casefold() != ".zip":
        if path.stat().st_size > MAX_UNCOMPRESSED_BYTES:
            raise DomainError("VALIDATION_ERROR", "订单页面超过 20 MB 安全上限")
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise DomainError("VALIDATION_ERROR", "压缩包文件数量超过 500 个安全上限")
            if sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES:
                raise DomainError("VALIDATION_ERROR", "压缩包解压后超过 20 MB 安全上限")
            for item in infos:
                parts = PurePosixPath(item.filename).parts
                if item.filename.startswith("/") or ".." in parts:
                    raise DomainError("VALIDATION_ERROR", "压缩包包含不安全路径")
            pages = [
                item
                for item in infos
                if not item.is_dir()
                and Path(item.filename).suffix.casefold() in {".html", ".htm", ".mhtml", ".mht"}
            ]
            if not pages:
                raise DomainError("VALIDATION_ERROR", "压缩包中没有订单 HTML/MHTML 页面")
            page = max(pages, key=lambda item: item.file_size)
            return archive.read(page).decode("utf-8", errors="replace")
    except BadZipFile as error:
        raise DomainError("VALIDATION_ERROR", "订单压缩包已损坏或不是 ZIP 文件") from error


def _clean_url(raw_url: str | None) -> str | None:
    """只保留公开 HTTPS 商品页的协议、主机和路径，删除会话查询参数。"""

    if not raw_url:
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.hostname not in {"item.szlcsc.com", "www.szlcsc.com"}:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _text(node: Tag) -> str:
    """把节点文字压缩为单空格形式。"""

    return " ".join(node.get_text(" ", strip=True).split())


def _category_details(
    category: str | None, description: str
) -> tuple[ResistorDetails | None, MosfetDetails | None]:
    """从商城描述中保守提取电阻或 MOS 专属参数。"""

    if category and "电阻" in category:
        resistance = RESISTANCE.search(description)
        tolerance = TOLERANCE.search(description)
        power = POWER.search(description)
        # 工艺只保存标准词本身。旧规则会把前后的商品标题一起吞入字段，导致
        # “厚膜”无法作为稳定的过滤值使用。
        technology_match = re.search(r"(厚膜|薄膜|金属膜|合金)", description)
        power_mw = None
        if power:
            power_mw = float(power.group(1)) * (1000 if power.group(2).casefold() == "w" else 1)
        return (
            ResistorDetails(
                resistance=resistance.group(1).replace(" ", "") if resistance else None,
                tolerance_percent=float(tolerance.group(1)) if tolerance else None,
                power_mw=power_mw,
                technology=technology_match.group(1).strip() if technology_match else None,
            ),
            None,
        )
    upper = f"{category or ''} {description}".upper()
    if "MOS" in upper and "CMOS" not in upper:
        channel = None
        if re.search(r"(?:N沟道|N[- ]?CHANNEL|N[- ]?MOS)", upper):
            channel = "N"
        elif re.search(r"(?:P沟道|P[- ]?CHANNEL|P[- ]?MOS)", upper):
            channel = "P"
        count = re.search(r"(\d+)\s*(?:个|路|颗)\s*(?:MOS|MOSFET|沟道)", upper)
        voltage = VOLTAGE.search(description)
        current = CURRENT.search(description)
        return (
            None,
            MosfetDetails(
                channel_type=channel,
                mosfet_count=int(count.group(1)) if count else None,
                voltage_v=float(voltage.group(1)) if voltage else None,
                current_a=float(current.group(1)) if current else None,
            ),
        )
    return None, None


def parse_saved_orders(
    path: Path,
    progress: Callable[[str], None] | None = None,
    warning: Callable[[str], None] | None = None,
    *,
    cancelled_orders: set[str] | None = None,
    allow_empty: bool = False,
) -> list[OrderImportLine]:
    """解析用户保存的静态订单页面并返回去重后的商品行。

    :param path: 本地 ZIP、HTML 或 MHTML 订单文件路径
    :param progress: 可选的线程安全进度回调，内容只包含订单日期和当前元件
    :param warning: 可选警告回调，用于报告静态文件中不存在的折叠商品
    :param cancelled_orders: 合并时用于在两种来源之间同步取消状态
    :param allow_empty: 有对账单补充时，空网页预览仅提示
    :return: 可供用户预览和确认的订单商品行列表
    :raises DomainError: 文件不安全、格式损坏或没有可识别商品时抛出

    注意：解析过程不执行脚本、不访问网络；订单号仅用于去重和幂等导入，联系人、
    物流信息及 URL 查询参数不会进入返回结果。
    """

    ### 安全读取并构建静态 DOM ###
    soup = BeautifulSoup(_read_html(path), "html.parser")
    output: list[OrderImportLine] = []
    seen: set[tuple[str, str]] = set()
    cancelled = cancelled_orders if cancelled_orders is not None else set()
    collapsed: set[str] = set()
    last_progress = ""
    ### 定位订单容器并提取商品行 ###
    order_labels = soup.find_all(string=lambda value: value and value.strip() == "订单编号：")
    for label in order_labels:
        header = label.parent.find_parent("section") if label.parent else None
        order_container = header.find_parent("li") if header else None
        if not isinstance(header, Tag) or not isinstance(order_container, Tag):
            continue
        header_text = _text(header)
        order_match = ORDER_NUMBER.search(header_text)
        date_match = ORDER_DATE.search(header_text)
        if not order_match:
            continue
        order_number = order_match.group(0)
        purchased_at = date_match.group(0) if date_match else None
        order_text = list(order_container.stripped_strings)
        # 仅匹配完整状态文字，仍可执行的「取消订单」按钮属于正常订单。
        if any(CANCELLED_ORDER.fullmatch(value) for value in order_text):
            cancelled.add(order_number)
            continue
        if any("查看全部商品" in value for value in order_text):
            collapsed.add(order_number)
        for product in order_container.find_all(
            "section", class_=lambda value: value and "justify-between" in value
        ):
            items = [_text(item) for item in product.find_all("li")]
            number_item = next((item for item in items if "编号" in item), "")
            price_item = next((item for item in items if "商品单价" in item), "")
            quantity_item = next((item for item in items if "购买数量" in item), "")
            number = LCSC_NUMBER.search(number_item)
            price = PRICE.search(price_item)
            quantity = QUANTITY.search(quantity_item)
            if not number or not quantity or len(items) < 4:
                continue
            lcsc = number.group(0).upper()
            key = (order_number, lcsc)
            if key in seen:  # 同一订单的重复展示区域只保留一份商品。
                continue
            seen.add(key)
            title = re.sub(r"\s+(?:编带|袋装|托盘|盒装)$", "", items[0]).strip()
            title_parts = [part.strip() for part in title.split(" / ", 1)]
            category = title_parts[0] or None
            # 类别已有独立字段，商品描述不再重复显示斜杠前的类别名称。
            description = title_parts[1] if len(title_parts) > 1 else title
            model_parts = [part.strip() for part in items[1].split(" / ", 1)]
            model = _optional_order_text(model_parts[0]) or lcsc
            resistor, mosfet = _category_details(category, description)
            link = next(
                (anchor.get("href") for anchor in product.find_all("a") if anchor.get("href")), None
            )
            if len(output) >= MAX_ORDER_FILE_ITEMS:
                raise DomainError(
                    "VALIDATION_ERROR",
                    f"订单文件超过 {MAX_ORDER_FILE_ITEMS} 条安全上限，请拆分保存订单页面",
                )
            last_progress = (
                f"{purchased_at[:10] if purchased_at else '日期缺省'} · "
                f"{model} · 已解析 {len(output) + 1} 条"
            )
            if progress and (not output or (len(output) + 1) % 25 == 0):
                progress(last_progress)
            output.append(
                OrderImportLine(
                    order_number=order_number,
                    purchased_at=purchased_at,
                    lcsc_part_number=lcsc,
                    name=model,
                    description=description,
                    category=category,
                    manufacturer_part_number=_optional_order_text(model_parts[0]),
                    package=_optional_order_text(model_parts[1]) if len(model_parts) > 1 else None,
                    brand=_optional_order_text(items[2]),
                    unit_price=float(price.group(1)) if price else None,
                    quantity=int(quantity.group(1)),
                    product_url=_clean_url(str(link) if link else None),
                    resistor=resistor,
                    mosfet=mosfet,
                )
            )
    ### 确认至少得到一条可导入记录 ###
    if cancelled:
        output = [line for line in output if line.order_number not in cancelled]
    if not output:
        if cancelled and not collapsed:
            message = "页面中没有可导入的商品，已剔除取消或关闭的订单"
        elif not order_labels and soup.title and "我的订单" in _text(soup.title):
            message = (
                "保存的“我的订单”页面不含订单明细。请在浏览器登录立创商城，"
                "检索后等订单和商品明细显示，再以“网页，全部”保存并重新导入。"
            )
        else:
            message = "页面中没有找到可导入的订单商品"
        if not allow_empty:
            raise DomainError("VALIDATION_ERROR", message)
        if warning:
            warning(message)
    if (active_collapsed := collapsed - cancelled) and warning:
        warning(
            f"检测到 {len(active_collapsed)} 个未展开订单；保存文件只包含网页已加载商品，"
            "额外商品需要在网页展开后重新保存"
        )
    if progress and last_progress:
        progress(last_progress)
    return output


def _optional_order_text(value: object) -> str | None:
    text = str(value).strip()
    return text if text not in {"", "-", "—"} else None


def _statement_number(value: object) -> Decimal:
    number = Decimal(str(value).strip().replace(",", "").lstrip("¥￥"))
    if not number.is_finite():
        raise ValueError("非有限数值")
    return number


def _statement_row(
    sheet: xlrd.sheet.Sheet, row: int, columns: dict[str, int], datemode: int
) -> dict[str, str | None]:
    fields = (
        "订单编号", "订单状态", "下单时间", "商品编号", "商品型号", "商品名称", "商品类型",
        "品牌", "封装格式", "订购数量", "单价（人民币含税）", "单价(人民币含税)",
    )
    values = {}
    for field in fields:
        if field in columns:
            cell = sheet.cell(row, columns[field])
            values[field] = (
                str(xlrd.xldate_as_datetime(cell.value, datemode))
                if cell.ctype == xlrd.XL_CELL_DATE else _optional_order_text(cell.value)
            )
    return values


def _parse_statement(
    path: Path, warning: Callable[[str], None] | None
) -> tuple[dict[tuple[str, str], OrderImportLine], set[str]]:
    """仅从有明细表头的工作表中提取订单字段，排除取消订单和费用行。"""

    if path.stat().st_size > MAX_UNCOMPRESSED_BYTES:
        raise DomainError("VALIDATION_ERROR", "对账单超过 20 MB 安全上限")
    lines: dict[tuple[str, str], OrderImportLine] = {}
    cancelled: set[str] = set()
    required = {"订单编号", "商品编号", "订购数量"}
    found_header = False
    try:
        with xlrd.open_workbook(path, on_demand=True) as workbook:
            for sheet in workbook.sheets():
                if sheet.name == "发票汇总清单" and workbook.nsheets > 1:
                    continue
                for index in range(min(20, sheet.nrows)):
                    columns = {
                        str(value).strip(): column
                        for column, value in enumerate(sheet.row_values(index))
                    }
                    if required <= columns.keys():
                        found_header = True
                        break
                else:
                    continue
                for row in range(index + 1, sheet.nrows):
                    try:
                        values = _statement_row(sheet, row, columns, workbook.datemode)
                        order = values.get("订单编号") or ""
                        part = (values.get("商品编号") or "").upper()
                        if not ORDER_NUMBER.fullmatch(order):
                            continue
                        if CANCELLED_ORDER.fullmatch(values.get("订单状态") or ""):
                            cancelled.add(order)
                            continue
                        if not LCSC_NUMBER.fullmatch(part):
                            continue
                        quantity = _statement_number(values.get("订购数量"))
                        if quantity != quantity.to_integral_value():
                            raise ValueError("订购数量须为整数")
                        price = values.get("单价（人民币含税）") or values.get("单价(人民币含税)")
                        date = values.get("下单时间")
                        purchased_at = (
                            datetime.fromisoformat(date.replace("/", "-")).isoformat(sep=" ")
                            if date else None
                        )
                        model = values.get("商品型号")
                        description = values.get("商品名称")
                        category = values.get("商品类型")
                        resistor, mosfet = _category_details(category, description or "")
                        line = OrderImportLine(
                            order_number=order,
                            purchased_at=purchased_at,
                            lcsc_part_number=part,
                            name=model or description or part,
                            description=description,
                            category=category,
                            manufacturer_part_number=model,
                            brand=values.get("品牌"),
                            package=values.get("封装格式"),
                            purchase_price=float(_statement_number(price)) if price else None,
                            quantity=int(quantity),
                            resistor=resistor,
                            mosfet=mosfet,
                        )
                    except (InvalidOperation, ValidationError, ValueError, OverflowError) as error:
                        raise DomainError(
                            "VALIDATION_ERROR", f"对账单第 {row + 1} 行的数量、单价或日期无效"
                        ) from error
                    key = (order, part)
                    if key in lines and lines[key] != line:
                        raise DomainError(
                            "VALIDATION_ERROR", f"对账单第 {row + 1} 行与同一订单商品的明细冲突"
                        )
                    lines[key] = line
                    if len(lines) > MAX_ORDER_FILE_ITEMS:
                        raise DomainError("VALIDATION_ERROR", "对账单超过 10000 条商品，请拆分后导入")
    except (xlrd.XLRDError, xlrd.compdoc.CompDocError, OSError) as error:
        raise DomainError("VALIDATION_ERROR", "无法读取 XLS 物料明细对账单") from error
    if not found_header:
        raise DomainError("VALIDATION_ERROR", "XLS 中没有物料明细对账单表头")
    if cancelled and warning:
        warning(f"已剔除对账单中 {len(cancelled)} 个取消或关闭的订单")
    return {key: line for key, line in lines.items() if key[0] not in cancelled}, cancelled


def parse_order_files(
    paths: list[Path],
    progress: Callable[[str], None] | None = None,
    warning: Callable[[str], None] | None = None,
) -> list[OrderImportLine]:
    """合并一份网页和一份对账单，按订单号及立创商品编号消除重复。"""

    statements = [path for path in paths if path.suffix.casefold() == ".xls"]
    pages = [path for path in paths if path.suffix.casefold() != ".xls"]
    if not paths or len(statements) > 1 or len(pages) > 1:
        raise DomainError("VALIDATION_ERROR", "请选择至多一份订单网页和一份 XLS 对账单")
    statement, cancelled = _parse_statement(statements[0], warning) if statements else ({}, set())
    page_lines = (
        parse_saved_orders(pages[0], progress, warning, cancelled_orders=cancelled, allow_empty=bool(statement))
        if pages else []
    )
    matched = 0
    quantity_conflicts = 0
    output: list[OrderImportLine] = []
    for page in page_lines:
        detail = statement.pop((page.order_number, page.lcsc_part_number), None)
        if detail is None:
            output.append(page)
            continue
        matched += 1
        quantity_conflicts += detail.quantity != page.quantity
        fields = (
            "purchased_at", "description", "category", "manufacturer_part_number",
            "brand", "package", "resistor", "mosfet",
        )
        updates = {
            field: getattr(detail, field) for field in fields if getattr(page, field) is None
        }
        if page.name == page.lcsc_part_number and detail.name != detail.lcsc_part_number:
            updates["name"] = detail.name
        output.append(page.model_copy(update={
            **updates, "purchase_price": detail.purchase_price, "quantity": detail.quantity,
        }))
    output.extend(line for line in statement.values() if line.order_number not in cancelled)
    if not output:
        raise DomainError("VALIDATION_ERROR", "文件中没有可导入的订单商品")
    if len(output) > MAX_ORDER_FILE_ITEMS:
        raise DomainError("VALIDATION_ERROR", "合并结果超过 10000 条商品，请拆分后导入")
    if pages and statements and warning:
        message = f"两份文件匹配 {matched} 条，仅出现在一份文件中的有 {len(output) - matched} 条"
        if quantity_conflicts:
            message += f"；{quantity_conflicts} 条数量不一致，预览采用对账单数量"
        warning(message)
    return output
