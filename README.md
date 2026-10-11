# Employee Attendance & Analytics API

REST API for employee attendance: punch-in and punch-out, manual corrections with an audit trail,
and monthly analytics. Built with FastAPI and MongoDB.

All application code is in `app/main.py`. The code review is in `REVIEW.md` and the design notes
are in `DECISIONS.md`.

## Requirements

| | |
|---|---|
| Python | 3.11 or newer |
| MongoDB | 6.0 or newer (tested on 7.0.14) |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | Connection string. SRV URIs are supported. |
| `MONGO_DB` | `attendance_db` | Database name. |

A `.env` file in the working directory is loaded if one exists. Real environment variables take
precedence over it.

## Running

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

OpenAPI documentation is served at `/docs`. There is no route at `/`.

Indexes are created at startup. The operation is idempotent, so restarts are cheap; on a database
of 90,000 attendance records with no indexes, startup takes about 5 seconds.

`GET /health` returns 200 when MongoDB answers a ping and 503 otherwise. If index creation fails at
startup it is retried on each health check, so the service recovers from a database outage without
being restarted.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Readiness probe. |
| POST | `/employees` | Create an employee. 409 on a duplicate `emp_code`. |
| GET | `/employees` | List employees, filtered by department, sorted by `emp_code`. |
| POST | `/attendance/punch-in` | Open the day's record. Computes `date` (R1) and `late_minutes` (R2). |
| POST | `/attendance/punch-out` | Close the most recent open record. Computes R3 to R5. |
| GET | `/attendance` | List records by employee, date range and status. |
| PATCH | `/attendance/{emp_code}/{date}` | Correct one record. Recomputes derived fields, appends to `history`. |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee. |
| GET | `/analytics/departments/summary` | Per-department summary for a month. |
| GET | `/analytics/leaderboard/late` | Late-comer ranking with competition ranks. |
| GET | `/analytics/departments/{department}/trend` | Daily series with gap filling and a 7-day moving average. |
| GET | `/admin/explain/{endpoint}` | `executionStats` explain for an endpoint's main query. |

Request and response shapes, status codes and the business rules R1 to R10 are defined in
`openapi.yaml`.

## Indexes

| Collection | Keys | Serves |
|---|---|---|
| `employees` | `emp_code` (unique) | Lookups by employee; makes a duplicate create a 409. |
| `employees` | `department, emp_code` | `GET /employees` filtered and sorted. |
| `employees` | `department, joined_on` | Headcount for one department (R9). |
| `employees` | `joined_on` | Headcount across departments (R9). |
| `attendance_logs` | `emp_code, date` (unique) | Record identity, the punch-in race, one employee's month. |
| `attendance_logs` | `date` desc, `emp_code` asc | `GET /attendance` sort order and date-range analytics. |
| `attendance_logs` | `emp_code, punch_in` desc | Finding the record a punch-out closes. |

## Implementation notes

Analytics are MongoDB aggregation pipelines. `$densify` fills missing days in the department trend,
`$setWindowFields` computes its moving average, and `$rank` produces competition ranking for the
leaderboard. Working-day counts (R7) and final rounding (R8) are done in Python.

Queries and pipelines are produced by shared builder functions. `GET /admin/explain/{endpoint}`
calls the same builders, so the explain output describes the filter, sort, pagination and index hint
that the endpoint itself uses.

Queries pass an index hint. This fixes the query plan, keeps the `GET /attendance` sort
index-backed with no blocking `SORT` stage, and leaves `rejectedPlans` empty.

Concurrency control is in the database:

- unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` for the two creates
- a conditional `find_one_and_update` on `punch_out: null` for punch-out
- a compare-and-set over the fields a regularization read, so a concurrent correction cannot drop
  another one's history entry; the losing request gets 409

Rounding uses `decimal.Decimal` with `ROUND_HALF_UP`. Python's `round()` and MongoDB's `$round` are
both half-even, which R4 and R8 do not allow. The department trend pipeline rounds in Decimal128 so
that values such as 0.03125 report as 0.0313.

## Interpretation of ambiguous rules

| Field | Reading used |
|---|---|
| `moving_avg_7d` | Mean of the rounded `attendance_rate` values, not of the raw `present_count / headcount` ratios. |
| `avg_work_hours` | Computed from a sum of whole hundredths of an hour. `work_hours` is a two-decimal value under R4, so the mean is exact. |
| 422 response body | `detail` is an array, matching `ValidationError` in `openapi.yaml`. 404, 409 and 503 use the string `detail` of `Error`. |

## Status

Every endpoint in `openapi.yaml` is implemented. Nothing is stubbed or omitted.

## Testing

Tested against a generated dataset of 1,174 employees and 89,070 attendance records covering
employees without logs, mid-month joiners, employees who join after the reporting period, weekend
records, open records, overnight shifts, records missing optional fields, and tied monthly late
totals.

Analytics output was compared against a separate implementation written in plain Python over the
same documents. Race conditions were tested with 64 simultaneous requests. Explain output was
checked for `IXSCAN` and for the absence of `COLLSCAN`.
