"""
pytest suite.

Covers the rules that are easy to get subtly wrong:
  * duplicate punch-in -> 409
  * punch-out with no open punch-in -> 404
  * a correction recomputes the derived fields and appends exactly one,
    immutable history entry (before/after + revision grows)
  * analytics run on the stored values (leaderboard ties use standard
    competition ranking)

Plus a handful of boundary tests for R2/R3/R4/R5 and R1.
"""
import threading
from datetime import datetime, timedelta

import pytest

from app.main import IST, UTC, attendance

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: datetime) -> int:
    return int(dt.timestamp()) * 1000


def ist(y, mo, d, h, mi, s=0) -> int:
    return to_ms(datetime(y, mo, d, h, mi, s, tzinfo=IST))


def seed_log(emp_code, date_str, *, status="PRESENT", late=0, hours=None, overtime=0, half_day=False):
    """Insert an attendance doc straight into Mongo (avoids punch-in rules)."""
    attendance.insert_one({
        "emp_code": emp_code,
        "date": date_str,
        "status": status,
        "punch_in": datetime.fromisoformat(f"{date_str}T09:30:00").replace(tzinfo=IST).astimezone(UTC) if status in ("PRESENT", "WFH", "ON_DUTY") else None,
        "punch_out": None,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    })


# --------------------------------------------------------------------------- #
# employees
# --------------------------------------------------------------------------- #
def test_create_and_duplicate_employee(api, make_employee):
    make_employee("EMP0001")
    dup = api.post("/employees", json={
        "emp_code": "EMP0001", "name": "Clone", "email": "c@e.com",
        "department": "Eng", "joined_on": "2026-01-05",
    })
    assert dup.status_code == 409


def test_unknown_employee_list_and_filter(api, make_employee):
    make_employee("EMP0001", department="Engineering")
    make_employee("EMP0002", department="Sales")
    r = api.get("/employees", params={"department": "Sales"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["emp_code"] == "EMP0002"


# --------------------------------------------------------------------------- #
# punch-in / punch-out
# --------------------------------------------------------------------------- #
def test_duplicate_punch_in_is_409(api, make_employee):
    make_employee("EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)}
    assert api.post("/attendance/punch-in", json=body).status_code == 201
    second = api.post("/attendance/punch-in", json=body)
    assert second.status_code == 409


def test_punch_out_without_punch_in_is_404(api, make_employee):
    make_employee("EMP0001")
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 18, 30)})
    assert r.status_code == 404


def test_unknown_employee_punch_in_is_404(api):
    r = api.post("/attendance/punch-in", json={"emp_code": "NOPE", "punched_at": ist(2026, 7, 6, 9, 30)})
    assert r.status_code == 404


@pytest.mark.parametrize("h, m, s, expected", [
    (9, 40, 0, 0),    # exactly 10:00 after shift start -> inside grace
    (9, 40, 1, 10),   # one second over -> 10 whole minutes late
    (10, 15, 59, 45),
])
def test_late_minutes_boundaries(api, make_employee, h, m, s, expected):
    make_employee("EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, h, m, s)})
    assert r.json()["late_minutes"] == expected


def test_punch_out_computes_hours_overtime_halfday(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 28)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 18, 35)})
    body = r.json()
    assert body["work_hours"] == 9.12
    assert body["overtime_minutes"] == 0  # 5 min < 30 min minimum
    assert body["half_day"] is False


def test_overtime_and_late_together(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 10, 5)})
    body = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 19, 10)}).json()
    assert body["late_minutes"] == 35
    assert body["overtime_minutes"] == 40
    assert body["work_hours"] == 9.08


def test_half_day(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)})
    body = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 13, 0)}).json()
    assert body["work_hours"] == 3.5
    assert body["half_day"] is True


def test_punch_out_before_punch_in_is_422(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 10, 0)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 0)})
    assert r.status_code == 422


def test_epoch_seconds_are_rejected(api, make_employee):
    make_employee("EMP0001")
    # 1.7e9 looks like seconds, not milliseconds -> 422
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert r.status_code == 422


def test_overnight_shift_date_and_hours(api, make_employee):
    make_employee("EMP0005", shift_start="22:00", shift_end="06:00")
    # shift that started the evening of 6 July
    body = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": ist(2026, 7, 6, 21, 55),
    }).json()
    assert body["date"] == "2026-07-06"
    out = api.post("/attendance/punch-out", json={
        "emp_code": "EMP0005", "punched_at": ist(2026, 7, 7, 6, 40),
    }).json()
    assert out["work_hours"] == 8.75
    assert out["overtime_minutes"] == 40


def test_overnight_punch_in_after_midnight_belongs_to_previous_day(api, make_employee):
    make_employee("EMP0005", shift_start="22:00", shift_end="06:00")
    body = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": ist(2026, 7, 7, 2, 0),
    }).json()
    assert body["date"] == "2026-07-06"


