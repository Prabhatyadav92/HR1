# Employee Attendance & Analytics API

A backend REST API built with **Python, FastAPI, and MongoDB** to manage employee information, attendance records, attendance corrections, and analytics.

## Features

- Create and list employees with department filtering and pagination.
- Record employee punch-in and punch-out times.
- Retrieve attendance records using filters and pagination.
- Correct attendance records with an audit history.
- Calculate work hours, late minutes, overtime, and half-day status.
- Generate monthly employee attendance reports.
- View department summaries, late-arrival leaderboards, and attendance trends.
- Inspect MongoDB query execution statistics.
- Validate incoming data and handle duplicate records.
- Create MongoDB indexes for efficient queries.

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI
- **Database:** MongoDB 6.0+
- **Database Driver:** PyMongo
- **API Documentation:** Swagger UI

## Project Structure

```text
HR1/
├── app/
│   └── main.py
├── sample_data/
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── PROBLEM_STATEMENT.docx
├── README.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Prerequisites

Make sure you have installed:

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Git

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/Prabhatyadav92/HR1.git
cd HR1
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root and configure your MongoDB connection:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

You can use a local MongoDB instance or a MongoDB Atlas connection string.

**Note:** Keep `.env` out of version control. Never commit database credentials or other secrets.

### 5. Load sample data (optional)

Make sure MongoDB is running, then execute:

```bash
python sample_seed.py
```

This loads the sample documents provided with the project.

### 6. Start the API server

```bash
uvicorn app.main:app --port 8000 --reload
```

The API will be available at:

- **Base URL:** `http://localhost:8000`
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **Health Check:** `http://localhost:8000/health`

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Check API and database health |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | Retrieve attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Get monthly employee analytics |
| GET | `/analytics/departments/summary` | Get department summaries |
| GET | `/analytics/leaderboard/late` | Get the late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Get department attendance trends |
| GET | `/admin/explain/{endpoint}` | Inspect query execution statistics |

Open `/docs` to explore the request schemas, parameters, and responses interactively.

## Business Rules

- Employee records are identified by `emp_code`.
- An employee can have only one attendance record per date.
- Attendance dates are interpreted in Indian Standard Time (IST).
- Timestamps are accepted in epoch milliseconds.
- Work hours, late minutes, overtime minutes, and half-day status are calculated by the API.
- Attendance corrections maintain an audit history.
- Analytics account for working days, employee joining dates, and attendance statuses.
- MongoDB indexes support uniqueness and query performance.

Refer to `openapi.yaml` and `DATA_MODEL.md` for the detailed API contract and database structure.

## Testing

Start MongoDB and run the API server before testing.

You can test endpoints through Swagger UI at:

```text
http://localhost:8000/docs
```

Check successful responses, validation errors, duplicate records, attendance calculations, analytics, and database connectivity.

## Documentation

- `PROBLEM_STATEMENT.docx` — assignment requirements.
- `openapi.yaml` — API contract and business rules.
- `DATA_MODEL.md` — MongoDB data model and sample documents.
- `REVIEW.md` — starter-code review.
- `DECISIONS.md` — technical decisions and trade-offs.

## Author

**Prabhat Yadav**

GitHub: [Prabhatyadav92](https://github.com/Prabhatyadav92)

