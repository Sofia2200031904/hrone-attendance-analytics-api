# Employee Attendance & Analytics API

FastAPI and MongoDB service implementing the HROne assignment contract for employee attendance, manual regularization with an audit trail, analytics, and MongoDB query explanations.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (HROne's grader uses MongoDB 7)

## Run on Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env with your own local or Atlas connection settings.
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the process environment or `.env`; process environment values take precedence. `.env` is ignored by Git. The API creates its indexes at startup. Open Swagger UI at <http://127.0.0.1:8000/docs>.

To load the supplied sample records, run `python sample_seed.py` only after configuring a disposable database. The seeder replaces documents in both configured collections.

The service exposes `GET /health`; employee create/list; attendance punch-in, punch-out, list, and regularization; employee monthly summary; department summary; late leaderboard; department daily trend; and `GET /admin/explain/{endpoint}`. See `openapi.yaml` for the complete request and response contract, parameters, and status codes.

## Tests

The pytest suite uses a fresh database named `hrone_test_<random-id>` for each test and drops only that generated database afterward. It refuses to target a configured application database. Start MongoDB, then run:

```powershell
python -m pytest -q
```

Set `TEST_MONGO_URI` to point the tests at another MongoDB instance. Verified locally on MongoDB 8.2.3: all 13 integration tests pass, including the five explain plans and live request checks. The test data includes the supplied sample and focused edge cases. The assignment grader uses MongoDB 7; that exact server version was not available for local verification.

## Project files

- `app/main.py` — all API, validation, database, and aggregation code
- `tests/` — MongoDB-backed endpoint and business-rule integration tests
- `openapi.yaml` — authoritative HTTP and business-rule contract
- `DATA_MODEL.md` — MongoDB collections and stored document shapes
- `REVIEW.md`, `DECISIONS.md` — starter review and design rationale
- `sample_data/`, `sample_seed.py` — sample fixtures and loader

No credentials, `.env`, or Dockerfile belong in the submission. Keep the assignment statement out of the public repository; `.gitignore` excludes it.
