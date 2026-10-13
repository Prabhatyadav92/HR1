"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)
"""
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
# tz_aware=True: PyMongo otherwise returns naive datetimes that are easy to mistake for local time.
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
MAX_CAS_RETRIES = 8



@asynccontextmanager
async def lifespan(_app):
    create_indexes()          # idempotent; runs before the first request
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_error_handler(request: Request, exc: PyMongoError):
    # Never leak a 500 for a database problem.
    return JSONResponse(status_code=503, content={"detail": "Database operation failed"})


def unprocessable(msg: str):
    """422 with FastAPI's validation-error shape (`detail` is an array), as the contract specifies."""
    return HTTPException(422, [{"loc": [], "msg": msg, "type": "value_error"}])


# ------------------------------- utilities ---------------------------------

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MIN_EPOCH_MS = 100_000_000_000          # anything smaller looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000        # 2100-01-01, per openapi EpochMillis


def now_ist():
    return datetime.now(IST).replace(microsecond=0)


def to_utc(value: datetime):
    if value.tzinfo is None:          # PyMongo semantics: naive means UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def from_db(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).replace(microsecond=0)


def to_ms(value):
    """datetime -> integer epoch milliseconds (whole seconds); None stays None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return calendar.timegm(value.astimezone(UTC).utctimetuple()) * 1000


def epoch_ms_to_ist(value):
    if isinstance(value, bool) or not isinstance(value, int) or not (MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
        raise unprocessable("Timestamp must be integer epoch milliseconds")
    # integer floor-division: truncates to whole seconds without float error (R1)
    return datetime.fromtimestamp(value // 1000, UTC).astimezone(IST)


def date_value(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise unprocessable("Date must use YYYY-MM-DD")


def month_bounds(month: str):
    try:
        parsed = datetime.strptime(month, "%Y-%m").date()
        if parsed.strftime("%Y-%m") != month:
            raise ValueError
    except (ValueError, TypeError):
        raise unprocessable("month must use YYYY-MM")
    return parsed.replace(day=1), parsed.replace(day=calendar.monthrange(parsed.year, parsed.month)[1])


def parse_hm(shift_time: str):
    if not isinstance(shift_time, str) or not HHMM.match(shift_time):
        raise unprocessable("Shift time must use HH:MM (24-hour)")
    return int(shift_time[:2]), int(shift_time[3:])


def shift_datetime(day: date, shift_time: str):
    h, m = parse_hm(shift_time)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    if isinstance(value, float):
        value = round(value, 9)          # drop binary-float noise (e.g. 58.333349999999996) before rounding
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def attendance_date(emp, ts: datetime) -> date:
    """R1: IST date of the punch-in; an overnight shift's early-morning punch-in belongs to the previous day."""
    start_hm, end_hm = parse_hm(emp["shift_start"]), parse_hm(emp["shift_end"])
    local = ts.astimezone(IST)
    if end_hm <= start_hm and (local.hour, local.minute) < end_hm:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    """R2. `day` is the ATTENDANCE date (not the punch's calendar date, which differs for overnight shifts)."""
    seconds = int((from_db(punch_in) - shift_datetime(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4, exact decimal arithmetic (no float noise before rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def shift_end_datetime(day: date, shift_start: str, shift_end: str):
    end = shift_datetime(day, shift_end)
    if parse_hm(shift_end) <= parse_hm(shift_start):
        end += timedelta(days=1)
    return end


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, day: date) -> int:
    """R3."""
    minutes = int((from_db(punch_out) - shift_end_datetime(day, shift_start, shift_end)).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def round_rate(numerator, denominator):
    if not denominator:
        return None
    return half_up(Decimal(str(numerator)) / Decimal(denominator) * 100, 4)


def derived_fields(emp, day: date, pin, pout):
    if pin is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, emp["shift_start"], day)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(pin, pout)
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(pout, emp["shift_start"], emp["shift_end"], day),
        "half_day": hours < 4.50,   # R5 on the already-rounded value
    }


def convert_instants(value):
    """Recursively turn BSON datetimes into epoch ms (used for history, whose shape varies)."""
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {k: convert_instants(v) for k, v in value.items()}
    if isinstance(value, list):
        return [convert_instants(v) for v in value]
    return value


def public_attendance(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        out[key] = to_ms(out.get(key))
    out["history"] = convert_instants(out.get("history") or [])   # seeded docs may omit history
    out["half_day"] = bool(out.get("half_day", False))             # ... or half_day
    out["late_minutes"] = out.get("late_minutes") or 0
    out["overtime_minutes"] = out.get("overtime_minutes") or 0
    out["work_hours"] = out.get("work_hours")
    return out


def get_employee(emp_code):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    return emp


def get_attendance(emp_code, day):
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not doc:
        raise HTTPException(404, "Attendance record not found")
    return doc


def weekday_expr(field="$date"):
    """Mon-Fri test on a YYYY-MM-DD string. $dayOfWeek: Sun=1 ... Sat=7, so Mon-Fri is 2..6."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d", "onError": None, "onNull": None}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def present_weight(prefix="", weekdays_only=False):
    """R5/R6/R7: 1 for present, 0.5 for a half day, 0 otherwise (optionally only on Mon-Fri)."""
    cond = {"$in": [f"${prefix}status", PRESENCE]}
    if weekdays_only:
        cond = {"$and": [cond, weekday_expr(f"${prefix}date")]}
    return {"$cond": [cond, {"$cond": [f"${prefix}half_day", 0.5, 1]}, 0]}


def late_flag(prefix=""):
    return {"$cond": [{"$gt": [f"${prefix}late_minutes", 0]}, 1, 0]}


# ------------------------------- indexes -----------------------------------

def create_indexes():
    client.admin.command("ping")
    e, a = db.employees, db.attendance_logs
    e.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    e.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    e.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    a.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")
    a.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_emp")
    a.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")


# -------------------------------- models -----------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    regularized_by: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=200)


# -------------------------------- health -----------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


# ------------------------------- employees ---------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    date_value(body.joined_on)
    if parse_hm(body.shift_start) == parse_hm(body.shift_end):
        raise unprocessable("shift_start must differ from shift_end")
    doc = body.model_dump()
    created = to_utc(now_ist())
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(created)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = to_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------- punch-in ----------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    ts = epoch_ms_to_ist(body.punched_at) if body.punched_at is not None else now_ist()   # validate input before touching the DB
    emp = get_employee(body.emp_code)
    day = attendance_date(emp, ts)
    doc = {
        "emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
        "punch_in": to_utc(ts), "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)   # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance already exists for this employee and date")
    return public_attendance(doc)


def build_attendance_query(emp_code, date_from, date_to, status):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        if status not in ALL_STATUSES:
            raise unprocessable("Invalid status")
        q["status"] = status
    if date_from:
        date_value(date_from)
    if date_to:
        date_value(date_to)
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from cannot be after date_to")
    if date_from:
        q.setdefault("date", {})["$gte"] = date_from
    if date_to:
        q.setdefault("date", {})["$lte"] = date_to
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = list(db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_attendance(x) for x in items], "total": total, "page": page, "page_size": page_size}


# ------------------------------- punch-out ---------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ts = epoch_ms_to_ist(body.punched_at) if body.punched_at is not None else now_ist()   # validate input before touching the DB
    emp = get_employee(body.emp_code)
    ts_utc = to_utc(ts)
    # Contract: the employee's most recent record whose punch_in is at or before punched_at.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "No matching punch-in record")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    elapsed = (ts_utc - to_utc(rec["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise unprocessable("Punch-out must be after punch-in and within 24 hours")
    calc = derived_fields(emp, date_value(rec["date"]), rec["punch_in"], ts_utc)
    # Atomic: only the first of several concurrent punch-outs matches punch_out == null.
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_utc, "work_hours": calc["work_hours"],
                  "overtime_minutes": calc["overtime_minutes"], "half_day": calc["half_day"]}})
    if result.matched_count != 1:
        raise HTTPException(409, "Already punched out")
    return public_attendance(db.attendance_logs.find_one({"_id": rec["_id"]}))


# ----------------------------- regularization ------------------------------

DERIVED_DEFAULTS = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}


def plan_regularization(emp, day: date, rec, body):
    """Pure computation: returns ($set fields, history entry). Raises 422 on invalid input."""
    new_status = body.status if body.status is not None else rec.get("status")
    new_pin = epoch_ms_to_ist(body.punch_in) if body.punch_in is not None else from_db(rec.get("punch_in"))
    new_pout = epoch_ms_to_ist(body.punch_out) if body.punch_out is not None else from_db(rec.get("punch_out"))

    if new_status in ("ABSENT", "LEAVE"):
        # A request must not provide punch times for an ABSENT/LEAVE record,
        # even when status is omitted and the existing record already has that status.
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("ABSENT/LEAVE cannot be supplied with punch times")
        new_pin = new_pout = None
    elif new_pin is None:
        raise unprocessable("Presence status requires punch_in")
    if new_pin is not None and attendance_date(emp, new_pin) != day:
        raise unprocessable("punch_in must fall on the attendance date")
    if new_pout is not None and (new_pout <= new_pin or (new_pout - new_pin).total_seconds() > 24 * 3600):
        raise unprocessable("Invalid punch_out interval")

    proposed = {"status": new_status,
                "punch_in": to_utc(new_pin) if new_pin else None,
                "punch_out": to_utc(new_pout) if new_pout else None}
    changes = {}
    for field, new_value in proposed.items():
        old_value = rec.get(field)
        if field == "status":
            if old_value != new_value:
                changes[field] = {"from": old_value, "to": new_value}
        elif to_ms(old_value) != to_ms(new_value):
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise unprocessable("Request does not change the record")

    calc = derived_fields(emp, day, proposed["punch_in"], proposed["punch_out"])
    for field, new_value in calc.items():
        old_value = rec.get(field, DERIVED_DEFAULTS[field])
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    entry = {"at": to_utc(now_ist()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    return {**proposed, **calc}, entry


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(emp_code: str, day: str, body: RegularizeRequest):
    day_d = date_value(day)
    emp = get_employee(emp_code)
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise unprocessable("ABSENT/LEAVE cannot be supplied with punch times")

    for attempt in range(MAX_CAS_RETRIES):
        rec = get_attendance(emp_code, day)
        try:
            fields, entry = plan_regularization(emp, day_d, rec, body)
        except HTTPException as exc:
            if attempt > 0 and exc.status_code == 422:
                # The record moved under us (e.g. an identical concurrent correction already applied):
                # the contract allows 409 for the loser, never 422.
                raise HTTPException(409, "Attendance changed concurrently; retry")
            raise
        # Optimistic concurrency: history is append-only, so its length is a version number.
        # If anyone changed the record since we read it, nothing matches and we re-read and retry,
        # so no history entry is ever overwritten or lost.
        result = db.attendance_logs.update_one(
            {"_id": rec["_id"], "status": rec.get("status"),
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
             "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}},
            {"$set": fields, "$push": {"history": entry}})
        if result.matched_count == 1:
            return public_attendance(db.attendance_logs.find_one({"_id": rec["_id"]}))
    raise HTTPException(409, "Attendance changed concurrently; retry")


# ---------------------------- analytics pipelines ---------------------------

def monthly_pipeline(emp_code, first, last):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": present_weight(weekdays_only=True)},   # R7: Mon-Fri only
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": late_flag()},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},       # all days incl. weekends
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    working_start = max(first, date_value(emp["joined_on"]))
    working_days = sum(1 for n in range((last - working_start).days + 1)
                       if (working_start + timedelta(days=n)).weekday() < 5) if working_start <= last else 0
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}
    present_days = half_up(agg.get("present_days", 0), 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present_days, "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": round_rate(present_days, working_days),
    }


def summary_pipeline(first, last, department):
    match = {"joined_on": {"$lte": last.isoformat()}}          # R9
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                     "pipeline": [{"$match": {"$expr": {"$and": [
                         {"$eq": ["$emp_code", "$$code"]},
                         {"$gte": ["$date", first.isoformat()]},
                         {"$lte": ["$date", last.isoformat()]}]}}}],
                     "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},   # keep employees with no logs
        {"$group": {"_id": "$department",
                    "headcount_codes": {"$addToSet": "$emp_code"},
                    "present_days": {"$sum": present_weight("logs.", weekdays_only=True)},
                    # $avg ignores nulls -> a straight average over RECORDS, not over employees
                    "avg_work_hours": {"$avg": {"$cond": [{"$in": ["$logs.status", PRESENCE]}, "$logs.work_hours", None]}},
                    "late_count": {"$sum": late_flag("logs.")},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
                      "present_days": 1, "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1,
                      "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.employees.aggregate(summary_pipeline(first, last, department)))
    for item in items:
        item["present_days"] = half_up(item["present_days"], 2)
        item["avg_work_hours"] = half_up(item.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(first, last, limit, department):
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:   # filter BEFORE ranking so ranks are within the department
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        # $rank: ties share a rank and the next rank is skipped (1,2,2,4); limit is applied AFTER ranking
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": 1, "_id": 1}},                       # tied rows ordered by emp_code
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department), allowDiskUse=True))
    return {"month": month, "items": items}


def half_up_rate_expr():
    """present_count / headcount * 100, rounded HALF-UP to 4 decimals, entirely in integer-valued arithmetic.

    Mongo's $round is ties-to-even and a float quotient can sit just under/over a true tie, so we avoid both:
    scaled = present * 100 * 10^4 / headcount; half-up = floor((2*present*10^6 + headcount) / (2*headcount)) / 10^4.
    present_count is a multiple of 0.5, so present*2e6 and headcount are exact integers (< 2^53) and the
    single division is correctly rounded; the floor can only be wrong for an exact integer quotient, which is correct.
    """
    return {"$divide": [
        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_count", 2000000]}, "$headcount"]},
                                {"$multiply": [2, "$headcount"]}]}},
        10000]}


def trend_pipeline(department, start: date, end: date):
    s, e = start.isoformat(), end.isoformat()
    start_dt = datetime(start.year, start.month, start.day)
    end_excl = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return [
        # Start from the department's employees (indexed), then pull only their logs in range.
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                     "pipeline": [{"$match": {"$expr": {"$and": [
                         {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", s]}, {"$lte": ["$date", e]}]}}}],
                     "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},   # guarantees >= 1 row for $densify
        {"$group": {"_id": "$logs.date",
                    "present_count": {"$sum": present_weight("logs.")},
                    "late_count": {"$sum": late_flag("logs.")}}},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "onNull": start_dt}}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        # one row per calendar day; the upper bound is made safe whether the server treats it as inclusive or not
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_excl]}}},
        {"$match": {"date": {"$lte": datetime(end.year, end.month, end.day)}}},
        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
        {"$addFields": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}},
                        "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2], }, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date_str"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", {"$literal": department}]}, {"$lte": ["$joined_on", "$$d"]}]}}},  # R9
            {"$count": "n"}], "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$addFields": {"date": "$date_str",
                        "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                                       half_up_rate_expr(), None]}}},
        # trailing 7 calendar days (shorter at the start); $avg skips null rates of non-working days
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = date_value(from_date), date_value(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise unprocessable("Invalid range: to must be >= from and range <= 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Unknown department")
    items = list(db.employees.aggregate(trend_pipeline(department, start, end), allowDiskUse=True))
    # Rate is already exact half-up 4dp (see half_up_rate_expr); the moving average is over those reported values.
    # half_up() only normalises the mean (drops float noise, rounds half-up).
    for item in items:
        item["present_count"] = half_up(item["present_count"], 2)
        item["attendance_rate"] = half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = half_up(item.get("moving_avg_7d"), 4)
    return {"department": department, "items": items}


# -------------------------------- explain -----------------------------------

def run_explain(endpoint: str, command: dict):
    collection = command.get("find") or command["aggregate"]
    out = db.command({"explain": command, "verbosity": "executionStats"})
    # explain output holds BSON types (Timestamp, ObjectId, datetime...) that plain JSON cannot encode
    raw = json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": raw}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        return run_explain(endpoint, {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
                            "skip": (page - 1) * page_size, "limit": page_size})
    if endpoint == "department_trend":
        if not department or from_date is None or to_date is None:
            raise unprocessable("department, from and to are required")
        start, end = date_value(from_date), date_value(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise unprocessable("Invalid range")
        return run_explain(endpoint, {"aggregate": "employees", "pipeline": trend_pipeline(department, start, end), "cursor": {}})
    if not month:
        raise unprocessable("month is required for this explain endpoint")
    first, last = month_bounds(month)
    if endpoint == "employee_monthly":
        if not emp_code:
            raise unprocessable("emp_code is required")
        return run_explain(endpoint, {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}})
    if endpoint == "department_summary":
        return run_explain(endpoint, {"aggregate": "employees", "pipeline": summary_pipeline(first, last, department), "cursor": {}})
    return run_explain(endpoint, {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(first, last, limit, department), "cursor": {}})