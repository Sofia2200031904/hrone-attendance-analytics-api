# Starter code review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the punch-in's existing date/timezone and returns negative or wrong values for overnight and pre-start punches. | A 22:00–06:00 shift or an early punch can be compared to the wrong shift start. | Replaced with UTC instant comparison against the attendance date's IST shift start. |
| 2 | `compute_late_minutes` | Grace is evaluated after converting duration to integer minutes; fractional minute behavior and second truncation are not explicit. | Punch exactly 10:00:01 late must be late with 10 minutes. | Truncate instants to seconds and apply strict `> 600` seconds before flooring minutes. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even, not required half-up rounding. | A duration exactly halfway at the second decimal boundary rounds down unexpectedly. | Decimal `ROUND_HALF_UP` rounding. |
| 4 | `compute_overtime` | Shift end is always placed on the same date and overtime has no 30-minute minimum. | Overnight 22:00–06:00 punch-out at 06:40 gets a wrong result; 29 minutes should be zero. | Compute next-day end for overnight shifts and apply the 30-minute threshold. |
| 5 | `health` | Always reports ready without checking MongoDB. | Stop MongoDB and `/health` still returns 200. | Ping MongoDB and return 503 on connection failure. |
| 6 | `create_employee` | Check-then-insert duplicate logic races, no database unique index, and `created_at` is naive. | Concurrent duplicate requests can both pass; timestamp serialization has no defined zone. | Create the unique index at startup, map duplicate-key errors to 409, store UTC BSON datetime. |
| 7 | `EmployeeIn` | Missing contract validation, including employee code, email, field lengths, date, shift format and differing shift times. | Invalid fields accepted or malformed values fail later in calculations. | Pydantic constraints and date/shift validation. |
| 8 | `list_employees` | `skip=page * page_size` skips page one; `total` ignores department; no explicit sort or page bounds. | First page omits its first 20 records and department total is global. | Apply `(page-1)*page_size`, filtered count, stable sort and bounds. |
| 9 | `punch_in` | Does not reject unknown employees before dereference; uses local naive `fromtimestamp`, falsey timestamp fallback, and UTC date rather than IST/overnight attendance date. | Unknown code produces 500; IST midnight/overnight records land on wrong day. | Explicit 404, strict timestamp model, UTC instants, IST date assignment and overnight adjustment. |
| 10 | `punch_in` | Check-then-insert permits concurrent duplicates; status is unrestricted; response leaks an `id`; no API millisecond/BSON conversion. | Parallel punch-ins both succeed, or invalid status is stored. | Unique `(emp_code,date)` index, presence-status validation, omit IDs, convert dates at boundary. |
| 11 | `list_attendance` | Loads and sorts the entire collection in Python, making 100k+ records slow and memory-heavy. | Memory and latency grow with collection size even for page one. | MongoDB filter, sort, skip, limit and count use indexes. |
| 12 | `list_attendance` | Date range/status validation is absent; sort has no employee tiebreaker; response includes Mongo `_id` as `id`. | Reversed dates are accepted, equal-date order varies, or response violates schema. | Validate filters, use required stable sort, and expose only contract fields. |
| 13 | All startup/configuration | No lifecycle management, ping timeout, startup indexes, or explicit connection cleanup. | Indexes are absent and processes can retain connections on shutdown. | Initialize indexes on startup and close the client on shutdown. |
| 14 | Missing `punch-out` | No close-session operation, atomic duplicate protection, midnight lookup, duration rules or computed fields. | Contract route returns 404; parallel close requests cannot be resolved correctly. | Added most-recent open record lookup and conditional atomic update. |
| 15 | Missing `regularize` | No correction validation, before/after audit record, derived-field recomputation, or concurrency guard. | History is missing or simultaneous corrections overwrite changes. | Added validation, single appended history event and compare-and-set update. |
| 16 | Missing analytics routes | Monthly, department, leaderboard and trend endpoints are absent. | Contract routes return 404. | Added MongoDB aggregation pipelines for each required analytic. Integration tests uncovered and fixed weekday handling, distinct headcount, record-weighted averages, and daily trend date generation. |
| 17 | Missing admin explain | No execution plan endpoint to verify indexed query behavior. | `/admin/explain/...` is unavailable. | Added executionStats explain output using the route's pipeline/query. |

## Reviewed and retained

- `load_dotenv()` is appropriate: python-dotenv does not override already-set process environment variables by default.
- The starter's choice of PyMongo is suitable for the synchronous route functions and is kept consistently.
- The natural attendance key (`emp_code`, `date`) matches the supplied data model; the implementation enforces it with a unique index rather than exposing MongoDB `_id`.

## Verification follow-up

The starter-specific findings above were fixed in the implementation. The follow-up pytest integration suite uses isolated `hrone_test_<uuid>` databases and passed 13 tests against local MongoDB 8.2.3. The suite checks the supplied sample, additional synthetic analytics cases, concurrent punches and corrections, legacy records, and query plans. MongoDB 7 (the grader's version) was not available locally, so exact-version verification remains outstanding.

The follow-up audit of the first implementation draft found and fixed these additional issues:

- Employee monthly presence and department present-day totals initially counted weekends. The aggregation now checks MongoDB weekday values (Monday-Friday) while late/overtime totals continue to include weekend records.
- Department summary initially counted each employee once per log and averaged employee averages. It now groups by employee before department, keeps employees with no logs, and divides total eligible work hours by the number of eligible attendance records.
- The daily trend initially used the wrong Sunday-based `$dayOfWeek` boundary. It now treats values 2-6 as weekdays and includes generated rows with no logs.
- Employee monthly `working_days` is calculated in MongoDB and returns zero for employees joining after the requested month. Attendance percentages use four-decimal half-up rounding.
- Aggregation explain commands initially used an invalid command shape for attendance listing. Explain tests now verify execution statistics and indexed scans without `COLLSCAN` on all five required explain operations.
- Corrections now support seeded legacy records with no `history` field, and regularization timestamps are truncated to whole seconds before storage/calculation. Explicit null values are rejected for request fields that the contract does not mark nullable.
