#!/usr/bin/env python
"""
Interactive API tour / conformance tester
=========================================

A friendly, menu-driven smoke test for the Employee Attendance & Analytics API.
It walks the whole contract (rules R1-R10), shows the expected result next to
the real one, and ends with a pass/fail summary. It only uses the HTTP API, so
it works against a running server exactly like the grader does.

Start the server first (in another terminal)::

    uvicorn app.main:app --port 8000

Then run this::

    python scripts/api_tour.py
    python scripts/api_tour.py --url http://127.0.0.1:8000
    python scripts/api_tour.py --all          # non-interactive: run everything

Tips
----
* Point the server at a scratch database so the tour does not touch real data::

      $env:MONGO_DB = "attendance_tour"; uvicorn app.main:app --port 8000

* Every employee it creates uses a fresh random 6-digit code, so re-running the
  tour never collides with earlier runs.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

try:
    import httpx
except ImportError:  # pragma: no cover
    print("This tour needs 'httpx' (it is in requirements.txt). Try: pip install -r requirements.txt")
    raise SystemExit(1)

# --------------------------------------------------------------------------- #
# pretty output
# --------------------------------------------------------------------------- #
if os.name == "nt":
    os.system("")  # enable ANSI escapes in Windows consoles

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str) -> str:
    return code if _COLOR else ""

GREEN, RED, YELLOW, CYAN, BOLD, DIM, RESET = (
    _c("\033[92m"), _c("\033[91m"), _c("\033[93m"),
    _c("\033[96m"), _c("\033[1m"), _c("\033[2m"), _c("\033[0m"),
)

IST = timezone(timedelta(hours=5, minutes=30))

def ms(y, mo, d, h, mi, s=0) -> int:
    """IST wall-clock -> epoch milliseconds (what every request expects)."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000

