from __future__ import annotations

import json
import sqlite3

from app.db import connect, rows_to_dicts


STEP_LABEL = {
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
    "late_recompute": "迟到日定点重算",
}

NOW_SQL = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


def _ts_key(ts: str | None) -> tuple[str, str]:
    """兼容秒级与毫秒级时间字符串的可比较键。"""
    ts = ts or ""
    return (ts[:19], ts[20:])


def _clean_paste_row(row: sqlite3.Row) -> dict | None:
    """现行清洗规则：门店名为空或金额为负的贴源行剔除。"""
    store_name = (row["store_name"] or "").strip()
    amount = float(row["amount"])
    if not store_name or amount < 0:
        return None
    return {
        "biz_day": row["biz_day"],
        "store_code": row["store_code"],
        "store_name": store_name,
        "category": row["category"],
        "amount": amount,
        "qty": int(row["qty"]),
    }


def list_domains() -> list[dict]:
    conn = connect()
    try:
        domains = rows_to_dicts(conn.execute("SELECT * FROM domains ORDER BY id").fetchall())
    finally:
        conn.close()
    pending = pending_late_count_by_domain()
    for d in domains:
        d["pending_late_days"] = pending.get(d["id"], 0)
    return domains


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
        }
    finally:
        conn.close()


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
            out.append(d)
        return out
    finally:
        conn.close()


