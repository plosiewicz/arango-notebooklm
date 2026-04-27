# Contributing

Short notes for anyone working in this repo.

## Prerequisites

- Python 3.12 (matches the Cloud Functions runtime).
- `gcloud` CLI authenticated as an ArangoDB Google account with
  access to `slack-notebooklm-sync`.
- Membership in the project's Google group / IAM bindings required
  to deploy Cloud Functions and read Secret Manager.

## The shared/ module

`shared/` lives at the repo root but is imported by all three services.
At deploy time, each service's `deploy.sh` rsyncs `shared/` into the
service directory so `gcloud functions deploy` uploads it with `main.py`:

```
notebooklm/
├── shared/            source of truth
├── slack-sync/
│   ├── main.py
│   └── shared/        deploy.sh rsyncs this in, cleans up on exit (gitignored)
├── gong-sync/
│   └── ...
└── config-sync/
    └── ...
```

The in-service copies are gitignored. Do not edit them - change
`shared/` at the root and redeploy.

Each service's `.gcloudignore` ends with `!shared/` so `shared/`
uploads even if a preceding rule would have excluded it.

## Local dev

Run a service locally with `functions-framework`, using `PYTHONPATH=.`
so the `shared/` import resolves:

```bash
cd slack-sync
pip install -r requirements.txt
pip install functions-framework
PYTHONPATH=.. functions-framework --target=slack_webhook --debug
```

Same pattern for `gong-sync` (`--target=gong_sync`) and
`config-sync` (`--target=config_sync`).

Local runs still read from real GCS, Secret Manager, Slack, and
Gong - there are no stubs. `gcloud auth application-default login`
covers the Google side.

## Deploying

```bash
./deploy.sh slack|gong|config|all [extra gcloud args...]
```

Each `deploy.sh`:

1. `rsync`s `shared/` into the service dir
2. `gcloud functions deploy <service> --source=.` with the right
   runtime / entry-point / memory / timeout baked in
3. removes the rsynced copy on exit (EXIT trap)

Extra args pass through, so you can tack on things like
`--update-env-vars=FOO=bar` or `--remove-env-vars=STALE`.

## Adding a new secret

1. Create the secret:
   ```bash
   echo -n "<value>" | gcloud secrets create <name> \
     --project=slack-notebooklm-sync \
     --replication-policy=automatic \
     --data-file=-
   ```
2. Grant the runtime SA access:
   ```bash
   gcloud secrets add-iam-policy-binding <name> \
     --project=slack-notebooklm-sync \
     --member=serviceAccount:399790122111-compute@developer.gserviceaccount.com \
     --role=roles/secretmanager.secretAccessor
   ```
3. Use it from code:
   ```python
   from shared.secrets import get_secret
   value = get_secret('<name>')
   ```
4. Redeploy. `get_secret` caches in-process, so new versions take
   effect on the next cold start.

## Cloud Scheduler jobs

gong-sync and config-sync are triggered hourly. The jobs live in
Cloud Scheduler in the same project. If you change the hourly cadence,
also revisit gong-sync's `hours` query param default (set to `2` to
give one hour of overlap for safety).

## Running tests

The repo ships a small pytest suite plus static-analysis gates that run
on every push and PR via [GitHub Actions](.github/workflows/ci.yml).
Run the same checks locally before pushing:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r dev-requirements.txt
pip install -r slack-sync/requirements.txt \
            -r gong-sync/requirements.txt \
            -r config-sync/requirements.txt
