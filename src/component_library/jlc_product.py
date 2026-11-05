"""解析用户在内嵌浏览器中明确选择的立创商品页。

本模块不发起网络请求，只接收浏览器已经渲染完成的 URL 和 HTML。解析失败时返回明确
错误，不猜测商品或访问其他页面。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .database import DomainError
from .models import JlcParameter, JlcParameterGroup, JlcProductDraft

LCSC_NUMBER = re.compile(r"(?<![A-Z0-9])C\d{4,10}(?!\d)", re.IGNORECASE)
DETAIL_PATHS = (
    re.compile(r"^/\d+\.html$"),
    re.compile(r"^/product/C\d{4,10}\.html$", re.IGNORECASE),
)
OFFICIAL_HOSTS = {"szlcsc.com", "www.szlcsc.com", "so.szlcsc.com", "item.szlcsc.com"}
BLOCKED_PATH_WORDS = ("login", "passport", "account", "member", "cart", "checkout", "order")
PARAMETER_SECTION_TITLE = "商品参数"
PARAMETER_SECTION_BOUNDARIES = {"替代料", "买了又买"}
PARAMETER_HEADER_LABELS = {"属性", "参数", "参数名称", "商品属性"}
PARAMETER_HEADER_VALUES = {"参数值", "值", "属性值"}
KNOWN_PARAMETER_KEYS = {
    "阻值": "resistance",
    "电阻值": "resistance",
    "精度": "tolerance",
    "容差": "tolerance",
    "功率": "power",
    "额定功率": "power",
    "封装": "package",
    "商品封装": "package",
    "品牌": "brand",
    "制造商": "brand",
    "型号": "manufacturer_part_number",
    "制造商型号": "manufacturer_part_number",
    "沟道类型": "channel",
    "漏源电压": "voltage",
    "耐压": "voltage",
    "连续漏极电流": "current",
    "电流": "current",
    "电容量": "capacitance",
    "容值": "capacitance",
}


def is_official_jlc_url(url: str) -> bool:
    """判断 URL 是否属于允许在匿名浏览器中显示的立创商城 HTTPS 页面。"""

    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in OFFICIAL_HOSTS


def is_blocked_jlc_navigation(url: str) -> bool:
    """阻止匿名选型窗口进入登录、账户、购物车、订单和结算页面。"""

    if not is_official_jlc_url(url):
        return True
    path = urlparse(url).path.casefold()
    return any(word in path for word in BLOCKED_PATH_WORDS)


def is_jlc_product_url(url: str) -> bool:
    """识别公开商品详情页，不把搜索结果页误当成已选商品。"""

    if not is_official_jlc_url(url):
        return False
    path = urlparse(url).path
    return any(pattern.fullmatch(path) for pattern in DETAIL_PATHS)


def _clean(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n:：")
    return text or None


def _find_product_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        return next((found for item in value if (found := _find_product_json(item))), None)
    if not isinstance(value, dict):
        return None
    kinds = value.get("@type", [])
    if kinds == "Product" or (isinstance(kinds, list) and "Product" in kinds):
        return value
    return _find_product_json(value.get("@graph"))


def _json_product(soup: BeautifulSoup) -> dict[str, Any]:
    for node in soup.select("script[type='application/ld+json']"):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if product := _find_product_json(value):
            return product
    return {}


def _brand(product: dict[str, Any]) -> str | None:
    raw = product.get("brand")
    if isinstance(raw, str):
        return _clean(raw)
    return _clean(raw.get("name")) if isinstance(raw, dict) else None


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    lowered = {name.casefold() for name in names}
    for node in soup.find_all("meta"):
        key = str(node.get("name") or node.get("property") or "").casefold()
        if key in lowered:
            return _clean(node.get("content"))
    return None


def _parameter_key(group: str, label: str, used: set[str]) -> str:
    normalized = re.sub(r"[\s:：()（）/_-]+", "", label).casefold()
    base = KNOWN_PARAMETER_KEYS.get(normalized)
    if not base:
        digest = hashlib.sha1(f"{group}\0{label}".encode()).hexdigest()[:12]
        base = f"jlc_{digest}"
    key = base
    suffix = 2
    while key in used:
        key = f"{base}_{suffix}"
        suffix += 1
    used.add(key)
    return key


def _is_exact_section_marker(node: Tag, titles: set[str]) -> bool:
    """判断节点是否为独立栏目标题，并排除包裹同名子节点的外层容器。"""

    if node.name not in {"h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "a", "li", "button"}:
        return False
    text = _clean(node.get_text(" ", strip=True))
    if text not in titles:
        return False
    return not any(
        isinstance(child, Tag) and _clean(child.get_text(" ", strip=True)) == text
        for child in node.children
    )


def _valid_parameter_pair(label: str | None, value: str | None) -> bool:
    """过滤空单元格、表头以及不能构成属性和值的行。"""

    if not label or not value or label == value:
        return False
    return not (label in PARAMETER_HEADER_LABELS and value in PARAMETER_HEADER_VALUES)


def _pairs_after_marker(nodes: list[Tag], marker_index: int) -> list[tuple[str, str, str]]:
    """读取“商品参数”标题之后、下一个并列栏目之前的键值对。"""

    pairs: list[tuple[str, str, str]] = []
    group = PARAMETER_SECTION_TITLE
    for node in nodes[marker_index + 1 :]:
        if _is_exact_section_marker(node, PARAMETER_SECTION_BOUNDARIES):
            break
        if node.name in {"h2", "h3", "h4", "h5", "h6"}:
            heading = _clean(node.get_text(" ", strip=True))
            if heading and heading != PARAMETER_SECTION_TITLE:
                group = heading[:80]
            continue
        if node.name == "tr":
            # 商品参数表首列通常是复选框，不能固定取第一列；取最后两个有文本的
            # 单元格，分别对应截图中的“属性”和“参数值”。
            cell_texts = [
                text
                for cell in node.find_all(["th", "td"], recursive=False)
                if (text := _clean(cell.get_text(" ", strip=True)))
            ]
            if len(cell_texts) >= 2:
                label, value = cell_texts[-2:]
                if _valid_parameter_pair(label, value):
                    pairs.append((group, label, value))
        elif node.name == "dt":
            value_node = node.find_next_sibling("dd")
            dt_label = _clean(node.get_text(" ", strip=True))
            dt_value = _clean(value_node.get_text(" ", strip=True)) if value_node else None
            if dt_label and dt_value and _valid_parameter_pair(dt_label, dt_value):
                pairs.append((group, dt_label, dt_value))
        elif node.has_attr("data-param-name") and node.has_attr("data-param-value"):
            data_label = _clean(node.get("data-param-name"))
            data_value = _clean(node.get("data-param-value"))
            if data_label and data_value and _valid_parameter_pair(data_label, data_value):
                pairs.append((group, data_label, data_value))
    return pairs


def _html_parameter_pairs(soup: BeautifulSoup) -> Iterable[tuple[str, str, str]]:
    """只提取商品介绍中“商品参数”栏目，不读取替代料和推荐商品。"""

    nodes = list(soup.find_all(True))
    candidates = [
        _pairs_after_marker(nodes, index)
        for index, node in enumerate(nodes)
        if _is_exact_section_marker(node, {PARAMETER_SECTION_TITLE})
    ]
    # 页面导航和正文可能各有一个“商品参数”文字。导航项之后会立刻遇到
    # “替代料”，没有参数行；选择有效参数最多的候选即可定位到正文栏目。
    return max(candidates, key=len, default=[])


def _parameter_groups(soup: BeautifulSoup) -> list[JlcParameterGroup]:
    raw = list(_html_parameter_pairs(soup))
    groups: dict[str, list[JlcParameter]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    used_keys: set[str] = set()
    for group, label, value in raw:
        pair = (label.casefold(), value.casefold())
        if pair in seen_pairs or len(label) > 80 or len(value) > 500:
            continue
        seen_pairs.add(pair)
        groups.setdefault(group, []).append(
            JlcParameter(key=_parameter_key(group, label, used_keys), label=label, value=value)
        )
    return [JlcParameterGroup(name=name, parameters=items) for name, items in groups.items()]


def _find_parameter(groups: list[JlcParameterGroup], *labels: str) -> str | None:
    candidates = {re.sub(r"\s+", "", label).casefold() for label in labels}
    for group in groups:
        for parameter in group.parameters:
            normalized = re.sub(r"\s+", "", parameter.label).casefold()
            if normalized in candidates:
                return parameter.value
    return None


def _page_label_value(soup: BeautifulSoup, *labels: str) -> str | None:
    """读取商品页顶部摘要中的标签和值，例如商品型号和商品封装。

    这些字段位于“商品参数”栏目之外，因此不能混入动态参数；这里只针对调用方
    明确给出的主字段标签查找相邻值，并限制长度以避免误取整个页面容器。
    """

    expected = {re.sub(r"\s+", "", label).casefold() for label in labels}

    def is_label(value: str | None) -> bool:
        return bool(value and re.sub(r"\s+", "", value).casefold() in expected)

    for row in soup.find_all("tr"):
        texts = [
            text
            for cell in row.find_all(["th", "td"], recursive=False)
            if (text := _clean(cell.get_text(" ", strip=True)))
        ]
        if len(texts) >= 2 and is_label(texts[-2]) and len(texts[-1]) <= 200:
            return texts[-1]
    for term in soup.find_all("dt"):
        label = _clean(term.get_text(" ", strip=True))
        value_node = term.find_next_sibling("dd")
        value = _clean(value_node.get_text(" ", strip=True)) if value_node else None
        if is_label(label) and value and len(value) <= 200:
            return value
    for node in soup.find_all(True):
        label = _clean(node.get_text(" ", strip=True))
        if not is_label(label):
            continue
        anchors = [node]
        if isinstance(node.parent, Tag):
            anchors.append(node.parent)
            if isinstance(node.parent.parent, Tag):
                anchors.append(node.parent.parent)
        for anchor in anchors:
            for sibling in anchor.next_siblings:
                value = (
                    _clean(sibling.get_text(" ", strip=True))
                    if isinstance(sibling, Tag)
                    else _clean(sibling)
                )
                if value and not is_label(value) and len(value) <= 200:
                    return value
    return None


def _without_parameters(groups: list[JlcParameterGroup], *labels: str) -> list[JlcParameterGroup]:
    """从动态参数中移除已经提升为主字段的项目，并保留其他分组顺序。"""

    excluded = {re.sub(r"\s+", "", label).casefold() for label in labels}
    output: list[JlcParameterGroup] = []
    for group in groups:
        parameters = [
            parameter
            for parameter in group.parameters
            if re.sub(r"\s+", "", parameter.label).casefold() not in excluded
        ]
        if parameters:
            output.append(group.model_copy(update={"parameters": parameters}))
    return output


def _datasheet_url(soup: BeautifulSoup, base_url: str) -> str | None:
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]))
        text = anchor.get_text(" ", strip=True).casefold()
        path = urlparse(href).path.casefold()
        if path.endswith(".pdf") or "数据手册" in text or "datasheet" in text:
            parsed = urlparse(href)
            if parsed.scheme == "https" and parsed.hostname:
                return href
    return None


def parse_jlc_product_page(url: str, html: str) -> JlcProductDraft:
    """解析当前商品详情页；输入只存在于调用期间，不写文件也不访问网络。"""

    if not is_jlc_product_url(url):
        raise DomainError("JLC_NOT_PRODUCT", "当前页面不是可确认的立创商品详情页")
    if not html.strip():
        raise DomainError("JLC_EMPTY_PAGE", "当前商品页没有可解析内容")
    soup = BeautifulSoup(html, "html.parser")
    product = _json_product(soup)
    page_text = soup.get_text(" ", strip=True)
    lcsc_match = LCSC_NUMBER.search(
        " ".join(
            filter(
                None,
                [
                    str(product.get("sku") or ""),
                    _meta(soup, "product:retailer_item_id", "sku") or "",
                    url,
                    page_text,
                ],
            )
        )
    )
    if not lcsc_match:
        raise DomainError("JLC_MISSING_NUMBER", "商品页中没有找到可确认的立创编号")
    lcsc = lcsc_match.group(0).upper()
    groups = _parameter_groups(soup)
    mpn = (
        _clean(product.get("mpn"))
        or _page_label_value(soup, "商品型号", "制造商型号", "产品型号")
        or _find_parameter(groups, "制造商型号", "型号", "产品型号")
    )
    brand = (
        _brand(product)
        or _page_label_value(soup, "品牌名称", "品牌", "制造商")
        or _find_parameter(groups, "品牌", "制造商")
    )
    package = _page_label_value(soup, "商品封装", "封装", "封装规格") or _find_parameter(
        groups, "商品封装", "封装", "封装规格"
    )
    # 商城参数表中的“商品目录”是最准确的元件类别。提升为主字段后从动态参数中
    # 删除，避免入库表单同时出现“元件类别”和重复的“商品目录”输入框。
    category = _find_parameter(groups, "商品目录") or _meta(soup, "product:category")
    groups = _without_parameters(groups, "商品目录")
    if not category:
        breadcrumb = soup.select("[itemprop='itemListElement'] [itemprop='name']")
        category = _clean(breadcrumb[-1].get_text(" ", strip=True)) if breadcrumb else None
    description = _clean(product.get("description")) or _meta(soup, "description")
    raw_name = mpn or _clean(product.get("name")) or lcsc
    warnings: list[str] = []
    if not mpn:
        warnings.append("页面未提供明确的制造商型号，名称已回退到商品名称或立创编号")
    if not groups:
        warnings.append("没有识别到商品参数，请在入库表单中手工补充")
    return JlcProductDraft(
        lcsc_part_number=lcsc,
        name=raw_name,
        manufacturer_part_number=mpn,
        brand=brand,
        package=package,
        category=category,
        description=description,
        product_url=url,
        datasheet_url=_datasheet_url(soup, url),
        parameter_groups=groups,
        warnings=warnings,
    )