def test_concurrent_punch_in_yields_one_201(api, make_employee):
    make_employee("EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 30)}
    codes = []
    lock = threading.Lock()

    def worker():
        r = api.post("/attendance/punch-in", json=body)
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(201) == 1
    assert codes.count(409) == 5


# --------------------------------------------------------------------------- #
# corrections + audit trail
# --------------------------------------------------------------------------- #
def test_regularize_recomputes_and_appends_history(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 18, 30)})

    new_in = ist(2026, 7, 7, 9, 28)
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": new_in, "reason": "biometric glitch", "regularized_by": "hr.admin",
    })
    assert r.status_code == 200, r.text
    rec = r.json()

    assert rec["late_minutes"] == 0                    # recomputed (R2)
    assert len(rec["history"]) == 1                    # exactly one entry
    entry = rec["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert entry["changes"]["punch_in"]["to"] == new_in
    assert entry["changes"]["late_minutes"]["from"] == 35
    assert entry["changes"]["late_minutes"]["to"] == 0


def test_regularize_history_is_append_only(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 10, 5)})

    api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ist(2026, 7, 7, 9, 28), "reason": "fix one", "regularized_by": "hr",
    })
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_out": ist(2026, 7, 7, 18, 0), "reason": "forgot to punch out", "regularized_by": "hr",
    })
    rec = r.json()
    assert len(rec["history"]) == 2                              # appended, not replaced
    assert rec["history"][0]["reason"] == "fix one"              # first entry untouched
    assert rec["history"][1]["reason"] == "forgot to punch out"


def test_regularize_no_change_is_422(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 9, 28)})
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": ist(2026, 7, 7, 9, 28), "reason": "same value", "regularized_by": "hr",
    })
    assert r.status_code == 422


def test_regularize_leave_clears_punch_times(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 9, 28)})
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr",
    })
    rec = r.json()
    assert rec["status"] == "LEAVE"
    assert rec["punch_in"] is None and rec["punch_out"] is None
    assert rec["work_hours"] is None


def test_absent_with_punch_in_is_422(api, make_employee):
    make_employee("EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 9, 28)})
    r = api.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "ABSENT", "punch_in": ist(2026, 7, 7, 9, 28),
        "reason": "invalid combo", "regularized_by": "hr",
    })
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #
def test_employee_monthly_pipeline(api, make_employee):
    make_employee("EMP0001")
    seed_log("EMP0001", "2026-07-06", hours=9.0)                 # Mon
    seed_log("EMP0001", "2026-07-08", status="LEAVE")          # Wed
    seed_log("EMP0001", "2026-07-11", status="WFH", hours=3.5, half_day=True)  # Sat -> not present

    body = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert body["present_days"] == 1        # weekend WFH does not count (R7)
    assert body["leave_days"] == 1
    assert body["working_days"] == 23


def test_late_leaderboard_ties_use_competition_rank(api, make_employee):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        make_employee(code)
    seed_log("EMP0001", "2026-07-06", late=30)
    seed_log("EMP0002", "2026-07-06", late=30)
    seed_log("EMP0003", "2026-07-06", late=10)

    full = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [row["rank"] for row in full] == [1, 1, 3]          # 1,1,3 not 1,2,3
    assert [row["emp_code"] for row in full][:2] == ["EMP0001", "EMP0002"]

    # limit applies AFTER ranking: only rows with rank <= 2 survive, both rank 1.
    cut = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert len(cut) == 2
    assert {row["emp_code"] for row in cut} == {"EMP0001", "EMP0002"}


def test_department_summary_counts_zero_log_employees(api, make_employee):
    make_employee("EMP0001", department="Engineering")   # has a log
    make_employee("EMP0002", department="Engineering")   # no logs, still counts
    seed_log("EMP0001", "2026-07-06", hours=9.0)

    items = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    eng = next(i for i in items if i["department"] == "Engineering")
    assert eng["headcount"] == 2
    assert eng["present_days"] == 1
    assert eng["avg_work_hours"] == 9.0


def test_department_trend_gap_fills_and_moving_average(api, make_employee):
    make_employee("EMP0001", department="Sales")
    seed_log("EMP0001", "2026-07-06", hours=8.0)

    items = api.get("/analytics/departments/Sales/trend", params={"from": "2026-07-06", "to": "2026-07-08"}).json()["items"]
    assert [i["date"] for i in items] == ["2026-07-06", "2026-07-07", "2026-07-08"]
    assert items[0]["attendance_rate"] == 1.0
    assert items[1]["attendance_rate"] == 0.0      # working day, no records -> 0.0
    assert items[2]["moving_avg_7d"] == 0.3333     # mean(1.0, 0.0, 0.0)


def test_attendance_list_rejects_bad_date_range(api):
    r = api.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"})
    assert r.status_code == 422
