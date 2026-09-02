# Deployment

This is the canonical operator runbook for Sub2API Native. It covers local
builds, the one-container runtime, optional external Nginx reverse proxying,
mailbox handoff, data migration, upgrades, verification, and rollback. Keep
deployment instructions here; do not create a second runbook in README.md,
AGENTS.md, or another deployment file.

## Runtime contract

The deployment unit is one repository, one image, and one Compose service:

~~~text
sub2api-native container
├── Sub2API Native FastAPI + React       container :8787
└── OutlookEmail Flask/Gunicorn          container :5000
                                           host :15000 (default)
~~~

The image uses dumb-init and a thin docker/entrypoint.sh wrapper. The wrapper
starts one gthread Gunicorn worker for OutlookEmail and headed FastAPI under
Xvfb. It forwards TERM/INT to both children and exits non-zero when either
core process exits, so Docker restart: unless-stopped restarts the whole
deployment unit. The image does not bundle a process manager or a
reverse-proxy layer. Nginx or Caddy may be operated outside the container as
an optional HTTP reverse proxy; do not mount Flask into FastAPI or add a WSGI
bridge.

The native ports are independent and remain visible:

- 8787 is the Sub2API Native console and API.
- 15000 on the host (container 5000) is the OutlookEmail native UI/API.

There is no /mail/ or /sub2api/ path-prefix deployment contract. Use separate
hostnames when a reverse proxy is needed so OutlookEmail remains at its
upstream root path.

## Prerequisites and files

Install Docker Engine with Compose v2 and Git. Initialize the pinned upstream
submodule before building:

~~~bash
git submodule sync --recursive
git submodule update --init --recursive
git submodule status --recursive
git -C vendor/outlookEmail status --short
~~~

The working tree inside vendor/outlookEmail must be clean. The superproject
records the exact upstream commit; do not fetch or update it automatically at
runtime.

There are two Compose files:

- compose.yaml is the source-build entry point and builds sub2api-native:local
  from the repository root context.
- docker-compose.yml is image-only runtime configuration. It has no build
  section and uses only the existing local image with pull_policy: never.

Create local configuration from the examples. These files and all runtime
data are ignored and must never be committed:

~~~bash
cd deploy
cp .env.example .env
cp outlookemail.env.example outlookemail.env
chmod 600 .env outlookemail.env
~~~

After bootstrap, data/outlookemail/runtime.env is the canonical private source
for LOGIN_PASSWORD and SECRET_KEY and must remain mode 0600. The entrypoint
reads those two values as data; it never sources arbitrary shell code. Do not
print them or put them in a ticket, log, screenshot, image, scope, or wiki
page.

## Configuration boundary

The mailbox host and port variables have separate meanings:

| Variable | Meaning | Typical value |
| --- | --- | --- |
| OUTLOOKEMAIL_BIND_HOST | Host address where Docker publishes host port OUTLOOKEMAIL_PORT | 127.0.0.1 |
| OUTLOOKEMAIL_PORT | Host port forwarded to container port 5000 | 15000 |
| OUTLOOKEMAIL_PUBLIC_HOST | Hostname/IP placed in the browser handoff URL | 127.0.0.1 |
| OUTLOOKEMAIL_PUBLIC_PORT | Browser-facing port in that URL; independent of the published port | follows OUTLOOKEMAIL_PORT (15000) |

BIND_HOST controls listening; PUBLIC_HOST and PUBLIC_PORT describe how the
browser reaches the service. They do not have to equal the bind values. If
PUBLIC_PORT is omitted, it follows OUTLOOKEMAIL_PORT. For a reverse proxy, the
published port can stay 15000 while the public port is 80.

OUTLOOKEMAIL_PUBLIC_HOST must be a concrete IP or hostname. It may not be
empty, 0.0.0.0, ::, [::], or another unspecified wildcard. When the mailbox
port is published to a non-loopback host address, the public target must not
be 127.x, localhost, ::1, or [::1].

