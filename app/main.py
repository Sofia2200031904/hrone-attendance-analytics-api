"""Employee attendance API for the HROne engineering assignment.

Run from the project root with ``uvicorn app.main:app --port 8000``.
MongoDB stores instants as UTC BSON dates; the HTTP API exposes epoch milliseconds.
"""
from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # existing environment variables take precedence over .env
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")

client: MongoClient
db: Any


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client, db
    client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_status")
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def bad(detail: str) -> None:
    raise HTTPException(status_code=422, detail=detail)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        bad("Date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        bad("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    return first, date(year, month, calendar.monthrange(year, month)[1])


def whole_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def epoch_datetime(ms: int) -> datetime:
    if type(ms) is not int or ms < 100_000_000_000 or ms > MAX_EPOCH_MS:
        bad("Timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        bad("Timestamp is out of range")


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def rounded(value: float | Decimal, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def shift_start_utc(start: str, attendance_day: str) -> datetime:
    day = parse_date(attendance_day)
    return datetime.combine(day, time.fromisoformat(start), IST).astimezone(UTC)


def shift_end_utc(start_utc: datetime, start: str, end: str, attendance_day: str) -> datetime:
    day = parse_date(attendance_day)
    end_day = day + (timedelta(days=1) if end <= start else timedelta())
    return datetime.combine(end_day, time.fromisoformat(end), IST).astimezone(UTC)


def derive(punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict, day: str) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    start = shift_start_utc(employee["shift_start"], day)
    late_delta = int((punch_in - start).total_seconds())
    late = late_delta // 60 if late_delta > 600 else 0
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = rounded(Decimal(seconds) / Decimal(3600))
    end = shift_end_utc(start, employee["shift_start"], employee["shift_end"], day)
    over_delta = int((punch_out - end).total_seconds())
    overtime = over_delta // 60 if over_delta >= 1800 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}


def api_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result["late_minutes"] = result.get("late_minutes") or 0
    result["overtime_minutes"] = result.get("overtime_minutes") or 0
    result["half_day"] = result.get("half_day", False)
    result["history"] = result.get("history", [])
    result["punch_in"] = to_ms(result["punch_in"])
    result["punch_out"] = to_ms(result["punch_out"])
    result["history"] = [
        {"at": to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"],
         "changes": {key: {"from": to_ms(ch["from"]) if key in ("punch_in", "punch_out") else ch["from"],
                           "to": to_ms(ch["to"]) if key in ("punch_in", "punch_out") else ch["to"]}
                     for key, ch in entry["changes"].items()}}
        for entry in result["history"]
    ]
    return result


def api_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["created_at"] = to_ms(result["created_at"])
    return result


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictBody):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictBody):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            try:
                epoch_datetime(value)
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc
        return value

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at may be omitted but cannot be null")
        return self


class PunchOutIn(StrictBody):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            try:
                epoch_datetime(value)
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc
        return value

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at may be omitted but cannot be null")
        return self


class RegularizeIn(StrictBody):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            try:
                epoch_datetime(value)
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc
        return value

    @model_validator(mode="after")
    def reject_explicit_null_changes(self):
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} may be omitted but cannot be null")
        return self


