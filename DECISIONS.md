# DECISIONS.md

### 1. Indexes created, why and what was rejected

- Created:

`Employees: { emp_code: 1 }` (unique) to keep each business code unique and `{ department: 1 emp_code: 1 }` for department‑filtered listings.

`Employees: { joined_on: 1 }` to speed up range filtering for headcount calculations.

`Attendance_logs: { emp_code: 1 date: 1 }` (unique compound index) to keep the business identity constraint and stop race conditions.

`Attendance_logs: { date: -1 emp_code: 1 }` `{ emp_code: 1, punch_in: -1 }` and `{ status: 1, date: 1 }` to allow `IXSCAN` on paginated lists handle punch‑out resolution and run aggregation pipelines.

- Rejected:

An index on `{ history: 1 }` was looked at but turned down. `History` is an audit trail; indexing arrays of subdocuments would add big write load with no benefit for queries.

### 2. Punch‑in race condition

When two identical punch‑in requests hit the system at the time both clear the initial Pydantic schema check. Both try to insert an attendance document for `(emp_code, date)`. MongoDBs WiredTiger engine locks the index `{ emp_code: 1 date: 1 }` so only one write can go through. That write. Sends back HTTP 201 Created. The other write sees a key error (`E11000`); our handler turns it into an HTTP 409 Conflict reply and keeps the data safe.

### 3. Ties at leaderboard cutoff

We calculate competition ranking (1, 2 2 4) over the monthly data with MongoDBs `$setWindowFields` and `$rank`. Employees that tie get the rank and are sorted by `emp_code` in ascending order. After ranking we cut the list with `$match: { rank: { $lte: limit } }`. So if several employees tie at or below the cut‑off rank every tied row comes back as the contract says.

### 4. Headcount with zero logs

The department summary aggregation begins with the `employees` collection filtered by `joined_on <= period_end`. It then looks up `attendance_logs` for the target month. By unwinding with `preserveNullAndEmptyArrays: true` we keep employees who have no logs. Headcount tallies distinct `emp_code` values and average work hours are calculated from non‑null attendance documents.

### 5. Strategy for 100× data volume (10M+ records)

At 100× the data size, real‑time cross‑collection `$lookup` joins would overload disk I/O and memory. We plan to use pre‑aggregated monthly rollup collections that update asynchronously with MongoDB Change Streams. We also intend to split `attendance_logs` across clusters by sharding on `{ emp_code: 1 date: 1 }`, as a compound shard key.