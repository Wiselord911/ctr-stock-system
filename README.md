# CTR STOCK SYSTEM

A small, production-ready Flask app for managing stock with categories, FIFO issue, login, and CSV export.
Designed to deploy on **Render** free Web Service with `gunicorn` and `render.yaml` included.

## Features
- User auth (register first admin, login/logout)
- Items & Categories CRUD (with confirm prompts)
- Receive stock (lots with optional expiry) and Issue stock using FIFO
- Transactions & Audit log
- Dashboard with quick stats
- CSV export (items, lots, transactions)
- Bootstrap 5 UI
- SQLite by default (file `data.db`), or provide `DATABASE_URL`

## Local run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit values
python app.py  # dev mode
```
App runs at http://127.0.0.1:5000

## Deploy to Render (Free Web Service)
1. Push this project to GitHub.
2. On Render, create **Web Service** → connect repo.
3. Runtime: Python 3.11+
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. Add Env Vars from `.env.example` (at least `SECRET_KEY`). For persistence on free plans, DB resets on redeploys — consider moving to a managed Postgres and set `DATABASE_URL`.

## Default Roles
- First registered user becomes admin.
- Admin-only endpoint: delete all items (`/admin/delete_all`) with confirm.

## Notes
- Free Render disk is ephemeral. For durable data use PostgreSQL (Render Managed DB) and set `DATABASE_URL`.
