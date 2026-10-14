"""
Employee Attendance & Analytics API
Full implementation strictly adhering to openapi.yaml, DATA_MODEL.md, and PROBLEM_STATEMENT.
"""
import os
import math
import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any, Annotated
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, BeforeValidator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
import bson.json_util

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Math & Rounding Helpers (R4, R8)
# --------------------------------------------------------------------------- #
def round_half_up_2(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Strict Timestamp Validation & Epoch Helpers
# --------------------------------------------------------------------------- #
def validate_epoch_ms_strict(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer in epoch milliseconds.")
    if not (100_000_000_000 <= v <= 4_102_444_800_000):
        raise ValueError("Timestamp must be epoch milliseconds between 1e11 and 4.1e12.")
    return v


EpochMs = Annotated[int, BeforeValidator(validate_epoch_ms_strict)]


def ms_to_datetime_utc(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_shift_datetimes(shift_date_str: str, shift_start_str: str, shift_end_str: str):
    base_d = date.fromisoformat(shift_date_str)
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    eh_h, eh_m = map(int, shift_end_str.split(":"))

    start_ist = datetime(base_d.year, base_d.month, base_d.day, sh_h, sh_m, 0, tzinfo=IST)
    if (eh_h, eh_m) <= (sh_h, sh_m):
        end_d = base_d + timedelta(days=1)
    else:
        end_d = base_d
    end_ist = datetime(end_d.year, end_d.month, end_d.day, eh_h, eh_m, 0, tzinfo=IST)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def compute_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    punch_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    eh_h, eh_m = map(int, shift_end_str.split(":"))

    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    punch_time = (punch_ist.hour, punch_ist.minute, punch_ist.second)
    shift_end_time = (eh_h, eh_m, 0)

    if is_overnight and punch_time < shift_end_time:
        att_date = punch_ist.date() - timedelta(days=1)
    else:
        att_date = punch_ist.date()

    return att_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    diff_sec = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_sec > 600:
        return math.floor(diff_sec / 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    diff_sec = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_sec >= 1800:
        return math.floor(diff_sec / 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0:
        return 0.0
    return round_half_up_2(diff_sec / 3600.0)


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)

    for fld in ("punch_in", "punch_out", "created_at"):
        if isinstance(res.get(fld), datetime):
            res[fld] = dt_to_epoch_ms(res[fld])

    res.setdefault("work_hours", None)
    res.setdefault("late_minutes", 0)
    res.setdefault("overtime_minutes", 0)
    res.setdefault("half_day", False)
    res.setdefault("history", [])

    for h in res.get("history", []):
        if isinstance(h.get("at"), datetime):
            h["at"] = dt_to_epoch_ms(h["at"])
        changes = h.get("changes", {})
        for fld in ("punch_in", "punch_out"):
            if fld in changes:
                if isinstance(changes[fld].get("from"), datetime):
                    changes[fld]["from"] = dt_to_epoch_ms(changes[fld]["from"])
                if isinstance(changes[fld].get("to"), datetime):
                    changes[fld]["to"] = dt_to_epoch_ms(changes[fld]["to"])

    return res


# --------------------------------------------------------------------------- #
# Lifespan Handler & Indexes
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class PatchAttendanceIn(BaseModel):
    regularized_by: Optional[str] = Field(None, min_length=1, max_length=50)
    by: Optional[str] = None
    reason: str = Field(..., min_length=5, max_length=200)
    status: Optional[str] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None

    def get_by(self) -> str:
        val = self.regularized_by or self.by
        if not val:
            raise ValueError("Field regularized_by is required.")
        return val


# --------------------------------------------------------------------------- #
# Query Builders
# --------------------------------------------------------------------------- #
def build_attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status_val: Optional[str]):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be greater than date_to")
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_val:
        q["status"] = status_val
    return q


def build_employee_monthly_pipeline(emp_code: str, month: str):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}"}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                        "timezone": "+05:30"
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0.0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }
        }
    ]


def build_department_summary_pipeline(month: str, month_end_date: str, department: Optional[str] = None):
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", month_end_date]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "date": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                                    "timezone": "+05:30"
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$logs.day_of_week", 2]},
                                    {"$lte": ["$logs.day_of_week", 6]}
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ifNull": ["$logs.work_hours", 0.0]},
                            0.0
                        ]
                    }
                },
                "valid_record_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]


