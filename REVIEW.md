# Code Review: Starter Defects and Fixes

This document records the defects identified in the starter
`app/main.py`, how they were detected, and how they were fixed.

## Defects Found

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `admin/explain/attendance_list` endpoint | MongoDB explain output may contain BSON values that cannot be serialized directly into a JSON response. | Calling the explain endpoint may fail when the response contains BSON-specific values. | Converted the explain output into JSON-compatible data before returning the response. |

## Reviewed but Not Considered Defects

- MongoDB index creation: Indexes are intentional for uniqueness and query performance. However, existing duplicate records can prevent unique index creation; this is a data-integrity issue that must be handled appropriately.
- HTTP status codes: Responses should be checked against the API contract rather than assuming that every successful request must return HTTP 200.
- MongoDB usage: Database queries and analytics should be evaluated against the specified business rules and expected results, not merely whether an endpoint responds successfully.

## Validation

The API endpoints were tested locally using Swagger UI.
Previously tested endpoints returned the expected HTTP status codes for the tested inputs.

Passing these tests does not guarantee correctness for every edge case or the larger grading dataset.
