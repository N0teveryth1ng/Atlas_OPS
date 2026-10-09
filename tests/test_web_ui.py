"""Web UI tests for setup/profile/login/password protection."""

from __future__ import annotations

import os
from fastapi.testclient import TestClient

from atlas.dashboard import create_app


def _login(client: TestClient, pwd="secret"):
    os.environ["DASHBOARD_PASSWORD"] = pwd
    r = client.post("/login", data={"password": pwd})
    return r


def test_login_required_on_routes(tmp_path):
    if "DASHBOARD_PASSWORD" in os.environ:
        del os.environ["DASHBOARD_PASSWORD"]
    client = TestClient(create_app(tmp_path / "db.db"))
    assert client.get("/").status_code == 200  # no pwd set, maybe allowed? depends
    # with pwd, redirect
    os.environ["DASHBOARD_PASSWORD"] = "pw"
    client = TestClient(create_app(tmp_path / "db.db"))
    assert client.get("/").status_code == 303
    assert "/login" in client.get("/").headers.get("location", "")


def test_refuse_non_loopback_without_password(tmp_path):
    if "DASHBOARD_PASSWORD" in os.environ:
        del os.environ["DASHBOARD_PASSWORD"]
    try:
        create_app(tmp_path / "db.db", host="0.0.0.0")
    except RuntimeError:
        pass
    else:
        assert False


def test_upload_size_limit(tmp_path):
    os.environ["DASHBOARD_PASSWORD"] = "pw"
    client = TestClient(create_app(tmp_path / "db.db"))
    _login(client, "pw")
    big = b"x" * (6 * 1024 * 1024)
    r = client.post("/setup", files={"resume": ("f.pdf", big, "application/pdf")}, data={"description": ""})
    assert "too large" in r.text


def test_upload_type_limit(tmp_path):
    os.environ["DASHBOARD_PASSWORD"] = "pw"
    client = TestClient(create_app(tmp_path / "db.db"))
    _login(client, "pw")
    r = client.post("/setup", files={"resume": ("f.txt", b"hi", "text/plain")}, data={"description": ""})
    assert "PDF only" in r.text


def test_pipeline_blocked_without_approved_profile(tmp_path):
    os.environ["DASHBOARD_PASSWORD"] = "pw"
    client = TestClient(create_app(tmp_path / "db.db"))
    _login(client, "pw")
    r = client.get("/profile")
    assert "No profile found" in r.text
