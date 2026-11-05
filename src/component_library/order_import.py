"""离线解析用户从浏览器保存的立创商城订单页面。

解析器绝不执行 HTML 中的脚本、不读取 Cookie，也不访问页面中的网络链接。压缩包只在
内存中读取一个 HTML/MHTML 文件，并限制文件数和解压后大小以避免恶意压缩包。
页面中的收货人、联系人、物流信息和 URL 查询参数不会进入返回结果。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup, Tag

from .database import DomainError
from .models import MosfetDetails, OrderImportLine, ResistorDetails

MAX_FILES = 500
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
ORDER_NUMBER = re.compile(r"SO\d{8,16}")
ORDER_DATE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
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


def parse_saved_orders(path: Path) -> list[OrderImportLine]:
    """解析静态订单页面并返回去重后的商品行。"""

    soup = BeautifulSoup(_read_html(path), "html.parser")
    output: list[OrderImportLine] = []
    seen: set[tuple[str, str]] = set()
    for label in soup.find_all(string=lambda value: value and value.strip() == "订单编号："):
        header = label.parent.find_parent("section") if label.parent else None
        order_container = header.find_parent("li") if header else None
        if not isinstance(header, Tag) or not isinstance(order_container, Tag):
            continue
        header_text = _text(header)
        order_match = ORDER_NUMBER.search(header_text)
        date_match = ORDER_DATE.search(header_text)
        if not order_match or not date_match:
            continue
        order_number = order_match.group(0)
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
            if not number or not price or not quantity or len(items) < 4:
                continue
            lcsc = number.group(0).upper()
            key = (order_number, lcsc)
            if key in seen:
                continue
            seen.add(key)
            title = re.sub(r"\s+(?:编带|袋装|托盘|盒装)$", "", items[0]).strip()
            title_parts = [part.strip() for part in title.split(" / ", 1)]
            category = title_parts[0] or None
            # 类别已有独立字段，商品描述不再重复显示斜杠前的类别名称。
            description = title_parts[1] if len(title_parts) > 1 else title
            model_parts = [part.strip() for part in items[1].split(" / ", 1)]
            model = model_parts[0] or lcsc
            resistor, mosfet = _category_details(category, description)
            link = next(
                (anchor.get("href") for anchor in product.find_all("a") if anchor.get("href")), None
            )
            output.append(
                OrderImportLine(
                    order_number=order_number,
                    purchased_at=date_match.group(0),
                    lcsc_part_number=lcsc,
                    name=model,
                    description=description,
                    category=category,
                    manufacturer_part_number=model_parts[0] or None,
                    package=model_parts[1]
                    if len(model_parts) > 1 and model_parts[1] != "-"
                    else None,
                    brand=items[2] or None,
                    unit_price=float(price.group(1)),
                    quantity=int(quantity.group(1)),
                    product_url=_clean_url(str(link) if link else None),
                    resistor=resistor,
                    mosfet=mosfet,
                )
            )
    if not output:
        raise DomainError("VALIDATION_ERROR", "页面中没有找到可导入的订单商品")
    return output
