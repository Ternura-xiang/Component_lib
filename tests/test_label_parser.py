"""两张实物袋贴的字段解析回归测试。"""

from component_library.label_parser import (
    merge_fields,
    parse_label_text,
    parse_ocr_lines,
    parse_qr_payload,
)
from component_library.models import OcrLine


def line(text: str, row: int, confidence: float = 0.98) -> OcrLine:
    """生成带稳定坐标的 OCR 测试行。"""

    return OcrLine(
        text=text, confidence=confidence, box=[[0, row], [100, row], [100, row + 1], [0, row + 1]]
    )


def test_first_real_bag_qr() -> None:
    payload = "{on:SO26090420076,pc:C270654,pm:0402WGF1072TCE,qty:100,mc:,cc:1,pdi:234895663,hp:12}"
    fields = parse_qr_payload(payload)
    assert fields["lcsc_part_number"].normalized_value == "C270654"
    assert fields["manufacturer_part_number"].normalized_value == "0402WGF1072TCE"
    assert fields["quantity"].normalized_value == "100"


def test_second_real_bag_from_printed_text() -> None:
    lines = [
        line("3439-B-B04-001", 0),
        line("C7380001", 1),
        line("26+ ROHS", 2),
        line("电压:40V 电流:2A", 3),
        line("SOD-323", 4),
        line("MS2A40LWS", 5),
        line("MSKSEMI(美森科)", 6),
        line("QTY:20个", 7),
        line("单肖特基二极管", 8),
        line("SO26090413864", 9),
    ]
    fields = parse_ocr_lines(lines)
    assert fields["lcsc_part_number"].normalized_value == "C7380001"
    assert fields["manufacturer_part_number"].normalized_value == "MS2A40LWS"
    assert fields["package"].normalized_value == "SOD-323"
    assert fields["quantity"].normalized_value == "20"
    assert fields["voltage"].normalized_value == "40V"
    assert fields["current"].normalized_value == "2A"


def test_conflicting_ocr_and_barcode_never_silently_merge() -> None:
    ocr = parse_ocr_lines([line("C7380001", 0)])
    barcode = parse_qr_payload("{pc:C270654,qty:100}")
    fields, warnings = merge_fields(ocr, barcode)
    assert fields["lcsc_part_number"].normalized_value == "C7380001"
    assert fields["lcsc_part_number"].validated is False
    assert warnings


def test_text_only_parser() -> None:
    draft = parse_label_text("C7380001\nSOD-323\nMS2A40LWS\nQTY:20个")
    assert draft.fields["quantity"].normalized_value == "20"
