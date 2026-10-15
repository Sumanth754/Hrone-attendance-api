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
# Query + pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status
    return q


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    m_start, m_end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {
            "$group": {
                "_id": None,
                # present_days: presence status AND Mon-Fri only; half day = 0.5.
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$toDate": "$date"}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department=None) -> list:
    m_start, m_end = month_bounds(month)
    # Start from employees so people with zero logs still count (R9): we match
    # on joined_on <= end of month first, then look up their logs.
    emp_match: dict = {"joined_on": {"$lte": m_end}}
    if department:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": m_start, "$lte": m_end}}}],
                "as": "logs",
            }
        },
        # preserveNullAndEmptyArrays keeps employees who have no logs at all.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$toDate": "$logs.date"}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                # avg_work_hours is over records (not averages of averages), so
                # accumulate the sum and the count separately.
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$addFields": {"headcount": {"$size": "$codes"}}},
        {"$match": {"headcount": {"$gt": 0}}},  # omit departments with nobody this month
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": ["$hours_sum", "$hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department=None) -> list:
    m_start, m_end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # inner join: a log whose employee does not exist is simply dropped.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is standard competition ranking (1, 2, 2, 4) - exactly what the
        # spec asks for. Using the array position would be wrong.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]
    return pipeline


def department_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    from_idx = (date.fromisoformat(from_str) - date(1970, 1, 1)).days
    to_idx = (date.fromisoformat(to_str) - date(1970, 1, 1)).days
    return [
        # Start from the department's employees, then pull only *their* logs.
        # Joining every log in the date range and filtering afterwards was ~20x
        # slower on 100k logs. Employees with no logs in range simply contribute
        # nothing here; $densify still emits the full date range below.
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "emp_code": 1}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {"$replaceRoot": {"newRoot": "$logs"}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        # $densify needs a date or a number, not a "YYYY-MM-DD" string, so I
        # carry a day index alongside and densify on that.
        {"$addFields": {"idx": {"$divide": [{"$toLong": {"$toDate": "$date"}}, 86400000]}}},
        # NB: $densify treats the upper bound as exclusive, so +1 to include `to`.
        {"$densify": {"field": "idx", "range": {"step": 1, "bounds": [from_idx, to_idx + 1]}}},
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "date": {"$toDate": {"$multiply": ["$idx", 86400000]}},
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": {"$toDate": {"$multiply": ["$idx", 86400000]}}}, 5]
                }
            }
        },
        # Headcount for the day = department employees who had joined by then.
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # Window: current row + up to 6 preceding rows in the range. $avg skips
        # nulls, so a window with no rate gives null.
        {
            "$setWindowFields": {
                "sortBy": {"idx": ASCENDING},
                "output": {"ma_raw": {"$avg": "$rate_raw", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"idx": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": "$rate_raw",
                "moving_avg_7d": "$ma_raw",
            }
        },
    ]


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


# --------------------------------------------------------------------------- #
# employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"], summary="Create an employee")
def create_employee(body: EmployeeCreate):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": now_utc(),
    }
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        # The unique index decides the winner; no check-then-insert race.
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"], summary="List employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    # (page - 1) - the starter skipped a full page on page 1.
    skip = (page - 1) * page_size
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"], summary="Record a punch-in")
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    punch_date = attendance_date_for(emp, punched_at)

    record = {
        "emp_code": body.emp_code,
        "date": punch_date,
        "status": body.status.value,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(emp, punched_at, punch_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(record)


@app.post("/attendance/punch-out", tags=["attendance"], summary="Record a punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent open record punched in at or before this instant. Sorting by
    # punch_in desc also makes overnight shifts work: the shift that started
    # yesterday evening is the latest punch_in.
    record = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # Nothing at or before this instant. If the employee has any punch-in
        # at all, it must be *after* punched_at, which is the "punch out before
        # punching in" case (422). Otherwise there is genuinely no punch-in.
        later = attendance.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if later is not None:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_at = record["punch_in"]
    if punch_in_at.tzinfo is None:
        punch_in_at = punch_in_at.replace(tzinfo=UTC)

    if record["punch_out"] is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    if punched_at <= punch_in_at:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (punched_at - punch_in_at) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at is more than 24 hours after punch_in")

    derived = derive_fields(emp, record["status"], punch_in_at, punched_at, record["date"])
    # Conditional update on punch_out is None: if a parallel punch-out already
    # closed the record, this matches nothing and we return 409.
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record already punched out")
    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"], summary="List attendance records")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = attendance_filter(
        emp_code=emp_code,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        status=status.value if status else None,
    )
    total = attendance.count_documents(q)
    skip = (page - 1) * page_size
    # Sort in the database (there is an index for it), not in Python.
    cursor = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    return {
        "items": [serialize_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch(
    "/attendance/{emp_code}/{date}",
    tags=["attendance"],
    summary="Regularize (manually correct) one day's record",
)
def regularize(emp_code: str, date: date, body: RegularizeRequest):
    # `date` is a real calendar date per the contract (path {date}); FastAPI
    # validates the format and returns 422 on garbage. Keep the "YYYY-MM-DD"
    # string form because that is how the record's `date` field is stored.
    date_str = date.isoformat()
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    record = attendance.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail="no record for that date")

    provided = body.model_fields_set

    # Merge supplied values over the stored ones.
    new_status = body.status.value if "status" in provided else record["status"]
    new_punch_in = dt_from_ms(body.punch_in) if ("punch_in" in provided and body.punch_in is not None) else record.get("punch_in")
    new_punch_out = dt_from_ms(body.punch_out) if ("punch_out" in provided and body.punch_out is not None) else record.get("punch_out")

    # Normalise timezone (tz_aware should already have done this).
    if new_punch_in is not None and new_punch_in.tzinfo is None:
        new_punch_in = new_punch_in.replace(tzinfo=UTC)
    if new_punch_out is not None and new_punch_out.tzinfo is None:
        new_punch_out = new_punch_out.replace(tzinfo=UTC)

    # --- rules --------------------------------------------------------- #
    if new_status in ("ABSENT", "LEAVE"):
        # Setting a non-presence status means the day has no punch times.
        if ("punch_in" in provided and body.punch_in is not None) or (
            "punch_out" in provided and body.punch_out is not None
        ):
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot carry punch times")
        new_punch_in, new_punch_out = None, None
    else:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires a punch_in")

    if new_punch_in is not None and attendance_date_for(emp, new_punch_in) != date_str:
        raise HTTPException(status_code=422, detail="punch_in must fall on the record's attendance date")

    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in) > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out is more than 24 hours after punch_in")

    derived = derive_fields(emp, new_status, new_punch_in, new_punch_out, date_str)

    # --- work out what actually changed --------------------------------- #
    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, **derived}

    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old[field] != new[field]:
            changes[field] = {"from": old[field], "to": new[field]}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if history is still exactly what we
    # read. Two simultaneous corrections cannot then silently lose each other -
    # the loser matches nothing and gets a 409.
    if "history" in record:
        history_filter: dict = {"history": record["history"]}
    else:
        history_filter = {"history": {"$exists": False}}

    updated = attendance.find_one_and_update(
        {"_id": record["_id"], **history_filter},
        {"$set": new, "$push": {"history": entry}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    tags=["analytics"],
    summary="Monthly attendance summary for one employee",
)
def employee_monthly(emp_code: str, month: MonthStr = Query(...)):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="unknown employee")

    m_start, m_end = month_bounds(month)
    rows = list(attendance.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}
    present_days = row.get("present_days", 0) or 0
    working_days = count_working_days(emp["joined_on"], m_start, m_end)
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0) or 0,
        "late_count": row.get("late_count", 0) or 0,
        "total_late_minutes": row.get("total_late_minutes", 0) or 0,
        "total_overtime_minutes": row.get("total_overtime_minutes", 0) or 0,
        "attendance_pct": pct,
    }


