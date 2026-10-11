"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real
      environment variables always win because load_dotenv does not override)

Layout of this file
    1. configuration and Mongo handles
    2. time / rounding / calendar helpers  (business rules R1-R5, R7, R8)
    3. request models -- all input validation lives here, so bad input is 422
    4. index management
    5. serializers (BSON -> the JSON shapes in openapi.yaml)
    6. query and pipeline builders -- shared by the real endpoints and
       /admin/explain, so an explain always describes the real plan
    7. endpoints

Conventions the whole file relies on:
    * emp_code identifies an employee; _id never leaves the process.
    * Instants cross the API as epoch milliseconds and are stored as BSON
      dates, truncated to whole seconds (R1).
    * Calendar dates ("YYYY-MM-DD", IST) and shift times ("HH:MM", IST) are
      stored and compared as strings: lexical order == chronological order.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override real environment variables

LOG = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

COL_EMPLOYEES = "employees"
COL_ATTENDANCE = "attendance_logs"

# tz_aware=True matters: PyMongo returns naive datetimes by default, and mixing
# those with aware ones either raises TypeError or silently shifts the clock.
client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    appname="attendance-api",
)
db = client[MONGO_DB]
employees = db[COL_EMPLOYEES]
attendance = db[COL_ATTENDANCE]

# --------------------------------------------------------------------------- #
# 2. Helpers: time, rounding, calendar
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_HOURS = 4.50            # R5
TREND_MAX_DAYS = 92

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
EMP_CODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(value: Any) -> Optional[int]:
    """BSON date -> epoch milliseconds, with integer arithmetic so nothing drifts."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:  # defensive: a datetime read without tz_aware
        value = value.replace(tzinfo=timezone.utc)
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_utc() -> datetime:
    """Server clock, truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift crosses midnight."""
    return shift_end <= shift_start


def ist_instant(date_str: str, hhmm: str, day_offset: int = 0) -> datetime:
    """The instant of an IST wall-clock time on a given IST calendar date."""
    year, month, day = (int(part) for part in date_str.split("-"))
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(year, month, day, hour, minute, tzinfo=IST) + timedelta(days=day_offset)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)  # an early-morning punch belongs to yesterday's shift
    return day.isoformat()


def shift_start_instant(date_str: str, emp: dict) -> datetime:
    return ist_instant(date_str, emp["shift_start"])


def shift_end_instant(date_str: str, emp: dict) -> datetime:
    """For an overnight shift the end falls on the next calendar day (R1)."""
    offset = 1 if is_overnight(emp["shift_start"], emp["shift_end"]) else 0
    return ist_instant(date_str, emp["shift_end"], offset)


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 late, then floor(minutes since shift_start)."""
    elapsed = int((punch_in - shift_start_instant(date_str, emp)).total_seconds())
    if elapsed <= GRACE_SECONDS:
        return 0
    return elapsed // 60


