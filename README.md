# NotebookLM Sync

Syncs customer context from Slack and Gong into Google Docs, which feed into NotebookLM as sources.

Two independent Cloud Functions:
- **slack-sync** — Real-time. Slack messages are appended to a Google Doc as they come in.
- **gong-sync** — Scheduled. Gong call transcripts are pulled daily and appended to a Google Doc.

Both run in the `slack-notebooklm-sync` GCP project in `us-central1`.

---

## Prerequisites

- `gcloud` CLI installed ([install guide](https://cloud.google.com/sdk/docs/install))
- Access to the `slack-notebooklm-sync` GCP project
- Slack credentials for slack-sync (ask Paul)

Authenticate:

```bash
gcloud auth login
gcloud config set project slack-notebooklm-sync
```

---

## Slack Sync

### How It Works

```
Slack message → Cloud Function (HTTP webhook) → Google Doc → NotebookLM
```

Slack sends a webhook to the Cloud Function on every message in a subscribed channel. The function looks up the channel in `channel-mapping.json`, resolves the sender's display name via the Slack API, and appends the formatted message to the mapped Google Doc.

Slack retries webhooks if it doesn't get a response within 3 seconds. The function detects retries via the `X-Slack-Retry-Num` header and drops them to prevent duplicate messages.

### Secrets

Slack credentials are passed as environment variables at deploy time. They live in `slack-sync/.env` (gitignored). Copy `.env.example` and fill in the values.

| Variable | Where to find it |
|---|---|
| `SLACK_BOT_TOKEN` | [Slack App Settings](https://api.slack.com/apps) > OAuth & Permissions |
| `SLACK_SIGNING_SECRET` | [Slack App Settings](https://api.slack.com/apps) > Basic Information |

### Deploy

```bash
cd slack-sync
source .env

gcloud functions deploy slack-sync \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point slack_webhook \
  --set-env-vars "SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN,SLACK_SIGNING_SECRET=$SLACK_SIGNING_SECRET" \
  --source . \
  --region us-central1
```

### Adding a New Customer Channel

1. Get the Slack channel ID (right-click channel > View channel details > scroll to bottom).
2. Create a Google Doc (or use an existing one). Copy the Doc ID from the URL:
   ```
   https://docs.google.com/document/d/1aBcDeFgHiJkLmNoPqRs/edit
                                      └──────────────────┘
                                         This is the Doc ID
   ```
3. Share the Google Doc with `399790122111-compute@developer.gserviceaccount.com` as **Editor**.
4. Add the channel to `slack-sync/channel-mapping.json`:
   ```json
   {
     "C0ABC123XYZ": {
       "docId": "your-google-doc-id",
       "customerName": "Customer Name"
     }
   }
   ```
5. Redeploy (see above).
6. Invite the bot to the channel: `/invite @NotebookLM Sync`
7. Send a test message — it should appear in the Google Doc within a few seconds.
8. (Optional) Add the Google Doc as a source in the customer's NotebookLM notebook.

### Logs

```bash
gcloud functions logs read slack-sync --region us-central1 --limit 30 --project slack-notebooklm-sync
```

### Troubleshooting

| Problem | Fix |
|---|---|
| "No mapping found for channel" | Channel ID not in `channel-mapping.json`. Add it and redeploy. |
| 403 "caller does not have permission" | Google Doc not shared with the service account. Share it as Editor. |
| Messages not appearing | Is the bot in the channel? `/invite @NotebookLM Sync`. Check logs. |
| "Invalid signature" | `SLACK_SIGNING_SECRET` doesn't match. Get the correct value and redeploy. |
| Duplicate messages | The retry-detection logic should handle this. Check logs for "Ignoring Slack retry" entries. |
| NotebookLM not updating | May take a few minutes. Try refreshing the notebook or re-adding the source. |

---

## Gong Sync

### How It Works

```
Cloud Scheduler (daily) → Cloud Function → Gong API → Google Doc → NotebookLM
```

The function is triggered daily by Cloud Scheduler. It pulls recent calls from the Gong API, matches each call to an account using `account-mapping.json`, fetches the transcript and AI summary, and appends everything to the mapped Google Doc.

Account matching works by:
1. CRM context on the call (Salesforce/HubSpot account ID) — most reliable
2. External participant's company name
3. External participant's email domain (excluding generic providers like gmail.com)

Duplicate calls are tracked in `/tmp/processed_gong_calls.json` on the function instance. This resets on cold starts, so the scheduled interval (daily) and lookback window (25 hours) are set to overlap slightly.

### Secrets

Gong API credentials are stored in **GCP Secret Manager**, not in environment variables or local files.

| Secret | Path in Secret Manager |
|---|---|
| Gong API key | `projects/slack-notebooklm-sync/secrets/gong-api-key/versions/latest` |

The secret value should be in `accessKeyId:accessKeySecret` format. The function base64-encodes it at runtime for the Gong API's Basic Auth.

The function's service account (`399790122111-compute@developer.gserviceaccount.com`) needs the **Secret Manager Secret Accessor** role to read it. This is already configured.

To view or update the secret:

```bash
# View the current secret value
gcloud secrets versions access latest --secret=gong-api-key --project=slack-notebooklm-sync

# Add a new version
echo -n "newAccessKeyId:newAccessKeySecret" | \
  gcloud secrets versions add gong-api-key --data-file=- --project=slack-notebooklm-sync
```

### Deploy

```bash
cd gong-sync

gcloud functions deploy gong-sync \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point gong_sync \
  --source . \
  --region us-central1
```

No `--set-env-vars` needed — credentials come from Secret Manager.

### Adding a New Customer Account

1. Create a Google Doc for the customer. Copy the Doc ID from the URL.
2. Share the Google Doc with `399790122111-compute@developer.gserviceaccount.com` as **Editor**.
3. Add the account to `gong-sync/account-mapping.json`. The key can be an account ID, account name, or email domain — whatever matches how Gong identifies the account on calls:
   ```json
   {
     "cadence.com": {
       "docId": "your-google-doc-id",
       "customerName": "Cadence Design Systems, Inc."
     }
   }
   ```
4. Redeploy (see above).

### Manual Trigger and Backfill

The function accepts query parameters:

| Parameter | Default | Description |
|---|---|---|
| `hours` | `25` | How many hours back to look for calls (normal mode) |
| `backfill` | `false` | Set to `true` to pull historical calls |
| `days` | `90` | How many days back to pull in backfill mode |

Normal run (last 25 hours):
```bash
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync"
```

Backfill last 90 days:
```bash
curl "https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync?backfill=true&days=90"
```

### Logs

```bash
gcloud functions logs read gong-sync --region us-central1 --limit 30 --project slack-notebooklm-sync
```

### Troubleshooting

| Problem | Fix |
|---|---|
| "Failed to get credentials from Secret Manager" | Service account may not have Secret Accessor role, or the secret doesn't exist. Check Secret Manager in GCP Console. |
| "No mapping found for account" | The account name/ID/domain from Gong doesn't match any key in `account-mapping.json`. Check logs for the exact value Gong returns and add it. |
| 403 on Google Doc | Doc not shared with the service account. Share as Editor. |
| Calls not syncing for a customer | The account might be identified differently in Gong than expected. Check logs for "skipped_accounts" to see what Gong is returning. |

---

## File Structure

```
├── README.md
├── .gitignore
├── slack-sync/
│   ├── main.py                 # Slack webhook handler
│   ├── google_docs.py          # Google Docs API helper
│   ├── channel-mapping.json    # Slack channel ID → Google Doc ID
│   ├── requirements.txt        # Python dependencies
│   ├── .env.example            # Template for local env vars
│   ├── .gcloudignore           # Files excluded from deploy
│   └── .gitignore              # Files excluded from git
└── gong-sync/
    ├── main.py                 # Cloud Function entry point + call processing
    ├── gong_api.py             # Gong API client (calls, transcripts, auth)
    ├── google_docs.py          # Google Docs API helper
    ├── account-mapping.json    # Account name/domain → Google Doc ID
    └── requirements.txt        # Python dependencies
```

---

## Quick Reference

| Item | Value |
|---|---|
| GCP Project | `slack-notebooklm-sync` |
| Region | `us-central1` |
| Service Account | `399790122111-compute@developer.gserviceaccount.com` |
| Slack Function URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/slack-sync` |
| Gong Function URL | `https://us-central1-slack-notebooklm-sync.cloudfunctions.net/gong-sync` |
| Slack App | `NotebookLM Sync` ([settings](https://api.slack.com/apps)) |
| Gong API Secret | `projects/slack-notebooklm-sync/secrets/gong-api-key` |
| GCP Console (slack-sync) | [Link](https://console.cloud.google.com/functions/details/us-central1/slack-sync?project=slack-notebooklm-sync) |
| GCP Console (gong-sync) | [Link](https://console.cloud.google.com/functions/details/us-central1/gong-sync?project=slack-notebooklm-sync) |
