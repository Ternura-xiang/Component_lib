"""把当前库存快照导出为 CSV 或 Excel 工作簿。"""

from __future__ import annotations

import csv
from collections.abc import Collection, Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .database import Database

DETAIL_COLUMNS = (
    ("component_name", "元件"),
    ("aliases", "别名"),
    ("lcsc_part_number", "立创编号"),
    ("manufacturer_part_number", "制造商料号"),
    ("custom_part_number", "自定义编号"),
    ("category", "类别"),
    ("brand", "品牌"),
    ("package", "封装"),
    ("specification", "类别参数"),
    ("quantity", "库存数量"),
    ("component_price", "元件当前单价"),
    ("value", "库存估值"),
    ("unpriced_quantity", "未定价数量"),
    ("warehouse_name", "位置"),
    ("location_name", "仓库"),
    ("purchase_date", "购买日期"),
    ("product_url", "商品链接"),
    ("datasheet_url", "数据手册"),
    ("notes", "备注"),
)

TRANSACTION_COLUMNS = (
    ("created_at", "时间"),
    ("kind", "类型"),
    ("component_name", "元件"),
    ("lcsc_part_number", "立创编号"),
    ("manufacturer_part_number", "制造商料号"),
    ("custom_part_number", "自定义编号"),
    ("quantity_delta", "数量变动"),
    ("warehouse_name", "位置"),
    ("location_name", "仓库"),
    ("source", "来源"),
    ("operation_platform", "操作端"),
    ("notes", "备注"),
    ("group_id", "事务组"),
    ("id", "流水编号"),
    ("lot_id", "批次库存编号"),
    ("reversal_of", "冲销原流水编号"),
)

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10, color="17202A")


def _style_table(sheet: Worksheet, widths: dict[int, float] | None = None) -> None:
    """为数据表应用一致的表头、筛选、冻结和列宽。"""

    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY_FONT
            cell.alignment = Alignment(vertical="center")
    for column in range(1, sheet.max_column + 1):
        if widths and column in widths:
            width = widths[column]
        else:
            values = [str(sheet.cell(row, column).value or "") for row in range(1, sheet.max_row + 1)]
            width = min(max(max(map(len, values), default=8) + 2, 10), 36)
        sheet.column_dimensions[get_column_letter(column)].width = width


def _append_rows(sheet: Worksheet, headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    """按顺序写入表头和数据，保持数字为原生数值。"""

    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))


def export_inventory_csv(
    database: Database, destination: Path, component_ids: Collection[str] | None = None,
    *, lot_ids: Collection[str] | None = None,
) -> Path:
    """把库存明细写为带 UTF-8 BOM 的 CSV，兼容 Excel 直接打开。"""

    rows = database.inventory_export_rows(component_ids, lot_ids=lot_ids)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in DETAIL_COLUMNS])
        for row in rows:
            writer.writerow([row.get(key) for key, _ in DETAIL_COLUMNS])
    temporary.replace(destination)
    return destination


def export_inventory_xlsx(
    database: Database, destination: Path, component_ids: Collection[str] | None = None,
    *, lot_ids: Collection[str] | None = None,
) -> Path:
    """导出概览、库存明细、分库统计和分类统计四张工作表。"""

    rows = database.inventory_export_rows(component_ids, lot_ids=lot_ids)
    statistics = database.inventory_statistics(component_ids, lot_ids=lot_ids)
    workbook = Workbook()

    overview = workbook.active
    overview.title = "概览"
    overview.sheet_view.showGridLines = False
    overview.append(["指标", "数值"])
    for label, key in (
        ("元件种类数", "component_types"),
        ("库存总件数", "total_quantity"),
        ("已定价库存件数", "priced_quantity"),
        ("未定价库存件数", "unpriced_quantity"),
        ("库存估值", "total_value"),
    ):
        overview.append([label, statistics[key]])
    _style_table(overview, {1: 22, 2: 18})
    overview["B6"].number_format = "#,##0.0000"

    detail = workbook.create_sheet("库存明细")
    _append_rows(
        detail,
        (label for _, label in DETAIL_COLUMNS),
        ([row.get(key) for key, _ in DETAIL_COLUMNS] for row in rows),
    )
    _style_table(detail)
    for row_number in range(2, detail.max_row + 1):
        detail.cell(row_number, 10).number_format = "#,##0"
        detail.cell(row_number, 11).number_format = "#,##0.000000"
        detail.cell(row_number, 12).number_format = "#,##0.0000"
        detail.cell(row_number, 13).number_format = "#,##0"

    for sheet_name, key, first_header in (
        ("分库统计", "by_location", "位置 / 仓库"),
        ("分类统计", "by_category", "元件类别"),
    ):
        sheet = workbook.create_sheet(sheet_name)
        _append_rows(
            sheet,
            (first_header, "元件种类数", "库存件数", "库存估值", "未定价件数"),
            (
                (
                    item["name"],
                    item["component_types"],
                    item["quantity"],
                    item["value"],
                    item["unpriced_quantity"],
                )
                for item in statistics[key]
            ),
        )
        _style_table(sheet, {1: 30, 2: 14, 3: 14, 4: 16, 5: 16})
        for row_number in range(2, sheet.max_row + 1):
            sheet.cell(row_number, 2).number_format = "#,##0"
            sheet.cell(row_number, 3).number_format = "#,##0"
            sheet.cell(row_number, 4).number_format = "#,##0.0000"
            sheet.cell(row_number, 5).number_format = "#,##0"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp.xlsx")
    workbook.save(temporary)
    temporary.replace(destination)
    return destination


def export_transactions(rows: Iterable[dict[str, Any]], destination: Path) -> Path:
    """以 CSV 或 Excel 保存筛选后的原始流水，一条库存变动占一行。"""

    headers = [label for _, label in TRANSACTION_COLUMNS]
    values = ([row.get(key) for key, _ in TRANSACTION_COLUMNS] for row in rows)
    is_csv = destination.suffix.casefold() == ".csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        if is_csv:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                for row in values:
                    writer.writerow([
                        "'" + value
                        if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}
                        else value
                        for value in row
                    ])
        else:
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "库存流水"
            _append_rows(sheet, headers, values)
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            _style_table(sheet)
            workbook.save(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
