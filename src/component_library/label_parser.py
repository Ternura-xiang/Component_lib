"""嘉立创袋贴文字与二维码字段解析。

解析器只生成候选字段，不负责创建元件或修改库存。规则采用版本化模块，未来出现
新版袋贴时可以并行添加解析器，避免修改已有样本的行为。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import LabelDraft, OcrLine, ParsedField

# OCR 可能把标签右侧的“静”等汉字紧贴编号。Unicode 词边界会把数字和汉字都视作
# “单词字符”，所以用显式的字母数字边界，确保 ``C7380001静`` 仍可提取编号。
LCSC_PATTERN = re.compile(r"(?<![A-Z0-9])C\d{4,10}(?!\d)", re.IGNORECASE)
ORDER_PATTERN = re.compile(r"(?<![A-Z0-9])SO\d{8,16}(?!\d)", re.IGNORECASE)
POSITION_PATTERN = re.compile(r"\b[A-Z0-9]{3,6}-[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]{3,6}\b", re.IGNORECASE)
QUANTITY_PATTERN = re.compile(r"(?:QTY|数量)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
PACKAGE_PATTERN = re.compile(
    r"\b(?:0[2468]0[1235]|1206|1210|SOD[- ]?\d+|SOT[- ]?\d+|SOIC[- ]?\d+|QFN[- ]?\d+|QFP[- ]?\d+)\b",
    re.IGNORECASE,
)
# 中文紧跟在单位字母后时，Unicode ``\b`` 不一定形成词边界，因此不能在这里使用。
VOLTAGE_PATTERN = re.compile(r"(?:电压\s*[:：]?\s*)?(\d+(?:\.\d+)?\s*[mMkK]?V)")
CURRENT_PATTERN = re.compile(r"(?:电流\s*[:：]?\s*)?(\d+(?:\.\d+)?\s*[mM]?A)")


def normalize_token(value: str) -> str:
    """规范化 OCR 常见空白和全角标点，不主动猜测字符。"""

    return re.sub(r"\s+", "", value.strip().replace("：", ":"))


def parse_qr_payload(payload: str) -> dict[str, ParsedField]:
    """解析嘉立创袋贴的 ``{key:value,...}`` 二维码文本。"""

    text = payload.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return {}
    pairs: dict[str, str] = {}
    for item in text[1:-1].split(","):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if value.strip():
            pairs[key.strip().lower()] = value.strip()
    mapping = {
        "on": "order_number",
        "pc": "lcsc_part_number",
        "pm": "manufacturer_part_number",
        "qty": "quantity",
        "pdi": "product_data_id",
    }
    result: dict[str, ParsedField] = {}
    for key, field_name in mapping.items():
        if qr_value := pairs.get(key):
            result[field_name] = ParsedField(
                raw_value=qr_value,
                normalized_value=normalize_token(qr_value),
                confidence=1.0,
                source="barcode",
                validated=True,
            )
    return result


def _field(value: str, confidence: float, line: OcrLine, *, validated: bool = True) -> ParsedField:
    """从 OCR 行创建字段并保留来源坐标。"""

    return ParsedField(
        raw_value=value,
        normalized_value=normalize_token(value),
        confidence=max(0.0, min(1.0, confidence)),
        source="ocr",
        region=line.box,
        validated=validated,
    )


def parse_ocr_lines(lines: Iterable[OcrLine]) -> dict[str, ParsedField]:
    """按页面坐标排序 OCR 行并提取袋贴字段。

    制造商料号没有全球统一格式，因此只在封装下一行或具有明显字母数字组合时生成
    候选，并把 ``validated`` 设为假，要求用户确认。
    """

    ordered = sorted(
        lines,
        key=lambda line: (min(point[1] for point in line.box), min(point[0] for point in line.box)),
    )
    result: dict[str, ParsedField] = {}
    package_index: int | None = None
    for index, line in enumerate(ordered):
        text = line.text.strip()
        compact = normalize_token(text)
        if match := LCSC_PATTERN.search(compact):
            result.setdefault(
                "lcsc_part_number", _field(match.group(0).upper(), line.confidence, line)
            )
        if match := ORDER_PATTERN.search(compact):
            result.setdefault("order_number", _field(match.group(0).upper(), line.confidence, line))
        if match := POSITION_PATTERN.search(compact):
            result.setdefault(
                "label_position_code", _field(match.group(0).upper(), line.confidence, line)
            )
        if match := QUANTITY_PATTERN.search(compact):
            result.setdefault("quantity", _field(match.group(1), line.confidence, line))
        if match := PACKAGE_PATTERN.search(compact):
            result.setdefault(
                "package", _field(match.group(0).upper().replace(" ", "-"), line.confidence, line)
            )
            package_index = index
        if "电压" in text and (match := VOLTAGE_PATTERN.search(compact)):
            result.setdefault("voltage", _field(match.group(1), line.confidence, line))
        if "电流" in text and (match := CURRENT_PATTERN.search(compact)):
            result.setdefault("current", _field(match.group(1), line.confidence, line))
        if ("(" in text and ")" in text) or ("（" in text and "）" in text):
            result.setdefault("brand", _field(text, line.confidence, line, validated=False))
        if "ROHS" in compact.upper():
            result.setdefault("compliance", _field("RoHS", line.confidence, line))

    if package_index is not None:
        # 不同旋转方向下 OCR 的阅读顺序可能把型号放在封装前或后，因此检查相邻两侧。
        # 含冒号或电气单位的行不是型号，必须排除，避免把“电压:40V”误填为 MPN。
        for neighbor in (package_index + 1, package_index - 1):
            if not 0 <= neighbor < len(ordered):
                continue
            line = ordered[neighbor]
            candidate = normalize_token(line.text)
            if (
                len(candidate) >= 5
                and ":" not in candidate
                and re.fullmatch(r"[A-Za-z0-9.+/]+", candidate)
                and re.search(r"[A-Za-z]", candidate)
                and re.search(r"\d", candidate)
                and not LCSC_PATTERN.fullmatch(candidate)
            ):
                result.setdefault(
                    "manufacturer_part_number",
                    _field(candidate, line.confidence * 0.95, line, validated=False),
                )
                break

    descriptions = [
        line
        for line in ordered
        if any(word in line.text for word in ("电阻", "电容", "二极管", "芯片", "MOS"))
    ]
    if descriptions:
        line = descriptions[0]
        result["description"] = _field(line.text, line.confidence, line, validated=False)
    return result


def merge_fields(
    ocr_fields: dict[str, ParsedField], barcode_fields: dict[str, ParsedField]
) -> tuple[dict[str, ParsedField], list[str]]:
    """合并 OCR 与条码结果；冲突时保留 OCR 候选并给出显式警告。"""

    merged = dict(ocr_fields)
    warnings: list[str] = []
    for name, barcode in barcode_fields.items():
        if name not in merged:
            merged[name] = barcode
            continue
        ocr = merged[name]
        if ocr.normalized_value.casefold() == barcode.normalized_value.casefold():
            merged[name] = ParsedField(
                raw_value=ocr.raw_value,
                normalized_value=ocr.normalized_value,
                confidence=min(1.0, max(ocr.confidence, 0.98)),
                source="merged",
                region=ocr.region,
                validated=True,
            )
        else:
            warnings.append(
                f"{name} 的文字识别值 {ocr.normalized_value} 与条码值 {barcode.normalized_value} 不一致"
            )
            merged[name] = ocr.model_copy(update={"validated": False})
    return merged, warnings


def parse_label_text(text: str) -> LabelDraft:
    """解析外部扫描器或手工粘贴的二维码/OCR 文本。"""

    barcode = parse_qr_payload(text)
    if barcode:
        return LabelDraft(fields=barcode, raw_barcode=text, model_version="text-only")
    lines = [
        OcrLine(
            text=line.strip(),
            confidence=1.0,
            box=[
                [0.0, float(index)],
                [1.0, float(index)],
                [1.0, float(index + 1)],
                [0.0, float(index + 1)],
            ],
        )
        for index, line in enumerate(text.splitlines())
        if line.strip()
    ]
    return LabelDraft(
        fields=parse_ocr_lines(lines),
        ocr_lines=lines,
        model_version="text-only",
        warnings=[] if lines else ["没有可解析的文字"],
    )
