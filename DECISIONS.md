# Technical Decisions

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes. The unique compound index on `attendance_logs.emp_code` and `date` ensures one attendance record per employee per day. I also added indexes on `date` with `emp_code`, `emp_code` with `punch_in`, and `status` with `date` to support attendance filtering and lookups. I would evaluate a separate `late_minutes` index only if query analysis showed a benefit.

### 2. Punch-in Race
If two identical punch-in requests arrive simultaneously, both may initially find the employee. Each tries to insert an attendance record. MongoDB's unique compound index allows only one record for that employee and date. The other insert raises `DuplicateKeyError`, which my code converts to HTTP 409 Conflict.

### 3. Ties
The leaderboard sorts by total late minutes descending and employee code ascending. It uses standard competition ranking. Employees tied at the cutoff receive the same rank, and all employees with rank within the limit are returned, even if the result contains more employees than the limit.

### 4. Headcount
The department summary first retrieves eligible employees and counts them by department. Attendance is aggregated separately. Therefore, employees without attendance logs still contribute to headcount.

### 5. Scaling
For 100 times more data, I would inspect query execution plans and benchmark aggregation pipelines, then optimize expensive queries and indexes based on actual usage.
