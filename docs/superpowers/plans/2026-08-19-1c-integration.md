# 1C Integration Implementation Plan

> **For agentic workers:** Execute task-by-task with tests first. Git operations are forbidden by project instructions.

**Goal:** Add switchable mock/live HTTPS integration with 1C for MAX-user authorization, meter loading, queued readings, and idempotent daily synchronization.

**Architecture:** `bot.py` calls protected internal FastAPI routes through `client_api.py`; FastAPI alone calls 1C through `client_1c.py`. SQLite stores durable authorization, queued readings, batch identity, and scheduler state. APScheduler runs only in the FastAPI process.

**Tech Stack:** Python 3, FastAPI, httpx, APScheduler, SQLite, Flask/Jinja, truststore, unittest.

## Global Constraints

- Never read, print, or log `.env`, Bearer tokens, authorization codes, or client e-mail.
- Keep existing `chat_id` names; it represents MAX user ID.
- Keep current behavior unchanged when `ENABLE_1C_INTEGRATION=false`.
- Live transport uses HTTPS certificate verification; never use `verify=False`.
- No Git commands.

---

### Task 1: Configuration and 1C client

**Files:**
- Modify: `config.py`
- Create: `client_1c.py`
- Test: `tests/test_client_1c.py`

**Interfaces:**
- `request_auth_code(ls: str, chat_id: int) -> tuple[dict | None, str | None]`
- `verify_auth_code(ls: str, chat_id: int, code: str) -> tuple[dict | None, str | None]`
- `sync_readings(batch_id: str, readings: list[dict]) -> tuple[dict | None, str | None]`

- [ ] Test URL joining, Bearer header, timeout mapping, safe errors, mock status behavior, ten-minute mock expiry, and five-attempt mock lockout.
- [ ] Run `python -m unittest tests.test_client_1c -v`; expect failure because `client_1c` does not exist.
- [ ] Add configuration and minimal client implementation.
- [ ] Re-run the focused test; expect PASS.

### Task 2: Durable SQLite state

**Files:**
- Modify: `database.py`
- Test: `tests/test_database_1c.py`

**Interfaces:**
- Store/upsert meters by `(ls, meter_number)`.
- Queue readings with `sent_to_1c`, `status_1c`, and `batch_id`.
- Persist one `sync_1c_state` row with `last_success_at`, `last_attempt_at`, and `pending_batch_id`.
- Apply `accepted`/`rejected` statuses and `added`/`changed`/`removed` meter changes.

- [ ] Test migration of a pre-integration database, batch stability across retries, status updates, and meter delta application.
- [ ] Run `python -m unittest tests.test_database_1c -v`; expect missing-interface failures.
- [ ] Implement migrations and database functions.
- [ ] Re-run the focused test; expect PASS.

### Task 3: Internal FastAPI authorization contract

**Files:**
- Modify: `api/schemas.py`
- Create: `api/routers/integration_1c.py`
- Modify: `api/main.py`
- Modify: `client_api.py`
- Test: `tests/test_api_integration_1c.py`

**Interfaces:**
- `POST /api/v1/integrations/1c/auth/request-code`
- `POST /api/v1/integrations/1c/auth/verify-code`
- Both protected by `INTERNAL_API_TOKEN` and delegate to `client_1c`.

- [ ] Test request validation, status forwarding, timeout mapping, successful meter persistence, and no code in logs.
- [ ] Run the focused test; expect missing-route failure.
- [ ] Implement schemas, router, registration, and `client_api` wrappers.
- [ ] Re-run the focused test; expect PASS.

### Task 4: MAX-bot authorization and reading queue

**Files:**
- Modify: `bot.py`
- Test: `tests/test_bot_1c.py`

**Interfaces:**
- Add `AWAIT_LS_1C` and `AWAIT_CODE_1C` states.
- Show authorization action only in integration mode for unbound users.
- `wrong_code` keeps code input; `expired_code` and `attempts_exceeded` return to main menu.
- In integration mode, local reading comparison is disabled and confirmation means queued, not accepted.

- [ ] Test state transitions and mode-off compatibility.
- [ ] Run the focused test; expect state/behavior failures.
- [ ] Implement minimal bot changes.
- [ ] Re-run the focused test; expect PASS.

### Task 5: Idempotent synchronization

**Files:**
- Create: `sync_1c.py`
- Modify: `api/main.py`
- Modify: `bot.py`
- Test: `tests/test_sync_1c.py`

**Interfaces:**
- `sync_1c_job()` runs hourly, sends daily or pending batches, reuses failed `batch_id`, and processes empty daily batches for meter deltas.

- [ ] Test daily gating, hourly retry, stable batch membership, empty batches, result validation, notifications, and first-failure logging.
- [ ] Run the focused test; expect missing-module failure.
- [ ] Implement worker and FastAPI lifespan scheduler; remove bot scheduler stub.
- [ ] Re-run the focused test; expect PASS.

### Task 6: Portal and deployment template

**Files:**
- Modify: `web.py`
- Modify: `templates/pokazaniya.html`
- Create: `.env.example`
- Test: `tests/test_portal_1c.py`

- [ ] Test that integration mode exposes 1C status and legacy mode retains previous/difference columns.
- [ ] Run the focused test; expect template behavior failure.
- [ ] Implement mode-dependent view and safe configuration template.
- [ ] Re-run the focused test; expect PASS.

### Task 7: Verification

- [ ] Run `python -m unittest discover -s tests -v`; expect all tests PASS.
- [ ] Run `python -m compileall -q . -x "[\\\\/](?:\\.venv|\\.venv-broken)[\\\\/]"`; expect exit code 0.
- [ ] Import `config`, `database`, `client_1c`, `sync_1c`, `api.main`, and `bot`; expect exit code 0 without secret output.
- [ ] Run FastAPI health smoke test with a temporary process; expect `{"status":"ok"}`.
