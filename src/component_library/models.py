"""应用共享的数据模型。

模型同时服务于桌面界面、业务层和 FastAPI。集中定义可以避免界面与外部 API
分别解释同一字段，尤其能防止数量、置信度和可空字段产生不一致行为。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ComponentCreate(BaseModel):
    """创建元件所需字段；只有名称是必填项。"""

    name: str = Field(min_length=1, description="用户可识别的元件名称")
    aliases: list[str] = Field(default_factory=list, description="搜索别名")
    lcsc_part_number: str | None = None
    manufacturer_part_number: str | None = None
    custom_part_number: str | None = None
    brand: str | None = None
    package: str | None = None
    category: str | None = None
    specification: str | None = None
    price: float | None = Field(default=None, ge=0)
    purchase_date: str | None = None
    product_url: str | None = None
    datasheet_url: str | None = None
    notes: str | None = None


class WarehouseCreate(BaseModel):
    """创建仓库的输入。"""

    name: str = Field(min_length=1)
    notes: str | None = None


class LocationCreate(BaseModel):
    """创建仓库内具体库位的输入。"""

    warehouse_id: str
    name: str = Field(min_length=1)
    notes: str | None = None


class ReceiveRequest(BaseModel):
    """批次入库请求。"""

    component_id: str
    location_id: str
    quantity: int = Field(gt=0)
    source: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    purchase_date: str | None = None
    supplier_part_id: str | None = None
    notes: str | None = None


class IssueRequest(BaseModel):
    """按 FIFO 出库请求；库位为空时允许跨库位领取。"""

    component_id: str
    quantity: int = Field(gt=0)
    location_id: str | None = None
    source: str | None = None
    notes: str | None = None


class TransferRequest(BaseModel):
    """库位间移动库存的请求。"""

    component_id: str
    from_location_id: str
    to_location_id: str
    quantity: int = Field(gt=0)
    notes: str | None = None


class AdjustRequest(BaseModel):
    """将某元件在某库位的库存调整到目标数量。"""

    component_id: str
    location_id: str
    target_quantity: int = Field(ge=0)
    reason: str = Field(min_length=1)


class BomInputRow(BaseModel):
    """完成字段映射后的单行 BOM。"""

    row_number: int = Field(ge=1)
    name: str | None = None
    quantity: int = Field(gt=0)
    lcsc_part_number: str | None = None
    manufacturer_part_number: str | None = None
    custom_part_number: str | None = None
    specification: str | None = None


class BomCommitLine(BaseModel):
    """用户确认后的 BOM 出库行。"""

    row_number: int
    component_id: str
    quantity: int = Field(gt=0)
    location_id: str | None = None


class BomCommitRequest(BaseModel):
    """需要在同一事务中提交的全部 BOM 行。"""

    name: str = Field(min_length=1)
    source_filename: str | None = None
    lines: list[BomCommitLine] = Field(min_length=1)


class OcrLine(BaseModel):
    """OCR 返回的一行文字及其在矫正图中的四点坐标。"""

    text: str
    confidence: float = Field(ge=0, le=1)
    box: list[list[float]] = Field(description="顺时针排列的四个 [x,y] 点")


class ParsedField(BaseModel):
    """从 OCR 或二维码中提取的一个结构化字段。"""

    raw_value: str
    normalized_value: str
    confidence: float = Field(ge=0, le=1)
    source: Literal["ocr", "barcode", "merged", "manual"]
    region: list[list[float]] | None = None
    validated: bool = False


class LabelDraft(BaseModel):
    """袋贴识别草稿；该对象本身永远不会修改库存。"""

    fields: dict[str, ParsedField] = Field(default_factory=dict)
    ocr_lines: list[OcrLine] = Field(default_factory=list)
    raw_barcode: str | None = None
    parser_version: str = "jlc-bag-v2"
    model_version: str = "unavailable"
    preprocessing: str = "none"
    quality: dict[str, float | bool | str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True
    annotated_image_png: bytes | None = Field(default=None, exclude=True)


class JlcParameter(BaseModel):
    """立创商品页中的单个参数；键用于跨页面版本稳定匹配。"""

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class JlcParameterGroup(BaseModel):
    """保持商品页显示顺序的一组参数。"""

    name: str = Field(min_length=1)
    parameters: list[JlcParameter] = Field(default_factory=list)


class JlcProductDraft(BaseModel):
    """用户明确选择当前商品页后生成的待确认草稿。"""

    lcsc_part_number: str = Field(pattern=r"^C\d{4,10}$")
    name: str = Field(min_length=1)
    manufacturer_part_number: str | None = None
    brand: str | None = None
    package: str | None = None
    category: str | None = None
    description: str | None = None
    product_url: str
    datasheet_url: str | None = None
    parameter_groups: list[JlcParameterGroup] = Field(default_factory=list)
    parser_version: str = "jlc-product-v1"
    warnings: list[str] = Field(default_factory=list)


class JlcMergeResult(BaseModel):
    """用户逐字段确认后可应用到入库表单的商城资料。"""

    name: str | None = None
    lcsc_part_number: str | None = None
    manufacturer_part_number: str | None = None
    brand: str | None = None
    package: str | None = None
    category: str | None = None
    product_url: str | None = None
    datasheet_url: str | None = None
    notes: str | None = None
    parameter_groups: list[JlcParameterGroup] = Field(default_factory=list)


class CorrectionRequest(BaseModel):
    """用户确认后可选择保存的本地 OCR 纠错样本。"""

    field_name: str
    original_text: str
    corrected_text: str
    model_version: str
    confidence: float = Field(ge=0, le=1)
    quality: dict[str, Any] = Field(default_factory=dict)
    corrected_image_png: bytes | None = Field(default=None, exclude=True)


class ResistorDetails(BaseModel):
    """电阻类别的结构化参数；未能可靠提取的字段保持为空。"""

    resistance: str | None = None
    tolerance_percent: float | None = Field(default=None, ge=0)
    power_mw: float | None = Field(default=None, ge=0)
    technology: str | None = None


class MosfetDetails(BaseModel):
    """MOSFET 类别的结构化参数。"""

    channel_type: str | None = None
    mosfet_count: int | None = Field(default=None, gt=0)
    voltage_v: float | None = Field(default=None, ge=0)
    current_a: float | None = Field(default=None, ge=0)


class OrderImportLine(BaseModel):
    """从用户保存的订单页面提取的一条待确认商品记录。"""

    order_number: str = Field(min_length=1)
    purchased_at: str = Field(min_length=1)
    lcsc_part_number: str = Field(pattern=r"^C\d{4,10}$")
    name: str = Field(min_length=1)
    description: str | None = None
    category: str | None = None
    manufacturer_part_number: str | None = None
    package: str | None = None
    brand: str | None = None
    unit_price: float = Field(ge=0)
    quantity: int = Field(gt=0)
    product_url: str | None = None
    resistor: ResistorDetails | None = None
    mosfet: MosfetDetails | None = None
