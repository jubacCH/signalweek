# signalweek

MVP built by the ai-company for opportunity #619.

Signalweek is a curated weekly digest of the AI industry: a fixed five-category
taxonomy (Models / Lawsuits & policy / Funding / Research / Industry moves),
each item citing a primary source. The service is a public publication with
no user accounts — every reader sees the same issue each week.

## Quickstart (Docker Compose)

Prerequisites: Docker Engine 24+ with the Compose plugin.

```sh
cp .env.example .env
docker compose up -d --build
```

Then open <http://localhost:8000>. The service applies its Alembic migrations
at container start, so the first boot creates the schema automatically.

Follow the logs with:

```sh
docker compose logs -f app
```

Stop and remove the container (the SQLite database survives on the
`signalweek_data` named volume):

```sh
docker compose down
```

## Configuration

All configuration is read from the environment — see [`.env.example`](.env.example)
for the full list. The variables that matter in production:

| Variable                | Default                                    | Notes                                                              |
| ----------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| `DATABASE_URL`          | `sqlite:////app/data/signalweek.db`        | Any SQLAlchemy URL. Point at Postgres for larger deployments.      |
| `SIGNALWEEK_HTTP_PORT`  | `8000`                                     | Host port compose publishes to (container always listens on 8000). |

## Self-hosting notes

- **Persistent data.** The compose file mounts the named volume
  `signalweek_data` at `/app/data`, where the SQLite database lives. Back it up
  with `docker run --rm -v signalweek_data:/data -v "$PWD":/backup alpine tar
  czf /backup/signalweek-$(date +%F).tgz -C /data .`. Restore by piping the
  tarball back into the same volume.
- **HTTPS.** The container serves plain HTTP on port 8000. Put it behind a
  reverse proxy (Caddy, Traefik, nginx) that terminates TLS and forwards to
  `http://signalweek:8000`.
- **Upgrades.** `git pull && docker compose up -d --build` rebuilds the image
  and restarts; the entrypoint applies any new Alembic migrations before the
  web server starts.
- **Postgres.** Set `DATABASE_URL=postgresql+psycopg://user:pass@host/db` in
  `.env` and add a `db` service to `docker-compose.yml` if you don't already
  run Postgres externally. The migrations use SQLAlchemy's `render_as_batch`
  only for SQLite, so they run cleanly on Postgres too.

## Development (without Docker)

```sh
pip install -e '.[dev]'
alembic upgrade head
uvicorn signalweek.main:app --reload
```

Run tests and lints:

```sh
pytest
ruff check .
```
