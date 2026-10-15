# Employee Attendance & Analytics API

A small FastAPI + MongoDB service for HR attendance. Employees punch in and out
(their first and last scan of the day), HR can regularize a day and every edit is
kept in an append-only audit trail, and a handful of analytics endpoints answer
"who is late", "how is a department doing" and "what does the trend look like".
All reporting is done with MongoDB aggregation pipelines.

## Requirements

* Python 3.11+ (developed on 3.12).
* MongoDB 5.0+ — the trend query uses `$densify` and `$setWindowFields`
  (developed and tested on MongoDB 8.0).

## Setup

```
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

## Configuration

The app reads two environment variables:

| Variable    | Default                     | Meaning               |
|-------------|-----------------------------|-----------------------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection    |
| `MONGO_DB`  | `attendance_db`             | Database name         |

A local `.env` is loaded for convenience (see `.env.example`), but real
environment variables win — python-dotenv does not overwrite values that are
already set. The `.env` file is git-ignored and is never required.

## Run

```
uvicorn app.main:app --port 8000
```

Interactive docs are at `http://localhost:8000/docs`.

## Try it with curl

Create an employee, then read that employee's monthly summary (run each line as
one command; on Windows use `curl.exe`, since PowerShell aliases `curl` to
`Invoke-WebRequest`):

```
curl -X POST http://localhost:8000/employees -H "Content-Type: application/json" -d "{\"emp_code\":\"EMP0001\",\"name\":\"Asha Rao\",\"email\":\"asha@example.com\",\"department\":\"Engineering\",\"joined_on\":\"2026-01-05\"}"

curl "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"
```

## Seed some data (optional)

`scripts/seed.py` generates synthetic employees and attendance logs so you can
try the endpoints and the `explain` diagnostic at scale:

```
python scripts/seed.py --employees 500 --months 3 --logs 50000 --drop
```

It honours `MONGO_URI` / `MONGO_DB` just like the app.

To load the assignment kit's real sample documents instead (a folder holding
`employees.json` and `attendance_logs.json`, kept outside this repo):

```
python scripts/seed.py --sample C:\path\to\candidate_kit\sample_data --drop
```

## Tests

```
pytest -q
```

The tests talk to a local MongoDB and use a separate database
(`attendance_test`), which they drop before each test. They cover the business
rules and their boundaries, overnight shifts, the audit trail, the
duplicate-punch guard, the leaderboard tie handling and pagination.

## Interactive API tour (optional)

`scripts/api_tour.py` is a menu-driven smoke test that walks the whole contract
over HTTP against a running server and prints the expected result next to the
real one, with a pass/fail summary at the end. Handy for a manual demo:

```
# terminal 1
$env:MONGO_DB = "attendance_tour"      # use a scratch DB
uvicorn app.main:app --port 8000

# terminal 2
python scripts/api_tour.py             # interactive menu
python scripts/api_tour.py --all       # or run everything once
```

It only calls the public API, so it works exactly like the grader does.

## Endpoints

| Method | Path                                          | Purpose                                   |
|--------|-----------------------------------------------|-------------------------------------------|
| GET    | `/health`                                     | Readiness; pings MongoDB (503 if down).   |
| POST   | `/employees`                                  | Create an employee.                       |
| GET    | `/employees`                                  | List employees (filter by `department`).  |
| POST   | `/attendance/punch-in`                        | Record a punch-in.                        |
| POST   | `/attendance/punch-out`                       | Close the open punch-in.                  |
| GET    | `/attendance`                                 | List records (emp/date/status filters).   |
| PATCH  | `/attendance/{emp_code}/{date}`               | Regularize a day (audited).               |
| GET    | `/analytics/employees/{emp_code}/monthly`     | One employee's month.                     |
| GET    | `/analytics/departments/summary`              | Per-department rollup for a month.        |
| GET    | `/analytics/leaderboard/late`                 | Top late employees for a month.           |
| GET    | `/analytics/departments/{department}/trend`   | Daily trend (rate + 7-day moving average).|
| GET    | `/admin/explain/{endpoint}`                   | MongoDB `explain` for an endpoint's query.|

