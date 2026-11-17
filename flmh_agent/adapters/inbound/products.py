from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from flmh_agent.adapters.outbound.products import ProductBrowseError


HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class BrowseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    region: str = Field(default="", pattern=r"^([A-Z]{2})?$")
    source: str = Field(default="", max_length=64)
    keyword: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=128)
    currency: str = Field(default="", pattern=r"^([A-Z]{3})?$")
    priceAmount: str = Field(default="", max_length=51)
    soldCount: str = Field(default="", max_length=51)
    saleAmount: str = Field(default="", max_length=51)
    day7SoldCount: str = Field(default="", max_length=51)
    day7SaleAmount: str = Field(default="", max_length=51)
    relateAuthorCount: str = Field(default="", max_length=51)
    commission: str = Field(default="", max_length=51)
    authorOrderRate: str = Field(default="", max_length=51)
    newProduct: Literal["true", "false"] = "false"
    freeShipping: Literal["true", "false"] = "false"
    hotProduct: Literal["true", "false"] = "false"
    productStatus: Literal["", "on_sale", "off_shelf"] = ""
    shopType: Literal["", "sshop", "local"] = ""
    sort: Literal["opportunityScore", "growthScore", "gmvScore", "creatorScore", "contentScore", "competitionScore",
                  "priceScore", "riskScore", "trendScore", "price", "sales7d", "createdAt"] = "opportunityScore"
    direction: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=10000)
    pageSize: int = Field(default=20, ge=1, le=100)

    @field_validator("priceAmount", "soldCount", "saleAmount", "day7SoldCount", "day7SaleAmount",
                     "relateAuthorCount", "commission", "authorOrderRate")
    @classmethod
    def numeric_range(cls, value: str) -> str:
        if not value.strip():
            return ""
        if value == "all":
            return value
        parts = value.split(":")
        if len(parts) != 2 or not any(parts) or any(
                part and re.fullmatch(r"[0-9]{1,18}(\.[0-9]{1,6})?", part) is None for part in parts):
            raise ValueError("invalid_product_catalog_range")
        if parts[0] and parts[1] and Decimal(parts[0]) > Decimal(parts[1]):
            raise ValueError("invalid_product_catalog_range")
        return value

    @model_validator(mode="after")
    def currency_scope(self):
        if not self.currency and (self.sort == "price" or any(value not in ("", "all") for value in
                                                            (self.priceAmount, self.saleAmount, self.day7SaleAmount))):
            raise ValueError("currency_required_for_amount_comparison")
        return self


class CatalogItem(BaseModel):
    id: str
    title: str
    region: str
    source: str
    sourceRef: str
    category: str | None = None
    externalCategory: str | None = None
    currency: str | None = None
    price: Decimal | None = None
    imageUrl: str | None = None
    detailUrl: str | None = None
    shopName: str | None = None
    sellerId: str | None = None
    metricVersion: int | None = None
    capturedAt: str | None = None
    metricSource: str | None = None
    metricSourceRef: str | None = None
    sales7d: int | None = None
    salesAmount7d: Decimal | None = None
    relatedAuthorCount: int | None = None
    commission: Decimal | None = None
    authorOrderRate: Decimal | None = None
    trendScore: Decimal | None = None
    soldCount: int | None = None
    saleAmount: Decimal | None = None
    freeShipping: bool | None = None
    offShelf: bool | None = None
    crossBorderShop: bool | None = None
    firstObservedAt: str | None = None
    scoreDate: str | None = None
    scoreVersion: str | None = None
    scores: dict[str, Decimal | None] = Field(default_factory=dict, max_length=20)


class CatalogPage(BaseModel):
    products: list[CatalogItem] = Field(max_length=100)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    pageSize: int = Field(ge=1, le=100)
    totalPages: int = Field(ge=0)
    metricScope: Literal["CURRENT_SNAPSHOT_WITH_MATCHING_DAILY"]
    limitations: list[str]


def product_routes() -> list[Route]:
    async def browse(request: Request):
        try:
            if len(request.query_params) != len(request.query_params.multi_items()):
                raise ValueError("duplicate_query_parameter")
            params = BrowseQuery.model_validate(dict(request.query_params)).model_dump()
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_product_catalog_input"}, status_code=422, headers=HEADERS)
        try:
            result = await request.app.state.products.browse(params, request.state.identity)
            output = CatalogPage.model_validate(result).model_dump(mode="json")
        except ProductBrowseError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)
        except ValidationError:
            return JSONResponse({"error": "invalid_product_catalog_response"}, status_code=502, headers=HEADERS)
        return JSONResponse(output, headers=HEADERS)
    return [Route("/v1/products/catalog", browse, methods=["GET"])]
