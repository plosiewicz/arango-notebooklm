# NotebookLM Sync

**Internal ArangoDB use only. Not for external distribution.**

Syncs customer context from Slack and Gong into per-customer Google
Docs, which are then attached as sources in NotebookLM notebooks.

Three Cloud Functions, all in GCP project `slack-notebooklm-sync`
(`us-central1`):

| Service | Trigger | What it does |
|---|---|---|
| `slack-sync` | Slack webhook + `?backfill=true` + `?drain=true` + `?full_backfill_all=true` | Appends new Slack messages to the mapped doc list; backfills history from `channel.created`; drains GCS-buffered messages once a capped doc is extended. |
| `gong-sync` | Cloud Scheduler (hourly) + `?full_backfill=true&account=...` + `?full_backfill_all=true` | Drains pending calls, pulls recent Gong calls, dedups against the customer's full doc list, appends to the tail doc; writes `first-call-recorded` / `last-call-recorded` columns. |
| `config-sync` | Cloud Scheduler (hourly) | Reads the customer-onboarding Google Sheet, updates the GCS mapping blobs, dispatches backfills (5s timeout, fire-and-forget) for new rows, fires `slack-sync?drain=true` at end of run. |

All three services share a small `shared/` library and rsync it into
each service directory at deploy time.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full picture of how
data flows between them, including the cap-hit runbook and the
existing-customer sweep.

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
│   ├── google_docs.py         get_doc_text, append_to_doc, DocFullError, DOC_CAP_BYTES
│   ├── gcs_mapping.py         load_mapping, save_mapping (5 min cache)
│   ├── secrets.py             get_secret (Secret Manager wrapper)
│   ├── sheets.py              read_tab, write_cell, get_column_letter, batch_update_values, parse_id_list
│   ├── pending.py             GCS-backed FIFO buffer for cap-hit calls/messages
│   └── alerts.py              SendGrid wrapper for doc-full operator alerts
├── tests/                     pytest suite (Tier 0 + Tier 1)
├── slack-sync/
│   ├── main.py                webhook + backfill + drain + sweep
│   ├── requirements.txt
│   ├── .gcloudignore
│   └── deploy.sh
├── gong-sync/
│   ├── main.py                drain + call processing + date-range writeback
│   ├── gong_api.py            Gong API client
│   ├── requirements.txt
│   ├── .gcloudignore
│   └── deploy.sh
└── config-sync/
    ├── main.py                sheet -> GCS + dispatch backfills + fire drain
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

First time deploying this version, set `ALERT_EMAIL` and
`SENDGRID_FROM` on the two sync services so the doc-full alerts can
fire:

```bash
gcloud functions deploy gong-sync \
  --update-env-vars=ALERT_EMAIL=ops@yourcompany.com,SENDGRID_FROM=noreply@yourcompany.com
gcloud functions deploy slack-sync \
  --update-env-vars=ALERT_EMAIL=ops@yourcompany.com,SENDGRID_FROM=noreply@yourcompany.com
```

---

## Secrets (GCP Secret Manager)

All three services pull credentials from Secret Manager via
`shared/secrets.py`. There are no `.env` files.

| Secret | Used by | Format |
|---|---|---|
| `gong-api-key` | gong-sync | `accessKeyId:accessKeySecret` |
| `slack-bot-token` | slack-sync | `xoxb-...` |
| `slack-signing-secret` | slack-sync | hex string |
| `sendgrid-api-key` | gong-sync, slack-sync (via `shared.alerts`) | SendGrid API key |

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

- `slack` tab: `Slack Channel ID`, `Document ID`, `Customer Name`,
  `Config done (Y/N)`.
- `gong` tab: `customer-email-domain`, `document-id`, `customer-name`,
  `Config done (Y/N)`, `first-call-recorded`, `last-call-recorded`
  (read-only, managed by gong-sync).

