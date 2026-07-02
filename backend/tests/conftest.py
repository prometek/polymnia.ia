"""Shared pytest fixtures.

Endpoints are sync `def` (threadpool) — use Starlette's sync TestClient, not an
async client. DB-touching tests use a real test database (no DB mocks), per
docs/code-standards.md. Wire a `client`/`session` fixture here when the first
integration test lands.
"""
