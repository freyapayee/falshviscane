# VISCANE Setup

This project supports two database modes:

1. Shared hosted PostgreSQL for the whole team
2. Local Docker PostgreSQL for individual development

## Recommended Team Setup: One Shared Database

`localhost` cannot be shared across different machines. To let everyone use the same database, host PostgreSQL somewhere reachable on the internet or your private network, then give every teammate the same `DATABASE_URL`.

Examples of where to host it:

- Render PostgreSQL
- Supabase
- Neon
- Railway
- A VPS running PostgreSQL

## 1. Create the team env file

Create `.env.local` and set the shared database URL:

```bash
cp .env.example .env.local
```

Update:

```env
DATABASE_URL=postgresql://shared_user:shared_password@your-db-host:5432/viscane_db
VISCANE_SECRET_KEY=replace-with-a-strong-secret-key
```

Keep `.env.local` out of Git. Only commit `.env.example`.

## 2. Run the Flask app locally against the shared DB

Create or activate your virtual environment, then install dependencies:

```bash
.env/bin/pip install -r requirements.txt
```

Start the app:

```bash
.env/bin/python app.py
```

The app will use `DATABASE_URL` from `.env.local` when set.

## Optional: Run with Docker

If your `.env.local` contains a hosted `DATABASE_URL`, the app container will use that value:

```bash
docker compose up --build app
```

## Optional: Local database fallback

If you do not want to use the shared database while developing, remove `DATABASE_URL` from `.env.local` and run:

```bash
docker compose up -d db
```

The app will then fall back to the local Docker PostgreSQL instance on `localhost:5433`.

## Team workflow

- Everyone pulls the same code from GitHub
- Everyone creates their own `.env.local`
- Everyone uses the same hosted `DATABASE_URL`
- The database data is shared because the host is shared, not because `localhost` is shared

## Important notes

- Do not commit real database passwords to GitHub
- Use a strong `VISCANE_SECRET_KEY`
- A shared database means everyone can affect the same data, so use backups and careful permissions
