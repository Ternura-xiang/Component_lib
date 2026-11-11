"""立创 EDA BOM 文件解析、可解释候选匹配和缺料预览。"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .database import Database, DomainError
from .models import BomInputRow


def bom_project_name(path: Path) -> str:
    """从文件名提取项目名称，去除独立的 BOM 前后缀。"""

    stem = path.stem.strip()
    name = re.sub(r"^BOM[\s_\-–—]+|[\s_\-–—]+BOM$", "", stem, flags=re.IGNORECASE)
    return name.strip() or "BOM 出库"


ALIASES = {
    "名称": "name", "元件名称": "name", "name": "name", "comment": "name", "元件": "name",
    "value": "specification", "规格": "specification", "specification": "specification",
    "数量": "quantity", "qty": "quantity", "quantity": "quantity",
    "立创编号": "lcsc_part_number", "lcsc": "lcsc_part_number",
    "lcsc part": "lcsc_part_number", "supplier part": "lcsc_part_number",
    "supplier part number": "lcsc_part_number", "供应商编号": "lcsc_part_number",
    "制造商料号": "manufacturer_part_number", "manufacturer part": "manufacturer_part_number",
    "manufacturer part number": "manufacturer_part_number", "mpn": "manufacturer_part_number",
    "自定义编号": "custom_part_number", "part number": "custom_part_number",
    "位号": "designators", "designator": "designators", "designators": "designators",
    "封装": "footprint", "footprint": "footprint", "package": "footprint",
    "制造商": "manufacturer", "manufacturer": "manufacturer", "品牌": "manufacturer",
    "供应商": "supplier", "supplier": "supplier",
}


def normalize(value: str | None) -> str:
    """生成适用于编号和普通文本的大小写无关规范值。"""

    text = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\s\-_/(),，（）]+", "", text)


def normalize_package(value: str | None) -> str:
    """统一立创 EDA 常见的 R0603/C0603 与 0603 表示。"""

    text = normalize(value)
    return re.sub(r"^[rcl]?(0[2468]0[1235]|1206|1210)$", r"\1", text)


def normalize_specification(value: str | None) -> str:
    """规范电阻、电容和电感的常见工程单位，其他规格退化为文本规范化。"""

    text = unicodedata.normalize("NFKC", value or "").strip().replace("Ω", "R")
    compact = re.sub(r"\s+", "", text).replace("µ", "u").replace("μ", "u")
    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)([pnumkM]?)(?:[fhr]|ohm)?", compact)
    if not match:
        embedded = re.fullmatch(r"(?i)(\d*)([rkm])(\d+)", compact)
        if embedded:
            left, marker, right = embedded.groups()
            compact = f"{left or '0'}.{right}{marker}"
            match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)([rkm])", compact)
    if match:
        number, prefix = match.groups()
        scales = {
            "p": Decimal("1e-12"), "n": Decimal("1e-9"), "u": Decimal("1e-6"),
            "m": Decimal("1e-3"), "r": Decimal(1), "": Decimal(1),
            "k": Decimal("1e3"), "M": Decimal("1e6"),
        }
        try:
            key = prefix if prefix == "M" else prefix.casefold()
            return f"{(Decimal(number) * scales[key]).normalize():f}"
        except (InvalidOperation, KeyError):
            pass
    return normalize(compact)


def bom_mapping_key(row: BomInputRow) -> str:
    """生成不依赖行号和数量的稳定 BOM 物料指纹。"""

    values = (
        normalize(row.lcsc_part_number), normalize(row.manufacturer_part_number),
        normalize(row.custom_part_number), normalize_specification(row.specification or row.name),
        normalize_package(row.footprint), normalize(row.manufacturer), normalize(row.name),
    )
    return "bom-v2:" + hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def _decode_csv(path: Path) -> str:
    """按常见 EDA 导出编码依次解码 CSV。

    参数 ``path`` 必须指向本地文件；所有候选编码失败时抛出可展示的领域错误，
    而不是用替换字符静默损坏料号。
    """

    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise DomainError("VALIDATION_ERROR", "CSV 编码无法识别，请使用 UTF-8、UTF-16 或 GB18030")


def _records_from_csv(path: Path) -> list[dict[str, Any]]:
    """自动探测逗号、制表符或分号分隔符，并返回以表头为键的记录。"""

    text = _decode_csv(path)
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",\t;").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in text.partition("\n")[0] else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def parse_bom(
    path: Path,
    sheet_name: str | None = None,
    field_mapping: dict[str, str] | None = None,
) -> list[BomInputRow]:
    """读取立创 EDA 导出的 CSV/XLSX，并转换为统一 BOM 行模型。

    :param path: 待解析的 BOM 文件路径，仅支持 ``.csv`` 和 ``.xlsx``
    :param sheet_name: 可选，XLSX 中需要读取的工作表名称；为空时读取活动工作表
    :param field_mapping: 可选，原始表头到内部字段名的人工映射，优先于内置别名
    :return: BOM 行列表；无效行不会丢弃，其错误会保存在 ``errors`` 中供界面提示
    :raises DomainError: 文件类型、工作表或 CSV 编码不受支持时抛出

    注意：本函数只解析和校验文件，不执行元件匹配，也不会修改库存。
    """

    ### 根据文件类型读取记录 ###
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        records = _records_from_csv(path)
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        if sheet_name and sheet_name not in workbook.sheetnames:
            raise DomainError("VALIDATION_ERROR", f"工作表不存在：{sheet_name}")
        worksheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = list(worksheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(value or "").strip() for value in rows[0]]
        records = [dict(zip(headers, values, strict=False)) for values in rows[1:]]
    else:
        raise DomainError("VALIDATION_ERROR", "只支持 CSV 和 XLSX 文件")

    ### 映射表头并逐行校验 ###
    output: list[BomInputRow] = []
    for row_number, record in enumerate(records, start=2):
        if not any(str(value or "").strip() for value in record.values()):
            continue
        mapped: dict[str, Any] = {"row_number": row_number}
        raw_fields = {str(key): None if value is None else str(value) for key, value in record.items()}
        for header, value in record.items():
            key = (field_mapping or {}).get(str(header)) or ALIASES.get(
                unicodedata.normalize("NFKC", str(header)).strip().casefold()
            )
            if key and value is not None and str(value).strip():
                mapped.setdefault(key, value)
        errors: list[str] = []
        raw_quantity = mapped.get("quantity")
        if raw_quantity in (None, "") and mapped.get("designators"):
            raw_quantity = len([x for x in re.split(r"[,，;；\s]+", str(mapped["designators"])) if x])
        try:
            numeric = float(raw_quantity or 0)
            quantity = int(numeric) if numeric.is_integer() and numeric > 0 else 0
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            errors.append("数量缺失或不是正整数")
        identifiers = [mapped.get(key) for key in (
            "name", "specification", "lcsc_part_number", "manufacturer_part_number", "custom_part_number"
        )]
        if not any(str(item or "").strip() for item in identifiers):
            errors.append("缺少名称、规格或物料编号")
        mapped.update(quantity=quantity, errors=errors, raw_fields=raw_fields)
        for key in (
            "name", "lcsc_part_number", "manufacturer_part_number", "custom_part_number",
            "specification", "designators", "footprint", "manufacturer", "supplier",
        ):
            if key in mapped:
                mapped[key] = str(mapped[key]).strip() or None
        output.append(BomInputRow.model_validate(mapped))
    return output


def _saved_mapping(database: Database, mapping_key: str) -> str | None:
    """查询用户之前确认的 BOM 指纹映射；已归档元件视为无映射。"""

    with database.read() as connection:
        row = connection.execute(
            """SELECT pm.component_id FROM part_mappings pm JOIN components c
            ON c.id=pm.component_id WHERE pm.mapping_key=? AND c.archived_at IS NULL""",
            (mapping_key,),
        ).fetchone()
    return str(row[0]) if row else None


def _candidate_score(component: dict[str, Any], row: BomInputRow) -> tuple[int, str, bool]:
    """计算库存元件与单条 BOM 需求的可解释匹配分数。

    :param component: 数据库返回的元件详情字典
    :param row: 已规范化的 BOM 输入行
    :return: ``(分数, 原因, 是否存在硬冲突)``；硬冲突候选不得自动选中

    匹配顺序为精确编号、规格、封装、品牌和名称相似度。编号或封装冲突即使得到
    较高分，也必须交由用户人工确认。
    """

    ### 优先检查稳定编号 ###
    for key, expected, score, reason in (
        ("lcsc_part_number", row.lcsc_part_number, 100, "立创编号精确匹配"),
        ("manufacturer_part_number", row.manufacturer_part_number, 95, "制造商料号精确匹配"),
        ("custom_part_number", row.custom_part_number, 92, "自定义编号精确匹配"),
    ):
        if expected and normalize(component.get(key)) == normalize(expected):
            package_conflict = bool(row.footprint and component.get("package") and
                normalize_package(row.footprint) != normalize_package(component.get("package")))
            return score, reason, package_conflict
    identifier_conflict = any(
        expected and component.get(key) and normalize(component.get(key)) != normalize(expected)
        for key, expected in (
            ("lcsc_part_number", row.lcsc_part_number),
            ("manufacturer_part_number", row.manufacturer_part_number),
            ("custom_part_number", row.custom_part_number),
        )
    )
    ### 累计语义字段分数 ###
    score = 0
    reasons: list[str] = []
    wanted_spec = normalize_specification(row.specification or row.name)
    component_specs = {
        normalize_specification(component.get("specification")),
        normalize_specification(component.get("name")),
        *[normalize_specification(alias) for alias in component.get("aliases", [])],
    }
    if wanted_spec and wanted_spec in component_specs:
        score += 35
        reasons.append("规格一致")
    wanted_package = normalize_package(row.footprint)
    component_package = normalize_package(component.get("package"))
    package_conflict = bool(wanted_package and component_package and wanted_package != component_package)
    if wanted_package and wanted_package == component_package:
        score += 30
        reasons.append("封装一致")
    if row.manufacturer and normalize(row.manufacturer) == normalize(component.get("brand")):
        score += 15
        reasons.append("品牌一致")
    names = [component.get("name") or "", *component.get("aliases", [])]
    query_name = row.name or row.specification or ""
    if query_name:
        try:
            from rapidfuzz.fuzz import ratio
            similarity = max((ratio(normalize(name), normalize(query_name)) for name in names), default=0)
        except ImportError:
            similarity = 100 if any(normalize(name) == normalize(query_name) for name in names) else 0
        name_score = round(similarity * 0.2)
        if name_score:
            score += name_score
            reasons.append(f"名称 {similarity:.0f}%")
    return score, "、".join(reasons), identifier_conflict or package_conflict


def score_component(component: dict[str, Any], row: BomInputRow) -> tuple[int, str]:
    """兼容公开辅助函数；硬冲突候选仍返回分数但不会自动选中。"""

    score, reason, _ = _candidate_score(component, row)
    return score, reason


def preview_bom(database: Database, rows: list[BomInputRow]) -> list[dict[str, Any]]:
    """为 BOM 行生成匹配候选，并按累计需求计算库存缺口。

    :param database: 当前项目数据库，用于读取元件、库存和历史人工映射
    :param rows: ``parse_bom`` 返回的 BOM 行
    :return: 与输入行一一对应的预览字典，包含候选、匹配原因、可用量和缺料量

    注意：同一元件可能被多条 BOM 行选中，因此缺料按该元件的累计需求计算，不能
    逐行独立判断。本函数为只读操作，不会扣减库存。
    """

    ### 建立元件与库存快照 ###
    components = database.list_components()
    component_by_id = {str(item["id"]): item for item in components}
    totals: dict[str, int] = {}
    for stock in database.list_stock():
        component_id = str(stock["component_id"])
        totals[component_id] = totals.get(component_id, 0) + int(stock["quantity"])
    ### 为每一行生成候选并决定是否可自动匹配 ###
    results: list[dict[str, Any]] = []
    for row in rows:
        mapping_key = bom_mapping_key(row)
        selected: str | None = None
        status, reason, confidence = "unmatched", "没有找到候选", 0
        candidates: list[dict[str, Any]] = []
        if row.errors:
            status, reason = "invalid", "；".join(row.errors)
        else:
            mapped_id = _saved_mapping(database, mapping_key)
            if mapped_id and mapped_id in component_by_id:
                component = component_by_id[mapped_id]
                candidates = [{"component": component, "score": 100,
                    "reason": "已保存的人工映射", "conflict": False}]
                selected, status, reason, confidence = mapped_id, "auto", "已保存的人工映射", 100
            else:
                for component in components:
                    score, candidate_reason, conflict = _candidate_score(component, row)
                    if score:
                        candidates.append({"component": component, "score": score,
                            "reason": candidate_reason, "conflict": conflict})
                candidates.sort(key=lambda item: (-item["score"], item["component"]["name"]))
                candidates = candidates[:5]
                if candidates:
                    top = candidates[0]
                    runner_up = candidates[1]["score"] if len(candidates) > 1 else 0
                    unique_exact = top["score"] >= 92 and (len(candidates) == 1 or top["score"] > runner_up)
                    semantic_auto = top["score"] >= 85 and top["score"] - runner_up >= 15
                    if not top["conflict"] and (unique_exact or semantic_auto):
                        selected, status = str(top["component"]["id"]), "auto"
                    else:
                        status = "ambiguous"
                    reason, confidence = str(top["reason"]), int(top["score"])
        results.append({
            "input": row.model_dump(), "candidates": candidates,
            "selected_component_id": selected, "required_quantity": row.quantity,
            "available_quantity": 0, "shortage": row.quantity, "match_status": status,
            "confidence": confidence, "reason": reason, "mapping_key": mapping_key,
            "warnings": list(row.errors),
        })
    ### 汇总同一元件的需求并计算缺口 ###
    demand: dict[str, int] = {}
    for item in results:
        if selected := item["selected_component_id"]:
            demand[selected] = demand.get(selected, 0) + int(item["required_quantity"])
    for item in results:
        if selected := item["selected_component_id"]:
            available = totals.get(selected, 0)
            item["available_quantity"] = available
            item["shortage"] = max(0, demand[selected] - available)
    return results
