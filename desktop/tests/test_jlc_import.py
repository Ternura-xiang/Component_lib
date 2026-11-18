"""用户复制的商城资料解析测试。"""

from component_library.jlc_import import parse_copied_product


def test_parse_copied_product() -> None:
    text = """厚声贴片电阻
C270654  0402
价格：￥0.0123
https://www.szlcsc.com/product/C270654.html
https://atta.szlcsc.com/upload/public/pdf/example.pdf
"""
    result = parse_copied_product(text)
    assert result["lcsc_part_number"] == "C270654"
    assert result["package"] == "0402"
    assert result["price"] == 0.0123
    assert result["product_url"].endswith("C270654.html")
    assert result["datasheet_url"].endswith("example.pdf")