@app.get(
    "/analytics/departments/summary",
    tags=["analytics"],
    summary="Department summary for a month",
)
def department_summary(month: MonthStr = Query(...), department: Optional[str] = None):
    # Starts from employees (so zero-log people count), then looks up logs.
    rows = list(employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for r in rows:
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": r["present_days"],
                "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    tags=["analytics"],
    summary="Late-comers leaderboard for a month",
)
def late_leaderboard(
    month: MonthStr = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(attendance.aggregate(late_leaderboard_pipeline(month, limit, department)))
    items = [
        {
            "rank": r["rank"],
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    tags=["analytics"],
    summary="Daily attendance trend for a department",
)
def department_trend(
    department: str = Path(...),
    from_: date = Query(..., alias="from"),
    to_: date = Query(..., alias="to"),
):
    # Unknown department (nobody with that department) is a 404.
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    if to_ < from_:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
    if (to_ - from_).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")

    from_str, to_str = from_.isoformat(), to_.isoformat()
    # The trend pipeline starts from the department's employees (see
    # department_trend_pipeline), so it runs against the employees collection.
    rows = list(employees.aggregate(department_trend_pipeline(department, from_str, to_str)))
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            # Stored as full precision by the pipeline; rounded here half-up (R8).
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# admin: explain
# --------------------------------------------------------------------------- #
EXPLAINABLE = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


def _explain_command(endpoint: str, params: dict) -> tuple[str, str, dict]:
    """Return (collection, explain_command_body) for the endpoint's main query,
    built exactly the way the real endpoint builds it."""
    if endpoint == "attendance_list":
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        q = attendance_filter(
            emp_code=params.get("emp_code"),
            date_from=date_from,
            date_to=date_to,
            status=params.get("status"),
        )
        skip = (params.get("page", 1) - 1) * params.get("page_size", 20)
        body = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": skip,
            "limit": params.get("page_size", 20),
        }
        return "attendance_logs", body

    if endpoint == "employee_monthly":
        pipeline = employee_monthly_pipeline(params["emp_code"], params["month"])
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

    if endpoint == "department_summary":
        pipeline = department_summary_pipeline(params["month"], params.get("department"))
        return "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

    if endpoint == "late_leaderboard":
        pipeline = late_leaderboard_pipeline(params["month"], params.get("limit", 10), params.get("department"))
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

    if endpoint == "department_trend":
        pipeline = department_trend_pipeline(params["department"], params["from"], params["to"])
        return "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

    raise HTTPException(status_code=422, detail=f"cannot explain '{endpoint}'")


@app.get(
    "/admin/explain/{endpoint}",
    tags=["admin"],
    summary="MongoDB explain for an endpoint's main query",
)
def explain_endpoint(
    endpoint: str = Path(..., description="One of: " + ", ".join(sorted(EXPLAINABLE))),
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to_: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    # Same required-parameter rules as the real endpoints.
    if endpoint == "employee_monthly" and (not emp_code or not month):
        raise HTTPException(status_code=422, detail="emp_code and month are required")
    if endpoint in ("department_summary", "late_leaderboard") and not month:
        raise HTTPException(status_code=422, detail="month is required")
    if endpoint == "department_trend" and (not department or not from_ or not to_):
        raise HTTPException(status_code=422, detail="department, from and to are required")

    params = {
        "emp_code": emp_code,
        "month": month,
        "department": department,
        "limit": limit,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "status": status.value if status else None,
        "from": from_.isoformat() if from_ else None,
        "to": to_.isoformat() if to_ else None,
        "page": page,
        "page_size": page_size,
    }
    collection, body = _explain_command(endpoint, params)
    explain = db.command({"explain": body, "verbosity": "executionStats"})
    # Raw explain can contain datetimes; json_util makes it JSON-safe.
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(explain))}
