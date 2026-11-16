from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  loaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 业务日明细水位线：该业务日最近一次被成功明细加工覆盖的时间与任务
CREATE TABLE IF NOT EXISTS day_watermarks (
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  last_detail_job_id INTEGER NOT NULL,
  last_detail_at TEXT NOT NULL,
  PRIMARY KEY(domain_id, biz_day),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 迟到业务日：贴源新行晚于该业务日最近一次成功明细任务时登记
CREATE TABLE IF NOT EXISTS late_days (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待重算',
  trigger_paste_id INTEGER,
  last_detail_job_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  UNIQUE(domain_id, biz_day),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 定点重算任务报告（只重算受影响业务日，禁止全表清空）
CREATE TABLE IF NOT EXISTS recompute_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  day_count INTEGER NOT NULL DEFAULT 0,
  detail_before INTEGER NOT NULL DEFAULT 0,
  detail_after INTEGER NOT NULL DEFAULT 0,
  summary_amount_before REAL NOT NULL DEFAULT 0,
  summary_amount_after REAL NOT NULL DEFAULT 0,
  consistent INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS recompute_day_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recompute_job_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  detail_before INTEGER NOT NULL,
  detail_after INTEGER NOT NULL,
  summary_amount_before REAL NOT NULL,
  summary_amount_after REAL NOT NULL,
  FOREIGN KEY(recompute_job_id) REFERENCES recompute_jobs(id)
);
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _upgrade(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        conn.commit()
    finally:
        conn.close()


def _upgrade(conn) -> None:
    """对早期库做幂等补列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(layer_paste)").fetchall()}
    if "loaded_at" not in cols:
        conn.execute(
            "ALTER TABLE layer_paste ADD COLUMN loaded_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%d %H:%M:%f', 'now'))"
        )


def _seed_domain(conn) -> None:
    fixture = "门店日销售-贴源.jsonl"
    cur = conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, description)
        VALUES (?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            fixture,
            "演示贴源装载、明细清洗与按门店日汇总",
        ),
    )
    domain_id = int(cur.lastrowid)
    path = FIXTURES_DIR / fixture
    if not path.exists():
        # 仓库内相对路径兜底
        alt = Path(__file__).resolve().parents[2] / "fixtures" / fixture
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                obj.get("业务日") or "",
                obj.get("门店编码") or "",
                obj.get("门店名称") or "",
                obj.get("品类") or "",
                float(obj.get("金额") or 0),
                int(obj.get("件数") or 0),
                json.dumps(obj, ensure_ascii=False),
            ),
        )
