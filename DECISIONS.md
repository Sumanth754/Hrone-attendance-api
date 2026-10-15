# DECISIONS.md

Notes on the choices I made, in the order they came up. For each one: **why**,
the **trade-off**, and **what I'd do differently** with more time.

## 1. MongoDB + pymongo (not an in-memory store)

**Why.** The kit's data model and sample data are Mongo documents, and the
grading explicitly looks at aggregation-pipeline usage. Storing in memory would
have made the schema questions moot but would miss the point of the task.
**Trade-off.** A running MongoDB is now a hard dependency; there is no fallback.
**What I'd do differently.** Nothing structural — this was forced by the kit. I
would add a health check that distinguishes "Mongo down" (503) from "empty DB" so
a reviewer can tell the two apart, which `/health` already does.

I picked **pymongo over motor** because the starter used pymongo and the app is
request/response CRUD where the driver being sync is fine. Async would only pay
off under high concurrency, which this service does not have.

## 2. Everything in `app/main.py` (not split into a package)

**Why.** The kit ships a single-file starter, and the brief says to build on it
rather than replace it. At ~1k lines one file with clearly labelled sections is
easier to follow than a six-package tree where every function is a one-line
import away from where it is used.
**Trade-off.** The file is long and the module-level Mongo handles make it
slightly harder to unit-test in isolation (the tests use a real test database
instead).
**What I'd do differently.** Past a couple of thousand lines I would split out
`analytics.py` and `attendance_rules.py`, which are the two parts most likely to
grow.

## 3. Aggregation pipelines, not Python loops

**Why.** The brief is explicit that the analytics must run in the database. More
practically, computing over "fetch everything then reduce in Python" loads the
whole collection into memory and can't use indexes.
**Trade-off.** Pipelines are harder to read and debug than a loop; I leaned on
`/admin/explain` to check the plans stay on indexes.
**What I'd do differently.** I would add a debug route that returns the pipeline
that was built (I return the explain already, which is close enough).

## 4. Corrections are append-only; the record is a projection

**Why.** A correction has to be auditable, so I never overwrite history. Each
PATCH recomputes the derived fields from the corrected punches and pushes a
single `history` entry `{at, by, reason, changes:{field:{from,to}}}`. The current
values are a projection of the latest state; the trail explains how they got
there.
**Trade-off.** The document grows by one entry per edit, and every reader has to
remember the top-level fields are "current", not "original".
**What I'd do differently.** For a busier system I would move `history` into its
own collection with the same append-only discipline, so the hot record stays
small.

Two smaller guardrails I added here: a PATCH that changes nothing is a 422 (not a
silent no-op), and the update is `find_one_and_update` filtered on the exact
`history` I read, so two concurrent corrections can't silently overwrite each
other (the loser gets 409).

## 5. Timezone: convert at the edges, compute in IST

**Why.** Instants arrive as epoch milliseconds (UTC). The *working day* and all
the shift maths only make sense in IST (UTC+05:30), so I convert to an aware
datetime once and compute there. `tz_aware=True` on the client means PyMongo
hands back aware datetimes rather than guessing the server's zone.
**Trade-off.** IST is hard-coded; a company with mixed timezones would need a
per-employee zone.
**What I'd do differently.** Store each employee's IANA timezone and use it
instead of a module constant.

## 6. Attendance date (R1) and the overnight rule

**Why.** A punch belongs to the day its shift started. For an overnight shift
(`shift_end <= shift_start`) a punch before the shift end belongs to the previous
day — that is what stops a 01:00 scan being filed under the wrong date.
**Trade-off.** It assumes a single fixed shift per employee.
**What I'd do differently.** Support a shift calendar per employee.

## 7. Business rules → HTTP status codes

**Why.** The contract is the source of truth, so I map each rule to the status it
names: unknown employee → 404, duplicate punch-in / already-closed → 409,
payload shape problems and semantic violations (punch-out before punch-in, range
> 92 days, no-op correction) → 422, dead Mongo → 503. Using unique indexes for the
"already exists" cases means the status comes from the database, not a racy check.
**Trade-off.** A few different causes share 422, so the `detail` string carries
the specifics.
**What I'd do differently.** A typed error `code` field in the body would make
the 422s machine-distinguishable.

## 8. Indexes