def run_paste_to_detail(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_detail WHERE domain_id=?", (domain_id,))
        paste = conn.execute(
            "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        out = drop = 0
        for row in paste:
            cleaned = _clean_paste_row(row)
            if cleaned is None:
                drop += 1
                continue
            conn.execute(
                """
                INSERT INTO layer_detail(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    cleaned["biz_day"],
                    cleaned["store_code"],
                    cleaned["store_name"],
                    cleaned["category"],
                    cleaned["amount"],
                    cleaned["qty"],
                    "已清洗",
                ),
            )
            out += 1
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count, created_at)
            VALUES (?, 'paste_to_detail', '成功', ?, ?, ?, ?, %s)
            """ % NOW_SQL,
            (domain_id, f"清洗完成，剔除门店名为空或金额为负的行", len(paste), out, drop),
        )
        job_id = int(cur.lastrowid)
        job_created_at = conn.execute(
            "SELECT created_at FROM layer_jobs WHERE id=?", (job_id,)
        ).fetchone()["created_at"]
        covered_days = [r[0] for r in conn.execute(
            "SELECT DISTINCT biz_day FROM layer_paste WHERE domain_id=? ORDER BY biz_day",
            (domain_id,),
        ).fetchall()]
        _touch_day_watermarks(conn, domain_id, covered_days, job_id, job_created_at)
        conn.commit()
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def run_detail_to_summary(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
        detail = conn.execute(
            "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        buckets: dict[tuple, dict] = {}
        for row in detail:
            key = (row["biz_day"], row["store_code"])
            b = buckets.setdefault(
                key,
                {"biz_day": row["biz_day"], "store_code": row["store_code"], "amount": 0.0, "qty": 0, "n": 0},
            )
            b["amount"] += float(row["amount"])
            b["qty"] += int(row["qty"])
            b["n"] += 1
        for b in buckets.values():
            conn.execute(
                """
                INSERT INTO layer_summary(
                  domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
            )
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count)
            VALUES (?, 'detail_to_summary', '成功', ?, ?, ?, 0)
            """,
            (domain_id, "按业务日+门店编码汇总", len(detail), len(buckets)),
        )
        # 仅当明细水位线已覆盖全部贴源行时，迟到标记才收敛；明细未重跑则保留待重算
        _converge_late_days(conn, domain_id)
        conn.commit()
        job_id = int(cur.lastrowid)
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def _latest_detail_watermark(conn: sqlite3.Connection, domain_id: int, biz_day: str) -> tuple[str | None, int | None]:
    """该业务日最近一次成功明细加工的时间与任务编号；从未加工过则返回 (None, None)。"""
    row = conn.execute(
        "SELECT last_detail_at, last_detail_job_id FROM day_watermarks "
        "WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    ).fetchone()
    if row is None:
        return None, None
    return row["last_detail_at"], int(row["last_detail_job_id"])


def _touch_day_watermarks(conn: sqlite3.Connection, domain_id: int,
                          days: list[str], job_id: int, job_created_at: str) -> None:
    for d in days:
        conn.execute(
            """
            INSERT INTO day_watermarks(domain_id, biz_day, last_detail_job_id, last_detail_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(domain_id, biz_day) DO UPDATE SET
              last_detail_job_id=excluded.last_detail_job_id,
              last_detail_at=excluded.last_detail_at
            """,
            (domain_id, d, job_id, job_created_at),
        )


def _converge_late_days(conn: sqlite3.Connection, domain_id: int) -> None:
    """水位线已不晚于该日全部贴源行装载时间时，迟到标记方可收敛为已重算。"""
    pending = conn.execute(
        "SELECT biz_day FROM late_days WHERE domain_id=? AND status='待重算'",
        (domain_id,),
    ).fetchall()
    for r in pending:
        biz_day = r["biz_day"]
        watermark_ts, _ = _latest_detail_watermark(conn, domain_id, biz_day)
        if watermark_ts is None:
            continue
        newest_paste = conn.execute(
            "SELECT MAX(loaded_at) AS m FROM layer_paste WHERE domain_id=? AND biz_day=?",
            (domain_id, biz_day),
        ).fetchone()["m"]
        if newest_paste is not None and _ts_key(watermark_ts) >= _ts_key(newest_paste):
            conn.execute(
                f"UPDATE late_days SET status='已重算', updated_at={NOW_SQL} "
                "WHERE domain_id=? AND biz_day=? AND status='待重算'",
                (domain_id, biz_day),
            )


def _mark_late(conn: sqlite3.Connection, domain_id: int, biz_day: str,
               paste_id: int, watermark_ts: str, watermark_job_id: int | None) -> None:
    note = f"贴源新行（编号{paste_id}）晚于该业务日最近一次成功明细任务（编号{watermark_job_id}，{watermark_ts}）"
    conn.execute(
        f"""
        INSERT INTO late_days(domain_id, biz_day, status, trigger_paste_id,
                              last_detail_job_id, note, created_at, updated_at)
        VALUES (?, ?, '待重算', ?, ?, ?, {NOW_SQL}, {NOW_SQL})
        ON CONFLICT(domain_id, biz_day) DO UPDATE SET
          status='待重算',
          trigger_paste_id=excluded.trigger_paste_id,
          last_detail_job_id=excluded.last_detail_job_id,
          note=excluded.note,
          updated_at={NOW_SQL}
        """,
        (domain_id, biz_day, paste_id, watermark_job_id, note),
    )


def append_paste_row(domain_id: int, payload: dict) -> dict:
    """贴源追加新行；若该业务日已有成功明细任务且新行更晚，则登记迟到。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    biz_day = str(payload.get("biz_day") or "").strip()
    store_code = str(payload.get("store_code") or "").strip()
    if not biz_day or not store_code:
        raise ValueError("业务日与门店编码不能为空")
    amount = float(payload.get("amount") or 0)
    qty = int(payload.get("qty") or 0)
    store_name = str(payload.get("store_name") or "")
    category = str(payload.get("category") or "")
    raw = {
        "业务日": biz_day,
        "门店编码": store_code,
        "门店名称": store_name,
        "品类": category,
        "金额": amount,
        "件数": qty,
    }
    conn = connect()
    try:
        cur = conn.execute(
            f"""
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json, loaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {NOW_SQL})
            """,
            (domain_id, biz_day, store_code, store_name, category, amount, qty,
             json.dumps(raw, ensure_ascii=False)),
        )
        paste_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM layer_paste WHERE id=?", (paste_id,)).fetchone()
        watermark_ts, watermark_job_id = _latest_detail_watermark(conn, domain_id, biz_day)
        # 新行装载时间晚于该业务日最近一次成功明细任务时登记迟到；从未加工过的日不算迟到
        is_late = bool(
            watermark_ts is not None
            and _ts_key(row["loaded_at"]) > _ts_key(watermark_ts)
        )
        if is_late:
            _mark_late(conn, domain_id, biz_day, paste_id, watermark_ts, watermark_job_id)
        conn.commit()
        out = dict(row)
        out["迟到"] = is_late
        return out
    finally:
        conn.close()


def rescan_late(domain_id: int) -> list[dict]:
    """不依赖追加动作，按库内装载时间重新扫描迟到业务日（供对账/手工触发）。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_rows = conn.execute(
            "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        marked: set[str] = set()
        for row in paste_rows:
            watermark_ts, watermark_job_id = _latest_detail_watermark(
                conn, domain_id, row["biz_day"]
            )
            if watermark_ts is not None and _ts_key(row["loaded_at"]) > _ts_key(watermark_ts):
                _mark_late(conn, domain_id, row["biz_day"], int(row["id"]),
                           watermark_ts, watermark_job_id)
                marked.add(row["biz_day"])
        conn.commit()
        return list_late_days(domain_id)
    finally:
        conn.close()


def list_late_days(domain_id: int | None = None, status: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM late_days WHERE 1=1"
        args: list = []
        if domain_id is not None:
            sql += " AND domain_id=?"
            args.append(domain_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY domain_id, biz_day"
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def pending_late_count_by_domain() -> dict[int, int]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT domain_id, COUNT(*) AS c FROM late_days WHERE status='待重算' GROUP BY domain_id"
        ).fetchall()
        return {int(r["domain_id"]): int(r["c"]) for r in rows}
    finally:
        conn.close()


def _rebuild_detail_for_days(conn: sqlite3.Connection, domain_id: int, days: list[str]) -> int:
    """按现行清洗规则只重跑指定业务日的明细；其它日明细行不动。返回写入行数。"""
    placeholders = ",".join("?" for _ in days)
    # 只删除受影响业务日的明细，其它日明细行不得删除或改写
    conn.execute(
        f"DELETE FROM layer_detail WHERE domain_id=? AND biz_day IN ({placeholders})",
        [domain_id, *days],
    )
    paste = conn.execute(
        f"SELECT * FROM layer_paste WHERE domain_id=? AND biz_day IN ({placeholders}) ORDER BY id",
        [domain_id, *days],
    ).fetchall()
    written = 0
    for row in paste:
        cleaned = _clean_paste_row(row)
        if cleaned is None:
            continue
        conn.execute(
            """
            INSERT INTO layer_detail(
              domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                cleaned["biz_day"],
                cleaned["store_code"],
                cleaned["store_name"],
                cleaned["category"],
                cleaned["amount"],
                cleaned["qty"],
                "已清洗",
            ),
        )
        written += 1
    return written


def _rebuild_summary_for_days(conn: sqlite3.Connection, domain_id: int, days: list[str]) -> int:
    """只重算指定业务日的汇总键；其它日汇总键不动。返回汇总键数。"""
    placeholders = ",".join("?" for _ in days)
    # 只删除受影响业务日的汇总键，其它日汇总键不得删除或改写
    conn.execute(
        f"DELETE FROM layer_summary WHERE domain_id=? AND biz_day IN ({placeholders})",
        [domain_id, *days],
    )
    detail = conn.execute(
        f"SELECT * FROM layer_detail WHERE domain_id=? AND biz_day IN ({placeholders}) ORDER BY id",
        [domain_id, *days],
    ).fetchall()
    buckets: dict[tuple, dict] = {}
    for row in detail:
        key = (row["biz_day"], row["store_code"])
        b = buckets.setdefault(
            key,
            {"biz_day": row["biz_day"], "store_code": row["store_code"],
             "amount": 0.0, "qty": 0, "n": 0},
        )
        b["amount"] += float(row["amount"])
        b["qty"] += int(row["qty"])
        b["n"] += 1
    for b in buckets.values():
        conn.execute(
            """
            INSERT INTO layer_summary(
              domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
        )
    return len(buckets)


def _day_snapshot(conn: sqlite3.Connection, domain_id: int, biz_day: str) -> dict:
    detail_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    ).fetchone()["c"])
    summary_amount = float(conn.execute(
        "SELECT COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    ).fetchone()["a"])
    detail_amount = float(conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=? AND biz_day=?",
        (domain_id, biz_day),
    ).fetchone()["a"])
    return {
        "biz_day": biz_day,
        "detail_count": detail_count,
        "summary_amount": round(summary_amount, 4),
        "detail_amount": round(detail_amount, 4),
    }


def run_late_recompute(domain_id: int, biz_days: list[str] | None = None) -> dict:
    """仅重算受影响业务日：先定点重跑明细，再定点重算汇总，全程不清空其它日。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        if biz_days is None:
            biz_days = [
                r["biz_day"] for r in conn.execute(
                    "SELECT biz_day FROM late_days WHERE domain_id=? AND status='待重算' ORDER BY biz_day",
                    (domain_id,),
                ).fetchall()
            ]
        days = sorted({d for d in (biz_days or []) if str(d).strip()})
        if not days:
            raise ValueError("没有需要定点重算的迟到业务日")
        unknown = [
            d for d in days
            if conn.execute(
                "SELECT 1 FROM layer_paste WHERE domain_id=? AND biz_day=? LIMIT 1",
                (domain_id, d),
            ).fetchone() is None
        ]
        if unknown:
            raise ValueError(f"业务日在贴源层不存在：{', '.join(unknown)}")

        before = {d: _day_snapshot(conn, domain_id, d) for d in days}
        detail_before_total = sum(s["detail_count"] for s in before.values())
        summary_amount_before_total = round(sum(s["summary_amount"] for s in before.values()), 4)

        # 阶段一：只重跑这些日的明细，其它日明细行不得删除或改写
        _rebuild_detail_for_days(conn, domain_id, days)
        # 阶段二：只重算这些日的汇总键
        _rebuild_summary_for_days(conn, domain_id, days)

        after = {d: _day_snapshot(conn, domain_id, d) for d in days}
        detail_after_total = sum(s["detail_count"] for s in after.values())
        summary_amount_after_total = round(sum(s["summary_amount"] for s in after.values()), 4)

        # 与分层表一致：每日汇总金额必须等于当日明细金额
        day_lines = []
        all_consistent = True
        for d in days:
            consistent = abs(after[d]["summary_amount"] - after[d]["detail_amount"]) < 0.0001
            all_consistent = all_consistent and consistent
            day_lines.append(
                {
                    "biz_day": d,
                    "detail_before": before[d]["detail_count"],
                    "detail_after": after[d]["detail_count"],
                    "summary_amount_before": before[d]["summary_amount"],
                    "summary_amount_after": after[d]["summary_amount"],
                    "consistent": consistent,
                }
            )

        job_cur = conn.execute(
            """
            INSERT INTO recompute_jobs(
              domain_id, status, day_count, detail_before, detail_after,
              summary_amount_before, summary_amount_after, consistent, message
            ) VALUES (?, '成功', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id, len(days), detail_before_total, detail_after_total,
                summary_amount_before_total, summary_amount_after_total,
                1 if all_consistent else 0,
                f"定点重算{len(days)}个业务日，仅改写这些日的明细与汇总",
            ),
        )
        recompute_job_id = int(job_cur.lastrowid)
        for line in day_lines:
            conn.execute(
                """
                INSERT INTO recompute_day_lines(
                  recompute_job_id, biz_day, detail_before, detail_after,
                  summary_amount_before, summary_amount_after
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recompute_job_id, line["biz_day"], line["detail_before"],
                    line["detail_after"], line["summary_amount_before"],
                    line["summary_amount_after"],
                ),
            )
        lj_cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count, created_at)
            VALUES (?, 'late_recompute', '成功', ?, ?, ?, 0, %s)
            """ % NOW_SQL,
            (
                domain_id,
                f"迟到日定点重算：{', '.join(days)}",
                detail_before_total,
                detail_after_total,
            ),
        )
        layer_job_id = int(lj_cur.lastrowid)
        layer_job_created_at = conn.execute(
            "SELECT created_at FROM layer_jobs WHERE id=?", (layer_job_id,)
        ).fetchone()["created_at"]
        # 定点重算同样推进这些业务日的明细水位线
        _touch_day_watermarks(conn, domain_id, days, layer_job_id, layer_job_created_at)
        # 标记收敛为已重算（刷新后仍保留可查询）
        placeholders = ",".join("?" for _ in days)
        conn.execute(
            f"UPDATE late_days SET status='已重算', updated_at={NOW_SQL} "
            f"WHERE domain_id=? AND biz_day IN ({placeholders})",
            [domain_id, *days],
        )
        conn.commit()
        return get_recompute_job(recompute_job_id)
    finally:
        conn.close()


def list_recompute_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM recompute_jobs WHERE 1=1"
        args: tuple = ()
        if domain_id is not None:
            sql += " AND domain_id=?"
            args = (domain_id,)
        sql += " ORDER BY id DESC"
        jobs = rows_to_dicts(conn.execute(sql, args).fetchall())
        grouped: dict[int, list[dict]] = {}
        for line in rows_to_dicts(
            conn.execute("SELECT * FROM recompute_day_lines ORDER BY biz_day").fetchall()
        ):
            grouped.setdefault(int(line["recompute_job_id"]), []).append(line)
        for job in jobs:
            job["step_label"] = STEP_LABEL["late_recompute"]
            job["days"] = sorted(grouped.get(job["id"], []), key=lambda x: x["biz_day"])
        return jobs
    finally:
        conn.close()


def get_recompute_job(job_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM recompute_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["step_label"] = STEP_LABEL["late_recompute"]
        job["days"] = rows_to_dicts(
            conn.execute(
                "SELECT * FROM recompute_day_lines WHERE recompute_job_id=? ORDER BY biz_day",
                (job_id,),
            ).fetchall()
        )
        return job
    finally:
        conn.close()


def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary = conn.execute(
            "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchone()
        summary_line_sum = int(summary["s"])
        summary_amount = float(summary["a"])
        detail_amount = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
                (domain_id,),
            ).fetchone()["a"]
        )
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = round(summary_amount - detail_amount, 4)
        status = "一致" if key_mismatch == 0 and abs(amount_drift) < 0.0001 else "存在差异"
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    late_days = list_late_days(status="待重算")
    recompute_jobs = list_recompute_jobs()[:5]
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "待重算迟到日数": len(late_days),
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
            }
            for r in reports
        ],
        "待重算迟到日": [
            {
                "主题域": d["domain_id"],
                "业务日": d["biz_day"],
                "状态": d["status"],
                "说明": d["note"],
            }
            for d in late_days
        ],
        "最近定点重算": [
            {
                "编号": j["id"],
                "主题域": j["domain_id"],
                "业务日数": j["day_count"],
                "明细行数": f"{j['detail_before']} → {j['detail_after']}",
                "汇总一致": "是" if j["consistent"] else "否",
            }
            for j in recompute_jobs
        ],
        "主题域": [
            {
                "编号": d["id"],
                "名称": d["name"],
                "业务键": d["biz_key"],
                "待重算迟到日": d["pending_late_days"],
            }
            for d in domains
        ],
    }
