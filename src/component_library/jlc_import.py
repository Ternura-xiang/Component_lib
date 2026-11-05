"""解析用户从立创商城页面主动复制的公开商品信息。

立创商城用户协议不允许未经书面许可的机器人访问网站，因此本模块不发起网络请求。
它只处理用户在普通浏览器中查看、确认并复制到剪贴板的文字，结果仍是可编辑草稿。
"""

from __future__ import annotations

import re
from typing import Any

LCSC = re.compile(r"(?<![A-Z0-9])C\d{4,10}(?!\d)", re.IGNORECASE)
PRICE = re.compile(r"(?:￥|¥|价格\s*[:：]?)\s*(\d+(?:\.\d{1,6})?)")
PACKAGE = re.compile(
    r"\b(?:0[2468]0[1235]|1206|1210|SOD[- ]?\d+|SOT[- ]?\d+|SOIC[- ]?\d+|QFN[- ]?\d+|QFP[- ]?\d+)\b",
    re.IGNORECASE,
)
URL = re.compile(r"https://[^\s<>\"']+")


def parse_copied_product(text: str) -> dict[str, Any]:
    """从复制文字中提取确定性较高的字段，无法确认的内容保持为空。"""

    result: dict[str, Any] = {}
    if match := LCSC.search(text):
        result["lcsc_part_number"] = match.group(0).upper()
    if match := PRICE.search(text):
        result["price"] = float(match.group(1))
    if match := PACKAGE.search(text):
        result["package"] = match.group(0).upper().replace(" ", "-")
    urls = [match.group(0).rstrip(".,;，；") for match in URL.finditer(text)]
    for url in urls:
        lowered = url.casefold()
        if lowered.endswith(".pdf") or "/pdf/" in lowered:
            result.setdefault("datasheet_url", url)
        elif "szlcsc.com" in lowered:
            result.setdefault("product_url", url)
    lines = [line.strip() for line in text.splitlines() if 2 <= len(line.strip()) <= 120]
    if lines:
        result["suggested_name"] = lines[0]
    return result
