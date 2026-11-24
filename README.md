# CineMind AI

An AI Systems Engineering learning project. This backend milestone provides
PostgreSQL persistence, email verification, Argon2id passwords, opaque server-side
sessions, and owner-scoped APIs. The learner will implement the AI engineering.
No LLM calls, retrieval algorithms, embeddings, memory extraction, evidence
validation, uploads, parsing, file storage provider, or frontend are included.

## Windows PowerShell setup

Python 3.14 and PostgreSQL 18 are supported and tested on Windows. Existing Python
installations are preserved; dependencies and the pinned `uv` tool live in `.venv`.
Activation and PowerShell execution-policy changes are unnecessary.

```powershell
Set-Location C:\Users\G\CineMindAI
# Only when .venv has not yet been created:
if (!(Test-Path .venv\Scripts\python.exe)) { py -3.14 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install uv==0.13.9
.\.venv\Scripts\uv.exe sync --locked --python .\.venv\Scripts\python.exe
```

`pyproject.toml` declares dependencies and `uv.lock` pins all resolved versions
and artifact hashes. Python downloads are disabled. `.uv-cache` is local and ignored.
There is no paid service dependency, cloud provisioning, or external AI request.

### Configuration

The repository-root `.env` is local and ignored. **Keep an existing file**:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit local `.env` with the real `DATABASE_URL`. Do not paste credentials into
source files, terminal commands, screenshots, or issue reports. Process environment
variables override `.env`. SQLAlchemy changes only the scheme to
`postgresql+psycopg`; credentials, host and all URI parameters (including
`sslmode` and `channel_binding`) are preserved.

Generate the verification HMAC key once, without printing it or replacing an
existing real key:

```powershell
@'
from pathlib import Path
import secrets
from dotenv import dotenv_values, set_key
path = Path('.env')
value = dotenv_values(path).get('VERIFICATION_SECRET', '') or ''
if not value or value.startswith('REPLACE_'):
    set_key(str(path), 'VERIFICATION_SECRET', secrets.token_urlsafe(48))
print('Verification key configured; its value was not printed.')
'@ | .\.venv\Scripts\python.exe -
```

Keep this key stable across restarts; rotating it invalidates outstanding codes.
Missing SMTP credentials do not prevent local signup: Mailpit uses no credentials.
If SMTP is unavailable, signup still returns its generic response and preserves
the account; the failed challenge is invalidated so resend can be attempted again.
Missing `DATABASE_URL` leaves liveness working and readiness unavailable. Missing
`VERIFICATION_SECRET` disables signup/verification with a generic 503.

The example file lists the actual settings:

| Settings | Meaning / defaults |
| --- | --- |
| `ENVIRONMENT` | `development`, `test`, or `production` |
| `DATABASE_URL` | Runtime PostgreSQL URL |
| `MIGRATION_DATABASE_URL` | Optional direct migration endpoint; falls back to runtime URL |
| `VERIFICATION_SECRET` | Random secret, at least 32 characters, outside the database |
| `VERIFICATION_TTL_SECONDS`, `VERIFICATION_MAX_ATTEMPTS`, `VERIFICATION_RESEND_COOLDOWN_SECONDS` | 600, 5, 60 |
| `SESSION_TTL_SECONDS` | 604800 (7 days) |
| `SESSION_COOKIE_NAME`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE` | `cinemind_session`, false for loopback development, `lax` |
| `TRUSTED_ORIGINS` | JSON array of exact origins; default local API origins on port 8001 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_TLS` | `127.0.0.1`, 1025, `none`; TLS alternatives: `starttls`, `ssl` |
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TIMEOUT_SECONDS` | Optional paired credentials, sender, timeout (5 seconds) |
| `DB_CONNECT_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS` | 5 seconds, 5000 ms, 3000 ms |
| `AUTH_RATE_WINDOW_SECONDS`, `AUTH_IP_LIMIT`, `AUTH_ACCOUNT_LIMIT`, `DELIVERY_ACCOUNT_LIMIT` | 60 seconds; 30 requests/client/action, 10/address/action, 3/address for delivery |
| `TEST_DATABASE_URL` | Test runner only; a separate local database named `cinemind_test*` |

## Migrations

There is no table creation at application startup. Inspect the target first;
then run forward migrations only on the intended development database:

```powershell
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