def rhu(value, places):
    """Round half-up, matching rule R8."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))

# --------------------------------------------------------------------------- #
# context: the HTTP client + a fresh-code generator
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, client: httpx.Client):
        self.c = client
        self._n = random.randint(100000, 900000)

    def code(self) -> str:
        self._n += 1
        return "EMP%06d" % self._n

    def employee(self, dept="Engineering", joined="2026-01-05", start="09:30", end="18:30") -> str:
        code = self.code()
        r = self.c.post("/employees", json={
            "emp_code": code, "name": "Tour User", "email": "tour@example.com",
            "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
        })
        if r.status_code != 201:
            raise RuntimeError(f"setup failed: could not create {code}: {r.status_code} {r.text}")
        return code

    def pin(self, code, when):
        return self.c.post("/attendance/punch-in", json={"emp_code": code, "punched_at": when})

    def pout(self, code, when):
        return self.c.post("/attendance/punch-out", json={"emp_code": code, "punched_at": when})


def emit(results, name, expected, ok, actual):
    results.append((name, expected, bool(ok), str(actual)))
    tag = "PASS" if ok else "FAIL"
    color = GREEN if ok else RED
    print(f"   [{color}{BOLD}{tag}{RESET}] {name}")
    if not ok:
        print(f"         {DIM}expected:{RESET} {expected}")
        print(f"         {DIM}actual:  {RESET} {actual}")


def close(a, b, tol=1e-9):
    return a is not None and abs(float(a) - float(b)) <= tol

# --------------------------------------------------------------------------- #
# section 1: system
# --------------------------------------------------------------------------- #
def section_system(ctx):
    res = []
    r = ctx.c.get("/health")
    emit(res, "GET /health -> 200 {\"status\":\"ok\"}", "200 {'status': 'ok'}",
         r.status_code == 200 and r.json() == {"status": "ok"}, f"{r.status_code} {r.text}")

    r = ctx.c.get("/", follow_redirects=False)
    emit(res, "GET / -> 307 redirect to /docs", "307 -> /docs",
         r.status_code == 307 and r.headers.get("location") == "/docs",
         f"{r.status_code} -> {r.headers.get('location')}")

    r = ctx.c.get("/docs")
    emit(res, "GET /docs -> 200 (interactive docs)", "200", r.status_code == 200, r.status_code)
    return res

# --------------------------------------------------------------------------- #
# section 2: employees
# --------------------------------------------------------------------------- #
def section_employees(ctx):
    res = []

    code = ctx.code()
    body = {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"}
    r = ctx.c.post("/employees", json=body)
    j = r.json()
    emit(res, "POST /employees -> 201 with echoed fields + created_at",
         "201, emp_code, default shift 09:30/18:30, created_at is int ms",
         r.status_code == 201 and j.get("emp_code") == code
         and j.get("shift_start") == "09:30" and j.get("shift_end") == "18:30"
         and isinstance(j.get("created_at"), int),
         f"{r.status_code} {j}")

    r = ctx.c.post("/employees", json=body)
    emit(res, "POST /employees duplicate emp_code -> 409", "409", r.status_code == 409, r.status_code)

    r = ctx.c.post("/employees", json=dict(body, emp_code="X1"))
    emit(res, "POST /employees bad emp_code 'X1' -> 422", "422", r.status_code == 422, r.status_code)

    r = ctx.c.post("/employees", json=dict(body, emp_code=ctx.code(), shift_start="09:30", shift_end="09:30"))
    emit(res, "POST /employees shift_start == shift_end -> 422", "422", r.status_code == 422, r.status_code)

    # a unique department, three members, to test filtering + pagination cleanly
    dept = f"TourDept-{random.randint(1000, 9999)}"
    codes = [ctx.employee(dept=dept) for _ in range(3)]
    r = ctx.c.get("/employees", params={"department": dept, "page": 1, "page_size": 20})
    j = r.json()
    got = [e["emp_code"] for e in j["items"]]
    emit(res, "GET /employees?department=... -> filtered, sorted by emp_code",
         f"total=3, items sorted ascending {sorted(codes)}",
         r.status_code == 200 and j["total"] == 3 and got == sorted(codes) and all(e["department"] == dept for e in j["items"]),
         f"total={j.get('total')} items={got}")

    p1 = ctx.c.get("/employees", params={"department": dept, "page": 1, "page_size": 2}).json()
    p2 = ctx.c.get("/employees", params={"department": dept, "page": 2, "page_size": 2}).json()
    emit(res, "GET /employees pagination -> page1:2, page2:1 (no off-by-one)",
         "page1 has 2 items, page2 has 1 item, total=3",
         len(p1["items"]) == 2 and len(p2["items"]) == 1 and p1["total"] == 3,
         f"p1={len(p1['items'])} p2={len(p2['items'])} total={p1['total']}")
    return res

# --------------------------------------------------------------------------- #
# section 3: punch in/out + rules R1-R5
# --------------------------------------------------------------------------- #
def section_rules(ctx):
    res = []
    emp = ctx.employee()  # 09:30-18:30

    r = ctx.c.post("/attendance/punch-in", json={"emp_code": "EMP000000"})
    emit(res, "punch-in unknown employee -> 404", "404", r.status_code == 404, r.status_code)

    r = ctx.pin(emp, ms(2026, 6, 1, 9, 45))
    j = r.json()
    emit(res, "punch-in 09:45 (shift 09:30) -> 201 late_minutes=15, rest defaults",
         "201 late_minutes=15 work_hours=null overtime=0 half_day=false history=[]",
         r.status_code == 201 and j["late_minutes"] == 15 and j["work_hours"] is None
         and j["overtime_minutes"] == 0 and j["half_day"] is False and j["history"] == [],
         f"{r.status_code} late={j.get('late_minutes')} wh={j.get('work_hours')}")

    r = ctx.pin(emp, ms(2026, 6, 1, 9, 45))
    emit(res, "punch-in same date again -> 409 (race-safe unique index)", "409", r.status_code == 409, r.status_code)

    r = ctx.c.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": 1783312500})
    emit(res, "punch-in with seconds-looking value -> 422", "422", r.status_code == 422, r.status_code)

    # R2 boundaries
    ctx.pin(emp, ms(2026, 6, 2, 9, 40, 0))
    r = ctx.pout(emp, ms(2026, 6, 2, 18, 30))
    j = r.json()
    emit(res, "R2 exactly 09:40:00 -> late_minutes=0 (inside grace)", "late_minutes=0", j["late_minutes"] == 0, j.get("late_minutes"))

    ctx.pin(emp, ms(2026, 6, 3, 9, 40, 1))
    j = ctx.pout(emp, ms(2026, 6, 3, 18, 30)).json()
    emit(res, "R2 09:40:01 (1s over grace) -> late_minutes=10", "late_minutes=10", j["late_minutes"] == 10, j.get("late_minutes"))

    ctx.pin(emp, ms(2026, 6, 4, 10, 15, 59))
    j = ctx.pout(emp, ms(2026, 6, 4, 18, 30)).json()
    emit(res, "R2 10:15:59 -> late_minutes=45", "late_minutes=45", j["late_minutes"] == 45, j.get("late_minutes"))

    # R3 overtime threshold
    ctx.pin(emp, ms(2026, 6, 5, 9, 30))
    j = ctx.pout(emp, ms(2026, 6, 5, 19, 10)).json()
    emit(res, "R3 out 19:10 (40m after 18:30) -> overtime_minutes=40", "overtime_minutes=40", j["overtime_minutes"] == 40, j.get("overtime_minutes"))

    ctx.pin(emp, ms(2026, 6, 8, 9, 30))
    j = ctx.pout(emp, ms(2026, 6, 8, 18, 50)).json()
    emit(res, "R3 out 18:50 (20m < 30m minimum) -> overtime_minutes=0", "overtime_minutes=0", j["overtime_minutes"] == 0, j.get("overtime_minutes"))

    # R4 / R5
    ctx.pin(emp, ms(2026, 6, 9, 9, 30))
    j = ctx.pout(emp, ms(2026, 6, 9, 13, 0)).json()
    emit(res, "R4/R5 09:30->13:00 -> work_hours=3.5, half_day=true",
         "work_hours=3.5 half_day=true", close(j["work_hours"], 3.5) and j["half_day"] is True,
         f"wh={j.get('work_hours')} half={j.get('half_day')}")

    ctx.pin(emp, ms(2026, 6, 10, 9, 0, 0))
    j = ctx.pout(emp, ms(2026, 6, 10, 11, 7, 30)).json()
    emit(res, "R4 rounding half-up: 2.125h -> 2.13 (not 2.12)",
         "work_hours=2.13", close(j["work_hours"], 2.13), j.get("work_hours"))

    # punch-out error cases
    lone = ctx.employee()
    r = ctx.pout(lone, ms(2026, 6, 1, 18, 30))
    emit(res, "punch-out with no punch-in -> 404", "404", r.status_code == 404, r.status_code)

    e2 = ctx.employee()
    ctx.pin(e2, ms(2026, 6, 1, 10, 0))
    r = ctx.pout(e2, ms(2026, 6, 1, 9, 0))
    emit(res, "punch-out before punch_in -> 422", "422", r.status_code == 422, r.status_code)

    r = ctx.pout(e2, ms(2026, 6, 3, 11, 0))
    emit(res, "punch-out more than 24h after punch_in -> 422", "422", r.status_code == 422, r.status_code)

    ctx.pout(e2, ms(2026, 6, 1, 18, 30))
    r = ctx.pout(e2, ms(2026, 6, 1, 19, 0))
    emit(res, "second punch-out on a closed record -> 409", "409", r.status_code == 409, r.status_code)

    # R1 overnight
    night = ctx.employee(start="22:00", end="06:00")
    j = ctx.pin(night, ms(2026, 7, 6, 21, 55)).json()
    emit(res, "R1 overnight punch-in 21:55 -> date=2026-07-06", "date=2026-07-06", j["date"] == "2026-07-06", j.get("date"))
    j = ctx.pout(night, ms(2026, 7, 7, 6, 40)).json()
    emit(res, "R1 overnight punch-out 06:40 -> work_hours=8.75, overtime=40",
         "work_hours=8.75 overtime_minutes=40", close(j["work_hours"], 8.75) and j["overtime_minutes"] == 40,
         f"wh={j.get('work_hours')} ot={j.get('overtime_minutes')}")

    night2 = ctx.employee(start="22:00", end="06:00")
    j = ctx.pin(night2, ms(2026, 7, 7, 2, 0)).json()
    emit(res, "R1 overnight after-midnight punch-in 02:00 -> date=2026-07-06",
         "date=2026-07-06", j["date"] == "2026-07-06", j.get("date"))
    return res

# --------------------------------------------------------------------------- #
# section 4: listing, filters, pagination (R10)
# --------------------------------------------------------------------------- #
def section_listing(ctx):
    res = []
    emp = ctx.employee()
    for d in (2, 3, 4):
        ctx.pin(emp, ms(2026, 3, d, 9, 30))

    r = ctx.c.get("/attendance", params={"emp_code": emp})
    j = r.json()
    dates = [i["date"] for i in j["items"]]
    emit(res, "GET /attendance?emp_code=... -> total matches, date DESC",
         "total=3, dates [03-04, 03-03, 03-02]",
         j["total"] == 3 and dates == ["2026-03-04", "2026-03-03", "2026-03-02"],
         f"total={j.get('total')} dates={dates}")

    p1 = ctx.c.get("/attendance", params={"emp_code": emp, "page": 1, "page_size": 2}).json()
    p2 = ctx.c.get("/attendance", params={"emp_code": emp, "page": 2, "page_size": 2}).json()
    emit(res, "GET /attendance pagination -> page1:2, page2:1",
         "page1=2 page2=1 total=3",
         len(p1["items"]) == 2 and len(p2["items"]) == 1 and p1["total"] == 3,
         f"p1={len(p1['items'])} p2={len(p2['items'])}")

    r = ctx.c.get("/attendance", params={"emp_code": emp, "status": "PRESENT"})
    emit(res, "GET /attendance?status=PRESENT -> only present records",
         "total=3 (all PRESENT)", r.json()["total"] == 3, r.json()["total"])

    r = ctx.c.get("/attendance", params={"date_from": "2026-03-10", "date_to": "2026-03-01"})
    emit(res, "GET /attendance date_from > date_to -> 422", "422", r.status_code == 422, r.status_code)
    return res

# --------------------------------------------------------------------------- #
# section 5: regularize + audit trail
# --------------------------------------------------------------------------- #
def section_regularize(ctx):
    res = []
    emp = ctx.employee()
    ctx.pin(emp, ms(2026, 5, 1, 10, 5))          # late 35
    ctx.pout(emp, ms(2026, 5, 1, 18, 30))

    new_in = ms(2026, 5, 1, 9, 28)
    r = ctx.c.patch("/attendance/EMP000000/2026-05-01",
                    json={"punch_in": new_in, "reason": "x x x", "regularized_by": "hr"})
    emit(res, "PATCH unknown employee -> 404", "404", r.status_code == 404, r.status_code)

    r = ctx.c.patch(f"/attendance/{emp}/2026-05-09",
                    json={"punch_in": new_in, "reason": "x x x", "regularized_by": "hr"})
    emit(res, "PATCH no record for that date -> 404", "404", r.status_code == 404, r.status_code)

    r = ctx.c.patch(f"/attendance/{emp}/not-a-date",
                    json={"punch_in": new_in, "reason": "x x x", "regularized_by": "hr"})
    emit(res, "PATCH malformed date -> 422", "422", r.status_code == 422, r.status_code)

    r = ctx.c.patch(f"/attendance/{emp}/2026-05-01",
                    json={"punch_in": new_in, "reason": "biometric glitch", "regularized_by": "hr.admin"})
    j = r.json()
    ch = (j.get("history") or [{}])[0].get("changes", {})
    emit(res, "PATCH punch_in 10:05->09:28 -> recompute + 1 history entry",
         "200 late_minutes=0 history len=1 changes.late_minutes {from:35,to:0}",
         r.status_code == 200 and j["late_minutes"] == 0 and len(j["history"]) == 1
         and ch.get("late_minutes") == {"from": 35, "to": 0},
         f"{r.status_code} late={j.get('late_minutes')} hist={len(j.get('history') or [])} changes={ch.get('late_minutes')}")

    r = ctx.c.patch(f"/attendance/{emp}/2026-05-01",
                    json={"punch_in": new_in, "reason": "same value", "regularized_by": "hr"})
    emit(res, "PATCH that changes nothing -> 422", "422", r.status_code == 422, r.status_code)

    r = ctx.c.patch(f"/attendance/{emp}/2026-05-01",
                    json={"punch_out": ms(2026, 5, 1, 18, 0), "reason": "forgot to punch out", "regularized_by": "hr"})
    j = r.json()
    emit(res, "PATCH history is append-only -> len=2, first entry untouched",
         "history len=2, history[0].reason='biometric glitch'",
         len(j["history"]) == 2 and j["history"][0]["reason"] == "biometric glitch",
         f"hist={len(j['history'])} first={j['history'][0].get('reason')}")

    leave_emp = ctx.employee()
    ctx.pin(leave_emp, ms(2026, 5, 2, 9, 28))
    r = ctx.c.patch(f"/attendance/{leave_emp}/2026-05-02",
                    json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    j = r.json()
    emit(res, "PATCH status=LEAVE -> clears punch times, work_hours null",
         "status=LEAVE punch_in=null punch_out=null work_hours=null",
         j["status"] == "LEAVE" and j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None,
         f"status={j.get('status')} in={j.get('punch_in')} wh={j.get('work_hours')}")

    bad_emp = ctx.employee()
    ctx.pin(bad_emp, ms(2026, 5, 3, 9, 28))
    r = ctx.c.patch(f"/attendance/{bad_emp}/2026-05-03",
                    json={"status": "ABSENT", "punch_in": ms(2026, 5, 3, 9, 28), "reason": "invalid", "regularized_by": "hr"})
    emit(res, "PATCH ABSENT together with punch_in -> 422", "422", r.status_code == 422, r.status_code)
    return res

# --------------------------------------------------------------------------- #
# section 6: analytics (aggregations)
# --------------------------------------------------------------------------- #
def section_analytics(ctx):
    res = []
    dept = f"Analytics-{random.randint(1000, 9999)}"

    # A: 2 present days in April (Mon 04-06, Tue 04-07), one late.
    a = ctx.employee(dept=dept)
    ctx.pin(a, ms(2026, 4, 6, 9, 45)); ctx.pout(a, ms(2026, 4, 6, 18, 30))   # 8.75h, late 15
    ctx.pin(a, ms(2026, 4, 7, 9, 30))                                        # open, counts present
    # B: no logs at all, still counts in headcount (joined 04-10, inside April).
    b = ctx.employee(dept=dept, joined="2026-04-10")
    # C: joins May, must NOT count in April headcount.
    c = ctx.employee(dept=dept, joined="2026-05-15")

    wd = sum(1 for d in range(1, 31) if date(2026, 4, d).weekday() < 5)
    pct = rhu(2 / wd * 100, 2)

    r = ctx.c.get("/analytics/employees/EMP000000/monthly", params={"month": "2026-04"})
    emit(res, "monthly unknown employee -> 404", "404", r.status_code == 404, r.status_code)

    j = ctx.c.get(f"/analytics/employees/{a}/monthly", params={"month": "2026-04"}).json()
    emit(res, "monthly for A -> present_days=2, late_count=1, working_days, attendance_pct",
         f"present_days=2 late_count=1 total_late=15 working_days={wd} attendance_pct={pct}",
         j["present_days"] == 2 and j["late_count"] == 1 and j["total_late_minutes"] == 15
         and j["working_days"] == wd and close(j["attendance_pct"], pct),
         f"present={j.get('present_days')} late={j.get('late_count')} wd={j.get('working_days')} pct={j.get('attendance_pct')}")

    items = ctx.c.get("/analytics/departments/summary", params={"month": "2026-04"}).json()["items"]
    mine = next((i for i in items if i["department"] == dept), None)
    emit(res, "department summary -> headcount counts zero-log people, excludes late joiners",
         "our dept: headcount=2, present_days=2, avg_work_hours=8.75, late_count=1",
         mine is not None and mine["headcount"] == 2 and mine["present_days"] == 2
         and close(mine["avg_work_hours"], 8.75) and mine["late_count"] == 1,
         mine)
    ordered = [i["department"] for i in items]
    emit(res, "department summary -> items sorted by department asc",
         "department list sorted ascending", ordered == sorted(ordered), ordered)

    # late leaderboard ties inside one department: two at 30, one at 10 -> 1,1,3
    ldept = f"Late-{random.randint(1000, 9999)}"
    l1 = ctx.employee(dept=ldept); l2 = ctx.employee(dept=ldept); l3 = ctx.employee(dept=ldept)
    ctx.pin(l1, ms(2026, 4, 6, 10, 0))   # late 30
    ctx.pin(l2, ms(2026, 4, 6, 10, 0))   # late 30
    ctx.pin(l3, ms(2026, 4, 6, 9, 40, 1))   # late 10
    j = ctx.c.get("/analytics/leaderboard/late",
                  params={"month": "2026-04", "department": ldept, "limit": 10}).json()["items"]
    ranks = [row["rank"] for row in j]
    emit(res, "leaderboard ties -> competition ranking 1,1,3 (not 1,2,3)",
         "ranks=[1,1,3]",
         ranks == [1, 1, 3] and j[0]["emp_code"] == sorted([l1, l2])[0],
         f"ranks={ranks} codes={[row['emp_code'] for row in j]}")

    j = ctx.c.get("/analytics/leaderboard/late",
                  params={"month": "2026-04", "department": ldept, "limit": 2}).json()["items"]
    emit(res, "leaderboard limit applied AFTER ranking -> 2 tied rows at rank 1",
         "len=2 rows, both rank 1", len(j) == 2 and all(row["rank"] == 1 for row in j),
         [row["rank"] for row in j])

    r = ctx.c.get("/analytics/departments/GhostDeptXYZ/trend",
                  params={"from": "2026-04-06", "to": "2026-04-09"})
    emit(res, "trend unknown department -> 404", "404", r.status_code == 404, r.status_code)

    r = ctx.c.get(f"/analytics/departments/{dept}/trend",
                  params={"from": "2026-01-01", "to": "2026-12-31"})
    emit(res, "trend range > 92 days -> 422", "422", r.status_code == 422, r.status_code)

    j = ctx.c.get(f"/analytics/departments/{dept}/trend",
                  params={"from": "2026-04-06", "to": "2026-04-09"}).json()["items"]
    dates = [i["date"] for i in j]
    emit(res, "trend -> exactly one row per calendar day (gap-filled)",
         "len=4 dates 04-06..04-09", len(j) == 4 and dates == ["2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09"],
         dates)
    empty_day = next((i for i in j if i["date"] == "2026-04-08"), {})
    emit(res, "trend working day with no records -> attendance_rate=0.0",
         "2026-04-08 is_working_day=true attendance_rate=0.0",
         empty_day.get("is_working_day") is True and close(empty_day.get("attendance_rate"), 0.0),
         empty_day)
    emit(res, "trend moving_avg_7d over preceding window (04-09 -> mean(1,0,0)*?)",
         "2026-04-09 moving_avg_7d=0.5",
         close(next((i for i in j if i["date"] == "2026-04-09"), {}).get("moving_avg_7d"), 0.5),
         next((i for i in j if i["date"] == "2026-04-09"), {}).get("moving_avg_7d"))
    return res

# --------------------------------------------------------------------------- #
# section 7: explain (indexes)
# --------------------------------------------------------------------------- #
def section_explain(ctx):
    res = []
    emp = ctx.employee()
    ctx.pin(emp, ms(2026, 4, 6, 9, 30))

    def explain(endpoint, **params):
        r = ctx.c.get(f"/admin/explain/{endpoint}", params=params)
        if r.status_code != 200:
            return r.status_code, ""
        j = r.json()
        return 200, str(j.get("explain", ""))

    targets = [
        ("attendance_list", {"page_size": 5}),
        ("employee_monthly", {"emp_code": emp, "month": "2026-04"}),
        ("department_summary", {"month": "2026-04"}),
        ("late_leaderboard", {"month": "2026-04"}),
        ("department_trend", {"department": "Engineering", "from": "2026-04-06", "to": "2026-04-08"}),
    ]
    for endpoint, params in targets:
        status, plan = explain(endpoint, **params)
        ok = status == 200 and "IXSCAN" in plan and "COLLSCAN" not in plan
        emit(res, f"explain {endpoint} -> IXSCAN present, no COLLSCAN",
             "200, plan has IXSCAN and no COLLSCAN", ok,
             f"{status} IXSCAN={'IXSCAN' in plan} COLLSCAN={'COLLSCAN' in plan}")

    r = ctx.c.get("/admin/explain/employee_monthly")
    emit(res, "explain employee_monthly missing required params -> 422", "422", r.status_code == 422, r.status_code)

    r = ctx.c.get("/admin/explain/not_an_endpoint")
    emit(res, "explain unknown endpoint -> 422", "422", r.status_code == 422, r.status_code)
    return res

# --------------------------------------------------------------------------- #
# menu / runner
# --------------------------------------------------------------------------- #
SECTIONS = OrderedDict([
    ("System (health, root, docs)", section_system),
    ("Employees (create, 409/422, filter, pagination)", section_employees),
    ("Punch in/out + rules R1-R5", section_rules),
    ("Listing, filters, pagination R10", section_listing),
    ("Regularize + audit trail", section_regularize),
    ("Analytics (aggregations)", section_analytics),
    ("Explain (index usage)", section_explain),
])


def run_section(ctx, title, fn):
    print(f"\n{CYAN}{BOLD}{'=' * 72}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{'=' * 72}{RESET}")
    try:
        return fn(ctx)
    except Exception as exc:  # a section should never crash the tour
        print(f"   {RED}[FAIL] section aborted: {exc}{RESET}")
        return [(title, "section completes", False, f"exception: {exc}")]


def summary(all_results):
    passed = sum(1 for _, _, ok, _ in all_results if ok)
    failed = [r for r in all_results if not r[2]]
    print(f"\n{BOLD}{'-' * 72}{RESET}")
    if not failed:
        print(f"{GREEN}{BOLD}  ALL {passed}/{len(all_results)} CHECKS PASSED - the API matches the contract.{RESET}")
    else:
        print(f"{RED}{BOLD}  {passed}/{len(all_results)} passed, {len(failed)} FAILED:{RESET}")
        for name, expected, _, actual in failed:
            print(f"   {RED}x{RESET} {name}")
            print(f"       expected: {expected}")
            print(f"       actual:   {actual}")
    print(f"{BOLD}{'-' * 72}{RESET}\n")


def main():
    ap = argparse.ArgumentParser(description="Interactive conformance tour for the attendance API.")
    ap.add_argument("--url", default=os.environ.get("API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--all", action="store_true", help="run every section once and exit")
    args = ap.parse_args()

    print(f"{BOLD}{CYAN}")
    print("  +----------------------------------------------------------------+")
    print("  |   Employee Attendance & Analytics API - interactive tour        |")
    print("  +----------------------------------------------------------------+")
    print(RESET)

    with httpx.Client(base_url=args.url, timeout=15.0) as client:
        try:
            h = client.get("/health")
        except httpx.HTTPError:
            print(f"{RED}Could not reach {args.url}.{RESET}")
            print("Start the API first, then re-run:")
            print(f"  {YELLOW}uvicorn app.main:app --port 8000{RESET}")
            raise SystemExit(1)
        if h.status_code != 200:
            print(f"{RED}Server answered /health with {h.status_code}. Is MongoDB running?{RESET}")
            raise SystemExit(1)
        print(f"{GREEN}Connected to {args.url}  (/health = 200){RESET}")

        ctx = Ctx(client)

        if args.all:
            results = []
            for title, fn in SECTIONS.items():
                results += run_section(ctx, title, fn)
            summary(results)
            return

        while True:
            print(f"\n{BOLD}Choose a test group:{RESET}")
            for i, title in enumerate(SECTIONS, start=1):
                print(f"   {YELLOW}{i}{RESET}  {title}")
            print(f"   {YELLOW}0{RESET}  Run ALL")
            print(f"   {YELLOW}q{RESET}  Quit")
            choice = input(f"{BOLD}tour> {RESET}").strip().lower()
            if choice in ("q", "quit", "exit"):
                print("Bye.")
                break
            if choice == "0":
                results = []
                for title, fn in SECTIONS.items():
                    results += run_section(ctx, title, fn)
                summary(results)
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(SECTIONS):
                title, fn = list(SECTIONS.items())[int(choice) - 1]
                results = run_section(ctx, title, fn)
                summary(results)
                continue
            print(f"{RED}Please enter 0, 1-{len(SECTIONS)}, or q.{RESET}")


if __name__ == "__main__":
    main()