**Why.** Every `$match`/sort should hit an index. `emp_code` (unique) and
`(emp_code, date)` (unique) back lookups and stop duplicates; `(date, emp_code)`
and `(status, date)` serve listings; `(department, joined_on)` serves the
summary/headcount/trend; `(date, late_minutes)` serves the leaderboard.
**Trade-off.** More indexes mean slower writes; acceptable for a read-heavy
reporting API.
**What I'd do differently.** Add a `(department, date)` index **on the logs** if
departments ever move off the employee — but today `department` lives on
`employees`, so filtering logs by it needs a join, not a log index.

## 9. Ranking with ties

**Why.** Ties must share a rank (1, 2, 2, 4). `$setWindowFields` with `$rank`
gives exactly that; array position would not.
**Trade-off.** At the `limit` cutoff this can return a few more rows than
`limit`, because I prefer not to drop a tied employee.
**What I'd do differently.** Nothing — but I document it so the extra rows aren't
mistaken for a bug.

## 10. Headcount counts zero-log people (R9)

**Why.** The summary and trend start from `employees` and left-join logs, so
someone with no logs that month still counts in headcount (with present 0), and
mid-month joiners are excluded from months before they joined.
**Trade-off.** The pipelines are join-heavy, so the employees index has to drive.
**What I'd do differently.** Precompute monthly rollups (see §11).

## 11. If the data were 100x larger

I would precompute a daily rollup document per employee/department on write, so
reports read a small summary instead of re-scanning every log. I did not build it
here because it is a second write path to keep correct, and the brief values
correctness over premature scale.

---

## The analytics endpoints, and how each proves a pipeline

Every analytics endpoint runs a real aggregation pipeline (no "fetch all, then
loop in Python"). The `$match` stages are index-backed, which I verify through
`/admin/explain/{endpoint}` (plans show `IXSCAN`, never a collection scan).

| Endpoint | Pipeline stages (in order) | Index used by `$match` |
|---|---|---|
| `/analytics/employees/{emp_code}/monthly` | `$match` → `$group` | `(emp_code, date)` |
| `/analytics/departments/summary` | `$match` → `$lookup` (sub-`$match` on logs) → `$unwind` (`preserveNullAndEmptyArrays`) → `$group` → `$addFields` (headcount) → `$match` → `$project` → `$sort` | `(department, joined_on)` |
| `/analytics/leaderboard/late` | `$match` (late>0) → `$group` → `$lookup` (employees) → `$unwind` → `$match` (dept) → `$setWindowFields` (`$rank`) → `$match` (rank ≤ limit) → `$project` → `$sort` | `(date, late_minutes)` |
| `/analytics/departments/{department}/trend` | `$match` → `$project` → `$lookup` (logs in range) → `$unwind` → `$replaceRoot` → `$group` (by date) → `$addFields` (day index) → `$densify` (fill missing days) → `$addFields` → `$lookup` (employees) → `$addFields` (headcount, rate) → `$setWindowFields` (7-day moving average) → `$sort` → `$project` | `(department, joined_on)` |
| `/admin/explain/{endpoint}` | not a pipeline of its own — it runs the endpoint's real `find(...).explain()` or `aggregate(..., explain=True)` and returns the raw plan | — |

`$densify` and `$setWindowFields` are why the README asks for MongoDB 5.0+ (I
developed on 8.0).

## Numbers I hand-checked (kit sample data)

After loading the kit's sample documents (6 employees, 9 attendance logs) I
compared the API to manual maths (sample values, not the grader's hidden data):

* **Engineering, 2026-07:** present_days **3**, headcount **2** (EMP0002 joined
  07-13), avg_work_hours `(9.12 + 9.03 + 8.98) / 3 = 9.04`.
* **Sales, 2026-07:** present_days **1.5** (EMP0004's WFH half-day on 07-07 counts
  0.5), late_count **1 / 35 minutes** (EMP0003 punched in 10:05),
  avg_work_hours `(9.08 + 3.5) / 2 = 6.29`.
* **Late leaderboard:** EMP0003 is rank 1 with 35 minutes.
* **EMP0001 monthly:** 2 present / 23 working days = **8.70%**.
* **Sales trend on 07-07:** present_count 0.5 / headcount 2 = **0.25** attendance_rate.

All matched the API output — which is also where I caught the late-grace bug
(09:40:01 must be 10 minutes late, not 0).
