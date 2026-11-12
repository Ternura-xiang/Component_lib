"""内嵌浏览器商品页的纯离线解析测试。"""

import sqlite3
from pathlib import Path

import pytest

from component_library.database import Database, DomainError
from component_library.jlc_product import (
    find_jlc_product_link,
    is_blocked_jlc_navigation,
    is_jlc_product_url,
    parse_jlc_product_page,
)
from component_library.models import ComponentCreate

PRODUCT_HTML = """
<html><head>
  <meta name="product:category" content="贴片电阻">
  <script type="application/ld+json">{
    "@type":"Product", "name":"厚膜贴片电阻 91Ω", "sku":"C270654",
    "mpn":"FRC0402F91R0TS", "brand":{"name":"FOJAN 丰甲"},
    "description":"0402 91Ω ±1% 62.5mW"
  }</script>
</head><body>
  <h3>商品概览</h3><table>
    <tr><th>页面库存</th><td>999</td></tr>
  </table>
  <h2>商品参数</h2>
  <h3>基础参数</h3><table>
    <tr><th></th><th>属性</th><th>参数值</th></tr>
    <tr><td><input type="checkbox"></td><th>商品封装</th><td>0402</td></tr>
    <tr><td><input type="checkbox"></td><th>阻值</th><td>91Ω</td></tr>
    <tr><td><input type="checkbox"></td><th>精度</th><td>±1%</td></tr>
  </table>
  <h3>环境参数</h3><dl><dt>工作温度</dt><dd>-55℃~155℃</dd></dl>
  <h2>替代料</h2><table>
    <tr><th>替代型号</th><td>不应保存</td></tr>
  </table>
  <h2>买了又买</h2><table>
    <tr><th>推荐商品</th><td>不应保存</td></tr>
  </table>
  <a href="https://atta.szlcsc.com/upload/public/pdf/C270654.pdf">数据手册</a>
</body></html>
"""


def test_parse_rendered_product_page_without_network() -> None:
    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    assert draft.lcsc_part_number == "C270654"
    assert draft.name == "FRC0402F91R0TS"
    assert draft.brand == "FOJAN 丰甲"
    assert draft.package == "0402"
    assert draft.category == "贴片电阻"
    assert draft.datasheet_url and draft.datasheet_url.endswith("C270654.pdf")
    groups = {group.name: group for group in draft.parameter_groups}
    assert [item.label for item in groups["基础参数"].parameters] == [
        "商品封装",
        "阻值",
        "精度",
    ]
    assert groups["基础参数"].parameters[1].key == "resistance"
    assert groups["环境参数"].parameters[0].key.startswith("jlc_")
    labels = {item.label for group in draft.parameter_groups for item in group.parameters}
    assert "页面库存" not in labels
    assert "替代型号" not in labels
    assert "推荐商品" not in labels


def test_online_product_matches_exact_component_and_preserves_existing_fields(tmp_path: Path) -> None:
    draft = parse_jlc_product_page(
        "https://item.szlcsc.com/12345.html", PRODUCT_HTML, require_page_number=True
    )
    database = Database(tmp_path / "inventory.db")
    item = database.create_component(
        ComponentCreate(name="我的电阻", brand="已有品牌", lcsc_part_number=draft.lcsc_part_number)
    )
    other = database.create_component(ComponentCreate(name="其他电阻", lcsc_part_number="C34567"))
    database.save_component_parameters(item["id"], {"resistance": ("阻值", "100Ω")})
    assert database.apply_jlc_product_match(other["id"], draft) is False
    assert database.apply_jlc_product_match(item["id"], draft) is True
    detail = database.get_component(item["id"])
    assert (detail["name"], detail["brand"], detail["package"]) == ("我的电阻", "已有品牌", "0402")
    parameters = {parameter["parameter_key"]: parameter["value"] for parameter in detail["category_parameters"]}
    assert parameters["resistance"] == "100Ω" and parameters["tolerance"] == "±1%"
    assert detail["jlc_matched"] == 1
    assert database.get_component(other["id"])["jlc_matched"] == 0
    assert database.apply_jlc_product_match(item["id"], draft) is False
    assert database.next_unmatched_jlc_component(item["id"])["id"] == other["id"]


def test_background_search_requires_exact_link_and_detail_identity() -> None:
    url = "https://so.szlcsc.com/global.html?k=C270654"
    html = """
    <div><a href="https://item.szlcsc.com/1000.html">A</a><span>C270654</span></div>
    <div><a href="https://item.szlcsc.com/2000.html">B</a><span>C2706540</span></div>
    """
    assert find_jlc_product_link(url, html, "C270654") == "https://item.szlcsc.com/1000.html"
    assert find_jlc_product_link(url, html, "C27065") is None
    with pytest.raises(DomainError, match="立创编号"):
        parse_jlc_product_page(
            "https://item.szlcsc.com/product/C270654.html",
            "<h1>其他商品</h1><aside>相关推荐 C270654</aside>",
            require_page_number=True,
        )


