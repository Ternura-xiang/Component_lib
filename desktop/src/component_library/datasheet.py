"""用字符串处理规格书地址。"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

JLC_ITEM_PATH = re.compile(r"/(?:mro/)?([0-9]+)\.html")
JLC_PART_PATH = re.compile(r"/product/(C[0-9]{4,10})\.html", re.IGNORECASE)
JLC_DATASHEET_PATH = re.compile(r"/datasheet/[^/]+/[0-9]+\.html")


def jlc_product_key(url: str | None) -> str | None:
    """提取公开商品链接的稳定标识，忽略查询参数和片段。"""

    try:
        parsed = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "item.szlcsc.com", "www.szlcsc.com", "szlcsc.com",
    }:
        return None
    if match := JLC_ITEM_PATH.fullmatch(parsed.path):
        return match[1]
    if match := JLC_PART_PATH.fullmatch(parsed.path):
        return match[1].upper()
    return None


def can_match_jlc_component(part_number: str | None, product_url: str | None) -> bool:
    return bool(
        jlc_product_key(product_url)
        or re.fullmatch(r"C[0-9]{4,10}", (part_number or "").strip(), re.IGNORECASE)
    )


def jlc_datasheet_url(
    product_url: str | None, manufacturer_part_number: str | None
) -> str | None:
    """用立创商品页的数字 ID 和型号构造规格书查看地址。"""

    model = (manufacturer_part_number or "").strip()
    if not product_url or not model or model in {".", ".."}:
        return None
    try:
        parsed = urlsplit(product_url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "item.szlcsc.com", "www.szlcsc.com"
    }:
        return None
    if match := JLC_ITEM_PATH.fullmatch(parsed.path):
        return f"https://item.szlcsc.com/datasheet/{quote(model, safe='')}/{match[1]}.html"
    return None


def normalize_datasheet_url(url: str | None) -> str | None:
    """去除 PDF 及立创规格书查看地址的查询参数和片段。"""

    url = (url or "").strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if parsed.scheme in {"http", "https"} and parsed.hostname and (
        parsed.path.lower().endswith(".pdf")
        or (parsed.hostname == "item.szlcsc.com" and JLC_DATASHEET_PATH.fullmatch(parsed.path))
    ):
        return parsed._replace(query="", fragment="").geturl()
    return url
