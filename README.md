# Employee Attendance Analytics API

A REST API for employee management, attendance tracking, and attendance analytics using FastAPI and MongoDB.

## Technologies Used

- Python
- FastAPI
- MongoDB
- PyMongo
- Uvicorn

## Features

- Create and retrieve employee records.
- Record employee punch-in and punch-out.
- Retrieve attendance records.
- Correct attendance records.
- Generate employee attendance analytics.
- Generate department-level summaries and trends.
- Retrieve late-arrival leaderboard information.
- Inspect MongoDB query execution plans through the admin explain endpoint.

## Prerequisites

- Python 3.10 or compatible version supported by the dependencies.
- MongoDB 6.0 or newer.
- Git.

## Setup

Clone the repository:

    git clone https://github.com/usha1625/employee-attendance-analytics-api.git

Navigate to the project directory:

    cd employee-attendance-analytics-api

Create and activate a virtual environment:

    python -m venv .venv

On Windows:

    .venv\Scripts\activate

Install dependencies:

    pip install -r requirements.txt

## Environment Configuration

Configure the following environment variables:

- MONGO_URI: MongoDB connection string.
- MONGO_DB: MongoDB database name.

Keep credentials in a local `.env` file or environment variables. Never commit secrets to Git.

## Run the Application

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

## API Documentation

After starting the application, open:

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json
- Health check: http://127.0.0.1:8000/health

## Sample Data

The `sample_data/` directory contains sample MongoDB Extended JSON documents.

To load the sample data, configure the environment variables and run:

    python sample_seed.py

## Project Structure

    app/
        __init__.py
        main.py
    sample_data/
        employees.json
        attendance_logs.json
    sample_seed.py
    requirements.txt
    openapi.yaml
    DATA_MODEL.md
    REVIEW.md
    DECISIONS.md

## Testing

API endpoints were tested locally using the interactive Swagger UI.

## Security

Do not commit `.env` files, database passwords, API keys, or other sensitive credentials.
