# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne engineering assignment.
All application code is in [app/main.py](app/main.py). `REVIEW.md` is Part A, `DECISIONS.md` is Part C.

Requires Python 3.11+ and MongoDB 6.0+ (developed and verified against 7.0.14).

## Run

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas SRV URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` come from the environment, and nothing is hard-coded. A local `.env`
is loaded for convenience but never overrides a real environment variable, so injected values
win. The seven indexes the app needs are created at startup, idempotently and batched per
collection — about 5 seconds on 90,000 records from a database that has only `_id`.
`GET /health` returns 200 only when MongoDB answers a ping, and retries index creation if the
database was unreachable at boot, so the app survives a database restart without one of its own.

Interactive docs at `/docs`. There is no route at `/`, so a 404 there is expected.

## Status

**Complete — nothing is missing or stubbed.** All 12 operations in `openapi.yaml` are
implemented: `/health`, both `/employees` endpoints, punch-in, punch-out,
`PATCH /attendance/{emp_code}/{date}`, the four analytics endpoints and
`GET /admin/explain/{endpoint}` for all five endpoint names.

## How it works

* **Analytics are aggregation pipelines**, never a fetch-and-loop. The department trend uses
  `$densify` to fill the gaps and `$setWindowFields` for the moving average; the leaderboard uses
  `$rank` for competition ranking; the department summary joins both collections. Only the R7
  working-day calendar arithmetic and the final R8 rounding happen in Python.
* **Queries and pipelines are built by shared functions**, so `/admin/explain/{endpoint}` runs the
  same filter, sort, pagination and index hint as the endpoint it describes.
* **Index hints are deliberate.** They pin the plan, keep the `/attendance` sort index-backed with
  no blocking `SORT` stage, and leave `rejectedPlans` empty so an explain cannot advertise a
  collection scan the endpoint never takes.
* **Concurrency is left to the database**: unique indexes for the two creates, a conditional
  `find_one_and_update` for punch-out, and a compare-and-set over every field a regularization
  read, so a concurrent correction gets 409 instead of losing its history entry.
* **Rounding is half-up on decimals, never `round()`**, which is half-even on binary floats. The
  trend pipeline rounds in Decimal128 for the same reason: MongoDB's `$round` is half-even too,
  and would report 0.03125 as 0.0312 where R8 wants 0.0313.

## Judgement calls

Three places where the contract allows more than one reading, and the reading taken:

1. **`moving_avg_7d` averages the reported `attendance_rate` values**, i.e. the 4-decimal numbers,
   following "mean of the non-null `attendance_rate` values" literally — not the raw
   `present_count / headcount` ratios.
2. **`avg_work_hours` sums whole hundredths of an hour.** `work_hours` is a 2-decimal value by R4,
   so this keeps the mean exact rather than letting float addition drift across a `.005` boundary.
3. **422 bodies always use the array `detail`** that `openapi.yaml`'s `ValidationError` defines,
   including for rules no field constraint can express (a date range the wrong way round, a
   correction that changes nothing). 404, 409 and 503 use the string `detail` of `Error`.

## Testing

Verified against MongoDB 7.0.14 on a generated dataset of 1,174 employees and 89,071 attendance
records built around the awkward cases: employees with no logs, mid-month joiners, people who join
after the period, a department nobody has joined yet, weekend records, open records, overnight
shifts, legacy records missing `half_day` / `history` / the minute counters, and engineered ties in
monthly late minutes including a tie at a `limit` cutoff.

The analytics were compared against an independent brute-force implementation in plain Python over
the same raw documents; the races were checked with 64 simultaneous requests (exactly one 201 for
punch-in, exactly one 200 for punch-out, every time); and the explain plans were checked to contain
`IXSCAN` and no `COLLSCAN` across twelve parameter combinations. 738 assertions in total.

The harness is not part of this submission, which is limited to the files the problem statement
asks for.
