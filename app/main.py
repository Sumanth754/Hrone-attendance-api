"""
Employee Attendance & Analytics API
===================================

A small FastAPI + MongoDB service for HR attendance:

  * employees punch in and out (with the business rules R1-R5, R7),
  * HR can regularize a single day's record and every edit is kept in an
    append-only ``history`` audit trail,
  * a handful of analytics endpoints answer "who is late", "how is a
    department doing" and "what does the trend look like", all computed with
    MongoDB aggregation pipelines.

Run it with::

    uvicorn app.main:app --port 8000

``MONGO_URI`` and ``MONGO_DB`` are read from the environment. A local ``.env``
is loaded for convenience, but real environment variables win (python-dotenv
does not overwrite values that are already set).

I deliberately kept everything in this one file. The assignment asks for it,
the whole service is small, and splitting it into a package of one-function
modules would have been ceremony for its own sake. The sections below are the
"modules": config, helpers, schemas, attendance, corrections, analytics,
admin.

Things that bit me while writing this (left here because they are easy to
forget):

  * Instants are epoch *milliseconds* in the API but BSON datetimes in Mongo.
    PyMongo returns NAIVE datetimes unless you ask for ``tz_aware``, so the
    client below is created with ``tz_aware=True`` and I convert at the edges.
  * Calendar dates ("YYYY-MM-DD") and clock times ("HH:MM") stay strings
    everywhere. A date is not an instant; casting it to one silently invents a
    timezone.
  * The starter used Python's ``round()`` for work hours. That is banker's
    rounding, and the spec explicitly wants round-half-up, so ``round_half_up``
    below uses ``decimal``.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True is the important bit: it makes PyMongo return datetimes with
# tzinfo so I can convert them to epoch ms without guessing the server's zone.
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")


# --------------------------------------------------------------------------- #
# Small helpers (time, rounding, shift maths)
# --------------------------------------------------------------------------- #
def dt_from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> whole-second UTC datetime.

    Milliseconds are dropped on purpose: rule R1 says every instant is
    truncated to whole seconds before any calculation, and the truncated value
    is what we store and return.
    """
    return (EPOCH + timedelta(milliseconds=int(ms))).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch milliseconds. Missing/None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Should not happen with tz_aware=True, but if it does, assume UTC
        # rather than silently using the machine's local zone.
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int):
    """Round half-up (not banker's rounding) to ``places`` decimals.

    I use Decimal because ``round(2.675, 2)`` gives 2.67 (float representation)
    and I want predictable behaviour per R8.
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quantum = Decimal(1).scaleb(-places)
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def parse_hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def is_overnight(emp: dict) -> bool:
    """A shift is overnight when shift_end <= shift_start (R1)."""
    return parse_hm(emp["shift_end"]) <= parse_hm(emp["shift_start"])


def ist_time_tuple(dt: datetime) -> tuple[int, int]:
    local = dt.astimezone(IST)
    return local.hour, local.minute


def attendance_date_for(emp: dict, punch_in: datetime) -> str:
    """The IST calendar date a punch-in belongs to (R1).

    For a normal shift that is the IST date of the instant. For an overnight
    shift, a punch-in whose IST clock time is earlier than ``shift_end``
    belongs to the shift that started the previous day.
    """
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and ist_time_tuple(punch_in) < parse_hm(emp["shift_end"]):
        day = day - timedelta(days=1)
    return day.isoformat()


def shift_start_dt(emp: dict, date_str: str) -> datetime:
    d = date.fromisoformat(date_str)
    h, m = parse_hm(emp["shift_start"])
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(emp: dict, date_str: str) -> datetime:
    """Shift end for the record date. For overnight shifts this lands on the
    next calendar day."""
    d = date.fromisoformat(date_str)
    h, m = parse_hm(emp["shift_end"])
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    if is_overnight(emp):
        end = end + timedelta(days=1)
    return end


def compute_late_minutes(emp: dict, punch_in: datetime, date_str: str) -> int:
    """R2: late only when strictly more than the 10-minute grace, and then the
    count is whole minutes since shift_start (not since the grace ended)."""
    start = shift_start_dt(emp, date_str)
    seconds = int((punch_in - start).total_seconds())
    # Compare the *elapsed time* to the grace (10 min = 600s), then report
    # whole floored minutes since shift_start. Comparing floored minutes > 10
    # would wrongly let 09:40:01 (10 min 1s) pass as "not late".
    if seconds <= 600:
        return 0
    return seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime_minutes(emp: dict, punch_out: datetime, date_str: str) -> int:
    """R3: whole minutes between shift_end and punch_out, only if >= 30."""
    end = shift_end_dt(emp, date_str)
    seconds = int((punch_out - end).total_seconds())
    if seconds <= 0:
        return 0
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, half day when the rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def derive_fields(emp: dict, status: str, punch_in: Optional[datetime],
                  punch_out: Optional[datetime], date_str: str) -> dict:
    """Recompute the derived fields from the final punch times (R2-R5).

    Used by punch-out and, crucially, after every correction so a regularized
    record is consistent again.
    """
    if status in PRESENCE_STATUSES and punch_in is not None:
        late = compute_late_minutes(emp, punch_in, date_str)
        if punch_out is not None:
            hours = compute_work_hours(punch_in, punch_out)
            overtime = compute_overtime_minutes(emp, punch_out, date_str)
            half_day = compute_half_day(hours)
        else:
            hours, overtime, half_day = None, 0, False
    else:
        # ABSENT / LEAVE keep the derived fields at their defaults.
        late, hours, overtime, half_day = 0, None, 0, False
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }


def month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> ('YYYY-MM-01', last day of month)."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(joined_on: str, m_start: str, m_end: str) -> int:
    """R7: Mon-Fri days in the month, but only from the joining date.

    This is pure calendar maths (no attendance data), so it lives in Python
    rather than being force-fed into a pipeline.
    """
    start = max(date.fromisoformat(joined_on), date.fromisoformat(m_start))
    end = date.fromisoformat(m_end)
    if start > end:
        return 0
    days = 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            days += 1
        d += timedelta(days=1)
    return days


def is_working_day(date_str: str) -> bool:
    return date.fromisoformat(date_str).weekday() < 5


# --------------------------------------------------------------------------- #
# Serialization: Mongo documents -> JSON-safe API shape
# --------------------------------------------------------------------------- #
def _jsonify(obj: Any) -> Any:
    """Recursively turn a Mongo document into API-safe JSON.

    Datetimes become epoch ms, ObjectIds are dropped (``_id`` never leaves the
    API). This is used for the nested ``history[].changes`` too, where the
    from/to values of punch times are datetimes sitting inside a dict.
    """
    if isinstance(obj, datetime):
        return dt_to_ms(obj)
    if isinstance(obj, list):
        return [_jsonify(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items() if k != "_id"}
    return obj


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": _jsonify(entry.get("changes", {})),
    }


def serialize_attendance(doc: dict) -> dict:
    """Mongo record -> API record, filling in the fields legacy rows omit
    (''treat missing as an empty array / false'' per DATA_MODEL.md)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Schemas (Pydantic) - these are the API contract
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
MonthStr = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$", examples=["EMP0001"])]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: date

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    # Only these five fields can ever be sent; anything else (including the
    # derived fields) is ignored, because Pydantic drops unknown keys.
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Indexes - created at startup, idempotently
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees.emp_code is the customer's unique business key. A unique index
    # is what makes two concurrent identical creates end with one 201 and a
    # 409, instead of a check-then-insert race.
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # Serves the department summary / trend filters and headcount lookups.
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # One record per (emp_code, date): the same unique index both prevents a
    # duplicate punch-in and serves the monthly analytics range match.
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # List endpoint sorts by date desc then emp_code asc.
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # Status / date filters.
    attendance.create_index([("status", ASCENDING), ("date", ASCENDING)], name="status_date")
    # Late leaderboard: date range + late_minutes > 0.
    attendance.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="date_late")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create indexes before we start serving. I swallow failures so a slow or
    # not-yet-ready Mongo does not crash the process on boot - /health will
    # report it instead.
    try:
        ensure_indexes()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[startup] index creation failed: {exc}")
    yield
    # No client.close() on shutdown: the MongoClient is a process-wide singleton
    # and closing it here would break anything that reuses the app object (e.g.
    # the test suite spins the app up once per test). The process closing is
    # enough.


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description=(
        "Small HR attendance backend: punch in/out with business rules, "
        "manual corrections with an audit trail, and analytics built on "
        "MongoDB aggregation pipelines. Employees are identified by the "
        "customer's `emp_code`, never by `_id`; instants are epoch "
        "milliseconds."
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "system", "description": "Health / readiness."},
        {"name": "employees", "description": "Create and list employees."},
        {"name": "attendance", "description": "Punch in/out and corrections."},
        {"name": "analytics", "description": "Aggregation-based reporting."},
        {"name": "admin", "description": "Diagnostics."},
    ],
)


# --------------------------------------------------------------------------- #
# system
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    """Convenience redirect so a browser hitting `/` lands on the API docs
    instead of a bare 404. Not part of the contract; purely cosmetic."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"], summary="Readiness probe")
def health():
    """200 only when MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="mongodb unavailable")
    return {"status": "ok"}