`emp_code` is the customer's identifier and is used in every URL and body;
MongoDB's `_id` is never exposed. All timestamps in requests and responses are
**epoch milliseconds** (UTC); calendar dates (`"YYYY-MM-DD"`) and clock times
(`"HH:MM"`) are plain strings.

## Business rules, briefly

* **Timezone.** Instants are UTC epoch ms. The working day is computed in **IST
  (UTC+05:30)** and truncated to whole seconds.
* **Attendance date (R1).** A punch belongs to the day its shift started. For an
  overnight shift (`shift_end <= shift_start`, e.g. 22:00–06:00) a punch before
  the shift end belongs to the previous day — that is what stops a 01:00 scan
  from being filed under the wrong date.
* **Late (R2).** More than 10 minutes after `shift_start`, counted in whole
  minutes from `shift_start`. The 10-minute grace is compared against the actual
  elapsed time, so 09:40:01 is late by 10, while 09:40:00 is not late at all.
* **Overtime (R3).** Whole minutes after `shift_end`, and only once it reaches 30
  minutes.
* **Work hours (R4).** `(punch_out - punch_in)` in hours, rounded half-up to 2dp.
* **Half day (R5).** Fewer than 4.5 hours worked.
* **Working days (R7).** Monday–Friday in the month, regardless of holidays.
* **Rounding (R8).** Half-up everywhere (numbers to 2dp, rates to 4dp) — never
  Python's banker's `round()`.

## Data model

Two collections (see the assignment's `DATA_MODEL.md`):

* `employees` — one document per employee (`emp_code` unique), with `shift_start`,
  `shift_end` and `joined_on`.
* `attendance_logs` — one document per employee per attendance date, with
  `punch_in` / `punch_out` datetimes, the derived `work_hours`, `late_minutes`,
  `overtime_minutes`, `half_day`, and an append-only `history` array.

Indexes created on startup (see `ensure_indexes()`):

| Collection        | Index                          | Serves                                        |
|-------------------|--------------------------------|-----------------------------------------------|
| `employees`       | `emp_code` (unique)            | lookups + duplicate guard                     |
| `employees`       | `(department, joined_on)`      | department filter, headcount, summary         |
| `employees`       | `(joined_on)`                  | joined-by date scans                          |
| `attendance_logs` | `(emp_code, date)` (unique)    | per-employee day lookups + duplicate guard    |
| `attendance_logs` | `(date, emp_code)`             | date-range listings, trend                    |
| `attendance_logs` | `(status, date)`               | status-filtered listings                      |
| `attendance_logs` | `(date, late_minutes)`         | leaderboard                                   |

## Project layout

```
app/main.py      the whole service (config, helpers, models, endpoints, pipelines)
scripts/seed.py  synthetic data generator
tests/           pytest suite
```

Everything lives in `app/main.py`. The assignment asks for it and the service is
small enough that splitting it into a package of one-function modules would be
ceremony rather than clarity; the file is divided into clearly labelled sections
instead.

## What's missing / scope notes

* **Auth.** No login or roles — the corrector in the audit trail is whatever the
  client sends, matching the kit's sample `history`.
* **Holidays.** Working days are Mon–Fri by the calendar; there is no company
  holiday / shift-calendar table.
* **One shift per employee.** The overnight rule (R1) assumes a single fixed
  shift per employee.
* **Leaderboard limit.** Ties mean it can return a few more rows than `limit`
  (see DECISIONS.md) — intentional.
* **No middleware.** No rate limiting, structured logging or request-ID
  correlation.
* **No pagination** on the analytics lists beyond the documented `limit`.
* **Seed data is synthetic** — realistic shapes (including half days, leaves and
  overnight shifts) but random values.