The chain is `0001` (nine application tables) → `0002` (reject whitespace-only
completed messages, including tabs/newlines). `alembic_version` is Alembic's
additional bookkeeping table. Revisions use named constraints, UUIDs, TIMESTAMPTZ,
indexes, and database cascades. Preserve applied revisions. Do not reset or
downgrade the configured development/Neon database.

Migration connections use `NullPool`. If a pooler cannot run the required DDL,
configure `MIGRATION_DATABASE_URL` with the project's **direct** endpoint rather
than stripping TLS settings or guessing another database. Its role must have DDL
privileges. The runtime pool uses transaction-local statement/lock timeouts.

## Run and probe

```powershell
.\.venv\Scripts\python.exe -m uvicorn cinemind.main:app --app-dir backend --host 127.0.0.1 --port 8001
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/ready
```

`GET /health` is liveness only: HTTP 200, `{"status":"ok"}`. `GET /ready` runs
`SELECT 1` with bounded connection/pool/query waits: 200 `{"status":"ready"}` or
503 with generic output. Neither claims AI-provider readiness. Port 8001 avoids
the Windows restriction observed on port 8000. Stop the server with `Ctrl+C`.

## Local email capture

The application uses real SMTP. An optional free local Mailpit inbox can be run
with an already working Docker daemon (no Docker configuration is part of this app):

```powershell
docker run --rm --name cinemind-mailpit -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit
```

Open `http://127.0.0.1:8025` to read captured mail. It stays in the local inbox;
do not configure forwarding/relay for tests. `Ctrl+C` stops this container.
The automated suite uses an in-memory test mailer and a separate loopback SMTP
capture server. It does not contact real recipients. Local capture is not proof
of external provider delivery; external SMTP credentials/TLS/deliverability need
their own verification before deployment. Never enable SMTP debug logging.

SMTP is dispatched by a sync background task after the generic HTTP response,
outside the database transaction and off the event loop. This also keeps provider
latency out of the signup/resend response. Background tasks are process-local:
a crash can lose a pending delivery; users can resend after the cooldown.

## API and browser security

All unsafe methods (including signup, verification, login and logout) require:

```text
Origin: http://127.0.0.1:8001
X-CineMind-CSRF: 1
Content-Type: application/json   (when sending a JSON body)
```

The Origin must match `TRUSTED_ORIGINS` exactly and the custom header must be `1`.
Requests with missing/null/foreign origins or missing custom headers are rejected.
This custom-header policy relies on browser same-origin/CORS enforcement;
credentialed CORS permits only the exact trusted origins and headers. CLI clients
must provide these headers too. SameSite is defense in depth, not the sole CSRF control.

Cookies are host-only, `HttpOnly`, path `/`, with explicit SameSite; set and clear
use matching attributes. Insecure cookies are restricted to loopback development
writes using both host and client checks. Production settings require Secure
cookies, HTTPS origins, a verification key and SMTP TLS. For separately hosted
frontend/API, add the exact frontend origin, send `credentials: 'include'` and
the custom header. Different **sites** require `SameSite=none` and Secure/HTTPS;
browser third-party-cookie blocking may still require a same-site API/proxy.
Do not trust arbitrary forwarded client addresses: Uvicorn must only trust your
known reverse proxy. The default loopback command needs no proxy setup.

The development rate limiter is thread-safe but **single-process** and resets on
restart. It uses keyed address/client signals. Multi-worker/production operation
requires a shared limiter and trusted-proxy client-IP configuration. No shared
service is provisioned in this milestone. Codes have independent database-backed
attempt caps, expiry, user-row serialization and a unique outstanding challenge.

Errors use `{"error":{"code":"...","message":"..."}}`. Validation output
does not echo request values. Private missing/inaccessible objects both return
404; invalid or missing sessions return 401. Signup/duplicate/resend responses
are generic. Only valid credentials can reveal `verification_required` (403).
Passwords, code digests, session token hashes, database URIs and storage keys are
not public response fields. Responses use `Cache-Control: no-store`.