def compute_overtime_minutes(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: floor(minutes after shift_end), but only when that reaches 30."""
    elapsed = int((punch_out - shift_end_instant(date_str, emp)).total_seconds())
    if elapsed < 0:
        return 0
    minutes = elapsed // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value: Any, places: int) -> Decimal:
    """R8: decimal rounding, half-up. `value` may be int, float or Decimal."""
    if not isinstance(value, Decimal):
        value = Decimal(value)  # Decimal(float) is exact, so there is no double rounding
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (round() is half-even, so not that)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def report(value: Any, places: int = 2) -> Optional[float]:
    """R8 rounding for a reported number, passing None through."""
    return None if value is None else float(round_half_up(value, places))


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    first = calendar_date(year, mon, 1)
    nxt = calendar_date(year + 1, 1, 1) if mon == 12 else calendar_date(year, mon + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def count_weekdays(start: calendar_date, end: calendar_date) -> int:
    """R7: Mon-Fri days in [start, end] inclusive, no holiday calendar."""
    if end < start:
        return 0
    total = (end - start).days + 1
    weeks, remainder = divmod(total, 7)
    days = weeks * 5  # any 7 consecutive days hold exactly 5 weekdays
    for offset in range(remainder):
        if (start + timedelta(days=offset)).weekday() < 5:
            days += 1
    return days


def working_days_in_month(month: str, joined_on: str) -> int:
    """R7: counted from joined_on, so a mid-month joiner owes fewer days."""
    first, last = month_bounds(month)
    start = calendar_date.fromisoformat(max(first, joined_on))
    return count_weekdays(start, calendar_date.fromisoformat(last))


def parse_calendar_date(value: str, field: str) -> calendar_date:
    """YYYY-MM-DD -> date. Well-formed but impossible dates (2026-02-30) are 422."""
    try:
        return calendar_date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} is not a valid calendar date")


def bson_to_json(doc: Any) -> Any:
    """Make raw BSON (explain output) JSON-serialisable, as MongoDB extended JSON."""
    return json.loads(json_util.dumps(doc))


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
# strict=True rejects floats, numeric strings and bools; the bounds reject
# seconds-looking values and anything outside the supported range.
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class EmployeeIn(BaseModel):
    """Derived fields sent by a client are ignored, not rejected (extra='ignore')."""

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, value: str) -> str:
        try:
            calendar_date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return value

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: PresenceLiteral = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    """`status`, `punch_in` and `punch_out` are optional but not nullable: the
    contract has no null for them, so an explicit null is 422. Which fields the
    client actually sent is read from model_fields_set, since "omitted" and
    "sent" mean different things here."""

    model_config = ConfigDict(extra="ignore")

    status: StatusLiteral = None  # type: ignore[assignment]
    punch_in: EpochMillis = None  # type: ignore[assignment]
    punch_out: EpochMillis = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Indexes
# --------------------------------------------------------------------------- #
# Keyed by the hint the endpoints pass, so index and hint can never drift apart.
IX_EMP_CODE = {"emp_code": 1}
IX_EMP_DEPT_CODE = {"department": 1, "emp_code": 1}
IX_EMP_DEPT_JOINED = {"department": 1, "joined_on": 1}
IX_EMP_JOINED = {"joined_on": 1}
IX_ATT_EMP_DATE = {"emp_code": 1, "date": 1}
IX_ATT_DATE_EMP = {"date": -1, "emp_code": 1}
IX_ATT_EMP_PUNCH_IN = {"emp_code": 1, "punch_in": -1}

INDEX_SPECS: tuple[tuple[Any, dict, dict], ...] = (
    # employees
    (employees, IX_EMP_CODE, {"name": "uq_emp_code", "unique": True}),
    (employees, IX_EMP_DEPT_CODE, {"name": "dept_emp_code"}),
    (employees, IX_EMP_DEPT_JOINED, {"name": "dept_joined_on"}),
    (employees, IX_EMP_JOINED, {"name": "joined_on"}),
    # attendance_logs
    (attendance, IX_ATT_EMP_DATE, {"name": "uq_emp_code_date", "unique": True}),
    (attendance, IX_ATT_DATE_EMP, {"name": "date_emp_code"}),
    (attendance, IX_ATT_EMP_PUNCH_IN, {"name": "emp_code_punch_in"}),
)

_indexes_ready = False
_index_lock = threading.Lock()


def _create_index(collection: Any, keys: dict, options: dict) -> bool:
    """One index, created idempotently. create_index() is a no-op when an
    identical index already exists, so this is safe to call on every start.

    A unique index can only fail for one interesting reason: the data already
    holds duplicates. The queries still need the index to exist (they hint it),
    so fall back to a non-unique one rather than leaving nothing behind.
    """
    try:
        collection.create_index(list(keys.items()), **options)
        return True
    except PyMongoError as exc:
        if not options.get("unique"):
            LOG.warning("could not create index %s on %s: %s", keys, collection.name, exc)
            return False
        LOG.warning("unique index %s on %s failed (%s); creating it non-unique",
                    keys, collection.name, exc)
        try:
            collection.create_index(list(keys.items()), name=options["name"] + "_nonunique")
            return True
        except PyMongoError as inner:
            LOG.warning("fallback index %s on %s failed too: %s", keys, collection.name, inner)
            return False


def ensure_indexes() -> bool:
    """Create every index this app needs. Each one is attempted independently, so
    a single failure cannot leave the rest missing, and nothing here raises: the
    process must start, and /health retries until the database cooperates."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return True
        created = [_create_index(*spec) for spec in INDEX_SPECS]
        _indexes_ready = all(created)
        if _indexes_ready:
            LOG.info("indexes ready on %s", MONGO_DB)
        else:
            LOG.warning("%d of %d indexes are missing, will retry",
                        created.count(False), len(created))
        return _indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------- #
# 5. Serializers
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = (
    "emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on",
)
EMPLOYEE_PROJECTION = {"_id": 0, **{field: 1 for field in EMPLOYEE_FIELDS}, "created_at": 1}
ATTENDANCE_PROJECTION = {"_id": 0}
TIME_VALUED_CHANGES = ("punch_in", "punch_out")


def employee_out(doc: dict) -> dict:
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def history_out(entries: Any) -> list[dict]:
    """Audit entries, with the BSON dates nested inside `changes` converted too."""
    result = []
    for entry in entries or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            if field in TIME_VALUED_CHANGES:
                changes[field] = {
                    "from": dt_to_ms(change.get("from")),
                    "to": dt_to_ms(change.get("to")),
                }
            else:
                changes[field] = {"from": change.get("from"), "to": change.get("to")}
        result.append({
            "at": dt_to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return result


def attendance_out(doc: dict) -> dict:
    """Seeded records may omit half_day / history / the minute counters (DATA_MODEL
    section 3): missing means 0, false and []."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# 6. Query and pipeline builders
# --------------------------------------------------------------------------- #
# Aggregation fragments. Every one of them reads the *stored* derived values,
# because analytics must not recompute them from the punch times.
_DATE_AS_DATE = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday
IS_WEEKDAY = {"$in": [{"$dayOfWeek": _DATE_AS_DATE}, WEEKDAYS]}
IS_PRESENCE = {"$in": ["$status", list(PRESENCE_STATUSES)]}
DAY_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
OVERTIME_MINUTES = {"$ifNull": ["$overtime_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}
# R7: present_days only counts Mon-Fri records.
PRESENT_DAYS_WEEKDAY = {"$cond": [{"$and": [IS_PRESENCE, IS_WEEKDAY]}, DAY_VALUE, 0]}
# The trend counts presence on any day of the week.
PRESENT_DAYS_ANY = {"$cond": [IS_PRESENCE, DAY_VALUE, 0]}
# avg_work_hours averages over present-status records that have a work_hours.
HAS_WORK_HOURS = {"$and": [IS_PRESENCE, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}


def count_if(condition: Any) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_4(expression: Any) -> dict:
    """4-decimal half-up rounding inside the pipeline (R8).

    $round rounds half to even, which would report 0.03125 as 0.0312. Decimal128
    makes floor(x * 10000 + 0.5) exact for these quotients, so this is half-up.
    """
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, 10000]}, 0.5]}},
            10000,
        ]
    }


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> tuple[dict, dict, dict]:
    """Filter, sort and index hint for GET /attendance.

    Hinting is deliberate: it pins the plan so /admin/explain describes exactly
    what the endpoint runs, and it keeps the sort index-backed (no blocking SORT).
    """
    if date_from is not None:
        parse_calendar_date(date_from, "date_from")
    if date_to is not None:
        parse_calendar_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    date_range = {}
    if date_from is not None:
        date_range["$gte"] = date_from
    if date_to is not None:
        date_range["$lte"] = date_to
    if date_range:
        query["date"] = date_range
    if status is not None:
        query["status"] = status

    sort = {"date": -1, "emp_code": 1}
    # One employee: seek straight into their slice of (emp_code, date) and read
    # it backwards. Otherwise walk (date desc, emp_code asc), which *is* the sort.
    hint = IX_ATT_EMP_DATE if emp_code is not None else IX_ATT_DATE_EMP
    return query, sort, hint


def build_employee_monthly_pipeline(emp_code: str, month: str) -> tuple[str, list, dict]:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS_WEEKDAY},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "total_overtime_minutes": {"$sum": OVERTIME_MINUTES},
        }},
        {"$project": {"_id": 0}},
    ]
    return COL_ATTENDANCE, pipeline, IX_ATT_EMP_DATE


def build_department_summary_pipeline(
    month: str, department: Optional[str]
) -> tuple[str, list, dict]:
    """Starts from `employees`, so headcount is driven by people, not by logs:
    an employee with zero logs still contributes 1 to headcount and 0 to
    everything else (R9). The month's logs are joined per employee on the
    indexed emp_code, and the per-employee numbers are summed per department.
    """
    first, last = month_bounds(month)

    match: dict = {"joined_on": {"$lte": last}}  # R9: nobody who joins later
    if department is not None:
        match["department"] = department
        hint = IX_EMP_DEPT_JOINED
    else:
        hint = IX_EMP_JOINED

    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS_WEEKDAY},
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
            # work_hours are 2-decimal values; summing whole hundredths keeps the
            # mean exact instead of accumulating float error.
            "work_hours_centis": {"$sum": {
                "$cond": [HAS_WORK_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]
            }},
            "work_hours_records": count_if(HAS_WORK_HOURS),
        }},
    ]

    totals = (
        "present_days", "late_count", "total_late_minutes", "leave_count",
        "on_duty_count", "work_hours_centis", "work_hours_records",
    )
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": COL_ATTENDANCE,
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": per_employee,
            "as": "agg",
        }},
        {"$set": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{field: {"$sum": {"$ifNull": [f"$agg.{field}", 0]}} for field in totals},
        }},
        {"$sort": {"_id": 1}},
    ]
    return COL_EMPLOYEES, pipeline, hint


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> tuple[str, list, dict]:
    """$rank is standard competition ranking (1, 2, 2, 4), and `limit` is applied
    to the rank rather than to the row count, so a tie at the cutoff keeps every
    tied row."""
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # An inner join: logs whose employee no longer exists are dropped.
        {"$lookup": {
            "from": COL_EMPLOYEES,
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        # Filter before ranking, so ranks are computed inside the department.
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return COL_ATTENDANCE, pipeline, IX_ATT_DATE_EMP


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str, roster: list[dict]
) -> tuple[str, list, dict]:
    """One row per calendar day in [date_from, date_to].

    $densify invents the missing days (weekends, days with no record at all) and
    $setWindowFields computes the moving average, so the gap filling and the
    window are the database's work. `roster` is the department's employees, read
    once; headcount per day is then a $filter over that constant array, which
    avoids a correlated per-day lookup.
    """
    codes = [member["emp_code"] for member in roster]
    # R9: only people who had joined by the end of the range can ever count.
    joined = sorted(m["joined_on"] for m in roster if m["joined_on"] <= date_to)

    lower = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    upper = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc) + timedelta(days=1)

    headcount = {"$size": {"$filter": {
        "input": {"$literal": joined},
        "as": "joined_on",
        "cond": {"$lte": ["$$joined_on", "$date"]},
    }}}
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        half_up_4({"$divide": ["$present_count", "$headcount"]}),
        None,
    ]}

    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": PRESENT_DAYS_ANY},
            "late_count": count_if(IS_LATE),
        }},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Upper bound is exclusive, hence to + 1 day. Explicit bounds also make
        # $densify produce the full range when there are no records at all.
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day", "bounds": [lower, upper],
        }}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]},
        }},
        {"$set": {"headcount": headcount}},
        {"$set": {"rate": rate}},
        # Current row plus up to 6 preceding rows, all inside the requested range,
        # and $avg skips the null rates exactly as the contract asks.
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": half_up_4("$moving")},
        }},
        {"$sort": {"date": 1}},
    ]
    return COL_ATTENDANCE, pipeline, IX_ATT_EMP_DATE


def department_roster(department: str) -> list[dict]:
    """The department's employees. 404 territory when it is empty."""
    cursor = employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
        hint=IX_EMP_DEPT_JOINED,
    )
    return list(cursor)


def validate_trend_range(date_from: str, date_to: str) -> None:
    start = parse_calendar_date(date_from, "from")
    end = parse_calendar_date(date_to, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > TREND_MAX_DAYS:
        raise HTTPException(422, f"the range must not exceed {TREND_MAX_DAYS} days")


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """200 only when MongoDB answers; also a second chance at index creation."""
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc(),
    }
    try:
        # Insert first and let the unique index decide. A find-then-insert would
        # let two simultaneous creates both succeed.
        employees.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    total = employees.count_documents(query)  # R10: total reflects the filter
    cursor = (
        employees.find(query, EMPLOYEE_PROJECTION)
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, date_str, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique index on (emp_code, date) is the race guard: of N identical
        # concurrent punch-ins exactly one insert wins, the rest raise here.
        attendance.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # The most recent punch-in at or before punched_at. Looking it up by instant
    # rather than by date is what makes an overnight shift work.
    record = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found at or before that time")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    opened_at = record["punch_in"]
    if punched_at <= opened_at:
        raise HTTPException(422, "punched_at must be after punch_in")
    if punched_at - opened_at > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(opened_at, punched_at)
    # punch_out: None in the filter is the real guard: two simultaneous
    # punch-outs both reach here, and only the first update matches.
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime_minutes(punched_at, record["date"], emp),
            "half_day": work_hours < HALF_DAY_HOURS,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[StatusLiteral] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
    total = attendance.count_documents(query)
    # Sort, skip and limit all happen in MongoDB: nothing but one page is read.
    cursor = (
        attendance.find(query, ATTENDANCE_PROJECTION, hint=hint)
        .sort(list(sort.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


REGULARIZABLE = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                 "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str = Path(pattern=DATE_RE, description="The record's attendance date."),
    body: RegularizeIn = Body(...),
):
    parse_calendar_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = attendance.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    status = body.status if "status" in sent else record["status"]
    is_presence = status in PRESENCE_STATUSES

    if not is_presence and ("punch_in" in sent or "punch_out" in sent):
        raise HTTPException(422, f"{status} clears the punch times; do not send them")

    if is_presence:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else record.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else record.get("punch_out")
        if new_in is None:
            raise HTTPException(422, f"status {status} requires a punch_in")
        if "punch_in" in sent and attendance_date(
            new_in, emp["shift_start"], emp["shift_end"]
        ) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
        work_hours = compute_work_hours(new_in, new_out) if new_out is not None else None
        final = {
            "status": status,
            "punch_in": new_in,
            "punch_out": new_out,
            "work_hours": work_hours,
            "late_minutes": compute_late_minutes(new_in, date, emp),
            "overtime_minutes": (
                compute_overtime_minutes(new_out, date, emp) if new_out is not None else 0
            ),
            "half_day": work_hours < HALF_DAY_HOURS if work_hours is not None else False,
        }
    else:
        # ABSENT / LEAVE: both punch times go, and every derived value resets.
        final = {
            "status": status, "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        }

    current = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day") or False),
    }
    changes = {
        field: {"from": current[field], "to": final[field]}
        for field in REGULARIZABLE
        if current[field] != final[field]
    }
    if not changes:
        raise HTTPException(422, "the request would not change anything")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,  # BSON dates stay dates inside `changes` (DATA_MODEL 4)
    }
    # Compare-and-set on every field we read, so a concurrent regularization
    # cannot be overwritten; the loser gets 409 instead of losing its history.
    guard: dict = {"_id": record["_id"]}
    for field in REGULARIZABLE:
        guard[field] = record[field] if field in record else {"$exists": False}

    updated = attendance.find_one_and_update(
        guard,
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record changed while this correction was being applied")
    return attendance_out(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=MONTH_RE),
):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    collection, pipeline, hint = build_employee_monthly_pipeline(emp_code, month)
    rows = list(db[collection].aggregate(pipeline, hint=hint))
    agg = rows[0] if rows else {}

    working_days = working_days_in_month(month, emp["joined_on"])
    present_days = agg.get("present_days", 0) or 0
    attendance_pct = (
        None if working_days == 0
        else report(Decimal(present_days) * 100 / Decimal(working_days))
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": report(present_days),
        "leave_days": int(agg.get("leave_days", 0) or 0),
        "late_count": int(agg.get("late_count", 0) or 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=MONTH_RE),
    department: Optional[str] = None,
):
    collection, pipeline, hint = build_department_summary_pipeline(month, department)
    items = []
    for row in db[collection].aggregate(pipeline, hint=hint):
        records = int(row.get("work_hours_records", 0) or 0)
        avg_work_hours = (
            None if records == 0
            # $round returns a double holding an exact integer; round() guards the cast.
            else report(Decimal(round(row["work_hours_centis"])) / (Decimal(100) * records))
        )
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": report(row.get("present_days", 0) or 0),
            "avg_work_hours": avg_work_hours,
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    collection, pipeline, hint = build_late_leaderboard_pipeline(month, limit, department)
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in db[collection].aggregate(pipeline, hint=hint)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=DATE_RE),
    date_to: str = Query(..., alias="to", pattern=DATE_RE),
):
    validate_trend_range(date_from, date_to)
    roster = department_roster(department)
    if not roster:
        raise HTTPException(404, "department not found")

    collection, pipeline, hint = build_department_trend_pipeline(
        department, date_from, date_to, roster
    )
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": report(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": report(row["attendance_rate"], 4),
            "moving_avg_7d": report(row["moving_avg_7d"], 4),
        }
        for row in db[collection].aggregate(pipeline, hint=hint)
    ]
    return {"department": department, "items": items}


