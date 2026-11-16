"""迟到业务日标记与定点重算回归测试。"""
from __future__ import annotations

import time


def _append(client, payload):
    return client.post("/api/layers/paste", json={"domain_id": 1, **payload})


def test_未加工的新业务日不标迟到(client, built_pipeline):
    r = _append(client, {
        "biz_day": "2026-11-12", "store_code": "S09", "store_name": "新店",
        "category": "饮料", "amount": 70.0, "qty": 1,
    })
    assert r.status_code == 200
    assert r.json()["迟到"] is False
    assert client.get("/api/late-days", params={"domain_id": 1}).json() == []


def test_已加工业务日的贴源新行被标为迟到(client, built_pipeline):
    r = _append(client, {
        "biz_day": "2026-11-10", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 50.0, "qty": 2,
    })
    assert r.json()["迟到"] is True

    late = client.get("/api/late-days", params={"domain_id": 1}).json()
    assert len(late) == 1
    item = late[0]
    assert item["biz_day"] == "2026-11-10"
    assert item["status"] == "待重算"
    assert item["last_detail_job_id"] is not None
    assert "晚于" in item["note"]

    # 主题域视图可查到待重算计数
    domains = client.get("/api/domains").json()
    assert domains[0]["pending_late_days"] == 1
    assert client.get("/api/dashboard").json()["待重算迟到日数"] == 1


def test_定点重算只改写受影响业务日(client, built_pipeline):
    # 重算前留存非受影响日（11-11）的明细与汇总
    detail_1111_before = [
        r for r in client.get("/api/layers/明细", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-11"
    ]
    summary_1111_before = [
        r for r in client.get("/api/layers/汇总", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-11"
    ]

    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 50.0, "qty": 2,
    })
    r = client.post("/api/jobs/late-recompute", json={"domain_id": 1})
    assert r.status_code == 200
    rep = r.json()

    # 报告列出业务日与重算前后明细行数、汇总金额
    assert rep["status"] == "成功"
    assert rep["day_count"] == 1
    assert rep["detail_before"] == 3 and rep["detail_after"] == 4
    assert rep["summary_amount_before"] == 261.5
    assert rep["summary_amount_after"] == 311.5
    assert rep["consistent"] == 1
    line = rep["days"][0]
    assert line["biz_day"] == "2026-11-10"
    assert line["detail_before"] == 3 and line["detail_after"] == 4
    assert line["summary_amount_before"] == 261.5
    assert line["summary_amount_after"] == 311.5

    # 其它业务日明细/汇总行不得删除或改写
    detail_1111_after = [
        r for r in client.get("/api/layers/明细", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-11"
    ]
    summary_1111_after = [
        r for r in client.get("/api/layers/汇总", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-11"
    ]
    assert detail_1111_before == detail_1111_after
    assert summary_1111_before == summary_1111_after

    # 受影响日明细金额与汇总金额一致（与分层表同源）
    detail_1110 = sum(
        r["amount"] for r in client.get("/api/layers/明细", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-10"
    )
    summary_1110 = sum(
        r["amount_sum"] for r in client.get("/api/layers/汇总", params={"domain_id": 1}).json()
        if r["biz_day"] == "2026-11-10"
    )
    assert abs(detail_1110 - summary_1110) < 1e-4


def test_重算报告刷新后仍在且迟到标记收敛(client, built_pipeline):
    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S02", "store_name": "城西店",
        "category": "零食", "amount": 33.0, "qty": 1,
    })
    rep = client.post("/api/jobs/late-recompute", json={"domain_id": 1}).json()

    # 历史报告持久可查
    listed = client.get("/api/recompute-jobs", params={"domain_id": 1}).json()
    assert any(j["id"] == rep["id"] for j in listed)
    fetched = [j for j in listed if j["id"] == rep["id"]][0]
    assert fetched["days"][0]["biz_day"] == "2026-11-10"

    # 标记变为已重算，但记录仍在
    late = client.get("/api/late-days", params={"domain_id": 1}).json()
    assert late[0]["status"] == "已重算"
    assert client.get("/api/late-days", params={"domain_id": 1, "status": "待重算"}).json() == []

    # 分层任务列表出现定点重算任务
    jobs = client.get("/api/jobs", params={"domain_id": 1}).json()
    recompute_jobs = [j for j in jobs if j["step"] == "late_recompute"]
    assert recompute_jobs and recompute_jobs[0]["step_label"] == "迟到日定点重算"


def test_再次迟到重新置待重算(client, built_pipeline):
    for amount in (33.0, 44.0):
        time.sleep(0.02)
        _append(client, {
            "biz_day": "2026-11-10", "store_code": "S02", "store_name": "城西店",
            "category": "零食", "amount": amount, "qty": 1,
        })
        client.post("/api/jobs/late-recompute", json={"domain_id": 1})
        assert client.get("/api/late-days", params={"domain_id": 1}).json()[0]["status"] == "已重算"

    late = client.get("/api/late-days", params={"domain_id": 1}).json()
    assert len(late) == 1  # 同一业务日只有一条标记


def test_全量重跑后迟到标记收敛(client, built_pipeline):
    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 50.0, "qty": 2,
    })
    assert client.get("/api/late-days", params={"domain_id": 1, "status": "待重算"}).json()
    client.post("/api/jobs/paste-to-detail", json={"domain_id": 1})
    client.post("/api/jobs/detail-to-summary", json={"domain_id": 1})
    pending = client.get("/api/late-days", params={"domain_id": 1, "status": "待重算"}).json()
    assert pending == []


