"""E2E tests — test API endpoints directly via TestClient."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Set test env vars before importing the app
os.environ.setdefault("VOIDAI_API_KEY", "test-key")
os.environ.setdefault("WAVESPEED_API_KEY", "test-key")
os.environ.setdefault("VOICEAPI_KEY", "test-key")
os.environ.setdefault("ACCESS_CODE", "test-code")

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)
HEADERS = {"X-API-Key": "test-code"}


class TestHealthEndpoints:
    def test_health_returns_ok(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data

    def test_health_has_active_jobs(self):
        resp = client.get("/api/health")
        data = resp.json()
        assert "active_jobs" in data
        assert isinstance(data["active_jobs"], int)


class TestMetricsEndpoint:
    def test_metrics_returns_prometheus_format(self):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        assert "videoforge_uptime_seconds" in resp.text
        assert "videoforge_jobs_running" in resp.text

    def test_metrics_no_auth_required(self):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200

    def test_metrics_has_help_and_type(self):
        lines = resp = client.get("/api/metrics").text.strip().split("\n")
        help_lines = [l for l in lines if l.startswith("# HELP")]
        type_lines = [l for l in lines if l.startswith("# TYPE")]
        assert len(help_lines) >= 4
        assert len(type_lines) >= 4


class TestAuthEndpoints:
    def test_no_api_key_rejected(self):
        resp = client.get("/api/jobs")
        assert resp.status_code == 401

    def test_wrong_api_key_rejected(self):
        resp = client.get("/api/jobs", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_correct_api_key_accepted(self):
        resp = client.get("/api/jobs", headers=HEADERS)
        assert resp.status_code == 200


class TestJobEndpoints:
    def test_list_jobs_returns_list(self):
        resp = client.get("/api/jobs", headers=HEADERS)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_nonexistent_job_404(self):
        resp = client.get("/api/jobs/nonexistent123", headers=HEADERS)
        assert resp.status_code == 404


class TestOpenAPIDocs:
    def test_openapi_schema_exists(self):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "info" in schema
        assert "paths" in schema

    def test_docs_page_accessible(self):
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_page_accessible(self):
        resp = client.get("/redoc")
        assert resp.status_code == 200
