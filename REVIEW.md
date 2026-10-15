# REVIEW.md

I reviewed the starter `app/main.py` from the kit. Line numbers below refer to it
as shipped. I ranked the issues by how badly they would bite in production, and
kept the long tail of smaller nits in an appendix at the end. The starter clearly
worked as a first draft; these are the things I would not have shipped as-is.

## Top 5 (ranked by severity)

### 1. HIGH — the 10-minute late grace is compared against *floored minutes* (R2 is wrong)

`compute_late_minutes` (L36-37) floors the elapsed time to whole minutes and then
does `minutes > 10`. That means anything from 09:40:01 to 09:40:59 floors to 10
and is treated as **not late**, when the spec says only 09:40:00 itself is inside
the grace. This is the one I found because a hand-computed number disagreed with
the API.

Before (starter):

```python
def compute_late_minutes(punch_in, shift_start):
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() // 60)
    if minutes > 10:                 # grace compared to floored minutes
        return minutes
    return 0
```

After (my fix):

```python
def compute_late_minutes(emp, punch_in, date_str):
    start = shift_start_dt(emp, date_str)            # correct day, IST-aware
    elapsed_seconds = (punch_in - start).total_seconds()
    if elapsed_seconds <= 10 * 60:                   # grace on real elapsed time
        return 0
    return int(elapsed_seconds // 60)                # then floor for the value
```

09:40:00 → 0, 09:40:01 → 10, 10:15:59 → 45.

### 2. HIGH — the shift start is built on the punch-in's own day, and everything is naive

`start` is built with `punch_in.replace(...)` (L35), so it lives on the punch-in's
own calendar day and can never represent a shift that began the previous evening.
Combined with naive datetimes, an overnight shift (e.g. 22:00-06:00) computes
late/overtime against the wrong day, and a 01:00 scan is filed under the wrong
attendance date. I compute shift start/end in IST for the *attendance date* and
roll the end into the next day when the shift is overnight.

### 3. HIGH — check-then-insert with no unique index (duplicate employees / punch-ins)

`create_employee` (L79-83) and `punch_in` (L104-105) both "find, then insert".
Two requests in flight both see "not found" and both insert, so duplicate
`emp_code`s and double punch-ins are possible. The fix is a unique index
(`emp_code`, and `(emp_code, date)`) plus catching `DuplicateKeyError` → 409, so
the database decides the winner instead of a race.

### 4. MEDIUM — `round()` is banker's rounding, not half-up (R8)

`compute_work_hours` (L41) uses Python's `round()`, so `round(2.125, 2)` gives
`2.12` but the spec wants `2.13`. I route all rounding through a
`Decimal(ROUND_HALF_UP)` helper so 2dp numbers and 4dp rates behave the same way.

### 5. MEDIUM — `list_attendance` sorts and paginates in Python, and the counts are off

`list_attendance` (L144-147) loads every matching document into memory and sorts
and slices in Python — O(n) memory and it ignores the indexes. The sibling
`list_employees` also has two arithmetic bugs: `skip = page * page_size` skips a
whole page on page 1 (L93), and `total` counts all employees regardless of the
`department` filter (L94). I moved sorting/paging into Mongo
(`find().sort().skip().limit()`) and reused the same filter for `total`.

## Smaller things I noticed (appendix)

* **L22-23** — `MongoClient` created without `tz_aware=True`, so PyMongo returns
  naive datetimes and every epoch-ms conversion is ambiguous.
* **L82-85** — `created_at` stored with naive `datetime.now()` and returned as a
  datetime instead of epoch ms.
* **L53-60** — `EmployeeIn` validates nothing: `shift_start="9am"` is accepted,
  and `shift_start == shift_end` is allowed (later breaks the late/overtime maths).
* **L101-113** — `punch_in` never checks the employee exists, so an unknown code
  raises `KeyError` → 500 instead of 404.
* **L102** — `fromtimestamp()` / `now()` produce machine-local naive times, and
  `punched_at` is not range-validated.
* **L114-115** — `overtime_minutes` hardcoded to 0, `half_day` hardcoded to
  `False`, `status` never validated, instant not truncated to seconds.
* **L145-149** — `list_attendance` sorts only by `date` (unstable ties) and
  returns datetimes instead of epoch ms.
* **L72-74** — `/health` returns `ok` without pinging Mongo, so it reports
  healthy even when the database is down.
* **L153-161** — punch-out, PATCH regularize, all analytics and `/admin/explain`
  are simply absent.

## Looked at and decided was *not* a defect

* **`emp_code` as the identifier** everywhere (URLs, bodies) — correct; the
  Mongo `_id` is an implementation detail and is never exposed.
* **`skip`/`limit` pagination** on `/employees` — fine at this scale; it is only
  the arithmetic (page skip) and the `total` that were wrong.
* **Default shift `09:30`/`18:30`** and **`status` defaulting to `PRESENT`** on
  punch-in — both match the contract.
* **Returning an `id` string** on insert — harmless; the contract keys off
  `emp_code` + `date`, so I dropped it, but it was not a bug.
* **`find().limit()` lookups** — fine; they hit the indexes I added.
