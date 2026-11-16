from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest


@pytest.fixture()
def temp_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")
    )
    # 确保模块级路径在测试中重新解析
    import app.db as db

    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "app.db")
    from app import seed

    seed.init_db()
    yield data_dir


@pytest.fixture()
def client(temp_data):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def built_pipeline(client):
    """跑通一次完整主链，返回主题域编号。"""
    r = client.post("/api/jobs/paste-to-detail", json={"domain_id": 1})
    assert r.status_code == 200
    r = client.post("/api/jobs/detail-to-summary", json={"domain_id": 1})
    assert r.status_code == 200
    time.sleep(0.02)
    return 1