def test_product_parser_rejects_login_wall_even_with_product_url() -> None:
    with pytest.raises(DomainError, match="要求登录或验证"):
        parse_jlc_product_page(
            "https://item.szlcsc.com/product/C270654.html",
            "<html><title>立创商城</title><p>请登录后访问</p></html>",
        )


def test_only_product_parameter_section_is_imported() -> None:
    """商品参数、替代料和买了又买并列时，只保存商品参数表。"""

    html = """
    <html><head><script type="application/ld+json">
      {"@type":"Product", "sku":"C123456", "name":"测试 MCU", "mpn":"MCU-1"}
    </script></head><body>
      <nav><span>商品参数</span><span>替代料</span><span>买了又买</span></nav>
      <section><h2><span>商品参数</span></h2><table>
        <tr><td><input type="checkbox"></td><td>商品目录</td><td>单片机(MCU/MPU/SOC)</td></tr>
        <tr><td><input type="checkbox"></td><td>CPU内核</td><td>ARM Cortex-M7</td></tr>
        <tr><td><input type="checkbox"></td><td>CPU最大主频</td><td>528MHz</td></tr>
      </table></section>
      <section><h2>替代料</h2><table><tr><td>型号</td><td>ALT-1</td></tr></table></section>
      <section><h2>买了又买</h2><table><tr><td>商品</td><td>OTHER-1</td></tr></table></section>
    </body></html>
    """
    draft = parse_jlc_product_page("https://item.szlcsc.com/654321.html", html)
    assert len(draft.parameter_groups) == 1
    assert draft.category == "单片机(MCU/MPU/SOC)"
    assert draft.parameter_groups[0].name == "商品参数"
    assert [(item.label, item.value) for item in draft.parameter_groups[0].parameters] == [
        ("CPU内核", "ARM Cortex-M7"),
        ("CPU最大主频", "528MHz"),
    ]


def test_parse_legacy_four_digit_number_and_summary_fields() -> None:
    """历史四位立创编号及参数栏外的商品型号、品牌、封装都应正确识别。"""

    html = """
    <html><body>
      <h1>STM32F103C8T6</h1>
      <div class="summary"><span>品牌名称</span><span>ST(意法半导体)</span></div>
      <div class="summary"><span>商品型号</span><span>STM32F103C8T6</span></div>
      <div class="summary"><span>商品编号</span><span>C8734</span></div>
      <div class="summary"><span>商品封装</span><span>LQFP-48(7x7)</span></div>
      <h2>商品参数</h2><table>
        <tr><td></td><td>商品目录</td><td>单片机(MCU/MPU/SOC)</td></tr>
        <tr><td></td><td>CPU内核</td><td>ARM Cortex-M3</td></tr>
      </table>
      <h2>替代料</h2>
    </body></html>
    """
    draft = parse_jlc_product_page("https://item.szlcsc.com/9243.html?fromZone=search", html)
    assert draft.lcsc_part_number == "C8734"
    assert draft.name == "STM32F103C8T6"
    assert draft.manufacturer_part_number == "STM32F103C8T6"
    assert draft.brand == "ST(意法半导体)"
    assert draft.package == "LQFP-48(7x7)"
    assert draft.category == "单片机(MCU/MPU/SOC)"


def test_product_parser_rejects_search_and_missing_number() -> None:
    assert is_jlc_product_url("https://item.szlcsc.com/123456.html")
    assert not is_jlc_product_url("https://so.szlcsc.com/global.html?k=0402")
    assert is_blocked_jlc_navigation("https://www.szlcsc.com/login.html")
    assert is_blocked_jlc_navigation("https://example.com/product/C270654.html")
    with pytest.raises(DomainError, match="不是可确认"):
        parse_jlc_product_page("https://www.szlcsc.com/", PRODUCT_HTML)
    with pytest.raises(DomainError, match="立创编号"):
        parse_jlc_product_page(
            "https://item.szlcsc.com/123456.html", "<html><h1>无编号商品</h1></html>"
        )


def test_group_name_migrates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    database = Database(path)
    component = database.create_component(ComponentCreate(name="测试元件"))
    draft = parse_jlc_product_page("https://item.szlcsc.com/123456.html", PRODUCT_HTML)
    database.save_component_parameter_groups(component["id"], draft.parameter_groups)
    detail = database.get_component(component["id"])
    assert {item["group_name"] for item in detail["category_parameters"]} == {
        "基础参数",
        "环境参数",
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone()
