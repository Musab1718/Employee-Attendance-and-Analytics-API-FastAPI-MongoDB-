# Employee Attendance & Analytics API

FastAPI and MongoDB backend service implementing automated attendance tracking, business regularization (R1–R10), and complex analytical aggregations according to the engineering contract.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas)

## Local Setup & Run

1. **Activate virtual environment and install dependencies:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: .\venv\Scripts\activate
   pip install -r requirements.txt
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            
# set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

1. Configure environment:

MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db

2. Start the API service:

uvicorn app.main:app --port 8000

3. Verify service readiness:

Health Probe: http://localhost:8000/health

Interactive Swagger Documentation: http://localhost:8000/docs