# Polymnia — Project Config

> Project-specific context. Durable personal preferences live in the global
> config. Keep this file thin: point to the source-of-truth docs below
> instead of duplicating them.

## Source of truth — read these first
- ./docs/code-standards.md — coding conventions, error handling, naming, testing rules
- ./docs/architecture.md — system design, components, data model, boundaries (ADRs)

> Read both before writing or reviewing code. If something here conflicts
> with them, they win. If they're silent on something, ask.

## Stack
- Language(s): Python 3.14 (backend), TypeScript 5.7 (render-motor)
- Framework(s): FastAPI (API) · Remotion 4 + React 19 (headless render worker)
- Database: PostgreSQL via SQLModel (SQLAlchemy + psycopg 3 driver)
- Package manager: `uv` (backend) · `npm` (render-motor)
- Key external services: Mistral LLM (`mistral-medium-latest`) · Mistral Voxtral TTS · Object Storage/CDN (`Storage` abstraction implemented, issue #12 — local dev / S3 prod; render worker's MP4 + audio output fully routed through it, no local blob left behind, issue #13; `GET /projects/{id}/video` now 302-redirects to a CloudFront signed URL when `STORAGE_BACKEND=s3`, issue #14; CloudFront distribution/OAC provisioning + data migration still pending)

## Environment & commands

Two subprojects with separate toolchains under a single git repo at the root.

**Backend (`backend/`)**
- Install deps:        `uv sync`
- Run dev server:      `uv run uvicorn api.main:app --reload`   (FastAPI app: `api.main:app`)
- Run generation worker:`uv run celery -A api.celery_app worker -Q generation -n generation@%h --loglevel=info`   # drains generation jobs (issue #7); needs a running Redis (`REDIS_URL`)
- Run full pipeline:   `./run.sh [input.txt] [styleId] [brand_kit.json]`   # input.txt → MP4
- Run tests:           `uv run pytest`                 # tests live in `backend/tests/` (mirrors api/ + pipeline/)
- Run a single test:   `uv run pytest tests/path::test_name`
- Lint + format:       `uv run ruff check` · `uv run ruff format`   (Ruff is the only linter/formatter)
- Type check:          `uv run mypy`   (strict; `files = ["api", "pipeline"]`)
- DB migrations:       `uv run alembic upgrade head` (apply) · `uv run alembic revision --autogenerate -m "..."` (new) — see `backend/alembic/README`
- Dev schema shortcut: set `POLYMNIA_DEV_CREATE_ALL=1` to build schema via `create_all` at startup (prod uses Alembic; create_all is a no-op without the flag)
- Pre-commit:          `uv run pre-commit install --install-hooks --hook-type commit-msg`
- Install S3 storage extra: `uv sync --extra s3`   # only needed for `STORAGE_BACKEND=s3` (pulls in `boto3`); local dev/CI don't need it
- Storage backend (issue #12): `STORAGE_BACKEND=local|s3` (default `local`, under `backend/out/storage/` or `STORAGE_LOCAL_ROOT`).
  For `s3`: `STORAGE_S3_BUCKET` (required), `STORAGE_S3_REGION`, `STORAGE_S3_ENDPOINT_URL` (e.g. moto/LocalStack for tests).
  Video downloads (`GET /projects/{id}/video`) are 302-redirected to a CloudFront signed URL on `s3` (issue #14) — requires `STORAGE_CLOUDFRONT_DOMAIN`, `STORAGE_CLOUDFRONT_KEY_PAIR_ID`, `STORAGE_CLOUDFRONT_PRIVATE_KEY_PATH` (RSA private key, PEM); optional `STORAGE_CLOUDFRONT_SIGNED_URL_TTL_S` (default 300). Missing/invalid config raises `StorageConfigError`. See `backend/api/storage.py`.

**Render-motor (`render-motor/`)**
- Install deps:        `npm install`
- Studio (preview):    `npm run studio`
- Render composition:  `npx remotion render src/index.ts Polymnia out/polymnia.mp4 --props=./render-input.json`
- Type check:          `npx tsc --noEmit`   (or `npm run typecheck`)
- Run tests:           `npm test`            # Vitest, pure-logic unit layer; tests in `render-motor/tests/`
- Run tests (watch):   `npm run test:watch`
- Build render image:  `docker build -t polymnia-render-poc .`
- Run render container: `docker run --rm -v "$PWD/out:/app/out" polymnia-render-poc`

**Render worker (repo root — issue #8, containerized Celery worker)**
- Build:                `docker build -f Dockerfile.render-worker -t polymnia-render-worker .`
  (build context is the repo **root**: needs both `backend/` and `render-motor/`,
  copied as siblings so `pipeline/pack_render.py`'s relative `RENDER_DIR` still resolves)
- Run stack (compose):  `docker compose up -d redis render-worker` — Redis broker +
  a Celery worker bound to the `render` queue only (`-Q render`); `DATABASE_URL`/
  `MISTRAL_API_KEY` are read from `backend/.env` via `env_file` (optional — never
  inlined in `docker-compose.yml`)
- Validate config:      `docker compose config --quiet` (validates only — plain
  `docker compose config` inlines `env_file` secrets as plaintext; never run
  that form somewhere it could be logged/captured)
- Worker logs:          `docker compose logs -f render-worker` → look for
  `celery@... ready.` and `[queues] .> render`
- Known limitation: `render_project()` still writes the rendered MP4 to a
  container-local path (`/app/render-motor/out/...`) first — `npx remotion
  render` is a subprocess, it can't target S3 directly — before promoting it
  into Storage (`storage.put`, issue #12). Since issue #13, that intermediate
  write (plus the packed `render-input-{pid}.json` and the audio
  materialized under `public/proj-{pid}/`) is deleted in a `finally` right
  after promotion is attempted, on both success and failure — so the compose
  `render-out` named volume no longer accumulates rendered MP4s across
  renders; it's transient scratch for the few seconds a render is in flight,
  not a place to inspect past output. Cross-service delivery goes entirely
  through the `Storage` abstraction (`STORAGE_BACKEND=local|s3`, see above) —
  the key persisted as `videos.mp4_path` is what other containers/hosts
  resolve. Client-facing delivery on `s3` now goes through a CloudFront
  signed URL (issue #14, `S3Storage.signed_url()`); the actual CloudFront
  distribution + OAC/OAI provisioning is still pending infra work (Étape 2).

> CI is live: `.github/workflows/ci.yml` runs on PRs + `main` — backend Python
> (`ruff check` + `ruff format --check`, `mypy` strict, `alembic upgrade` +
> `alembic check`, `pytest`), render-motor TS (`tsc --noEmit`, Vitest), and a
> render-image `docker build`. Deployment (dev/staging/prod) is not wired yet.

## How to validate a change end-to-end
> Required before any change is handed back. Don't rely on unit tests alone.
- Backend pipeline change: run `./run.sh` from `backend/` end-to-end → confirm
  it produces `render-motor/out/polymnia.mp4` without error, and that each
  stage artefact (`out/plan.json` → `outline.json` → `scenes_full.json` →
  `scene_audio.json`) is well-formed.
- API change: start the server (`uv run uvicorn api.main:app --reload`), hit the
  affected endpoint, check `response_model` shape + `status_code` (202 job /
  201 create / 200 read / 404·409 errors) and that no DB column leaks (`user_id`).
- Render/style change: `npm run studio` (or render a composition) and inspect
  the output frames/MP4 — `<TODO: confirm preferred visual-evidence step>`.
  Vitest (`npm test`) covers only pure logic (theme/cosmetic/routing); visual
  correctness is not yet tested — a still-snapshot regression layer
  (`@remotion/renderer` `renderStill` + `pixelmatch`) is deferred to P1/P2,
  once the style catalogue stabilizes.
- Requires `MISTRAL_API_KEY` + `DATABASE_URL` in `backend/.env` to exercise the
  real LLM/TTS/DB path.

## Project conventions (not already in code-standards.md)
- Layout catalogue is mirrored in two places: `backend/pipeline/layout_store.py`
  (Python) and the TS render components. **Keep them in sync** — manual mirror,
  drift is a known POC debt.
- `pipeline/` scripts import siblings (the script's own dir is on `sys.path`);
  `print()` is allowed there (stdout = chainable artefact) but forbidden in `api/`.
- mypy strict passes over `api/` + `pipeline/` (both conform). Keep it green —
  mypy runs in CI (blocking), not pre-commit (too slow). Pipeline scripts wrap
  JSON/LLM boundaries as `dict[str, Any]`.
- Known POC debt: EN voice reading FR text (clone FR for prod); audio/render
  artefacts are regenerated, never committed.

## Out of scope / don't read or touch
- Secret files — do not read or open: `backend/.env`, any `.env*`, `*.pem`, `*.key`
- Generated/vendored: `backend/.venv/`, `backend/out/`, `backend/.mypy_cache/`,
  `backend/.ruff_cache/`, `render-motor/node_modules/`, `render-motor/out/`,
  `render-motor/public/audio/`, `render-motor/render-input*.json`, `__pycache__/`,
  `*.src.wav`, `voice_src.wav`, all `.DS_Store`
- `inputs/voice_sample.wav` — intentional TTS voice-clone input, leave as-is
