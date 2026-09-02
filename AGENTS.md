# AGENTS.md - Sub2API Native project contract

This file is the contributor and agent contract for Sub2API Native. It keeps
the product model, source boundaries, and development rules in one place.
The canonical operator runbook for building, deploying, reverse proxying,
migrating, upgrading, verifying, and rolling back is
[deploy/README.md](deploy/README.md). Do not duplicate that runbook here.

## Project contract

Sub2API Native aggregates sites deployed with Sub2API. Its resource model is:

~~~text
Profile (site) -> Account (identity) -> ApiKey (credential) -> Relay (runtime)
~~~

Relay derives its schedulable set from enabled Profiles, active Accounts, and
selected active ApiKeys. Relay is runtime-only and does not own a second asset
inventory. Registration and manual verification are Account intake paths;
registration attempts are read-only audit evidence.

The stable user-facing console terms are 站点池, 账户池, 密钥池, API 聚合, and
邮箱设置. Registration starts from a site row and its active or recent state
stays below the site pool. /registration-attempts is an unlisted,
read-only audit route.

The public API is intentionally small and Responses-oriented:

- GET /v1/models
- POST /v1/responses

The runtime architecture is one repository, one locally built image, and one
container. The container runs Sub2API Native and the pinned OutlookEmail
submodule as two native processes. They keep independent application and
SQLite ownership and exchange business data only through HTTP.

## Repository and source rules

- Keep vendor/outlookEmail as a clean Git submodule at an explicit commit.
  Never patch, fork, copy, or import its Python source into backend code.
- Initialize a checkout with git submodule update --init --recursive.
  The superproject gitlink, not an unpinned working tree, defines the deployed
  OutlookEmail version.
- Keep credentials, cookies, API keys, real proxy values, private deployment
  addresses, runtime databases, logs, screenshots, and operator input out of
  tracked files and commit history.
- .wiki/ and .scopes/ are local, ignored architecture and maintenance
  records. When they exist in an internal checkout, agents may read
  .wiki/index.md and the relevant scope contract or phase notes for context.
  Public forks may not contain them; tracked source must never depend on them
  or copy their private content into public documentation.
- front/src is the frontend source of truth. front/dist is generated
  output; do not hand-edit it or mix generated churn into focused source
  changes.
- Preserve existing package managers, lockfiles, schema ownership, and the
  internal English names Profile, Account, ApiKey, and Relay.

## Development

Backend development uses a local virtual environment:

~~~bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest backend/tests -q
~~~

Frontend development uses the checked-in lockfile:

~~~bash
cd front
npm ci
npx tsc --noEmit
npm run build
~~~

Run focused tests for changed modules first, then the full backend suite and
frontend typecheck/build before publication. Use reserved example addresses
and synthetic credentials in tests; never place live account data in fixtures.

## Registration safety

Batch registration remains fail-closed until live browser-identity isolation
acceptance is complete. SUB2API_GATE_L_MAX_COUNT is the single limit
(default 1) used by both the API and the site-pool controls. Before raising
it, verify a second account with a clean browser identity: cookies and
sessionStorage must not inherit the first account's authenticated state. The
code-enforced Gate L rejects higher counts until that acceptance is recorded;
documentation alone is not sufficient.

## Maintenance and records

- Keep changes scoped to the requested behavior and preserve unrelated local
  work. Do not amend an existing user commit unless explicitly asked.
- Update the relevant ignored .wiki/ page or .scopes/ phase record when an
  architecture or maintenance decision changes. These records are aids for
  agents and maintainers, not public release artifacts.
- Keep upstream integration thin: Sub2API calls OutlookEmail's documented HTTP
  contract and never reads its SQLite tables directly.
- An OutlookEmail upgrade is a reviewed candidate commit, updated superproject
  gitlink, compatibility tests, focused regressions, local acceptance, and a
  new commit pin. Runtime or unattended git submodule update --remote is not
  an upgrade mechanism.
- Keep LICENSE, user-facing documentation, and generated artifacts aligned
  with the source change. Do not add secrets while preparing release notes or
  screenshots.

## Deployment boundary

All Docker, Compose, local image, dumb-init process, mailbox handoff gate,
Nginx deployment modes, data migration, backup, rollback, disk-safety, and
runtime verification instructions live in [deploy/README.md](deploy/README.md).
Read that file before changing deployment files or operating a running stack.
The application image does not bundle a process manager or reverse-proxy
layer; an external reverse proxy is an optional operator concern. Keep Flask
and FastAPI as separate native applications and keep their HTTP boundary.

## CI and publication boundary

.github/workflows/test.yml is the push/PR test gate: it may install
dependencies, compile Python, run pytest, execute the read-only mailbox gate
fixtures, and build the frontend. It must not deploy or mutate a host.

.github/workflows/docker-build-push.yml is a separate, explicitly manual
workflow. It is started by an operator (for example with gh workflow run),
never by every commit, and its registry publication details belong to the
deployment runbook. No credential is stored in the repository.

## Verification

For source changes, run the focused tests and the complete checks appropriate
to the touched surface. At minimum before a public commit, use:

~~~bash
python -m compileall -q backend docker/config_bootstrap.py
python -m pytest backend/tests -q
cd front && npx tsc --noEmit && npm run build
cd ..
git diff --check
~~~

For any runtime or Docker change, follow the additional checks in
[deploy/README.md](deploy/README.md), including both rendered Compose files,
the read-only mailbox handoff fixtures, and the pinned submodule cleanliness
check. Never print credential-bearing output while validating.
