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
- Key external services: Mistral LLM (`mistral-medium-latest`) · Mistral Voxtral TTS · Object Storage/CDN (target)

## Environment & commands

Two subprojects with separate toolchains under a single git repo at the root.

**Backend (`backend/`)**
- Install deps:        `uv sync`
- Run dev server:      `uv run uvicorn api.main:app --reload`   (FastAPI app: `api.main:app`)
- Run full pipeline:   `./run.sh [input.txt] [styleId] [brand_kit.json]`   # input.txt → MP4
- Run tests:           `uv run pytest`                 # tests live in `backend/tests/` (mirrors api/ + pipeline/)
- Run a single test:   `uv run pytest tests/path::test_name`
- Lint + format:       `uv run ruff check` · `uv run ruff format`   (Ruff is the only linter/formatter)
- Type check:          `uv run mypy`   (strict; `files = ["api", "pipeline"]`)
- Pre-commit:          `uv run pre-commit install --install-hooks --hook-type commit-msg`

**Render-motor (`render-motor/`)**
- Install deps:        `npm install`
- Studio (preview):    `npm run studio`
- Render composition:  `npx remotion render src/index.ts Polymnia out/polymnia.mp4 --props=./render-input.json`
- Type check:          `npx tsc --noEmit`   (or `npm run typecheck`)
- Run tests:           `npm test`            # Vitest, pure-logic unit layer; tests in `render-motor/tests/`
- Run tests (watch):   `npm run test:watch`
- Build render image:  `docker build -t polymnia-render-poc .`
- Run render container: `docker run --rm -v "$PWD/out:/app/out" polymnia-render-poc`

> CI is described in code-standards.md (GitHub Actions: lint, format check,
> mypy, tsc, tests, image build) but **no `.github/workflows/` exists yet** —
> it's a target, not current state.

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
- mypy strict is the target everywhere; `api/` is conform, `pipeline/`/`db.py`
  legacy is typed over time. mypy runs in CI only (too slow for pre-commit).
- Known POC debt: EN voice reading FR text (clone FR for prod); audio/render
  artefacts are regenerated, never committed.

## Out of scope / don't read or touch
- Secret files — do not read or open: `backend/.env`, any `.env*`, `*.pem`, `*.key`
- Generated/vendored: `backend/.venv/`, `backend/out/`, `backend/.mypy_cache/`,
  `backend/.ruff_cache/`, `render-motor/node_modules/`, `render-motor/out/`,
  `render-motor/public/audio/`, `render-motor/render-input*.json`, `__pycache__/`,
  `*.src.wav`, `voice_src.wav`, all `.DS_Store`
- `inputs/voice_sample.wav` — intentional TTS voice-clone input, leave as-is
