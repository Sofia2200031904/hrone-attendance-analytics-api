from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.main import IST, derive


def ms(y, m, d, hour, minute, second=0):
    return int(datetime(y, m, d, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def employee(code="EMP1001", department="Engineering", joined="2026-01-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": code, "email": f"{code.lower()}@example.com", "department": department,
            "shift_start": start, "shift_end": end, "joined_on": joined}


def seed_employee(db, **kwargs):
    doc = employee(**kwargs)
    doc["created_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.employees.insert_one(doc)
    return doc


def seed_log(db, code, day, status="PRESENT", late=0, hours=8.0, half=False, overtime=0):
    db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": None, "punch_out": None,
                                   "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half})


def test_health_and_employee_crud_pagination(client, db):
    assert client.get("/health").json() == {"status": "ok"}
    first = client.post("/employees", json=employee(code="EMP1001"))
    assert first.status_code == 201
    assert isinstance(first.json()["created_at"], int)
    assert "_id" not in first.json()
    assert client.post("/employees", json=employee(code="EMP1001")).status_code == 409
    assert client.post("/employees", json=employee(code="bad")).status_code == 422
    for i in range(1002, 1005):
        client.post("/employees", json=employee(code=f"EMP{i:04d}", department="Sales"))
    page = client.get("/employees", params={"department": "Sales", "page": 1, "page_size": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert [x["emp_code"] for x in page["items"]] == ["EMP1002", "EMP1003"]


def test_punch_in_out_validation_and_atomicity(client, db):
    seed_employee(db)
    instant = ms(2026, 7, 6, 9, 40)
    body = {"emp_code": "EMP1001", "punched_at": instant}
    response = client.post("/attendance/punch-in", json=body)
    assert response.status_code == 201
    assert response.json()["late_minutes"] == 0
    assert response.json()["date"] == "2026-07-06"
    assert client.post("/attendance/punch-in", json=body).status_code == 409
    assert client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1783323600}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1783323600000.0}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": None}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": instant}).status_code == 422
    closed = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 18, 30)})
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 8.83
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 18, 31)}).status_code == 409


def test_concurrent_punch_in_and_out_have_single_winner(client, db):
    seed_employee(db)
    pin = {"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.post("/attendance/punch-in", json=pin).status_code, range(8)))
    assert results.count(201) == 1
    assert results.count(409) == 7
    pout = {"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.post("/attendance/punch-out", json=pout).status_code, range(8)))
    assert results.count(200) == 1
    assert results.count(409) == 7


def test_overnight_punch_and_rules(client, db):
    seed_employee(db, code="EMP1005", start="22:00", end="06:00")
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP1005", "punched_at": ms(2026, 7, 7, 2, 0)})
    assert response.status_code == 201 and response.json()["date"] == "2026-07-06"
    out = client.post("/attendance/punch-out", json={"emp_code": "EMP1005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert out.status_code == 200
    assert out.json()["overtime_minutes"] == 40
    assert out.json()["work_hours"] == 4.67
    assert derive(datetime.fromtimestamp(ms(2026, 7, 6, 9, 30) / 1000, timezone.utc),
                  datetime.fromtimestamp(ms(2026, 7, 6, 14, 0, 18) / 1000, timezone.utc),
                  {"shift_start": "09:30", "shift_end": "18:30"}, "2026-07-06")["work_hours"] == 4.51


def test_punch_out_requires_open_record_and_enforces_24_hour_limit(client, db):
    seed_employee(db)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP1001"}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 9, 30)})
    too_late = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 7, 9, 30, 1)})
    assert too_late.status_code == 422
    exactly_24 = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 7, 9, 30)})
    assert exactly_24.status_code == 200


def test_regularization_audit_and_concurrent_edit(client, db):
    seed_employee(db)
    pin = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 10, 5)}).json()
    url = "/attendance/EMP1001/2026-07-06"
    correction = {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin"}
    updated = client.patch(url, json=correction)
    assert updated.status_code == 200
    assert updated.json()["late_minutes"] == 0
    hist = updated.json()["history"]
    assert len(hist) == 1 and hist[0]["changes"]["punch_in"] == {"from": pin["punch_in"], "to": correction["punch_in"]}
    assert client.patch(url, json=correction).status_code == 422
    assert client.patch("/attendance/EMP1001/2026-07-08", json=correction).status_code == 404
    assert client.patch(url, json={"status": "ABSENT", "reason": "mark absent", "regularized_by": "hr"}).json()["punch_in"] is None
    assert client.patch(url, json={"status": "LEAVE", "punch_in": ms(2026, 7, 6, 9, 0), "reason": "leave entry", "regularized_by": "hr"}).status_code == 422


def test_regularization_supports_legacy_missing_history(client, db):
    seed_employee(db)
    db.attendance_logs.insert_one({"emp_code": "EMP1001", "date": "2026-07-06", "status": "PRESENT",
                                   "punch_in": datetime.fromtimestamp(ms(2026, 7, 6, 10, 5) / 1000, timezone.utc),
                                   "punch_out": None, "work_hours": None, "late_minutes": 35, "overtime_minutes": 0})
    response = client.patch("/attendance/EMP1001/2026-07-06", json={"punch_in": ms(2026, 7, 6, 9, 28),
                         "reason": "legacy repair", "regularized_by": "hr"})
    assert response.status_code == 200, response.text
    assert len(response.json()["history"]) == 1


def test_concurrent_regularizations_preserve_history(client, db):
    seed_employee(db)
    client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": ms(2026, 7, 6, 10, 5)})
    url = "/attendance/EMP1001/2026-07-06"
    changes = [
        {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "fix source one", "regularized_by": "hr.one"},
        {"punch_in": ms(2026, 7, 6, 9, 29), "reason": "fix source two", "regularized_by": "hr.two"},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda body: client.patch(url, json=body).status_code, changes))
    assert responses.count(200) >= 1
    assert all(code in (200, 409) for code in responses)
    stored = db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-07-06"})
    assert len(stored["history"]) == responses.count(200)