## Deployment modes

Choose one mode and set the four mailbox variables accordingly. All modes use
the same server-side one-time login flow: Sub2API calls
http://127.0.0.1:5000/api/extension/login, then returns a browser URL for the
native OutlookEmail root UI.

### A. Full LAN / direct ports

Use this when the console and mailbox UI are reached directly from the local
network. Example for a host whose documentation LAN address is 192.0.2.153:

~~~dotenv
OUTLOOKEMAIL_BIND_HOST=192.0.2.153
OUTLOOKEMAIL_PORT=15000
OUTLOOKEMAIL_PUBLIC_HOST=192.0.2.153
OUTLOOKEMAIL_PUBLIC_PORT=15000
~~~

Open:

~~~text
http://192.0.2.153:8787       Sub2API Native
http://192.0.2.153:15000      OutlookEmail native UI
~~~

For a host-local-only mailbox, keep BIND_HOST and PUBLIC_HOST at 127.0.0.1
instead. The read-only gate must pass before any Compose APPLY.

### B. Public main site, mailbox remains LAN-only

Run Nginx on the Docker host as an external reverse proxy for the main console
and keep mailbox administration on the LAN:

~~~dotenv
OUTLOOKEMAIL_BIND_HOST=192.0.2.153
OUTLOOKEMAIL_PORT=15000
OUTLOOKEMAIL_PUBLIC_HOST=192.0.2.153
OUTLOOKEMAIL_PUBLIC_PORT=15000
~~~

Nginx sends sub2api.example.com to 127.0.0.1:8787; the native mailbox port
stays reachable only on the LAN. A user outside the LAN can use the public
console and Responses API, but the mailbox-management tab naturally requires
a route to 192.0.2.153:15000.

### C. Both services public on independent hostnames

Keep both upstream applications at their root paths and give them separate
DNS names. The host port remains private to Nginx while the browser target is
the mailbox hostname:

~~~dotenv
OUTLOOKEMAIL_BIND_HOST=127.0.0.1
OUTLOOKEMAIL_PORT=15000
OUTLOOKEMAIL_PUBLIC_HOST=mail.example.com
OUTLOOKEMAIL_PUBLIC_PORT=80
~~~

The resulting public URLs are:

~~~text
http://sub2api.example.com       Sub2API Native
http://mail.example.com          OutlookEmail native UI
~~~

The one-time handoff URL is generated on mail.example.com, so the session
cookie is scoped to the mailbox hostname. Do not expose the internal
127.0.0.1:15000 address to the browser and do not rewrite either application
under /mail/ or /sub2api/.


## External Nginx (HTTP) examples

These snippets assume Nginx runs on the same host as Docker. They are
operator-side configuration: Nginx is not installed in the image and does not
participate in the local Compose service count. DNS for both names must resolve
to that host.

For the main console and streaming Responses API:

~~~nginx
server {
    listen 80;
    server_name sub2api.example.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /v1/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
~~~

window.location.origin + /v1 makes the console show the address users actually
used, so the API service address follows the public hostname without a
hard-coded 8787. This is the only Nginx server block required for mode B. Mode
C uses this block plus the dedicated mailbox server below.

For the public OutlookEmail hostname, proxy the upstream root path directly:

~~~nginx
server {
    listen 80;
    server_name mail.example.com;

    location / {
        proxy_pass http://127.0.0.1:15000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
~~~

The mailbox server must be a separate server_name. A same-host path prefix such
as location /mail/ is not supported because upstream static paths, redirects,
APIs, and one-time login paths are root-relative.

The examples intentionally use plain HTTP for small LAN/public-network tests.
If an operator later terminates TLS, validate the public scheme and proxy
policy as a separate change; do not silently publish an HTTP upstream as an
HTTPS browser target.

## Read-only mailbox handoff gate

Run check-mailbox-handoff.sh before every build or Compose recreate and again
immediately before the single APPLY command. It renders the final Compose
configuration with docker compose config --format json, reads the rendered
OUTLOOKEMAIL_PUBLIC_HOST and OUTLOOKEMAIL_PUBLIC_PORT, and inspects the target
5000 mapping's published host_ip. It rejects empty/wildcard hosts and invalid
ports before a browser handoff can be published. It never contacts a
container, reads a credential file, or creates Docker resources.

Allowed inspection commands are docker inspect, docker ps, docker compose
config (including --format json), and docker system df. docker compose create
is an APPLY operation, not an inspection shortcut. The gate and its fixtures
must never execute create, up, down, start, stop, restart, build, pull, rm,
exec, run, or prune.

Deterministic fixtures use synthetic YAML and real Compose rendering only:

~~~bash
bash deploy/test-check-mailbox-handoff.sh
~~~

The fixture matrix covers loopback direct mode, LAN IP, reachable hostname,
LAN plus loopback public target (reject), missing/empty public target
(reject), and wildcard public targets (reject).

## Build and start

The shortest supported local flow is:

~~~bash
git submodule sync --recursive
git submodule update --init --recursive

deploy/check-mailbox-handoff.sh
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/docker-compose.yml config --quiet
docker compose -f deploy/compose.yaml build --pull=false

deploy/check-mailbox-handoff.sh
docker compose -f deploy/docker-compose.yml up -d --no-build
~~~

update.sh is the normal repeatable update entry point. It performs the same
pre-build and pre-APPLY gate, builds the pinned local source, starts only the
image-only Compose service, waits for health, and runs the mailbox HTTP
contract. It never pulls or moves the submodule automatically.

compose.yaml keeps build.network: host and forwards only shell HTTP_PROXY,
HTTPS_PROXY, and NO_PROXY as build arguments. Use --pull=false; do not use
docker compose pull or a remote build service. The two Python dependency
graphs remain isolated in /opt/sub2api-venv and /opt/outlookemail-venv, and
each environment must pass its own pip check. Camoufox is valid only after its
executable path resolves, not merely after a successful CLI return code.

## Runtime and data ownership

The canonical host data root is mounted at /app/data:

~~~text
data/
├── config.json
├── web_auth.json
├── accounts/
│   ├── registration_results.sqlite3
│   └── api_keys.key
├── relay/
├── screenshots/registration-failures/
└── outlookemail/
    ├── outlook_accounts.db
    ├── runtime.env
    └── upstream-owned resources
~~~

Sub2API owns its account, key, registration, and relay state. OutlookEmail owns
data/outlookemail/ and its schema. A unified data root is only a backup
convenience; it is not a merged database. Sub2API must never open an
OutlookEmail SQLite file. Use the documented HTTP contract:

- root availability;
- POST /api/extension/login;
- GET /api/external/accounts;
- GET /api/external/emails.

The native account-management button authenticates the current Sub2API admin,
uses the private runtime login value server-side, obtains a one-time upstream
path, and opens the configured public host/port. It does not iframe or copy
the OutlookEmail UI. If the native password is intentionally changed, use
scripts/sync-outlookemail-runtime-password.py; it validates over HTTP and
atomically updates runtime.env without modifying the upstream database.

## Migration, backup, and rollback

Historical migrations are not a routine deployment step. For an older
installation, stop all writers, create a restricted backup, and verify
PRAGMA integrity_check (and PRAGMA foreign_key_check where applicable) before
starting the new image. Registration schema v2, Relay schema v3, and the
canonical account key at data/accounts/api_keys.key are current contracts;
missing keys or failed migrations must fail closed rather than generate
replacements.

For the former two-container OutlookEmail layout, stop the old mailbox process
before copying its real /app/data source into data/outlookemail/. Copy, never
move or delete. Preserve the old source, runtime credentials, and a restricted
backup until acceptance is complete. Verify DB integrity, account/group counts,
existing secret decryption, native UI, scheduler, and the HTTP contract before
cutover.

The current canonical production source is data/outlookemail/. The old
container and legacy bind mount are not a normal rollback path. A rollback
stops the new service and restores the restricted backup with the retained
rollback image/source metadata. Do not delete canonical data, volumes,
backups, or rollback images while validating a release.

## Disk and BuildKit safety

Before a large build, inspect disk usage without mutating Docker:

~~~bash
df -h /
docker system df -v
~~~

If space is genuinely insufficient, stop. With explicit authorization only,
remove unused BuildKit cache and recheck df -h /. Never use docker system prune
-a, and never delete running images, rollback images, volumes, canonical data,
or restricted backups. Let BuildKit run to completion; do not pipe active
output through head/tail, terminate the client mid-build, or start a duplicate
build after losing a session.

## Verification checklist

For source and deployment changes, run focused checks first and then the
relevant full checks:

~~~bash
python -m compileall -q backend docker/config_bootstrap.py
python -m pytest backend/tests -q
(cd front && npx tsc --noEmit && npm run build)
bash deploy/test-check-mailbox-handoff.sh
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/docker-compose.yml config --quiet
git submodule status --recursive
git -C vendor/outlookEmail status --short
git diff --check
~~~

For a running stack, confirm all of the following at the effective boundary:

- exactly one Compose service and one container;
- process tree dumb-init -> entrypoint.sh -> Gunicorn + Xvfb/FastAPI;
- 8787/api/health and native OutlookEmail :15000/ are reachable;
- the real mailbox settings button produces the correct LAN or public-host
  launch URL in a browser;
- all four compatibility-smoke HTTP checks pass;
- Camoufox headed smoke passes;
- SQLite integrity and stable account/group counts;
- no credential values appear in logs and restart count is expected;
- the submodule is clean and the worktree contains only intentional changes;
- the exact commit's GitHub Actions test workflow is green.

Contract smoke does not replace browser handoff validation: health and API
contract checks cannot detect a wrong PUBLIC_HOST or PUBLIC_PORT.

## CI and manual image publication

.github/workflows/test.yml is test-only and may run on pushes or pull
requests. It initializes the recursive submodule, runs Python/frontend checks,
and executes read-only gate fixtures. It does not build or deploy a runtime
container.

.github/workflows/docker-build-push.yml is intentionally workflow_dispatch
only. An operator starts it explicitly, for example:

~~~bash
gh workflow run docker-build-push.yml --ref main -f tag=latest
~~~

The workflow checks the pinned source and publishes the requested image tag
plus an immutable commit tag. This publication path is separate from local
Compose deployment and never stores credentials in the repository.

## OutlookEmail upgrade model

Keep the upstream submodule unchanged in production. A deliberate upgrade is:

~~~text
review candidate upstream commit
  -> update the superproject gitlink
  -> run HTTP compatibility and focused tests
  -> build with --pull=false
  -> perform local acceptance and data checks
  -> commit the explicit pin
~~~

Do not combine an upstream bump with an architecture change, and do not run
git submodule update --remote as part of a runtime deployment.

## Known pitfalls

- A local :local image must not be pulled by Compose.
- docker compose create changes state and is an APPLY operation.
- BIND and PUBLIC host/port settings describe different network boundaries.
- Standard HTTP public port 80 is configured with OUTLOOKEMAIL_PUBLIC_PORT; the
  published upstream can remain 15000.
- Responses streaming needs Nginx buffering disabled and a long read timeout.
- Root-path hostnames are supported; /mail/ and /sub2api/ path prefixes are
  intentionally outside the contract.
- A Camoufox CLI exit code alone does not prove browser installation.
- Build proxy values belong only at the host BuildKit boundary.
- front/dist is generated output, not frontend source.
- Do not merge SQLite databases or add an in-container process manager.
