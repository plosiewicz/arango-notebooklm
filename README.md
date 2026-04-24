# NotebookLM Sync

**Internal ArangoDB use only. Not for external distribution.**

Syncs customer context from Slack and Gong into per-customer Google
Docs, which are then attached as sources in NotebookLM notebooks.

Three Cloud Functions, all in GCP project `slack-notebooklm-sync`
(`us-central1`):

| Service | Trigger | What it does |
|---|---|---|
| `slack-sync` | Slack webhook + ad-hoc `?backfill=true` | Appends new Slack messages to the mapped Google Doc; also handles historical backfills. |
| `gong-sync` | Cloud Scheduler (hourly) + ad-hoc | Pulls recent Gong calls, dedups against each customer doc, appends transcript + summary. |
| `config-sync` | Cloud Scheduler (hourly) | Reads the customer-onboarding Google Sheet, updates the GCS mapping blobs, triggers a backfill for any brand-new rows. |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full picture of how
data flows between them.

---

## Repo layout

```
.
├── README.md
├── ARCHITECTURE.md
├── CONTRIBUTING.md
├── deploy.sh                  root dispatcher (./deploy.sh slack|gong|config|all)
├── dev-requirements.txt       pytest + freezegun + ruff
├── pytest.ini                 test discovery + scoped DeprecationWarning errors
├── pyproject.toml             ruff config (F ruleset)
├── .github/workflows/ci.yml   ruff + pytest + shellcheck on push/PR
├── shared/                    imported by every service
│   ├── google_docs.py         get_docs_client, get_doc_text, append_to_doc
│   ├── gcs_mapping.py         load_mapping, save_mapping (5 min cache)
│   ├── secrets.py             get_secret (Secret Manager wrapper)
│   └── sheets.py              read_tab, write_cell, get_column_letter, batch_update_values
├── tests/                     pytest suite (Tier 0 + Tier 1)
│   ├── conftest.py            per-service loaders + autouse no-real-IO
│   ├── test_import_smoke.py
│   ├── test_shared_gcs_mapping.py
│   ├── test_shared_sheets.py
│   ├── test_config_sync_helpers.py
│   ├── test_slack_sync_helpers.py
│   ├── test_gong_sync_helpers.py
│   └── test_gong_api_helpers.py
├── slack-sync/
│   ├── main.py                webhook + backfill handler
│   ├── requirements.txt
│   ├── .gcloudignore
│   └── deploy.sh
├── gong-sync/
│   ├── main.py                call processing + dedup
│   ├── gong_api.py            Gong API client
│   ├── requirements.txt
│   ├── .gcloudignore
│   └── deploy.sh
└── config-sync/
    ├── main.py                sheet -> GCS + backfill orchestration
    ├── requirements.txt
    ├── .gcloudignore
    └── deploy.sh
```

`shared/` is rsynced into each service directory at deploy time - the
copies are gitignored and cleaned up after deploy. See
[CONTRIBUTING.md](./CONTRIBUTING.md) for how local dev works.

