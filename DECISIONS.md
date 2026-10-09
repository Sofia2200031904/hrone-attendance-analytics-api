# Design decisions

1. **Indexes.** `employees.emp_code` is unique. `attendance_logs(emp_code, date)` enforces one record per employee-day. `attendance_logs(date, emp_code)` supports attendance sorting/month filters; `attendance_logs(emp_code, date, status)` supports employee reads. `employees(department, joined_on)` and `employees(joined_on)` support department and headcount queries. I considered an index on `late_minutes`, but monthly date filtering happens first.

2. **Punch-in race.** Both requests may read the employee, then insert the same natural key. MongoDB’s unique index accepts one insert and rejects the other; duplicate-key becomes 409. Our concurrency test observed exactly one 201.

3. **Ties.** `$rank` gives equal totals the same rank and skips the next rank. Filtering by `rank <= limit` keeps everyone tied at the cutoff; the final sort orders ties by employee code. The test confirms both rank-one tied rows are returned.

4. **Headcount.** Department summary starts from employees joined by month-end, then looks up logs. It groups by employee before department, so employees with no logs count and employees with multiple logs count once. Tests cover zero-log and mid-month joiners.

5. **100x scale.** All five explain plans used indexed scans without `COLLSCAN` on local MongoDB 8.2.3. At larger scale I’d measure workload, then consider daily pre-aggregation and targeted indexes; both add write and backfill complexity.
