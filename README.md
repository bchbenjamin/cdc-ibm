# CDC Registration Portal

A Django-based FCFS event registration and lab allocation system built for serverless deployment on Vercel. The UI uses Material Design 3 via Material Web components and custom theming.

## Key Features

- Concurrent-safe lab allocation with database-level locking.
- CSV import and export handled by pandas with chunked processing.
- FCFS lab assignment across A1-A5 and A11-A15 with 20 seats each.
- Session capacity enforcement with prompts for new sessions.
- Admin-only audit logs and CSV management.

## Requirements

- Python 3.11 or 3.12 (pandas wheels are not available for Python 3.14 yet)
- PostgreSQL (Neon recommended for production)

## Local Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a local environment file:
   ```bash
   cp .env.example .env
   ```
   If you use Neon, ensure SSL is enabled via `DATABASE_SSLMODE=require` or by
   appending `?sslmode=require` to `DATABASE_URL`.
4. Run migrations:
   ```bash
   python manage.py migrate
   ```
5. Start the server:
   ```bash
   python manage.py runserver
   ```

Default admin account (created on first migrate):
- Username: admin
- Password: admin

Change these credentials in production.

## CSV Import Notes

- Expected columns: `SL no`, `Sl #`, `Zone`, `Candidate Full Name`.
- The first `SL no` column is ignored; `Sl #` and subsequent columns are stored.
- `Candidate Full Name` is stored exactly as provided.
- Imports are chunked to avoid serverless timeouts.
- Exported CSV appends: `present/absent`, `lab alloted`, `session alloted`, `signature`.

## Vercel Deployment

1. Create a Neon database and copy its connection string.
2. In the Vercel project settings, set environment variables:
   - `DATABASE_URL`
   - `DATABASE_SSLMODE=require` (recommended for Neon)
   - `DJANGO_SECRET_KEY`
   - `DJANGO_DEBUG=false`
   - `ALLOWED_HOSTS` (comma-separated, include your Vercel domain)
   - `CSRF_TRUSTED_ORIGINS` (comma-separated, include https://your-domain.vercel.app)
   - `TIME_ZONE` (optional, default UTC)
3. Set the Vercel Build Command to:
   ```bash
   python manage.py collectstatic --noinput
   ```
4. Run migrations against Neon from your machine:
   ```bash
   python manage.py migrate
   ```
5. Deploy. Vercel will use `vercel.json` and `cdc_portal/wsgi.py`.
   Vercel will use `api/index.py` as the serverless entrypoint.

## Admin Access

- Admin site: `/admin/`
- CSV imports and audit logs are managed in the admin panel.

## Basic Checks

Run Django system checks before deployment:
```bash
python manage.py check
```