`Document ID` / `document-id` cells hold a single id (`doc-abc`) or a
comma-separated list (`doc-abc,doc-def`) once a doc hits the size cap.
New content always lands on the LAST id; dedup runs against the
concatenation of all ids.

There is no `Backlog through` column anymore. slack-sync defaults
`oldest` to `channel.created` and gong-sync's `?full_backfill` walks
the entire 5-year window, so onboarding always captures the full
history without operator math.

Flow (runs hourly from config-sync):

1. config-sync reads both tabs.
2. Rebuilds the mapping JSON for each tab and writes it to the
   `slack-notebooklm-config` GCS bucket
   (`channel-mapping.json`, `account-mapping.json`).
3. For each row where `Config done (Y/N)` is blank and the key isn't
   already in the GCS mapping, config-sync **dispatches** a backfill
   with a 5-second timeout (fire-and-forget). The receiving function
   runs the backfill in its own 540s budget.
4. Marks `Config done = Y` on dispatch (not completion). Sync services
   are content-dedup idempotent so a repeated dispatch is safe.
5. At the end of every run, fires `slack-sync?drain=true` to flush
   any messages that were buffered to GCS during a doc-cap-hit window.

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
5. config-sync flips `Config done` to `Y` once dispatch returns. The
   actual backfill runs in its own slack-sync / gong-sync invocation.
6. Add the Google Doc as a source in the customer's NotebookLM.

### When a doc hits the cap

A SendGrid email lands in `ALERT_EMAIL` saying
`[notebooklm] <service> doc full for <Customer>`. Calls/messages keep
landing safely in GCS (`pending-calls/<domain>/`,
`pending-messages/<channel_id>/`). To resume:

1. Create a new Google Doc, share with the service account.
2. Edit the customer's row: `document-id` becomes `doc-old,doc-new`.
3. Wait up to an hour, or:
   ```bash
   curl ".../slack-sync?drain=true"
   curl ".../gong-sync?hours=2"     # gong drains at the start of every run
   ```

Full runbook in [ARCHITECTURE.md](./ARCHITECTURE.md#cap-hit-runbook).

---

## Manual triggers & backfills

```bash
# slack-sync backfill for one channel (default oldest = channel.created)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync?backfill=true&channel=C0ABC123XYZ"

# slack-sync drain (flush GCS-buffered messages)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync?drain=true"

# slack-sync sweep all channels (dispatcher, returns immediately)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync?full_backfill_all=true"

# gong-sync, last 2 hours (same as scheduler)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?hours=2"

# gong-sync full backfill (5 years), one account only
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?full_backfill=true&account=cadence.com"

# gong-sync sweep all accounts (dispatcher, returns immediately)
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?full_backfill_all=true"

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
| Duplicate Slack messages | Slack retried before the function returned 200 | Function drops `X-Slack-Retry-Num` headers. Check logs. |
| Sheet row stuck on blank `Config done` | DNS / IAM error during the dispatch (5xx from the sync service is treated as success). | Check `gcloud functions logs read config-sync` for `Failed to dispatch ...`; re-trigger config-sync once the underlying issue is fixed. |
| `[notebooklm] doc full` email | Customer's tail doc hit 6 MB. Buffered items are safe in GCS. | See [Cap-hit runbook](./ARCHITECTURE.md#cap-hit-runbook). |
| `first-call-recorded` / `last-call-recorded` blank | Columns missing from the gong tab, or doc has no parseable `GONG CALL:` blocks yet, or the customer was dormant on the last run. | Add column headers; confirm doc has at least one call; kick `?full_backfill=true&account=<domain>`. |
| Pending-* objects piling up in GCS | Operator hasn't extended the customer's doc list, or the new doc isn't shared with the SA. | `gsutil ls gs://slack-notebooklm-config/pending-calls/` and `pending-messages/`; resolve per the cap-hit runbook. |
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
| Doc cap (plaintext) | 6 MB (Google's hard wall is 10 MB) |
| slack-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync` |
| gong-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync` |
| config-sync URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/config-sync` |
