# DECISIONS.md

**1. Indexes.** On `employees`: unique `emp_code` (identity, and what makes a duplicate create
409), `(department, emp_code)` for the filtered sorted list, `(department, joined_on)` and
`joined_on` for R9 headcount. On `attendance_logs`: unique `(emp_code, date)` (identity, the
punch-in race, one employee's month), `(date desc, emp_code asc)` which *is* the list sort, and
`(emp_code, punch_in desc)` to find the record a punch-out closes. I rejected an index on
`status`: five values cannot narrow anything, and the date index already bounds the scan.

**2. Punch-in race.** Neither request reads before writing: both call `insert_one`, the unique
`(emp_code, date)` index lets exactly one commit, and the other's `DuplicateKeyError` becomes 409.

**3. Ties.** `$rank` is competition ranking and `limit` filters the rank, not the row count, so both
tied rows stay: `limit=3` over 120, 120, 90, 90, 90 returns five rows, ranked 1, 1, 3, 3, 3.
Cutting one would be arbitrary.

**4. Headcount.** The pipeline starts on `employees` (`joined_on <= month end`) and `$lookup`s the
logs, so somebody with no logs still adds 1 to `headcount`, 0 to the rest.

**5. At 100x.** I would stop scanning raw logs: maintain a daily per-employee rollup with `$merge`
and let the summary, leaderboard and trend read that instead.