def test_只重跑汇总且明细未重跑时迟到标记保留(client, built_pipeline):
    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 50.0, "qty": 2,
    })
    # 只跑明细到汇总：明细水位线未推进，迟到日必须仍是待重算
    client.post("/api/jobs/detail-to-summary", json={"domain_id": 1})
    pending = client.get("/api/late-days", params={"domain_id": 1, "status": "待重算"}).json()
    assert [d["biz_day"] for d in pending] == ["2026-11-10"]


def test_无待重算日时定点重算被拦截(client, built_pipeline):
    r = client.post("/api/jobs/late-recompute", json={"domain_id": 1})
    assert r.status_code == 400
    assert "没有需要定点重算" in r.json()["detail"]


def test_指定不存在业务日被拦截(client, built_pipeline):
    r = client.post(
        "/api/jobs/late-recompute",
        json={"domain_id": 1, "biz_days": ["2099-01-01"]},
    )
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_清洗规则与全量任务保持一致(client, built_pipeline):
    """迟到行若按现行规则应被剔除（空门店名/负金额），重算后不进明细。"""
    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S05", "store_name": "",
        "category": "饮料", "amount": 12.0, "qty": 1,
    })
    rep = client.post("/api/jobs/late-recompute", json={"domain_id": 1}).json()
    # 11-10 原有 3 条合格明细，新行被剔除 -> 明细行数不变
    line = rep["days"][0]
    assert line["detail_before"] == line["detail_after"] == 3


def test_重扫接口可补登迟到(client, built_pipeline):
    # 直接走服务层模拟一条已在库但未被追加流程登记的迟到行
    from app.services import pipeline

    time.sleep(0.02)
    pipeline.append_paste_row(1, {
        "biz_day": "2026-11-11", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 18.0, "qty": 1,
    })
    rescanned = client.post("/api/late-days/rescan", json={"domain_id": 1}).json()
    days = {r["biz_day"]: r["status"] for r in rescanned}
    assert days.get("2026-11-11") == "待重算"


def test_定点重算不清空全表(client, built_pipeline):
    before = client.get("/api/layers/counts", params={"domain_id": 1}).json()
    _append(client, {
        "biz_day": "2026-11-10", "store_code": "S01", "store_name": "城东店",
        "category": "饮料", "amount": 50.0, "qty": 2,
    })
    client.post("/api/jobs/late-recompute", json={"domain_id": 1})
    after = client.get("/api/layers/counts", params={"domain_id": 1}).json()
    # 明细只增加受影响日的新行（+1），汇总键总数不变（11-10 的键已存在，被就地重算）
    assert after["明细层"] == before["明细层"] + 1
    assert after["汇总层"] == before["汇总层"]
