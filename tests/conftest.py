"""
Test setup.

The app reads MONGO_DB at import time, so we point it at a throwaway test
database *before* importing anything from app.main, then wipe the collections
and recreate indexes before each test.
"""
import os

os.environ["MONGO_DB"] = "attendance_test"
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app, attendance, client, employees, ensure_indexes  # noqa: E402


@pytest.fixture()
def api():
    # Cleaning per test keeps them independent; the database is a *test* DB.
    client.drop_database("attendance_test")
    ensure_indexes()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_employee(api):
    def _make(emp_code="EMP0001", **overrides):
        body = {
            "emp_code": emp_code,
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        }
        body.update(overrides)
        r = api.post("/employees", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    return _make


def ms(dt):
    """Whole-second epoch ms helper for tests."""
    return int(dt.timestamp()) * 1000