def page_bounds(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 100:
        bad("page must be >= 1 and page_size must be between 1 and 100")


def presence_fraction() -> dict:
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                      {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}


def month_filter(month: str) -> tuple[date, date, dict]:
    first, last = parse_month(month)
    return first, last, {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def employee_month_pipeline(emp_code: str, month: str) -> list:
    first, last, match = month_filter(month)
    return [
        {"$match": {"emp_code": emp_code, **match}},
        {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$lte": ["$_weekday", 6]}, {"$in": ["$status", list(PRESENCE)]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]


def working_days_in_month_pipeline(emp_code: str, joined_on: str, month: str) -> list:
    first, last = parse_month(month)
    begin = max(first, parse_date(joined_on))
    return [
        {"$match": {"emp_code": emp_code}},
        {"$project": {"_id": 0, "working_days": {"$size": {"$filter": {
            "input": {"$map": {"input": {"$cond": [{"$lte": [datetime.combine(begin, time(), UTC), datetime.combine(last, time(), UTC)]},
                {"$range": [0, {"$add": [{"$dateDiff": {"startDate": datetime.combine(begin, time(), UTC), "endDate": datetime.combine(last, time(), UTC), "unit": "day"}}, 1]}]}, []]},
                "as": "offset", "in": {"$dateAdd": {"startDate": datetime.combine(begin, time(), UTC), "unit": "day", "amount": "$$offset"}}}},
            "as": "day", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$$day"}, 6]}]}}}}}},
    ]


def department_pipeline(month: str, department: Optional[str]) -> list:
    first, last = parse_month(month)
    employee_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                     "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                                                               {"$gte": ["$date", first.isoformat()]},
                                                               {"$lte": ["$date", last.isoformat()]}]}}}],
                     "as": "_logs"}},
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_weekday": {"$cond": [{"$ne": ["$_logs.date", None]},
                                           {"$dayOfWeek": {"$dateFromString": {"dateString": "$_logs.date"}}}, 0]}}},
        {"$group": {"_id": {"department": "$department", "employee": "$emp_code"},
                    "department": {"$first": "$department"},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$lte": ["$_weekday", 6]}, {"$in": ["$_logs.status", list(PRESENCE)]}]}, {"$cond": [{"$eq": ["$_logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "_work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$_logs.status", list(PRESENCE)]}, {"$ne": ["$_logs.work_hours", None]}]}, "$_logs.work_hours", 0]}},
                    "_work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$_logs.status", list(PRESENCE)]}, {"$ne": ["$_logs.work_hours", None]}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$_logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$_logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
                    "_work_hours_sum": {"$sum": "$_work_hours_sum"}, "_work_hours_count": {"$sum": "$_work_hours_count"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$_work_hours_count", 0]}, {"$divide": ["$_work_hours_sum", "$_work_hours_count"]}, None]},
                      "late_count": 1, "total_late_minutes": 1,
                      "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list:
    first, last = parse_month(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, start: date, end: date) -> list:
    start_s = start.isoformat()
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_seed": {"$literal": True},
                       "_days": {"$map": {"input": {"$range": [0, (end-start).days + 1]}, "as": "offset",
                                           "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start_s}}, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$_days"},
        {"$set": {"_date": "$_days", "date": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"},
        ], "as": "_hc"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
            {"$match": {"_employee.department": department}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        ], "as": "_logs"}},
        {"$set": {"date": {"$dateToString": {"date": "$_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                  "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_date"}, 2]}, {"$lte": [{"$dayOfWeek": "$_date"}, 6]}]}}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.n", 0]}, 0]},
                  "present_count": {"$ifNull": [{"$arrayElemAt": ["$_logs.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$_logs.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_date": 1}, "output": {"_moving": {"$avg": {"$ifNull": ["$attendance_rate", None]}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": "$_moving"}},
        {"$sort": {"date": 1}},
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = [api_employee(d) for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = whole_seconds(epoch_datetime(body.punched_at)) if body.punched_at is not None else now_seconds()
    ist = ts.astimezone(IST)
    shift_start_min = int(emp["shift_start"][:2]) * 60 + int(emp["shift_start"][3:])
    shift_end_min = int(emp["shift_end"][:2]) * 60 + int(emp["shift_end"][3:])
    punch_min = ist.hour * 60 + ist.minute
    base = ist.date() - timedelta(days=1) if shift_end_min <= shift_start_min and punch_min < shift_end_min else ist.date()
    day = base.isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, **derive(ts, None, emp, day), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return api_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = whole_seconds(epoch_datetime(body.punched_at)) if body.punched_at is not None else now_seconds()
    filt = {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}
    record = db.attendance_logs.find_one(filt, sort=[("punch_in", DESCENDING), ("date", DESCENDING)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    if punched_at <= record["punch_in"] or punched_at - record["punch_in"] > timedelta(hours=24):
        bad("punched_at must be after punch_in and no more than 24 hours later")
    derived = derive(record["punch_in"], punched_at, emp, record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            bad("Invalid status")
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from)
            q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            bad("date_from must be on or before date_to")
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = date
    parse_date(day)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if old is None or db.employees.find_one({"emp_code": emp_code}) is None:
        raise HTTPException(404, "employee or attendance record not found")
    emp = db.employees.find_one({"emp_code": emp_code})
    new_status = body.status if body.status is not None else old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            bad("ABSENT and LEAVE cannot include punch times")
        new_in = new_out = None
    else:
        new_in = whole_seconds(epoch_datetime(body.punch_in)) if body.punch_in is not None else old.get("punch_in")
        new_out = whole_seconds(epoch_datetime(body.punch_out)) if body.punch_out is not None else old.get("punch_out")
        if new_in is None:
            bad("presence status requires punch_in")
        local = new_in.astimezone(IST)
        si, se = emp["shift_start"], emp["shift_end"]
        overnight = se <= si
        mins = local.hour * 60 + local.minute
        endmins = int(se[:2]) * 60 + int(se[3:])
        record_day = local.date() - timedelta(days=1) if overnight and mins < endmins else local.date()
        if record_day.isoformat() != day:
            bad("punch_in must belong to the attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            bad("punch_out must be after punch_in and within 24 hours")
    derived = derive(new_in, new_out, emp, day)
    proposed = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        before = old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None)
        after = proposed.get(key)
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        bad("regularization must change at least one field")
    history_entry = {"at": now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_guard = {"history": old["history"]} if "history" in old else {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], **history_guard},
        {"$set": proposed, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently; retry")
    return api_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    day_result = next(db.employees.aggregate(working_days_in_month_pipeline(emp_code, emp["joined_on"], month)), {})
    working = day_result.get("working_days", 0)
    result = next(db.attendance_logs.aggregate(employee_month_pipeline(emp_code, month)), {})
    present = result.get("present_days", 0)
    pct = rounded(Decimal(str(present)) / Decimal(working) * 100, 4) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": rounded(present),
            "leave_days": result.get("leave_days", 0), "late_count": result.get("late_count", 0),
            "total_late_minutes": result.get("total_late_minutes", 0), "total_overtime_minutes": result.get("total_overtime_minutes", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    parse_month(month)
    items = list(db.employees.aggregate(department_pipeline(month, department)))
    for item in items:
        if item["headcount"] == 0:
            continue
        item["present_days"] = rounded(item["present_days"])
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = rounded(item["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, department, limit)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date), parse_date(to_date)
    if end < start:
        bad("to must be on or after from")
    if (end - start).days + 1 > 92:
        bad("date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, start, end)))
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = rounded(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = rounded(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


def explain_command(collection: str, pipeline: Optional[list] = None, query: Optional[dict] = None,
                    sort: Optional[list] = None, skip: int = 0, limit: int = 0) -> dict:
    if pipeline is not None:
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        stages = [{"$match": query or {}}]
        if sort:
            stages.append({"$sort": dict(sort)})
        if skip:
            stages.append({"$skip": skip})
        if limit:
            stages.append({"$limit": limit})
        stages.append({"$project": {"_id": 0}})
        cmd = {"aggregate": collection, "pipeline": stages, "cursor": {}}
    return db.command("explain", cmd, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100)):
    page_bounds(page, page_size)
    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if status is not None:
            if status not in STATUSES:
                bad("Invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                parse_date(date_from); q["date"]["$gte"] = date_from
            if date_to:
                parse_date(date_to); q["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                bad("date_from must be on or before date_to")
        collection = "attendance_logs"
        result = explain_command(collection, query=q, sort=[("date", -1), ("emp_code", 1)], skip=(page - 1) * page_size, limit=page_size)
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            bad("emp_code and month are required")
        if db.employees.find_one({"emp_code": emp_code}) is None:
            raise HTTPException(404, "employee not found")
        collection = "attendance_logs"; result = explain_command(collection, employee_month_pipeline(emp_code, month))
    elif endpoint == "department_summary":
        if month is None:
            bad("month is required")
        collection = "employees"; result = explain_command(collection, department_pipeline(month, department))
    elif endpoint == "late_leaderboard":
        if month is None:
            bad("month is required")
        collection = "attendance_logs"; result = explain_command(collection, leaderboard_pipeline(month, department, limit))
    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            bad("department, from and to are required")
        start, end = parse_date(from_date), parse_date(to_date)
        if end < start or (end - start).days + 1 > 92:
            bad("invalid trend range")
        collection = "employees"; result = explain_command(collection, trend_pipeline(department, start, end))
    else:
        bad("Unknown endpoint")
    return {"endpoint": endpoint, "collection": collection,
            "explain": json_util.loads(json_util.dumps(result))}
