# Postgres Cutover

This backend can run in two modes:

- `SQLite fallback`: good for local demo use
- `Render Postgres`: recommended for production

## Recommended production shape

- Backend web service on Render
- Managed `Render Postgres` as the system of record
- Optional persistent disk only for local files, not the main database

## Environment changes

Set these on the backend service:

```env
DATABASE_URL=postgresql://...
AUTO_SEED_SAMPLE_DATA=false
ALLOWED_ORIGINS=https://frontend-cyan-beta-65.vercel.app
```

`AUTO_SEED_SAMPLE_DATA=false` prevents the app from auto-seeding the demo dataset into a brand-new production database before you migrate existing data.

## Render helper

If you are managing the service from this Windows machine, use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/render-postgres-cutover.ps1 `
  -RenderApiKey "your_render_api_key" `
  -DatabaseUrl "postgresql://..."
```

That wrapper calls the existing Render API helper and sets `AUTO_SEED_SAMPLE_DATA=false` for the Postgres cutover by default.

## Migrate the current SQLite data

Run this from the `backend` directory after you have a Postgres URL:

```bash
python scripts/migrate_sqlite_to_postgres.py --target-url postgresql://...
```

If the Postgres database already contains disposable test data, replace it:

```bash
python scripts/migrate_sqlite_to_postgres.py --target-url postgresql://... --reset-target
```

Defaults:

- source database: local SQLite fallback from `DATA_DIR`
- target database: `DATABASE_URL`

## Verify after cutover

Check:

- `GET /health`
- `GET /api/v1/schedule/dashboard-summary`
- `GET /api/v1/operations/workspace`

The health response now includes:

- `database_backend`
- `auto_seed_sample_data`

For a successful production cutover, `database_backend` should be `postgresql`.
