from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import pipeline

router = APIRouter(tags=["分层任务"])


class JobBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


class RecomputeBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")
    biz_days: list[str] | None = Field(default=None, description="指定重算业务日；缺省取全部待重算迟到日")


@router.get("/jobs")
def list_jobs(domain_id: int | None = None):
    return pipeline.list_jobs(domain_id)


@router.post("/jobs/paste-to-detail")
def paste_to_detail(body: JobBody):
    try:
        job = pipeline.run_paste_to_detail(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job["step_label"] = "贴源到明细"
    return job


@router.post("/jobs/detail-to-summary")
def detail_to_summary(body: JobBody):
    try:
        job = pipeline.run_detail_to_summary(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job["step_label"] = "明细到汇总"
    return job


@router.get("/late-days")
def list_late(domain_id: int | None = None, status: str | None = None):
    return pipeline.list_late_days(domain_id, status)


@router.post("/late-days/rescan")
def rescan_late(body: JobBody):
    try:
        return pipeline.list_late_days(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/recompute-jobs")
def list_recompute(domain_id: int | None = None):
    return pipeline.list_recompute_jobs(domain_id)


@router.post("/jobs/late-recompute")
def late_recompute(body: RecomputeBody):
    try:
        return pipeline.run_late_recompute(body.domain_id, body.biz_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