def test_attendance_listing_filters_and_validation(client, db):
    seed_employee(db)
    seed_log(db, "EMP1001", "2026-07-06", "PRESENT")
    seed_log(db, "EMP1001", "2026-07-07", "LEAVE")
    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    result = client.get("/attendance", params={"status": "LEAVE", "date_from": "2026-07-06"}).json()
    assert result["total"] == 1 and result["items"][0]["date"] == "2026-07-07"
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422


def test_analytics_sample_fixtures_and_edge_cases(client, db):
    # Six employees and nine supplied records, with the two legacy fields intentionally absent.
    from bson import json_util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    db.employees.insert_many(json_util.loads((root / "sample_data" / "employees.json").read_text()))
    db.attendance_logs.insert_many(json_util.loads((root / "sample_data" / "attendance_logs.json").read_text()))

    monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.0,
                       "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
                       "attendance_pct": 8.6957}
    assert client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-01"}).json()["working_days"] == 0
    sales = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()["items"][0]
    assert sales == {"department": "Sales", "headcount": 2, "present_days": 1.5, "avg_work_hours": 6.29,
                     "late_count": 1, "total_late_minutes": 35, "leave_count": 1, "on_duty_count": 0}
    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"]
    assert [(x["rank"], x["emp_code"], x["total_late_minutes"]) for x in leaderboard] == [(1, "EMP0003", 35)]
    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()["items"]
    assert len(trend) == 7
    assert [row["date"] for row in trend] == [f"2026-07-{day:02d}" for day in range(6, 13)]
    assert trend[0]["headcount"] == 1 and trend[0]["present_count"] == 1 and trend[0]["attendance_rate"] == 1.0
    assert trend[1]["headcount"] == 1 and trend[1]["present_count"] == 1
    assert trend[5]["is_working_day"] is False and trend[5]["attendance_rate"] is None
    too_long = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-10-01"})
    assert too_long.status_code == 422


def test_synthetic_analytics_weekends_no_logs_joiners_ties(client, db):
    seed_employee(db, code="EMP1001", joined="2026-07-06")
    seed_employee(db, code="EMP1002", joined="2026-07-06")
    seed_employee(db, code="EMP1003", joined="2026-07-08")
    seed_log(db, "EMP1001", "2026-07-06", late=35, hours=6.0)
    seed_log(db, "EMP1001", "2026-07-07", late=35, hours=8.0)
    seed_log(db, "EMP1002", "2026-07-06", late=70, hours=10.0)
    seed_log(db, "EMP1003", "2026-07-11", status="PRESENT", late=1, hours=1.0, half=True)
    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert summary["headcount"] == 3
    assert summary["present_days"] == 3.0  # weekend record does not count
    assert summary["avg_work_hours"] == 6.25
    ranked = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in ranked] == [(1, "EMP1001"), (1, "EMP1002")]
    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()["items"]
    assert len(trend) == 7
    assert [x["headcount"] for x in trend] == [2, 2, 3, 3, 3, 3, 3]
    assert trend[5]["present_count"] == 0.5 and trend[5]["attendance_rate"] is None
    assert client.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-01"}).json()["working_days"] == 0
    no_log_day = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-09", "to": "2026-07-09"}).json()["items"][0]
    assert no_log_day["headcount"] == 3 and no_log_day["present_count"] == 0 and no_log_day["attendance_rate"] == 0.0
    weekend = client.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert weekend["present_days"] == 0.0 and weekend["total_late_minutes"] == 1


def test_explain_endpoints_validate_and_return_execution_stats(client, db):
    seed_employee(db)
    seed_log(db, "EMP1001", "2026-07-06")
    endpoints = [
        ("attendance_list", {}), ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-07"}),
    ]
    for name, params in endpoints:
        response = client.get(f"/admin/explain/{name}", params=params)
        assert response.status_code == 200, response.text
        explain = response.json()["explain"]
        def walk(node):
            if isinstance(node, dict):
                yield node
                for child in node.values():
                    yield from walk(child)
            elif isinstance(node, list):
                for child in node:
                    yield from walk(child)
        stats = [node["executionStats"] for node in walk(explain) if "executionStats" in node]
        assert stats, (name, explain.keys())
        assert all(item["nReturned"] >= 0 for item in stats)
        if name in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
            stages = [node.get("stage") for node in walk(explain) if node.get("stage")]
            assert any(stage in {"IXSCAN", "EXPRESS_IXSCAN"} for stage in stages), (name, stages)
            assert "COLLSCAN" not in stages, (name, stages)
    indexes = {index["name"] for index in db.attendance_logs.list_indexes()}
    assert {"uq_attendance_day", "attendance_date_employee", "attendance_employee_status"} <= indexes
    employee_indexes = {index["name"] for index in db.employees.list_indexes()}
    assert {"uq_employee_code", "department_joined", "employee_joined_on"} <= employee_indexes
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422


def test_range_boundaries_and_empty_analytics(client, db):
    seed_employee(db)
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]["headcount"] == 1
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"] == []