| Method | Route | Behavior |
| --- | --- | --- |
| POST | `/auth/signup` | Validate and normalize input, create unverified account, schedule code delivery; 202 |
| POST | `/auth/verify-email` | Consume a valid code once, verify address; separate login required |
| POST | `/auth/resend-verification` | Generic 202, cooldown, invalidate earlier challenge |
| POST | `/auth/login` | Verified credentials → fresh opaque session cookie and safe profile |
| POST | `/auth/logout` | Revoke only the presented session, clear cookie; repeated calls harmless |
| GET | `/auth/me` | Safe authenticated profile |
| GET | `/auth/sessions` | Caller-owned session metadata, `is_current`, bounded `limit`/`offset` |
| DELETE | `/auth/sessions/{session_id}` | Revoke one owned session, clear current cookie if necessary |
| POST / GET | `/conversations` | Create or list owned conversations |
| GET / PATCH / DELETE | `/conversations/{id}` | Read, rename/change mode, or delete owned conversation |
| POST / GET | `/conversations/{id}/messages` | Persist completed user text or read ordered history (`after` sequence cursor) |
| GET | `/memories` | Active account-wide records, or account-wide + exact `conversation_id`; optional scope/status filters |
| PATCH / DELETE | `/memories/{id}` | Edit content or delete owned memory |
| GET | `/documents`, `/documents/{id}` | Permitted metadata only; optional `scope`/`conversation_id` filters |

Lists default to 50 and cap at 100. Offset-based lists cap offset at 100000.
Caller-supplied ownership/role/status fields are rejected. The first user message
sets the default title without an LLM; a private `title_is_customized` flag preserves
an explicit rename, even a rename back to `New chat`. Conversation-row locks
allocate stable message sequence numbers for concurrent user and assistant writes.

## Internal persistence boundary for the future AI layer

Use authenticated identity from `routes.dependencies.current_user`; never trust a
body-supplied owner. The services own transactions and return typed Pydantic DTOs:

| Module under `backend/cinemind/services/` | Operations |
| --- | --- |
| `auth.py` / `AuthService` | Signup, case-insensitive account lookup, verify/resend, credential checks, safe profile, sessions, internal `delete_account` |
| `conversations.py` / `Conversations` | CRUD, `append_user`, `append_assistant` (pending only, explicit generation mode), `complete_assistant` with citation mappings in the same transaction, `fail_assistant`, `messages`, `recent_history`, `resolve_citation` |
| `memories.py` / `Memories` | Internal `create`, account/exact-conversation `list`, `edit`, `supersede`, `delete`; active-only by default |
| `documents.py` / `Documents` | Trusted `create_global`, owner-aware `create_private`, metadata `read`/`list`, scoped `find_duplicate`, `set_ingestion_state`, `delete`, batch `save_chunks`, permitted `allowed_chunks` |

These are synchronous operations: call from sync endpoints or
`await run_in_threadpool(...)` in future async orchestration. User-message writes
commit independently of any future generation call. No assistant response is
generated or simulated by this application.

`save_chunks` accepts already extracted text (1–2000 records per atomic batch).
It performs no parsing, chunking, embedding or search. Global creation, ingestion,
chunk/citation writes and ownership/scope changes have no public write endpoints.
Stored document scope/ownership is immutable through these methods. Storage keys
are internal references, never public URLs. No files are uploaded or downloaded.

Memory source provenance is checked against its user and, when scoped, its exact
conversation. Database composite FKs enforce matching conversation owners for
documents/memories. Citation writes require a pending assistant message, a ready
source and visibility under that message's recorded `response_mode`: global only
for `general`/`grounded`; the user's personal or exact-conversation sources only
for `personal_library`. History rechecks visibility and exposes only permitted
title/page data; hidden sources have `available=false`. This is an authorization
check, not citation evidence validation.

Deleting a user cascades their private rows and preserves global/other-user data.
Deleting a conversation removes its messages, scoped documents and scoped memories;
account-wide memories survive and deleted source-message FKs become null.
Document deletion removes chunks/citation mappings; saved answer content remains,
and `resolve_citation` returns unavailable for a deleted mapping. Database cascades
do not delete files. Once a storage provider exists, coordinate deletion and file
cleanup there before exposing file deletion/upload/download features.

## Tests with isolated real PostgreSQL

Tests never use SQLite or fall back to `DATABASE_URL`. Without `TEST_DATABASE_URL`,
database-dependent tests are explicitly skipped. A configured test target must be
loopback and its database name must start with `cinemind_test`; tests truncate that
database's application rows. Do not point it at a database containing useful data.

Create/start a separate local cluster, leaving the existing Windows service alone:

```powershell
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$testData = Join-Path (Get-Location) '.local-postgres\pgdata'
$testLog = Join-Path (Get-Location) '.local-postgres\server.log'
if (!(Test-Path (Join-Path $testData 'PG_VERSION'))) {
    & "$pgBin\initdb.exe" -D $testData -U cinemind_test --auth=trust --encoding=UTF8 --no-locale
}
& "$pgBin\pg_ctl.exe" -D $testData status
if ($LASTEXITCODE -ne 0) {
    & "$pgBin\pg_ctl.exe" -D $testData -l $testLog -o '-h 127.0.0.1 -p 55432' -w start
}
$exists = & "$pgBin\psql.exe" -h 127.0.0.1 -p 55432 -U cinemind_test -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = 'cinemind_test'"
if ($exists -ne '1') {
    & "$pgBin\createdb.exe" -h 127.0.0.1 -p 55432 -U cinemind_test cinemind_test
}
$env:TEST_DATABASE_URL = 'postgresql://cinemind_test@127.0.0.1:55432/cinemind_test'
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:\TEST_DATABASE_URL
```

This ignored disposable cluster accepts trusted connections **only on loopback**;
it is solely for local tests. Use appropriate credentials for any other database.
Tests upgrade their own target through Alembic and compare the actual schema to
ORM metadata. Coverage includes auth/session expiry and revocation, delivery
failure, old/reused/expired codes, persistent failure counts, CSRF, concurrent
verification/resend/message writes, uniqueness/check/FK constraints, two-user
isolation, citation modes/provenance and deletion cascades. SMTP capture has no relay.

Stop only this test cluster when finished:

```powershell
& "$pgBin\pg_ctl.exe" -D $testData -m fast -w stop
```

## Smoke flow with a PowerShell cookie jar

Run the API, apply development migrations and start the local inbox first.
Use an example address in the local inbox only. `WebRequestSession` is the cookie
jar; do not print it or its cookies. Password/code prompts do not echo input.

```powershell
$base = 'http://127.0.0.1:8001'
$headers = @{ Origin = $base; 'X-CineMind-CSRF' = '1' }
$email = 'learner@example.com'
$passwordSecret = Read-Host 'Choose a local test password (at least 12 characters)' -AsSecureString
$password = [System.Net.NetworkCredential]::new('', $passwordSecret).Password
$signup = @{ username = 'learner'; email = $email; password = $password } | ConvertTo-Json
Invoke-RestMethod "$base/auth/signup" -Method Post -Headers $headers -ContentType 'application/json' -Body $signup -SessionVariable cookieJar

# Read the code in http://127.0.0.1:8025, not in API output or logs.
$codeSecret = Read-Host 'Code from the local inbox' -AsSecureString
$code = [System.Net.NetworkCredential]::new('', $codeSecret).Password
$verification = @{ email = $email; code = $code } | ConvertTo-Json
Invoke-RestMethod "$base/auth/verify-email" -Method Post -Headers $headers -ContentType 'application/json' -Body $verification -WebSession $cookieJar

$login = @{ email = $email; password = $password } | ConvertTo-Json
Invoke-RestMethod "$base/auth/login" -Method Post -Headers $headers -ContentType 'application/json' -Body $login -WebSession $cookieJar
Invoke-RestMethod "$base/auth/me" -WebSession $cookieJar
$conversation = Invoke-RestMethod "$base/conversations" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' -WebSession $cookieJar
$message = @{ content = 'My first persisted user message' } | ConvertTo-Json
Invoke-RestMethod "$base/conversations/$($conversation.id)/messages" -Method Post -Headers $headers -ContentType 'application/json' -Body $message -WebSession $cookieJar
Invoke-RestMethod "$base/conversations/$($conversation.id)/messages" -WebSession $cookieJar
Invoke-RestMethod "$base/auth/logout" -Method Post -Headers $headers -WebSession $cookieJar
Remove-Variable password,passwordSecret,signup,code,codeSecret,verification,login,cookieJar
```

This manual flow writes an example account and conversation to the configured
development database. Automated verification uses only the isolated test database
and never inserts smoke/test rows into Neon.

## Implementation references

- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [SQLAlchemy constraints](https://docs.sqlalchemy.org/en/21/core/constraints.html)
- [Psycopg installation](https://www.psycopg.org/install/)
- [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP Email Validation and Verification](https://cheatsheetseries.owasp.org/cheatsheets/Email_Validation_and_Verification_Cheat_Sheet.html)
- [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Mailpit Docker setup](https://mailpit.axllent.org/docs/install/docker/)
