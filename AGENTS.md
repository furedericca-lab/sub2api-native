# AGENTS.md - Sub2API Native project contract

This document is the canonical development, build, deployment, maintenance,
upgrade, migration, and rollback contract for Sub2API Native. It is written
for operators and contributors who work from a local or LAN checkout.

## Project contract

Sub2API Native manages Sub2API sites and the accounts and keys attached to
them. The resource model is:

~~~text
Profile (site) -> Account (identity) -> ApiKey (credential) -> Relay (runtime)
~~~

Relay derives its schedulable set from enabled Profiles, active Accounts, and
the selected active API Keys. Relay is runtime-only: it does not own a second
asset inventory. Registration and manual verification are Account intake
paths, while registration attempts remain read-only audit evidence.

The user-facing console uses the terms 站点池, 账户池, 密钥池, API 聚合, and
邮箱设置. Registration starts from a site row and its active or recent state
is shown below the site pool; it is not a permanent navigation page.

The API surface is intentionally Responses-only:

- GET /v1/models
- POST /v1/responses

The deployment unit is one repository, one locally built image, and one
container. The container runs Sub2API Native and the pinned OutlookEmail
submodule as two native processes. They retain independent application and
SQLite ownership and exchange business data only through HTTP.

## Repository and source rules

- Keep vendor/outlookEmail as a clean Git submodule at an explicit commit.
  Never patch, fork, copy, or import its Python source into backend code.
- Initialize checkouts with git submodule update --init --recursive. A
  superproject commit, not an unpinned working tree, defines the deployed
  OutlookEmail version.
- Keep credentials, cookies, API keys, real proxy values, deployment
  addresses, runtime databases, logs, screenshots, and operator input out of
  tracked files.
- Local planning and acceptance records under .wiki/ and .scopes/ are ignored
  and are not part of the public source distribution.
- When working from an internal checkout where these directories exist, use
  .wiki/index.md and the relevant .scopes contract or phase notes as optional
  architecture and decision context. Public forks may not contain them; do
  not make tracked source depend on their presence or copy private content
  into published documentation.
- front/src is the frontend source of truth. front/dist is generated output:
  do not hand-edit it or mix generated churn into focused source changes.
- Preserve existing package managers, lockfiles, database ownership, and
  internal English names such as Profile, Account, ApiKey, and Relay.

## Development

Use a local Python virtual environment for backend work:

~~~bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest backend/tests -q
~~~

For the frontend:

~~~bash
cd front
npm ci
npx tsc --noEmit
npm run build
~~~

Run focused tests for the modules being changed, then the full backend suite
and frontend typecheck/build before publication. Do not place real credentials
in test fixtures; use reserved example addresses and synthetic values.

### Registration batch gate

Batch registration remains fail-closed until live isolation acceptance is
complete. `SUB2API_GATE_L_MAX_COUNT` is the single limit (default `1`), and
the API and site-pool controls use the same value. Before raising it, verify
the second account with a clean browser identity: its cookies and
`sessionStorage` must not inherit the first account's authenticated state.
The code-enforced **Gate L** rejects higher counts until that acceptance is
recorded; do not treat a documentation-only check as sufficient.

## Build contract

deploy/compose.yaml is the only source-build entry point. Its build context
is the repository root and its image is sub2api-native:local. Run the build
from the repository root or deploy directory with the explicit file:

~~~bash
docker compose -f deploy/compose.yaml build --pull=false
~~~

deploy/docker-compose.yml is image-only runtime configuration. It has no
build section and starts only the locally built image:

~~~bash
docker compose -f deploy/docker-compose.yml up -d --no-build
~~~

Both Compose files must stay aligned for environment, mounts, ports,
healthcheck, container name, and pull_policy: never. For local Compose, do not
use docker compose pull, remote build services, registry pulls, or runtime
image updates. Optional registry publication is isolated to the explicit
manual `.github/workflows/docker-build-push.yml` workflow below and is never
part of the local deployment path.

The source-build file keeps build.network: host and forwards only the shell's
HTTP_PROXY, HTTPS_PROXY, and NO_PROXY as build arguments. Proxy values are
build-boundary inputs; never hard-code, log, or copy them into the image.
The Dockerfile must not depend on a remote syntax/frontend image.

BuildKit output must run to completion. Do not pipe an active build through
head or tail, terminate the client while it is running, or start a duplicate
build after losing the session. Before a large build, inspect disk space with
df -h and docker system df -v. If cleanup is explicitly authorized, remove
only unused BuildKit cache. Never use docker system prune -a to make space and
never remove running images, rollback images, volumes, canonical data, or
restricted backups.

The image contains two independent Python environments:

- /opt/sub2api-venv for Sub2API Native;
- /opt/outlookemail-venv for OutlookEmail and Gunicorn.

Each environment must pass its own pip check. Camoufox installation is valid
only after resolving its executable path; a successful CLI return code alone
is insufficient.

The push/PR workflow is test-only: it may install dependencies, compile
Python, execute pytest, run the read-only gate fixtures, and build the
frontend. Docker image publication is intentionally separate and manual.

