# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne engineering assignment. All application code is in
[app/main.py](app/main.py). `REVIEW.md` is Part A, `DECISIONS.md` is Part C.

## Run

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas SRV URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` is loaded for convenience
but never overrides real environment variables, so injected values win. The seven indexes the app
needs are created at startup, idempotently; `/health` returns 200 only when MongoDB answers a ping
and retries index creation if the database was unreachable at boot.

## What is implemented

Everything in the contract: `/health`, both `/employees` endpoints, punch-in, punch-out,
`PATCH /attendance/{emp_code}/{date}`, the four analytics endpoints and
`GET /admin/explain/{endpoint}`. Nothing is missing or stubbed.

Notes on a few choices:

* **Analytics are aggregation pipelines.** The department trend uses `$densify` for gap filling and
  `$setWindowFields` for the moving average; the leaderboard uses `$rank` for competition ranking.
  Only calendar arithmetic (R7 working days) and the final R8 rounding happen in Python.
* **Pipelines and queries are built by shared functions**, so `/admin/explain/{endpoint}` runs the
  same filters, sort, pagination and index hint as the endpoint it describes.
* **Index hints are deliberate.** They pin the plan, keep the `/attendance` sort index-backed with
  no blocking `SORT` stage, and leave `rejectedPlans` empty so an explain cannot advertise a
  collection scan the endpoint never takes.
* **Rounding is half-up on decimals, never `round()`**, which is half-even on binary floats. Inside
  the trend pipeline the 4-decimal rounding is done in Decimal128 so that a value such as 0.03125
  reports as 0.0313.
* **Concurrency is left to the database**: unique indexes for the two creates, a conditional
  `find_one_and_update` for punch-out, and a compare-and-set on every field a regularization reads,
  so a concurrent correction gets 409 rather than losing its history entry.
* **`avg_work_hours` sums whole hundredths of an hour** rather than floats. `work_hours` is a
  2-decimal value by R4, so this makes the mean exact instead of drifting; a `.005` tie then rounds
  the way R8 says.

## Testing

Verified against MongoDB 7.0.14 with a generated dataset of 1,174 employees and 89,071 attendance
records containing the awkward cases: employees with no logs, mid-month joiners, people who join
after the period, a department nobody has joined yet, weekend records, open records, overnight
shifts, legacy records missing `half_day` / `history` / the minute counters, and engineered ties in
monthly late minutes including a tie at a `limit` cutoff.

The analytics were checked against an independent brute-force implementation in plain Python over
the same raw documents, and the races were checked by firing 64 simultaneous requests (exactly one
201 for punch-in, exactly one 200 for punch-out, every time). The test scripts are not part of this
submission, which is limited to the files the problem statement asks for.
