"""BOM 文件解析、候选匹配和缺料预览。"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from .database import Database, DomainError
from .models import BomInputRow

ALIASES = {
    "名称": "name",
    "元件名称": "name",
    "name": "name",
    "comment": "name",
    "value": "name",
    "数量": "quantity",
    "qty": "quantity",
    "quantity": "quantity",
    "立创编号": "lcsc_part_number",
    "lcsc": "lcsc_part_number",
    "lcsc part": "lcsc_part_number",
    "制造商料号": "manufacturer_part_number",
    "mpn": "manufacturer_part_number",
    "manufacturer part number": "manufacturer_part_number",
    "自定义编号": "custom_part_number",
    "part number": "custom_part_number",
    "规格": "specification",
    "specification": "specification",
}


def normalize(value: str | None) -> str:
    """生成用于元件匹配的大小写无关键。"""

    return re.sub(r"[\s\-_]+", "", value or "").casefold()


def parse_bom(path: Path, sheet_name: str | None = None) -> list[BomInputRow]:
    """读取 CSV 或 XLSX 并映射常见中英文表头。"""

    suffix = path.suffix.casefold()
    if suffix == ".csv":
        raw = path.read_bytes()
        text: str | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise DomainError("VALIDATION_ERROR", "CSV 编码无法识别，请转换为 UTF-8 或 GB18030")
        records = list(csv.DictReader(io.StringIO(text)))
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = list(worksheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(value or "").strip() for value in rows[0]]
        records = [dict(zip(headers, values, strict=False)) for values in rows[1:]]
    else:
        raise DomainError("VALIDATION_ERROR", "只支持 CSV 和 XLSX 文件")

    output: list[BomInputRow] = []
    for row_number, record in enumerate(records, start=2):
        mapped: dict[str, Any] = {"row_number": row_number}
        for header, value in record.items():
            key = ALIASES.get(str(header).strip().casefold())
            if key:
                mapped[key] = value
        try:
            raw_quantity = float(mapped.get("quantity") or 0)
            quantity = int(raw_quantity) if raw_quantity.is_integer() else 0
        except (TypeError, ValueError):
            quantity = 0
        identifiers = [
            mapped.get(key)
            for key in (
                "name",
                "lcsc_part_number",
                "manufacturer_part_number",
                "custom_part_number",
            )
        ]
        if quantity <= 0 or not any(str(item or "").strip() for item in identifiers):
            continue
        mapped["quantity"] = quantity
        for key in (
            "name",
            "lcsc_part_number",
            "manufacturer_part_number",
            "custom_part_number",
            "specification",
        ):
            if key in mapped:
                mapped[key] = str(mapped[key]).strip() or None
        output.append(BomInputRow.model_validate(mapped))
    return output


def preview_bom(database: Database, rows: list[BomInputRow]) -> list[dict[str, Any]]:
    """按稳定优先级生成候选；歧义候选不会被静默选中。"""

    components = database.list_components()
    stock = database.list_stock()
    totals: dict[str, float] = {}
    for item in stock:
        totals[item["component_id"]] = totals.get(item["component_id"], 0.0) + float(
            item["quantity"]
        )
    results: list[dict[str, Any]] = []
    for row in rows:
        candidates: list[dict[str, Any]] = []
        for component in components:
            score, reason = score_component(component, row)
            if score:
                candidates.append({"component": component, "score": score, "reason": reason})
        candidates.sort(key=lambda item: item["score"], reverse=True)
        if len(candidates) > 1 and candidates[0]["score"] > candidates[1]["score"]:
            candidates = candidates[:1]
        else:
            candidates = candidates[:5]
        selected = candidates[0]["component"]["id"] if len(candidates) == 1 else None
        available = totals.get(selected, 0.0) if selected else 0.0
        results.append(
            {
                "input": row.model_dump(),
                "candidates": candidates,
                "selected_component_id": selected,
                "required_quantity": row.quantity,
                "available_quantity": available,
                "shortage": max(0.0, row.quantity - available),
            }
        )
    return results


def score_component(component: dict[str, Any], row: BomInputRow) -> tuple[int, str]:
    """计算候选分数；编号精确匹配始终高于名称匹配。"""

    checks = (
        ("lcsc_part_number", row.lcsc_part_number, 100, "立创编号精确匹配"),
        ("manufacturer_part_number", row.manufacturer_part_number, 95, "制造商料号精确匹配"),
        ("custom_part_number", row.custom_part_number, 90, "自定义编号精确匹配"),
    )
    for key, expected, score, reason in checks:
        if expected and normalize(component.get(key)) == normalize(expected):
            return score, reason
    if row.name:
        names = [component["name"], *component.get("aliases", [])]
        if any(normalize(name) == normalize(row.name) for name in names):
            bonus = (
                5
                if row.specification
                and normalize(component.get("specification")) == normalize(row.specification)
                else 0
            )
            return 70 + bonus, "名称候选匹配"
        try:
            from rapidfuzz.fuzz import ratio

            best = max((ratio(normalize(name), normalize(row.name)) for name in names), default=0)
            if best >= 88:
                return 50, f"名称模糊候选（{best:.0f}%）"
        except ImportError:
            pass
    return 0, ""
