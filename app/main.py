"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000

All code lives in this ONE file (kit rule).
Business rules R1-R10 come from openapi.yaml.
Storage shape comes from DATA_MODEL.md.
"""
import os
import json
from bson.json_util import dumps
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError, DuplicateKeyError
from pydantic import BaseModel, Field, field_validator

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# load_dotenv does NOT override real env vars by default -> real env wins
load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# ---- constants derived from openapi.yaml ---------------------------------
LATE_GRACE_MIN     = 10       # R2
OVERTIME_MIN_MIN   = 30       # R3
HALF_DAY_THRESHOLD = 4.50     # R5
PRESENCE_STATUSES  = {"PRESENT", "WFH", "ON_DUTY"}   # R6
PUNCHABLE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES       = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EPOCH_MS_MIN       = 100_000_000_000
EPOCH_MS_MAX       = 4_102_444_800_000
MONTH_RE           = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE            = r"^\d{4}-\d{2}-\d{2}$"

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.get("/")
def home():
    return {
        "message": "Employee Attendance & Analytics API is running successfully!",
        "status": "success",
        "docs": "/docs"
    }



# =========================================================================== #
# Startup: indexes (openapi "Configuration": create idempotently at startup)
# =========================================================================== #
@app.on_event("startup")
def _ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])


# =========================================================================== #
# Time / number helpers
# =========================================================================== #
def _truncate_seconds(dt: datetime) -> datetime:
    """R1: truncate instants to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def _to_ist(dt: datetime) -> datetime:
    """Normalise a datetime to IST. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means overnight."""
    return shift_end <= shift_start


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: date = IST calendar date of punch_in, EXCEPT overnight shifts where
    a punch-in earlier than shift_end belongs to the previous day's shift.
    """
    ist = _to_ist(punch_in)
    if _is_overnight(shift_start, shift_end):
        h, m = map(int, shift_end.split(":"))
        cutoff = ist.replace(hour=h, minute=m, second=0, microsecond=0)
        if ist < cutoff:
            ist = ist - timedelta(days=1)
    return ist.date().isoformat()


def _half_up(value: float, places: int) -> float:
    """
    R4/R8: half-up rounding. Python's round() uses banker's rounding,
    which is WRONG for this spec.
    """
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _serialize(doc: dict) -> dict:
    """
    Mongo doc -> API response:
      - drop _id (never in the API)
      - recursively convert datetimes to epoch ms, INCLUDING inside
        history[].changes.{from,to}
    """
    def conv(v: Any) -> Any:
        if isinstance(v, datetime):
            return _to_ms(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return {k: conv(v) for k, v in doc.items() if k != "_id"}


# =========================================================================== #
# Derived-value calculators (R2-R5)
# =========================================================================== #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """
    R2: grace = 10 min. Late only if strictly more than 10:00 past shift_start.
    When late, late_minutes = floor(minutes past shift_start), NOT past grace.
      09:40:00 -> 0 ; 09:40:01 -> 10 ; 10:15:59 -> 45
    """
    pi = _to_ist(_truncate_seconds(punch_in))
    h, m = map(int, shift_start.split(":"))
    start = pi.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_min = int((pi - start).total_seconds() // 60)
    if delta_min <= LATE_GRACE_MIN:
        return 0
    return delta_min


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, half-up to 2 decimals."""
    pi = _truncate_seconds(punch_in)
    po = _truncate_seconds(punch_out)
    seconds = (po - pi).total_seconds()
    return _half_up(seconds / 3600.0, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str,
                     shift_start: str) -> int:
    """
    R3: floor minutes between shift_end and punch_out, only when >= 30.
    For overnight shifts, shift_end falls on the day AFTER `date_str`.
    """
    po = _to_ist(_truncate_seconds(punch_out))
    h, m = map(int, shift_end.split(":"))
    d = date.fromisoformat(date_str)
    end_date = d + timedelta(days=1) if _is_overnight(shift_start, shift_end) else d
    end = datetime(end_date.year, end_date.month, end_date.day, h, m, tzinfo=IST)
    delta_min = int((po - end).total_seconds() // 60)
    if delta_min < OVERTIME_MIN_MIN:
        return 0
    return delta_min


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50 -> True."""
    if work_hours is None:
        return False
    return work_hours < HALF_DAY_THRESHOLD


# =========================================================================== #
# Working days (R7)
# =========================================================================== #
def _month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return first, last


def working_days_in_month(month: str, joined_on: str) -> int:
    """
    R7: Mon-Fri, no holidays. If joined mid-month, count from joined_on.
    If joined_on is after the month, return 0.
    """
    first, last = _month_bounds(month)
    start = max(first, date.fromisoformat(joined_on))
    if start > last:
        return 0
    count = 0
    d = start
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# =========================================================================== #
# Pydantic models
# =========================================================================== #
class Status(str, Enum):
    PRESENT = "PRESENT"
    WFH     = "WFH"
    ON_DUTY = "ON_DUTY"
    ABSENT  = "ABSENT"
    LEAVE   = "LEAVE"


class EmployeeCreate(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., max_length=120,
                             pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str = Field(..., pattern=DATE_RE)

    @field_validator("shift_end")
    @classmethod
    def _start_ne_end(cls, v, info):
        if info.data.get("shift_start") == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status:     Status = Status.PRESENT


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeRequest(BaseModel):
    status:         Optional[Status] = None
    punch_in:       Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out:      Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason:         str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# =========================================================================== #
# System
# =========================================================================== #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "mongodb unavailable"})
    return {"status": "ok"}


# =========================================================================== #
# Employees
# =========================================================================== #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = _truncate_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = [
        _serialize(d)
        for d in db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# =========================================================================== #
# Punch-in
# =========================================================================== #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    if body.status.value not in PUNCHABLE_STATUSES:
        raise HTTPException(422, f"status {body.status.value} not allowed at punch-in")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = _truncate_seconds(
        _from_ms(body.punched_at) if body.punched_at is not None
        else datetime.now(timezone.utc)
    )
    d = _attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code":         body.emp_code,
        "date":             d,
        "status":           body.status.value,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for that date")
    return _serialize(doc)


# =========================================================================== #
# Punch-out
# =========================================================================== #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = _truncate_seconds(
        _from_ms(body.punched_at) if body.punched_at is not None
        else datetime.now(timezone.utc)
    )

    # openapi: "most recent record whose punch_in is at or before punched_at"
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code,
         "punch_in": {"$ne": None, "$lte": ts},
         "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not log:
        raise HTTPException(404, "no punch-in found")

    punch_in = _truncate_seconds(log["punch_in"])
    if ts <= punch_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in) > timedelta(hours=24):
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in, ts)
    overtime   = compute_overtime(ts, emp["shift_end"], log["date"], emp["shift_start"])
    half_day   = compute_half_day(work_hours)

    # 409-safe: only update if still open
    res = db.attendance_logs.update_one(
        {"_id": log["_id"], "punch_out": None},
        {"$set": {
            "punch_out":        ts,
            "work_hours":       work_hours,
            "overtime_minutes": overtime,
            "half_day":         half_day,
        }},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "already punched out")

    return _serialize(db.attendance_logs.find_one({"_id": log["_id"]}))


# =========================================================================== #
# List attendance
# =========================================================================== #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code: q["emp_code"] = emp_code
    if status:   q["status"] = status.value
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to:   q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [_serialize(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


# =========================================================================== #
# Regularize (manual correction with history)
# =========================================================================== #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status.value if body.status else log["status"]
    new_pi     = _truncate_seconds(_from_ms(body.punch_in)) if body.punch_in is not None else log.get("punch_in")
    new_po     = _truncate_seconds(_from_ms(body.punch_out)) if body.punch_out is not None else log.get("punch_out")

    # rule checks
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times not allowed for ABSENT/LEAVE")
        new_pi = None
        new_po = None
    else:
        if new_pi is None:
            raise HTTPException(422, "presence status requires punch_in")
        # R1: punch_in must remain on the record's attendance date
        pi_date = _attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, "punch_in does not match record date")
        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_po - new_pi) > timedelta(hours=24):
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    # recompute derived
    if new_status in {"ABSENT", "LEAVE"}:
        new_late = 0
        new_wh   = None
        new_ot   = 0
        new_hd   = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
        if new_po:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    updates = {
        "status":           new_status,
        "punch_in":         new_pi,
        "punch_out":        new_po,
        "late_minutes":     new_late,
        "work_hours":       new_wh,
        "overtime_minutes": new_ot,
        "half_day":         new_hd,
    }

    # only actually-changed fields go into history
    changes = {}
    for k, v in updates.items():
        old = log.get(k)
        if old != v:
            changes[k] = {"from": old, "to": v}
    if not changes:
        raise HTTPException(422, "no effective change")

    entry = {
        "at":      _truncate_seconds(datetime.now(timezone.utc)),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # concurrency-safe: require the old history length to still match
    res = db.attendance_logs.update_one(
        {"_id": log["_id"], "history": {"$size": len(log.get("history", []))}},
        {"$set": updates, "$push": {"history": entry}},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "record was modified concurrently")

    return _serialize(db.attendance_logs.find_one({"_id": log["_id"]}))


# =========================================================================== #
# Analytics
# =========================================================================== #
def _presence_contribution_expr():
    """R5/R6: presence-status -> 1.0, half_day -> 0.5, else 0."""
    return {
        "$cond": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
            0.0,
        ]
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=MONTH_RE),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    wd = working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {"$group": {
            "_id": None,
            "present_days":     {"$sum": _presence_contribution_expr()},
            "leave_days":       {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":       {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    base = agg[0] if agg else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }

    present_days = round(base["present_days"], 2)  # R7 present_days already sums to 1.0/0.5
    pct = None
    if wd > 0:
        pct = _half_up((present_days / wd) * 100.0, 2)   # R8

    return {
        "emp_code":                emp_code,
        "month":                   month,
        "working_days":            wd,
        "present_days":            present_days,
        "leave_days":              base["leave_days"],
        "late_count":              base["late_count"],
        "total_late_minutes":      base["total_late_minutes"],
        "total_overtime_minutes":  base["total_overtime_minutes"],
        "attendance_pct":          pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_RE),
    department: Optional[str] = None,
):
    first, last = _month_bounds(month)
    last_str = last.isoformat()
    prefix = month

    # employees in scope (R9: joined_on <= last day of month)
    emp_filter = {"joined_on": {"$lte": last_str}}
    if department:
        emp_filter["department"] = department

    employees = list(db.employees.find(emp_filter))

    # group employees by department, count headcount + min joined_on
    by_dept: dict[str, dict] = {}
    for e in employees:
        d = e["department"]
        slot = by_dept.setdefault(d, {"headcount": 0, "emp_codes": []})
        slot["headcount"] += 1
        slot["emp_codes"].append(e["emp_code"])

    if not by_dept:
        return {"month": month, "items": []}

    all_codes = [c for v in by_dept.values() for c in v["emp_codes"]]

    # one aggregation over attendance_logs for the month
    pipeline = [
        {"$match": {"emp_code": {"$in": all_codes}, "date": {"$regex": f"^{prefix}"}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days":    {"$sum": _presence_contribution_expr()},
            "work_hours_sum":  {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                "$work_hours",
                0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                1, 0,
            ]}},
            "late_count":       {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count":      {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count":    {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    per_emp = {d["_id"]: d for d in db.attendance_logs.aggregate(pipeline)}

    items = []
    for dept, info in by_dept.items():
        present_days = 0.0
        wh_sum = 0.0
        wh_count = 0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0
        for code in info["emp_codes"]:
            row = per_emp.get(code)
            if not row:
                continue
            present_days += row.get("present_days", 0.0)
            wh_sum += row.get("work_hours_sum", 0.0)
            wh_count += row.get("work_hours_count", 0)
            late_count += row.get("late_count", 0)
            total_late_minutes += row.get("total_late_minutes", 0)
            leave_count += row.get("leave_count", 0)
            on_duty_count += row.get("on_duty_count", 0)

        avg_wh = None
        if wh_count > 0:
            avg_wh = _half_up(wh_sum / wh_count, 2)

        items.append({
            "department":         dept,
            "headcount":          info["headcount"],
            "present_days":       round(present_days, 2),
            "avg_work_hours":     avg_wh,
            "late_count":         late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count":        leave_count,
            "on_duty_count":      on_duty_count,
        })

    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    match = {"date": {"$regex": f"^{month}"}}
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "_id",
            "foreignField": "emp_code", "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    rows = list(db.attendance_logs.aggregate(pipeline))
    # order: total DESC, then emp_code ASC
    rows.sort(key=lambda r: (-r["total_late_minutes"], r["_id"]))

    # standard competition ranking
    out = []
    prev_total = None
    prev_rank = 0
    for i, r in enumerate(rows, start=1):
        if r["total_late_minutes"] != prev_total:
            rank = i
            prev_total = r["total_late_minutes"]
            prev_rank = rank
        else:
            rank = prev_rank
        if rank > limit:
            break
        out.append({
            "rank":                rank,
            "emp_code":            r["_id"],
            "name":                r["emp"].get("name"),
            "department":          r["emp"].get("department"),
            "total_late_minutes":  r["total_late_minutes"],
            "late_count":          r["late_count"],
        })

    return {"month": month, "items": out}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    d_from = date.fromisoformat(from_)
    d_to   = date.fromisoformat(to)
    if d_to < d_from:
        raise HTTPException(422, "to must be >= from")
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "range must be <= 92 days")

    dept_emps = list(db.employees.find({"department": department}))
    if not dept_emps:
        raise HTTPException(404, "unknown department")

    # build per-day headcount (R9: joined_on <= day)
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    headcounts: dict[str, int] = {}
    for d in days:
        ds = d.isoformat()
        headcounts[ds] = sum(
            1 for e in dept_emps if e.get("joined_on", "9999-12-31") <= ds
        )

    emp_codes = [e["emp_code"] for e in dept_emps]

    # fetch logs in range for those employees
    cursor = db.attendance_logs.find({
        "emp_code": {"$in": emp_codes},
        "date": {"$gte": from_, "$lte": to},
    })
    # group by date
    by_date: dict[str, dict] = {}
    for log in cursor:
        d = log["date"]
        slot = by_date.setdefault(d, {"present": 0.0, "late": 0})
        if log["status"] in PRESENCE_STATUSES:
            slot["present"] += 0.5 if log.get("half_day") else 1.0
        if log.get("late_minutes", 0) > 0:
            slot["late"] += 1

    items = []
    rates: list[Optional[float]] = []
    for d in days:
        ds = d.isoformat()
        is_wd = d.weekday() < 5
        hc = headcounts[ds]
        slot = by_date.get(ds, {"present": 0.0, "late": 0})

        if is_wd and hc > 0:
            rate = _half_up(slot["present"] / hc, 4)   # R8: 4 decimals
        else:
            rate = None
        rates.append(rate)
        items.append({
            "date":            ds,
            "is_working_day":  is_wd,
            "headcount":       hc,
            "present_count":   round(slot["present"], 2),
            "late_count":      slot["late"],
            "attendance_rate": rate,
            "moving_avg_7d":   None,  # filled below
        })

    # moving average of non-null rates among current + up to 6 preceding rows
    for i in range(len(items)):
        window = [r for r in rates[max(0, i - 6): i + 1] if r is not None]
        if window:
            items[i]["moving_avg_7d"] = _half_up(sum(window) / len(window), 4)

    return {"department": department, "items": items}


# =========================================================================== #
# Admin — explain
# =========================================================================== #
def _build_explain_query(
    endpoint: str,
    *,
    emp_code=None, month=None, department=None, limit=10,
    date_from=None, date_to=None, status=None, from_=None, to=None,
    page=1, page_size=20,
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status:   q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to:   q["date"]["$lte"] = date_to
        return db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip((page - 1) * page_size).limit(page_size)

    if endpoint == "employee_monthly":
        if not (emp_code and month):
            raise HTTPException(422, "emp_code and month are required")
        q = {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}
        return db.attendance_logs.find(q)

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        q = {"date": {"$regex": f"^{month}"}}
        if department:
            q["emp_code"] = {"$regex": "^"}  # placeholder; not really used
        return db.attendance_logs.find(q)

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        return db.attendance_logs.find(
            {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}
        )

    if endpoint == "department_trend":
        if not (department and from_ and to):
            raise HTTPException(422, "department, from and to are required")
        return db.attendance_logs.find({
            "date": {"$gte": from_, "$lte": to},
        })

    raise HTTPException(422, "unknown endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary",
               "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, f"endpoint must be one of {sorted(allowed)}")

    cur = _build_explain_query(
        endpoint,
        emp_code=emp_code, month=month, department=department, limit=limit,
        date_from=date_from, date_to=date_to,
        status=status.value if status else None,
        from_=from_, to=to, page=page, page_size=page_size,
    )
    plan = cur.explain()
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": json.loads(dumps(plan)),
}