def build_leaderboard_pipeline(month: str, department: Optional[str] = None):
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$regex": f"^{month}"},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])
    return pipeline


def build_trend_pipeline(department: str, date_from: str, date_to: str):
    date_from_dt = datetime.fromisoformat(f"{date_from}T00:00:00+05:30")
    date_to_dt = datetime.fromisoformat(f"{date_to}T23:59:59+05:30")

    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": "$logs"},
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d", "timezone": "+05:30"}},
                "date_str": "$logs.date",
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "late_minutes": "$logs.late_minutes"
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "date_str": {"$first": "$date_str"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                }
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [date_from_dt, date_to_dt]
                }
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unreachable: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shift_start and shift_end must differ")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(now_utc)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for item in cursor:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = dt_to_epoch_ms(item["created_at"])
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid punch-in status")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    p_in_utc = ms_to_datetime_utc(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    att_date = compute_attendance_date(p_in_utc, emp["shift_start"], emp["shift_end"])
    s_start_utc, _ = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(p_in_utc, s_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    p_out_utc = ms_to_datetime_utc(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_utc}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already closed")

    p_in_utc = record["punch_in"]
    if p_in_utc.tzinfo is None:
        p_in_utc = p_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (p_out_utc - p_in_utc).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch out must be after punch in")
    if diff_sec > 86400:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch out cannot be more than 24h after punch in")

    att_date = record["date"]
    _, s_end_utc = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])

    w_hours = compute_work_hours(p_in_utc, p_out_utc)
    ot_min = compute_overtime(p_out_utc, s_end_utc)
    h_day = compute_half_day(w_hours)

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_utc,
                "work_hours": w_hours,
                "overtime_minutes": ot_min,
                "half_day": h_day,
            }
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record was concurrently updated")

    closed_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_doc(closed_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_val: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status_val)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    return {"items": [serialize_doc(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: PatchAttendanceIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    old_history = record.get("history", [])
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    changes: Dict[str, Any] = {}
    set_fields: Dict[str, Any] = {}

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    new_status = body.status if body.status is not None else old_status
    new_punch_in = ms_to_datetime_utc(body.punch_in) if body.punch_in is not None else old_punch_in
    new_punch_out = ms_to_datetime_utc(body.punch_out) if body.punch_out is not None else old_punch_out

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punches cannot be supplied for ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    elif new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        if new_punch_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in is required for present status")

    if new_punch_in and new_punch_out:
        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
        if new_punch_out.tzinfo is None:
            new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
        diff = (new_punch_out - new_punch_in).total_seconds()
        if diff <= 0 or diff > 86400:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in and within 24h")

    if new_punch_in:
        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in must stay on the record's attendance date")

    if body.status is not None and body.status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
        set_fields["status"] = new_status

    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
        set_fields["punch_in"] = new_punch_in

    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
        set_fields["punch_out"] = new_punch_out

    s_start_utc, s_end_utc = get_shift_datetimes(date, emp["shift_start"], emp["shift_end"])
    new_late = compute_late_minutes(new_punch_in, s_start_utc) if new_punch_in else 0

    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
        set_fields["late_minutes"] = new_late

    if new_punch_in and new_punch_out:
        new_work = compute_work_hours(new_punch_in, new_punch_out)
        new_ot = compute_overtime(new_punch_out, s_end_utc)
        new_half = compute_half_day(new_work)
    else:
        new_work = None
        new_ot = 0
        new_half = False

    if new_work != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work}
        set_fields["work_hours"] = new_work

    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
        set_fields["overtime_minutes"] = new_ot

    if new_half != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half}
        set_fields["half_day"] = new_half

    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes detected")

    history_entry = {
        "at": now_utc,
        "by": body.get_by(),
        "reason": body.reason,
        "changes": changes,
    }

    res = db.attendance_logs.update_one(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": len(old_history)}
        },
        {
            "$set": set_fields,
            "$push": {"history": history_entry}
        }
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent update conflict on history")

    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return serialize_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m_val = map(int, month.split("-"))
    next_m = date(year + 1, 1, 1) if m_val == 12 else date(year, m_val + 1, 1)
    month_end = next_m - timedelta(days=1)
    month_start = date(year, m_val, 1)

    join_d = date.fromisoformat(emp["joined_on"])
    period_start = max(month_start, join_d)

    working_days = 0
    cur = period_start
    while cur <= month_end:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if not res:
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": 0.0 if working_days > 0 else None
        }

    agg = res[0]
    p_days = round_half_up_2(agg["present_days"])
    att_pct = round_half_up_2((p_days / working_days * 100.0)) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": p_days,
        "leave_days": int(agg["leave_days"]),
        "late_count": int(agg["late_count"]),
        "total_late_minutes": int(agg["total_late_minutes"]),
        "total_overtime_minutes": int(agg["total_overtime_minutes"]),
        "attendance_pct": att_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None
):
    year, m_val = map(int, month.split("-"))
    next_m = date(year + 1, 1, 1) if m_val == 12 else date(year, m_val + 1, 1)
    month_end_date = (next_m - timedelta(days=1)).isoformat()

    pipeline = build_department_summary_pipeline(month, month_end_date, department)
    raw = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw:
        headcount = len(r["employees_set"])
        if headcount == 0:
            continue
        v_count = r["valid_record_count"]
        avg_wh = round_half_up_2(r["total_work_hours"] / v_count) if v_count > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": round_half_up_2(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"])
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50)
):
    pipeline = build_leaderboard_pipeline(month, department)
    pipeline.append({"$match": {"rank": {"$lte": limit}}})
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to cannot be before from")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    dept_employees = list(db.employees.find({"department": department}))
    if not dept_employees:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    pipeline = build_trend_pipeline(department, from_date, to_date)
    raw = list(db.employees.aggregate(pipeline))

    # Map raw rows by calendar date string
    log_map = {}
    for r in raw:
        d_str = r["_id"].strftime("%Y-%m-%d")
        log_map[d_str] = r

    items = []
    rates_for_window: List[Optional[float]] = []

    cur = d_from
    while cur <= d_to:
        d_str = cur.isoformat()
        is_working = cur.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= d_str)

        r_data = log_map.get(d_str, {})
        p_count = round_half_up_2(r_data.get("present_count", 0.0))
        l_count = int(r_data.get("late_count", 0))

        if is_working and hc > 0:
            rate = round_half_up_4(p_count / hc)
        else:
            rate = None

        rates_for_window.append(rate)

        # 7-day window including current and up to 6 preceding rows inside the range
        window = [x for x in rates_for_window[-7:] if x is not None]
        mov_avg = round_half_up_4(sum(window) / len(window)) if window else None

        items.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": p_count,
            "late_count": l_count,
            "attendance_rate": rate,
            "moving_avg_7d": mov_avg
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain Endpoint (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    month: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_val: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
    limit: int = 10,
):
    m_str = month or "2026-07"
    year, m_val = map(int, m_str.split("-"))
    next_m = date(year + 1, 1, 1) if m_val == 12 else date(year, m_val + 1, 1)
    month_end_date = (next_m - timedelta(days=1)).isoformat()

    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code, date_from, date_to, status_val)
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        plan = cursor.explain()
        col = "attendance_logs"

    elif endpoint == "employee_monthly":
        pipe = build_employee_monthly_pipeline(emp_code or "EMP0001", m_str)
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        col = "attendance_logs"

    elif endpoint == "department_summary":
        pipe = build_department_summary_pipeline(m_str, month_end_date, department)
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        col = "employees"

    elif endpoint == "late_leaderboard":
        pipe = build_leaderboard_pipeline(m_str, department)
        pipe.append({"$match": {"rank": {"$lte": limit}}})
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        col = "attendance_logs"

    elif endpoint == "department_trend":
        dept = department or "Engineering"
        f_d = from_date or "2026-07-01"
        t_d = to_date or "2026-07-31"
        pipe = build_trend_pipeline(dept, f_d, t_d)
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        col = "employees"

    else:
        raise HTTPException(status_code=400, detail=f"Unknown explain endpoint: {endpoint}")

    return {
        "endpoint": endpoint,
        "collection": col,
        "explain": json.loads(bson.json_util.dumps(plan))
    }