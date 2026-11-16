from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import pipeline

router = APIRouter(tags=["层间对账"])


class ReconcileBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


@router.get("/reconcile")
def list_reports(domain_id: int | None = None):
    return pipeline.list_reconcile(domain_id)


@router.post("/reconcile")
def run_report(body: ReconcileBody):
    try:
        return pipeline.run_reconcile(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
