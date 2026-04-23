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

## Style

- No secrets in code or env vars - only in Secret Manager.
- No local `.env` files.
- No mapping JSONs in the working tree - source of truth is the
  onboarding Google Sheet, distributed via GCS by config-sync.
- Comments should explain why, not what. Refactor over commenting.
