"""
Local dev seed - NOT required to run the app.

The grader seeds its own (much larger) dataset. This script exists so I could
build my own awkward data while developing: mid-month joiners, overnight
shifts, weekend records, half days, absences, and - with --logs - a big enough
collection to actually test the explain plans.

Usage::

    python scripts/seed.py --drop
    python scripts/seed.py --employees 2000 --months 3 --logs 100000 --drop

It reads MONGO_URI / MONGO_DB the same way the app does.

To load the kit's real sample data instead, point it at the folder that holds
``employees.json`` and ``attendance_logs.json`` (that folder stays OUTSIDE the
repo, per the brief)::

    python scripts/seed.py --sample C:\\path\\to\\candidate_kit\\sample_data
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

# make "app" importable when run as `python scripts/seed.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

from app.main import (  # noqa: E402  (import after path fix)
    IST,
    UTC,
    PRESENCE_STATUSES,
    attendance_date_for,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    ensure_indexes,
)

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Marketing"]
FIRST = ["Asha", "Vikram", "Neha", "Rohit", "Meera", "Karan", "Isha", "Dev", "Priya", "Arjun"]
LAST = ["Rao", "Shah", "Iyer", "Das", "Nair", "Bedi", "Kapoor", "Menon", "Gupta", "Singh"]


def make_employees(n: int) -> list[dict]:
    rng = random.Random(7)
    out = []
    for i in range(1, n + 1):
        code = f"EMP{i:04d}"
        # A chunk join mid-month so R7/R9 have something to chew on.
        if i % 7 == 0:
            joined = "2026-07-14"
        elif i % 11 == 0:
            joined = "2026-08-20"  # joins AFTER July -> not in July headcount
        else:
            joined = f"2026-0{rng.randint(1, 6)}-{rng.randint(1, 28):02d}"
        overnight = i % 13 == 0
        out.append({
            "emp_code": code,
            "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "email": f"emp{i:04d}@example.com",
            "department": rng.choice(DEPARTMENTS),
            "shift_start": "22:00" if overnight else "09:30",
            "shift_end": "06:00" if overnight else "18:30",
            "joined_on": joined,
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC),
        })
    return out


def make_log(emp: dict, day) -> dict:
    """Build one attendance document with the derived fields computed by the
    same helpers the API uses (so seeded rows stay consistent with R2-R5)."""
    rng = random.Random(f"{emp['emp_code']}:{day}")

    roll = rng.random()
    if roll < 0.05:
        return _no_punch(emp, day, "ABSENT")
    if roll < 0.10:
        return _no_punch(emp, day, "LEAVE")

    status = rng.choices(PRESENCE_STATUSES, weights=[8, 1, 1])[0]
    h, m = (int(x) for x in emp["shift_start"].split(":"))
    # minutes relative to shift start: mostly on time, sometimes late
    offset = rng.choice([-5, -2, 0, 0, 1, 3, 8, 10, 25, 45, 61])
    punch_in_local = datetime(day.year, day.month, day.day, h, m, tzinfo=IST) + timedelta(minutes=offset)
    # work between ~3h and ~10h
    worked = rng.choice([180, 240, 260, 300, 480, 500, 540, 600])
    punch_out_local = punch_in_local + timedelta(minutes=worked)

    punch_in = punch_in_local.astimezone(UTC).replace(microsecond=0)
    punch_out = punch_out_local.astimezone(UTC).replace(microsecond=0)
    att_date = attendance_date_for(emp, punch_in)
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "emp_code": emp["emp_code"],
        "date": att_date,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(emp, punch_in, att_date),
        "overtime_minutes": compute_overtime_minutes(emp, punch_out, att_date),
        "half_day": compute_half_day(work_hours),
        "history": [],
    }


def _no_punch(emp: dict, day, status: str) -> dict:
    return {
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed a local attendance database.")
    ap.add_argument("--employees", type=int, default=30)
    ap.add_argument("--months", type=int, default=1, help="number of months of attendance to generate")
    ap.add_argument("--start", default="2026-07-01", help="first day of attendance, YYYY-MM-DD")
    ap.add_argument("--logs", type=int, default=0, help="stop after roughly this many attendance docs (0 = no cap)")
    ap.add_argument("--drop", action="store_true", help="drop both collections first")
    ap.add_argument("--sample", metavar="DIR", help="load the kit's sample_data/*.json from DIR instead of generating")
    args = ap.parse_args()

    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "attendance_db")
    client = MongoClient(uri, tz_aware=True)
    db = client[db_name]

    if args.drop:
        db["employees"].drop()
        db["attendance_logs"].drop()
    ensure_indexes()

    # Load the kit's own sample documents (MongoDB extended JSON, so the $date
    # wrappers become real aware datetimes via json_util).
    if args.sample:
        opts = json_util.JSONOptions(tz_aware=True)
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((Path(args.sample) / f"{name}.json").read_text(), json_options=opts)
            db[name].delete_many({})
            if docs:
                db[name].insert_many(docs)
            print(f"loaded {len(docs)} sample documents into {db_name}.{name}")
        print("done.")
        return

    start_day = datetime.fromisoformat(args.start).date()
    employees = make_employees(args.employees)
    db["employees"].delete_many({})
    db["employees"].insert_many(employees)
    print(f"seeded {len(employees)} employees into {db_name}.employees")

    db["attendance_logs"].delete_many({})
    total = 0
    batch: list[dict] = []
    days = args.months * 31
    stop = False
    for emp in employees:
        if stop:
            break
        for offset in range(days):
            day = start_day + timedelta(days=offset)
            # weekends have fewer records (people do not always log them)
            if day.weekday() >= 5 and random.Random(f"{emp['emp_code']}:{day}:we").random() < 0.6:
                continue
            batch.append(make_log(emp, day))
            total += 1
            if args.logs and total >= args.logs:
                stop = True
                break
            if len(batch) >= 2000:
                db["attendance_logs"].insert_many(batch, ordered=False)
                batch = []
    if batch:
        db["attendance_logs"].insert_many(batch, ordered=False)
    print(f"seeded ~{db['attendance_logs'].estimated_document_count()} attendance_logs into {db_name}")
    print("done.")


if __name__ == "__main__":
    main()
