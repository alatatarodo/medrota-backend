## Render Cutover

The current public service can be switched from the Node compatibility layer to the Docker/FastAPI backend with the helper below once you have a Render API key.

### What the script does

- updates the Render service to use the Docker runtime
- keeps the repo root as-is unless you explicitly pass `--root-dir`
- points Render at `./Dockerfile`
- sets `ALLOWED_ORIGINS` for the live Vercel frontend
- optionally sets `DATABASE_URL` if you provide one
- triggers a fresh deploy and waits for it to go live

### Default target

- Render service ID: `srv-d7053bdm5p6s73amk5f0`
- Frontend URL: `https://frontend-cyan-beta-65.vercel.app`

### Command

```bash
RENDER_API_KEY=your_render_api_key node scripts/render-cutover.mjs
```

### Optional Postgres

If you want FastAPI to boot against Postgres immediately, provide `DATABASE_URL` too:

```bash
RENDER_API_KEY=your_render_api_key DATABASE_URL=your_postgres_url node scripts/render-cutover.mjs
```

If `DATABASE_URL` is omitted, the FastAPI backend now falls back to SQLite at `./data/medrota.db` and seeds the baseline 1,600-doctor Wythenshawe/Trafford dataset on first boot.

### Optional persistent data directory

If you later attach a Render disk, you can point both the Node compatibility layer and the FastAPI SQLite fallback at that mount path:

```bash
RENDER_API_KEY=your_render_api_key DATA_DIR=/var/data node scripts/render-cutover.mjs
```

### Live verification

After any deployment, you can verify the live frontend/backend pair with:

```bash
npm run verify:live
```
