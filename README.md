# signalweek

MVP built by the ai-company for opportunity #619.

Signalweek is a curated weekly digest of the AI industry: a fixed five-category
taxonomy (Models / Lawsuits & policy / Funding / Research / Industry moves),
each item citing a primary source. The service is a public publication with
no user accounts — every reader sees the same issue each week.

## How the pipeline works

The digest is assembled automatically from a checked-in list of feeds
(`sources.yaml`); nothing about the pipeline is per-reader.

1. **Ingest** (hourly). Every active source in the `sources` table is fetched
   with `httpx`, parsed with `feedparser`, and written into `raw_items`
   deduplicated on `(source_id, canonical_url)`.
2. **Cluster** (part of the ingest tick). New `raw_items` are grouped into
   `clusters` — one per real-world story — using canonical URL first, then a
   fuzzy title fallback for the same story mirrored on different outlets.
3. **Classify** (at build time). Each cluster is mapped to exactly one of the
   five sections by a keyword-based classifier, with the originating source's
   `category_hint` as a tiebreaker.
4. **Rank & pick** (at build time). Clusters from the last 7 days are ranked
   by source authority, cross-outlet corroboration, and recency; the top N per
   section become the week's items. A 12-issue URL-dedup window prevents
   repeating stories across weeks.
5. **Build**. A new `issues` row is written for the ISO week. If it collects
   fewer than the minimum item count it is recorded as `held` for editorial
   review; otherwise `published` with a `published_at` timestamp. Each item
   gets a rule-based extractive summary (headline + first sentence of the
   source body).
6. **Verify**. Every item's `primary_url` is probed (`HEAD`, falling back to
   `GET` on 405/501). Dead links are deleted from the issue before it is
   served publicly.
7. **Serve**. The FastAPI app renders the latest published issue at `/`, the
   full archive at `/issues`, and a permalink per week at `/issues/{YYYY-MM-DD}`.

**Not in this stage.** Email delivery (SMTP, per-subscriber send, double
opt-in), LLM-based per-item summarisation, and the LLM source-support check
are all deferred to a later stage. There is no `send` job in the scheduler
and no `subscriber` table in the schema.

## Quickstart (Docker Compose)

Prerequisites: Docker Engine 24+ with the Compose plugin.

```sh
cp .env.example .env
docker compose up -d --build
```

Then open <http://localhost:8000>. The service applies its Alembic migrations
at container start, so the first boot creates the schema automatically. See
the [operator runbook](#operator-runbook) below for how to load the initial
source list before the first ingest tick fires.

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

## Operator runbook

The publication is fully automated: an APScheduler background thread runs the
ingest tick every hour on the hour (UTC) and the weekly build → verify pass
every Monday at 09:00 America/New_York. The commands below cover the manual
overrides — bootstrapping a new source, pushing a build off-schedule, or
retracting an issue after it went live.

All commands are invoked as `python -m signalweek.cli …` (or `signalweek …`
if you've installed the package with `pip install -e .`). Every command reads
the same `DATABASE_URL` as the web server.

### Source registry

```sh
python -m signalweek.cli sources list
python -m signalweek.cli sources add \
    --url https://example.com/feed --kind rss --category models --name Example
python -m signalweek.cli sources disable --url https://example.com/feed
```

`add` is an upsert on `url`, so re-running it flips a disabled source back on
and updates its `kind`/`category`. `disable` sets `active=false` — the ingest
tick then skips the source until you re-`add` it.

### Weekly issue lifecycle

```sh
# 1. Build. Assembles the issue for the current ISO week (or --week YYYY-MM-DD).
#    Automatically records status='published' or status='held' depending on
#    whether it hit the minimum item count.
python -m signalweek.cli issue build

# 2. Verify. Deletes items whose primary_url is dead (non-200 after redirects).
#    Idempotent — safe to re-run after fixing a source outage.
python -m signalweek.cli issue verify --week 2026-07-27

# 3. Publish. Marks an issue as published and stamps published_at=now.
#    Use this to release a held issue after the editor has filled it out,
#    or to re-publish an issue that was previously held for correction.
python -m signalweek.cli issue publish --week 2026-07-27

# 4. Hold. Marks an issue as held and clears published_at.
#    Use this to *retract* an issue after publication (e.g. a bad-source
#    incident) — the /issues archive and landing page immediately stop
#    serving it, since both filter on status='published'.
python -m signalweek.cli issue hold --week 2026-07-27
```

`verify`, `publish`, and `hold` accept `--issue-id <int>` as an alternative
to `--week` — useful if a week has been rebuilt and multiple `issues` rows
exist for the same date range in test/staging databases.

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
