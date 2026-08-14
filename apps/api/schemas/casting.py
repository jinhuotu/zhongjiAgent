from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


class YieldAnalysisBody(BaseModel):
    inventoryGuid: str | None = Field(
        default=None,
        max_length=64,
        description="MES 物料 InventoryGUID（已选定型号时传）",
    )
    query: str | None = Field(
        default=None,
        max_length=128,
        description="按物料名称 / 编码 / GUID 检索；多条命中时返回 candidates 供选择",
    )
    includeWeather: bool = Field(
        default=True,
        description="是否按砂型/浇铸/一检/二检各工序日期关联厂区历史气温",
    )
    orderContext: dict[str, Any] | None = Field(
        default=None,
        description="本次查询的销售订单及本行订货数量，写入 rawContext / 导出文档",
    )

    @model_validator(mode="after")
    def _require_guid_or_query(self) -> Self:
        if not (self.inventoryGuid or "").strip() and not (self.query or "").strip():
            raise ValueError("inventoryGuid 或 query 至少填一个")
        return self


class InventorySearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=128, description="名称 / 编码 / GUID")
    top: int = Field(default=20, ge=1, le=50)


class OrderSearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=64, description="订单编号")
    top: int = Field(default=20, ge=1, le=50)


class OrderItemsBody(BaseModel):
    saleOrderGuid: str | None = Field(default=None, max_length=64)
    saleOrderCode: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _require_guid_or_code(self) -> Self:
        if not (self.saleOrderGuid or "").strip() and not (self.saleOrderCode or "").strip():
            raise ValueError("saleOrderGuid 或 saleOrderCode 至少填一个")
        return self


class YieldAnalysisResponse(BaseModel):
    found: bool
    message: str
    inventoryGuid: str
    inventory: dict[str, Any] | None = None
    lines: list[dict[str, Any]] = Field(default_factory=list)
    bestLine: dict[str, Any] | None = None
    productionCount: int = 0
    weatherSummary: dict[str, Any] | None = None
    rawContext: str = ""
    warnings: list[str] = Field(default_factory=list)
    needSelect: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    query: str | None = None
