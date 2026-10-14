# REVIEW.md

# REVIEW.md

# | Where (function / line) | What is wrong | How you would notice it (test, input or symptom) | How you fixed it

|---|---|---|---|---|

| 1 | Global / Startup | Indexes were not created during startup. The default `_id` index existed. | When I ran queries on 100k records the explain plan showed `COLLSCAN`. Query time was very slow. Duplicate keys appeared during concurrent requests. | I added an `lifespan handler that created unique compound indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)`. I also added index coverage on `(date, emp_code)` `(joined_on)` and `(status date)`. |

2 | `GET /health` | The endpoint always returned `{"status": "ok"}` with HTTP 200 even when MongoDB could not be reached. | When I stopped MongoDB the endpoint still returned HTTP 200 of HTTP 503 Service Unavailable. | I wrapped `db.command("ping")` in a block. If the ping fails I raise `HTTPException(503)`.

| 3 | `POST /employees` | `created_at` was set with naive `datetime.now()` instead of UTC BSON datetime. The serialization produced an ISO string than integer epoch milliseconds. Duplicate checks used race‑prone `find_one`. | When I created two employees at the time with the same `emp_code` both creations succeeded. The client received string dates that broke the schema. | I store a timezone‑aware UTC datetime truncated to seconds. I serialize it to epoch milliseconds. I enforce an index on `emp_code` and catch `DuplicateKeyError` to return HTTP 409.

| 4 | `GET /employees` | Pagination skip used `page * page_size` so page 1 was skipped. The `total` counted all documents ignoring the `department` filter. Records were not sorted. | When I requested `?page=1&department=Engineering` the results started at index 20 the total was wrong. The records were unordered. | I changed the skip to `(page. 1) * Page_size`. I filtered the total with `db.employees.count_documents(q)`. I sorted the results by `ascending.

| 5 | `POST /attendance/punch-in` | There was no check to see if the employee exists. When `emp` was `None` accessing `emp["shift_start"]` caused a `TypeError`. Returned HTTP 500. | If I punched in with a non‑existent `emp_code` I got HTTP 500 Internal Server Error of HTTP 404. | I added a check: `if not emp: raise HTTPException(404 "Employee not found")`.

| 6 | `POST /attendance/punch-in` | There was a concurrency race condition. I used `find_one`. Then `insert_one` which is a check‑then‑insert antipattern. | When I sent requests for the same employee on the same date duplicate records were created. | I added a compound index on `(emp_code, date)` and caught `DuplicateKeyError` to return HTTP 409. |

| 7 | `POST /attendance/punch-in`. R1 | I ignored IST (UTC+05:30) conversion and overnight shifts. Punch‑in timestamps were not truncated to seconds. | Punches in the morning or night shift were assigned to the wrong date. Millisecond seconds leaked into the stored data. | I converted epoch milliseconds to UTC datetime. I applied IST logic for overnight shifts moving punches before `shift_end` to the previous day. I truncated to seconds before calculation. |

| 8 Compute_late_minutes` & R2 | The grace period rule was calculated from the grace window of from `shift_start`. The naive `replace()` was not timezone‑safe. | When I punched at 09:40:01 on a 09:30 shift the function returned 0 or 1 of 10 late minutes. | I calculated elapsed time from the shift start. If it was greater than 600 seconds (10 minutes) I returned `floor(diff_seconds / 60)`. I tested it: 09:40:00 -> 0 09:40:01 -> 10, 10:15:59 -> 45. |

| 9 Compute_overtime` & R3 | The function returned any minutes, past `shift_end` instead of enforcing a 30‑minute threshold. | When I punched out 15 minutes after the shift end the function returned 15 of 0. | I calculated floor minutes from `shift_end` to `punch_out`. I return the value only if it is 30 or more; otherwise I return 0.

| 10 | `compute_work_hours` & R4 R8 | Used Pythons built-in round() function, which applies half-to-even (bankers rounding) instead of standard round-half-up. | Durations ending in `.xx5` rounded down to numbers instead of rounding up. | Used decimal.Decimal with ROUND_HALF_UP to round compute_work_hours to two places. |

11 | `GET /attendance` | Loaded the entire attendance_logs collection into application memory with list(db.attendance_logs.find(q)) and sorted in Python. | High memory. Request timeouts on large datasets (~100k documents). | Applied database-level cursor pagination to GET /attendance:.sort([("date" -1) ("emp_code" 1)]).skip(skip).limit(page_size) backed by compound indexes. |

| 12 | Boundary Serialization | Returned internal id (str(res.inserted_id)). Iso date strings. | Response failed schema validation against openapi.yaml. | Removed _id and id from Boundary Serialization; converted all BSON datetimes (including nested history[].changes timestamps) to integer epoch milliseconds.

### Examined and Decided Was Not a Defect

- **emp_code as primary identifier:** emp_code retained rather than exposing MongoDBs internal _id across all routes.

- **history array:** history kept inside the attendance_logs document as an audit array, than normalizing it into a separate collection ensuring single-document atomic updates.
