from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import pipeline

router = APIRouter(tags=["分层"])


class PasteRowBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")
    biz_day: str = Field(..., description="业务日")
    store_code: str = Field(..., description="门店编码")
    store_name: str = Field(default="", description="门店名称")
    category: str = Field(default="", description="品类")
    amount: float = Field(default=0, description="金额")
    qty: int = Field(default=0, description="件数")


@router.get("/layers/counts")
def counts(domain_id: int | None = Query(default=None)):
    return pipeline.layer_counts(domain_id)


@router.post("/layers/paste")
def append_paste(body: PasteRowBody):
    try:
        return pipeline.append_paste_row(
            body.domain_id,
            {
                "biz_day": body.biz_day,
                "store_code": body.store_code,
                "store_name": body.store_name,
                "category": body.category,
                "amount": body.amount,
                "qty": body.qty,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/layers/{layer}")
def list_layer(layer: str, domain_id: int | None = Query(default=None)):
    mapping = {"paste": "paste", "detail": "detail", "summary": "summary",
               "贴源": "paste", "明细": "detail", "汇总": "summary"}
    key = mapping.get(layer)
    if not key:
        raise HTTPException(status_code=400, detail="分层只能是贴源/明细/汇总")
    try:
        return pipeline.layer_rows(key, domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
