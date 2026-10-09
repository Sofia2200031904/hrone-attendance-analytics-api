# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** for employee attendance, punch-in/punch-out, manual corrections with an audit trail, analytics, and query explanations. It implements the HROne assignment contract in `openapi.yaml`.

## Features

- Employee creation and paginated employee and attendance listing.
- Attendance punch-in and punch-out with state, timestamp, and duplicate-operation validation.
- Manual attendance corrections with recalculated derived fields and append-only audit history.
- Monthly employee summaries, department summaries, late-comer leaderboard, and daily department trends, calculated with MongoDB aggregation pipelines.
- MongoDB index creation and explain plans for the five analytics/list query families.
- Interactive Swagger documentation and MongoDB-backed automated tests.

Attendance rules include the configured late-arrival grace period, overnight shifts, half days, overtime, and working-day calculations. See `openapi.yaml` and `DECISIONS.md` for the precise contract and decisions.

## Technology

- Python 3.11 or newer
- FastAPI and Pydantic
- MongoDB 6.0 or newer with PyMongo
- Pytest and FastAPI `TestClient`

## Project structure

```text
hrone-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── tests/
│   ├── conftest.py
│   └── test_api.py
├── .env.example
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── openapi.yaml
├── README.md
├── requirements.txt
├── REVIEW.md
└── sample_seed.py
```

The assignment statement and local `.env` file are intentionally excluded from Git.

## Setup and run (Windows PowerShell)

Prerequisites: Python 3.11 or newer and MongoDB 6.0 or newer, running locally or reachable through a MongoDB URI.

```powershell
git clone https://github.com/Sofia2200031904/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with the MongoDB settings for your environment:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Environment variables take precedence over `.env`. The API creates its indexes at startup. Start the service with:

```powershell
uvicorn app.main:app --port 8000
```

Open Swagger UI at <http://127.0.0.1:8000/docs>, ReDoc at <http://127.0.0.1:8000/redoc>, or the OpenAPI JSON at <http://127.0.0.1:8000/openapi.json>. `GET /health` checks API and MongoDB readiness. The API does not provide a homepage at `/`.

To load sample records, run `python sample_seed.py` **only after pointing `.env` at a disposable database**. The seeder replaces the contents of both configured collections.

## API endpoints

The implementation exposes these 12 method/path operations across 11 API paths:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | API and MongoDB readiness |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees with optional department filter and pagination |
| `POST` | `/attendance/punch-in` | Record a punch-in |
| `POST` | `/attendance/punch-out` | Record a punch-out |
| `GET` | `/attendance` | List attendance with filters and pagination |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| `GET` | `/analytics/departments/summary` | Department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard for a month |
| `GET` | `/analytics/departments/{department}/trend` | Daily department attendance trend |
| `GET` | `/admin/explain/{endpoint}` | Explain a supported query or aggregation |

See `openapi.yaml` for exact request/response schemas, parameters, validation rules, status codes, and business rules.

## Tests

Tests use a fresh randomly named `hrone_test_<id>` database for each test and drop only that generated database afterward. They refuse to target a configured application database. Start MongoDB, then run:

```powershell
python -m pytest -q
```

Set `TEST_MONGO_URI` to use a separate MongoDB test server. The current suite has 13 MongoDB-backed integration tests. The suite passed locally on MongoDB 8.2.3; MongoDB 7 was not available for direct local verification.

## Design and review documents

- `openapi.yaml` — authoritative API contract and business rules.
- `DATA_MODEL.md` — MongoDB collections, fields, and sample data shape.
- `DECISIONS.md` — implementation decisions and rationale.
- `REVIEW.md` — review findings and verification notes.

## Security and submission

Never commit `.env`, credentials, or private records. `.gitignore` excludes `.env` variants (except the safe `.env.example`) and the assignment statement. No deployment URL is required by the assignment.

**Public repository:** <https://github.com/Sofia2200031904/hrone-attendance-analytics-api>