TZ=UTC pytest
ruff check .
shellcheck deploy.sh slack-sync/deploy.sh gong-sync/deploy.sh config-sync/deploy.sh
```

Why `TZ=UTC`: one Slack helper (`format_timestamp`) pins a specific
human-readable format used as a dedup key. The test pins the expected
string; without `TZ=UTC` the conversion drifts by your local offset.
Cloud Run containers are already UTC, so prod matches the test.

Why three separate things (pytest + ruff + shellcheck):

- **ruff** catches undefined-name bugs (`F821`) at AST time - the
  regression class where someone drops `import json` and a call inside
  a function body only blows up at runtime. Plain `import` smoke cannot
  catch that because Python only resolves free names when the function
  runs.
- **pytest** covers the pure-helper contracts, the security-critical
  Slack signature verification, the cap-aware append/buffer/drain/alert
  contracts in `shared.google_docs`/`shared.pending`/`shared.alerts`,
  and the dispatcher behaviour for `?full_backfill_all=true`.
- **shellcheck** catches quoting and unset-variable mistakes in the
  deploy scripts. It does *not* catch control-flow bugs (a function
  that forgets `shift` is valid shell); those remain candidates for a
  later behavioral-test tier.

### Adding a test

- Put it under `tests/test_<thing>.py`.
- Service code lives under aliases - take the matching fixture from
  `tests/conftest.py` (`slack_main`, `gong_main`, `gong_api`,
  `config_main`) rather than a bare `import main`.
- `shared.secrets.get_secret`, `shared.gcs_mapping._get_client`,
  `shared.sheets.get_sheets_client`, `shared.google_docs.get_docs_client`,
  and `shared.pending._get_client` are poisoned by the autouse
  `_no_real_io` fixture. Any test that needs them must patch the
  specific binding it uses - e.g.
  `monkeypatch.setattr(slack_main, "get_secret", ...)` because the
  service does `from shared.secrets import get_secret` which creates
  a local binding. Same applies to `read_tab` / `batch_update_values`
  / `write_cell` / `append_to_doc` / `get_doc_text` /
  `pending.{enqueue,drain,delete,count,list_partitions}` in service
  modules that import them.
- `shared.alerts.send_doc_full_alert` is unconditionally
  side-effect-free (just a `logger.warning`) and does NOT need to be
  mocked. Tests under `tests/test_shared_alerts.py` use `caplog` to
  assert on the emitted record. Other tests can let the real call
  run.
- Keep tests pure: no real network, no real GCS, no real Secret
  Manager. Orchestration-level tests for `process_*` remain out of
  scope until we next need them.

## Onboarding sheet columns

Most columns on the `slack` and `gong` tabs are humans-write,
config-sync-reads. Exceptions:

- `Config done (Y/N)` (both tabs) - config-sync writes `Y` on
  **dispatch** of the backfill (not completion). Sync services are
  content-dedup idempotent so a repeated dispatch is safe. Humans
  can pre-populate it to opt a row out of backfill (treated as
  already-handled).
- `Document ID` / `document-id` (slack / gong tab) - holds either a
  single id or a comma-separated list (`doc-old,doc-new`). New
  content lands on the LAST id; dedup runs across the concatenation
  of all ids. Used to extend a customer past a doc cap-hit.
- `first-call-recorded` / `last-call-recorded` (`gong` tab) -
  gong-sync writes `MM/DD/YYYY` for the earliest and latest call
  date that's actually present in the customer's doc list, derived
  from the anchored `GONG CALL:` headers. Reads docs FRESH (never
  the in-process buffer cache) so calls buffered to GCS during a
  cap-hit don't leak into the date range. Hand-edits are overwritten
  on the next sync. Safe to add at any time - the columns auto-fill
  on the next run.

Removed columns (formerly on the gong/slack tabs; ignore if still
present, no-op):

- `Backlog through` (slack tab) - was the manual oldest-ts. slack-sync
  now defaults to `channel.created`.
- `backlog-through` (gong tab) - was the manual lookback. gong-sync's
  `?full_backfill` walks 5 years.
- `calls-scraped` (gong tab) - replaced by the date-range columns
  above.

## Style

- No secrets in code or env vars - only in Secret Manager.
- No local `.env` files.
- No mapping JSONs in the working tree - source of truth is the
  onboarding Google Sheet, distributed via GCS by config-sync.
- Comments should explain why, not what. Refactor over commenting.