Automated tests live in [`tests/`](./tests) and run on every push and
PR via [`.github/workflows/ci.yml`](.github/workflows/ci.yml). See the
"Running tests" section of [CONTRIBUTING.md](./CONTRIBUTING.md#running-tests)
to run them locally.

---

## Prerequisites

- `gcloud` CLI authenticated as an ArangoDB Google account that has
  access to the `slack-notebooklm-sync` project.
- The GCP compute service account
  (`399790122111-compute@developer.gserviceaccount.com`) needs:
  - Secret Manager Secret Accessor
  - editor access on every customer Google Doc

```bash
gcloud auth login
gcloud config set project slack-notebooklm-sync
```

---

## Deploying

One service:

```bash
./deploy.sh slack
./deploy.sh gong
./deploy.sh config
```

All three:

```bash
./deploy.sh all
```

Any trailing args pass through to `gcloud functions deploy`, so you
can e.g. `./deploy.sh slack --update-env-vars=FOO=bar`.

---

## Secrets (GCP Secret Manager)

All three services pull credentials from Secret Manager via
`shared/secrets.py`. There are no `.env` files.

| Secret | Used by | Format |
|---|---|---|
| `gong-api-key` | gong-sync | `accessKeyId:accessKeySecret` |
| `slack-bot-token` | slack-sync, config-sync | `xoxb-...` |
| `slack-signing-secret` | slack-sync | hex string |

View / rotate a secret:

```bash
gcloud secrets versions access latest --secret=<name> --project=slack-notebooklm-sync

echo -n "<new-value>" | \
  gcloud secrets versions add <name> --data-file=- --project=slack-notebooklm-sync
```

A new version is picked up on the next cold start.

---

## Configuration (Google Sheet)

Customer onboarding lives in **one** Google Sheet:
[NotebookLM customer config](https://docs.google.com/spreadsheets/d/1p8CZ5RBGkFSf6aPnUIz8DXai9_UgNZhj7g1JtbPMvzI).

- `slack` tab: Slack Channel ID, Document ID, Customer Name,
  `Config done (Y/N)`, (optional) `Backlog through`
- `gong` tab: customer-email-domain, document-id, customer-name,
  `Config done (Y/N)`, (optional) `backlog-through`, `calls-scraped`
  (read-only, managed by gong-sync)

Flow (runs hourly from config-sync):

1. config-sync reads both tabs.
2. Rebuilds the mapping JSON for each tab and writes it to the
   `slack-notebooklm-config` GCS bucket
   (`channel-mapping.json`, `account-mapping.json`).
3. For each row where `Config done (Y/N)` is blank and the key isn't
   already in the GCS mapping, config-sync triggers a one-shot
   backfill on slack-sync (from `Backlog through` or channel creation)
   or gong-sync (last N days since `backlog-through`, scoped to the
   row's email domain via `?account=`). Writes `Y` back into the
   sheet only if the backfill returned HTTP 200; failed rows stay
   blank for manual remediation (see Troubleshooting).

slack-sync / gong-sync read the GCS mapping on every request with a
5-minute in-memory cache.

### Adding a new customer

1. Create a Google Doc, share it with
   `399790122111-compute@developer.gserviceaccount.com` as Editor.
2. Add a row to the `slack` and/or `gong` tab with the doc ID. Leave
   `Config done` blank.
3. (Slack only) Invite `@NotebookLM Sync` to the channel.
4. Wait up to an hour - or trigger config-sync manually:
   ```bash
   curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/config-sync"
   ```
5. config-sync flips `Config done` to `Y` once the backfill returns
   HTTP 200. If the cell stays blank after the next hourly run, the
   backfill failed - see Troubleshooting.
6. Add the Google Doc as a source in the customer's NotebookLM.

---

## Manual triggers & backfills

```bash
# slack-sync backfill for one channel (default oldest = Jan 1 2024)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync?backfill=true&channel=C0ABC123XYZ"

# gong-sync, last 2 hours (same as scheduler)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?hours=2"

# gong-sync backfill, last 90 days, one account only
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?backfill=true&days=90&account=cadence.com"

# config-sync once
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/config-sync"
```

---

## Logs

```bash
gcloud functions logs read slack-sync  --region=us-central1 --project=slack-notebooklm-sync --limit=30
gcloud functions logs read gong-sync   --region=us-central1 --project=slack-notebooklm-sync --limit=30
gcloud functions logs read config-sync --region=us-central1 --project=slack-notebooklm-sync --limit=30
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No mapping found for channel X` | Row missing from sheet, or config-sync hasn't run yet | Add the row, then `curl` config-sync. |
| `403` / `caller does not have permission` on a doc | Doc not shared with the service account | Share the doc with `399790122111-compute@developer.gserviceaccount.com` as Editor. |
| `Invalid signature` on slack-sync | `slack-signing-secret` doesn't match the Slack app | Copy from Slack app "Basic Information", add a new version to `slack-signing-secret`. |
| `Failed to get credentials from Secret Manager` | Service account lacks Secret Accessor, or the secret name doesn't exist | Grant role or create/rename the secret. |
| gong-sync "skipped_accounts" in logs | Account on the call doesn't match any sheet row | Add the account (email domain, name, or CRM id) to the `gong` tab. |
| Duplicate Slack messages | Slack retried before the function returned 200 | Verified dedup: function drops `X-Slack-Retry-Num` headers. Check logs. |
| Sheet row stuck on blank `Config done` after onboarding | Backfill returned non-200, or config-sync was killed before writing the cell. Mapping was saved at the start of the run so config-sync won't auto-retry. | Check `gong-sync` / `slack-sync` logs for that customer, re-run the backfill manually (`?account=` for Gong, `?channel=` for Slack), then mark `Y` by hand. |
| `calls-scraped` stays blank for an account | Either the column header is missing from the gong tab, or gong-sync hasn't processed a call for that customer yet. | Add the `calls-scraped` column header to the gong tab if it's missing. Otherwise wait - the cell auto-populates on the next gong-sync run that touches the account, or kick a backfill with `?backfill=true&account=<domain>`. |
| NotebookLM source not updating | Doc updated, but NotebookLM cache | Re-index in NotebookLM UI. |

---

## Quick reference

| Item | Value |
|---|---|
| GCP project | `slack-notebooklm-sync` |
| Region | `us-central1` |
| Runtime service account | `399790122111-compute@developer.gserviceaccount.com` |
| Config GCS bucket | `slack-notebooklm-config` |
| Onboarding sheet | `1p8CZ5RBGkFSf6aPnUIz8DXai9_UgNZhj7g1JtbPMvzI` |
| slack-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync` |
| gong-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync` |
| config-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/config-sync` |