`.github/workflows/docker-build-push.yml` runs only on `workflow_dispatch`,
never on every commit or pull request. It repeats the source/submodule and
application checks, then builds and publishes the pinned source as a
`linux/amd64` image to `ghcr.io/furedericca-lab/sub2api-native`. The workflow
uses the repository `GITHUB_TOKEN`; no credential belongs in the repository.
The release tag is supplied explicitly by the operator, and the workflow also
publishes an immutable full-commit `sha-<commit>` tag. Trigger it locally with:

~~~bash
gh workflow run docker-build-push.yml --ref main -f tag=latest
~~~

Local Docker Compose remains the normal deployment path. A registry image is
an optional publication artifact, not a replacement for the local data and
runtime contract.

## Production safety rules

Inspection of a production Compose project is read-only. Without explicit
deployment authorization, allowed commands include:

- docker inspect;
- docker ps;
- docker compose config;
- docker compose config --format json;
- docker system df.

The following are APPLY operations and are forbidden during inspection:

- docker compose create;
- docker compose up, down, start, stop, restart, rm, or build;
- docker pull, push, prune, or image deletion;
- any command that changes containers, images, volumes, networks, or data.

In particular, docker compose create is not a harmless preview. It can
recreate a service and must never be used to test a gate. Do not print
environment values, database contents, session cookies, or credential-bearing
command output.

## Pre-deploy mailbox handoff gate

deploy/check-mailbox-handoff.sh is an independent, read-only gate. It renders
each supplied Compose file with docker compose config --format json and
examines the rendered service environment and the target 5000 port mapping.
It never contacts the Docker daemon, reads a container, creates a resource, or
loads a credential file.

The gate exists because service health and the OutlookEmail HTTP contract do
not consume the browser's public redirect target. A container can be healthy
while a LAN browser is sent to an unusable loopback address.

OUTLOOKEMAIL_BIND_HOST and OUTLOOKEMAIL_PUBLIC_HOST have different meanings:

- BIND is the host-side address where Docker publishes container port 5000.
- PUBLIC is the address put into the browser handoff URL.

PUBLIC does not have to equal BIND. A LAN bind with a reachable hostname is
valid. The rendered configuration must satisfy these simple rules:

1. PUBLIC must be present and non-empty.
2. PUBLIC must not be 0.0.0.0, ::, [::], or another unspecified wildcard.
3. If port 5000 is published to a non-loopback address, PUBLIC must not be
   127.x, localhost, ::1, or [::1].
4. Ambiguous multiple target-5000 mappings fail closed.

Run the gate before any build or recreate, and again immediately before the
single APPLY command. Human operators must run the standalone script before
docker compose up -d --no-build. Deterministic fixtures in
deploy/test-check-mailbox-handoff.sh use synthetic Compose YAML and real
docker compose config only.

## Deployment

The shortest supported deployment flow is:

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

Run commands from the repository root, or adjust paths consistently when
working inside deploy/. The normal update entry point is deploy/update.sh. It
must perform the same gate before build/recreate and after build, then verify
health and the HTTP contract. It never fetches the superproject or moves a
submodule to an unreviewed upstream commit.

The single service publishes Sub2API on host port 8787 and native OutlookEmail
on host port 15000 by default. OUTLOOKEMAIL_BIND_HOST controls the published
listen address; OUTLOOKEMAIL_PUBLIC_HOST controls the browser target. Keep the
mailbox port loopback-only unless LAN administration is intentional.

## Runtime topology

~~~text
dumb-init
  -> docker/entrypoint.sh
       -> Gunicorn OutlookEmail (one gthread worker, four threads)
       -> Xvfb + FastAPI Sub2API Native
~~~

OutlookEmail listens on container port 5000. Sub2API Native listens on
container port 8787. The wrapper forwards TERM and INT to both children and
exits non-zero if either core process exits, allowing restart: unless-stopped
to restart the complete appliance. Do not add Supervisor, systemd, Nginx,
Caddy, a WSGI bridge, or a Flask mount.

## OutlookEmail integration

OutlookEmail is the pinned submodule at vendor/outlookEmail and is copied
unchanged into the local image. Check its current version and commit with:

~~~bash
git submodule status --recursive
git -C vendor/outlookEmail status --short
~~~

The current checkout may report v3.0.6; that is evidence for the checked-in
pin, not a permanent version requirement. Keep the submodule working tree
clean.

The embedded process owns data/outlookemail/ and starts with:

- DATABASE_PATH set to /app/data/outlookemail/outlook_accounts.db;
- a private runtime.env at data/outlookemail/runtime.env with mode 0600;
- LOGIN_PASSWORD and SECRET_KEY loaded as data, not sourced as shell code;
- DOCKER_UPDATE_ENABLED=false and no Docker socket mount.

Sub2API Native uses the OutlookEmail HTTP contract only. It may call the root
endpoint, POST /api/extension/login, GET /api/external/accounts, and
GET /api/external/emails. It must not open OutlookEmail SQLite tables or rely
on undocumented schema fields.

