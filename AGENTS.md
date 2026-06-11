# AGENTS.md

## Cursor Cloud specific instructions

JumpServer **core** (this repo) is the Django backend (REST API + Django auth pages). The web
UI (Lina) and web terminal (Luna) are **separate repos** and are NOT bundled here, so visiting
`/` redirects to `/ui/` and returns a 404 in this environment — that is expected. The functional
surfaces of this backend are the Django login page (`/core/auth/login/`), the REST API
(`/api/v1/...`), and the Swagger docs (`/api/docs/`).

Python runs from the uv-managed venv at `.venv` (Python 3.11). Activate with
`source .venv/bin/activate` before any `manage.py`/`jms` command. The dependency refresh is
handled by the startup update script (`uv pip install -r pyproject.toml`).

### Services must be started manually (not auto-started on a fresh VM)

PostgreSQL and Redis are installed but are NOT started automatically. Start them once per VM
before running the app or tests:

```bash
sudo pg_ctlcluster 16 main start
sudo redis-server /etc/redis/redis.conf
```

DB: database `jumpserver`, role `jumpserver` / password `jumpserver123` (role has `CREATEDB`,
required for the Django test runner to create `test_jumpserver`).

### Configuration

Config is read from `/workspace/config.yml` (gitignored; it holds `SECRET_KEY`/`BOOTSTRAP_TOKEN`
and the Postgres/Redis settings). It is created during setup and persists in the snapshot. If it
is ever missing, copy `config_example.yml` to `config.yml` and set `SECRET_KEY`,
`BOOTSTRAP_TOKEN`, `DB_ENGINE: postgresql`, the DB creds above, and `REDIS_HOST: 127.0.0.1`.

### Running the application (dev)

```bash
source .venv/bin/activate
python jms start web   # gunicorn + uvicorn ASGI workers, auto --reload when DEBUG: true
```

The first `start`/`prepare` run migrates the DB, collects static, and downloads IP GeoIP dbs
(needs network). Listens on `HTTP_LISTEN_PORT` (8080).

### Login / admin credentials caveat

The default `admin` / `ChangeMe` password is rejected by the login flow as "too simple" and
forces a password change; the API token endpoint (`POST /api/v1/authentication/auth/`) even
returns a 500 in that state (a known error-formatting bug, not an env problem). Set a strong
admin password once so login + API work:

```bash
cd apps && python manage.py shell -c "from users.models import User; u=User.objects.get(username='admin'); u.set_password('Cursor@JMS2026'); u.is_first_login=False; u.need_update_password=False; u.save()"
```

The web login encrypts the password client-side via RSA, so scripted form-POST login does not
work — use a real browser for UI login, or use the API token endpoint for programmatic access.

### Lint / test

- Lint: no flake8/pylint/isort are bundled as dependencies. Use the built-in
  `python apps/manage.py check` as the validation/lint step.
- Tests: `cd apps && python manage.py test <module>` (Django test runner). Most `tests.py`
  files are empty placeholders. `apps/ops/test_utils.py` is a stale/broken test (imports
  `Task`/`AdHoc` which no longer exist in `ops.models`) — exclude it. A known-good module is
  `orgs.tests`.