ExplainName = Literal[
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend",
]


def require(value: Optional[str], name: str) -> str:
    if value is None:
        raise HTTPException(422, f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[StatusLiteral] = None,
    date_range_from: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    date_range_to: Optional[str] = Query(None, alias="to", pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain for the real query of the named endpoint.

    Every plan is pinned with the same hint the endpoint uses, so the winning
    plan here is the plan that actually runs -- and rejectedPlans stays empty
    instead of advertising a collection scan the endpoint never takes.
    """
    if endpoint == "attendance_list":
        query, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
        collection = COL_ATTENDANCE
        command = {
            "find": collection,
            "filter": query,
            "projection": ATTENDANCE_PROJECTION,
            "sort": sort,
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": hint,
        }
    else:
        if endpoint == "employee_monthly":
            code = require(emp_code, "emp_code")
            collection, pipeline, hint = build_employee_monthly_pipeline(
                code, require(month, "month")
            )
        elif endpoint == "department_summary":
            collection, pipeline, hint = build_department_summary_pipeline(
                require(month, "month"), department
            )
        elif endpoint == "late_leaderboard":
            collection, pipeline, hint = build_late_leaderboard_pipeline(
                require(month, "month"), limit, department
            )
        else:  # department_trend
            name = require(department, "department")
            trend_from = require(date_range_from, "from")
            trend_to = require(date_range_to, "to")
            validate_trend_range(trend_from, trend_to)
            roster = department_roster(name)
            if not roster:  # same 404 as the endpoint, and it keeps the plan real
                raise HTTPException(404, "department not found")
            collection, pipeline, hint = build_department_trend_pipeline(
                name, trend_from, trend_to, roster
            )
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
            "hint": hint,
        }

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": bson_to_json(raw),
    }