The mailbox settings page exposes service status and an account-management
button. The backend endpoint /api/mailbox/launch authenticates the current
Sub2API session, uses the private runtime login value server-side, obtains a
one-time upstream launch path, and opens the native UI on the published
15000 host. Do not iframe or duplicate the upstream account-management UI.

If the native password is intentionally changed, use the credential-safe
scripts/sync-outlookemail-runtime-password.py tool. It validates through the
HTTP endpoint and atomically updates runtime.env without printing the value or
modifying the upstream database.

The compatibility smoke is scripts/check-outlookemail-contract.py. Keep it
small and contract-focused: root availability, extension login shape,
external account response shape, and external email response shape. It must
not import upstream internals or inspect the upstream database.

## Data layout

The host data root is mounted at /app/data:

~~~text
data/
├── config.json
├── web_auth.json
├── accounts/
│   ├── registration_results.sqlite3
│   └── api_keys.key
├── relay/
│   └── relay runtime state
├── screenshots/registration-failures/
└── outlookemail/
    ├── outlook_accounts.db
    ├── runtime.env
    └── upstream-owned resources
~~~

Container logs are mounted separately at /app/logs. The unified data root is
for lifecycle and backup convenience only. It does not merge schemas:
Sub2API owns accounts and relay state, while OutlookEmail owns its database
and resources.

## Schema and migration

### Current deployment contract

- The Sub2API registration database is schema v2 and owns Profile, Account,
  ApiKey, registration attempts, mailbox consumption records, and job
  snapshots.
- Relay runtime state uses schema v3 and is not an asset inventory.
- Account API Key ciphertext requires data/accounts/api_keys.key. If the
  canonical key is missing or a migration fails, startup fails closed; never
  generate a replacement key or silently fall back to an old path.
- Validate SQLite copies with PRAGMA integrity_check and, where applicable,
  PRAGMA foreign_key_check before switching a deployment.

### Legacy upgrade notes

Historical migrations are not required on every deployment. When upgrading an
older installation, stop writers, make a restricted backup, perform the
documented migration once, and verify counts and integrity before starting
the new image. A legacy data/relay/relay.key must be copied byte-for-byte to
data/accounts/api_keys.key only when the old installation actually uses it.

For the former two-container OutlookEmail layout, stop the old mailbox process
before copying its real /app/data source into data/outlookemail/. Copy; do not
move or delete. Preserve the old source, runtime credentials, and backup until
acceptance is complete. Verify database integrity, account/group counts,
existing secret decryption, native UI, and the HTTP contract before cutover.

## Backup and rollback

Back up the complete canonical data root, runtime configuration, database
checksums, submodule commit, and the deployed image metadata to a restricted
operator location. Never put the backup itself in the repository.

The canonical production source is data/outlookemail/. The old dual-container
and legacy bind-mount sources are not a normal rollback path. After the
single-container cutover, rollback means stopping the new service and
rebuilding the prior version from a restricted backup plus a retained rollback
image. Do not delete canonical data, volumes, or rollback artifacts while
validating a release.

## Verification

Use the smallest focused checks first, then the complete acceptance surface:

~~~bash
python -m compileall -q backend docker/config_bootstrap.py
python -m pytest backend/tests -q
cd front && npx tsc --noEmit && npm run build
cd ..
bash deploy/test-check-mailbox-handoff.sh
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/docker-compose.yml config --quiet
git diff --check
~~~

For a running local deployment also verify, without relying on only one
signal:

- exactly one Compose service/container;
- both child processes and the dumb-init process tree;
- port 8787 /api/health;
- native OutlookEmail port 15000 and its root UI;
- the real mailbox settings launch URL from a LAN browser;
- the four compatibility-smoke contract checks;
- Camoufox headed smoke through docker/camoufox_smoke.py;
- SQLite integrity and stable account/group counts;
- restart count and logs without credential values;
- a clean submodule and a clean, intentional worktree;
- final GitHub Actions status for the exact commit.

Do not use contract smoke as a substitute for testing the browser handoff
URL. They exercise different configuration boundaries.

## Submodule upgrade

Production never runs git submodule update --remote. The only supported
upgrade model is:

~~~text
checkout a reviewed upstream commit
  -> update the superproject gitlink
  -> run compatibility tests and focused regressions
  -> build the local image
  -> perform local acceptance
  -> commit the explicit pin
~~~

Do not combine an upstream bump with an unrelated architecture change.

## Known pitfalls

- A local :local image must not be pulled by Compose.
- --pull=false is required for the local source build; missing base images may
  still need to be provisioned on the host.
- Never truncate live BuildKit output or start a duplicate build.
- A Camoufox CLI exit code does not prove that its executable was installed.
- Build proxy settings belong only at the host BuildKit boundary.
- docker compose create changes state and is an APPLY operation.
- BIND and PUBLIC host settings must not be conflated.
- front/dist is generated output, not the frontend source of truth.
- Do not merge SQLite databases or add a second process manager.
