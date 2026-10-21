# Deploying CLIO to Google Cloud Run

This folder deploys CLIO as a **private** Cloud Run service. The data (NetCDF + Chroma DB)
lives in a Cloud Storage bucket that is mounted read-only into the container. Nothing here
changes how CLIO behaves: local development still runs with `python -m clio.app`.

```
browser ──(gcloud run services proxy)──> Cloud Run: clio (gunicorn + clio.wsgi)
                                            ├── /data      <- gs://BUCKET (read-only mount)
                                            ├── /tmp/chromadb (copied from /data at startup)
                                            └── Gemini API  <- key from Secret Manager
```

## Usage and cost safeguards

| Layer | Safeguard | Where |
| --- | --- | --- |
| Access | Service is private (`--no-allow-unauthenticated`); only people granted `roles/run.invoker` can call it | `deploy.sh`, step 6 |
| Scaling | Max 2 instances, min 0 (no idle cost) | `config.env` |
| Per client | 6 questions/minute and 6 map selections/minute, per IP | `src/clio/wsgi.py` |
| Per day | 100 questions per instance per UTC day (max 200/day at 2 instances) | `src/clio/wsgi.py` |
| Request size | Bodies over 256 KB and messages over 4,000 characters are rejected | `src/clio/wsgi.py` |
| CORS | Cross-origin browser calls are blocked unless listed in `ALLOWED_ORIGINS` | `src/clio/wsgi.py` |
| Errors | 500 responses return a generic message; details stay in Cloud Logging | `src/clio/wsgi.py` |
| Model | A pinned `GEMINI_MODEL` is required (no `-latest` aliases) | `deploy.sh` |
| Hard Gemini cap | Daily request quota on the Generative Language API | step 3 (manual) |
| Budget | $50/month budget with email alerts at 50%, 90%, 100% | `setup.sh` |
| Secrets | Gemini key in Secret Manager, readable only by the runtime service account | `setup.sh` |
| Data | Private bucket (public access prevention on), mounted read-only | `setup.sh`, `deploy.sh` |
| Storage | Artifact Registry keeps the 5 newest images, deletes older ones after 30 days | `cleanup-policy.json` |

Two of these need care:

- **The in-app daily limit is per instance and resets when an instance restarts.** It stops
  runaway loops, but the Gemini quota in step 3 is the real hard cap.
- **Budget alerts only send email; they don't stop spending.** The quota does.

## Prerequisites

- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), logged in with `gcloud auth login`
- A Google Cloud project with a billing account linked
- A Gemini API key **created in that same project** (in Google AI Studio, pick the project
  when creating the key), so its usage is billed to the project, counts against the budget
  and can be capped with a quota
- Bash (Git Bash on Windows works)

## Steps

### 1. Prepare the data locally

- NetCDF files in `data/netcdf/` (or set `LOCAL_NETCDF_DIR` to where they are)
- Build the Chroma DB once with the PDFs from the main README: put them in `documents/rag/`,
  fill in `.env`, run `python rag/rag_manager.py` and use `add`. This creates `data/chromadb/`.

### 2. Configure

```bash
cp deploy/config.env.example deploy/config.env
```

Fill in `PROJECT_ID`, `BILLING_ACCOUNT`, `BUCKET` and `GEMINI_MODEL`. `config.env` is gitignored.

### 3. One-time cloud setup

```bash
bash deploy/setup.sh
```

This enables APIs and creates the image repository, the private bucket, the service account,
the secret (it asks you to paste the key) and the budget.

**Then cap Gemini requests manually:** Cloud Console → APIs & Services → Generative Language
API → Quotas & System Limits → find the "requests per day" limits for the model you use →
Edit → set a ceiling (e.g. 200). A request over the quota fails instead of being billed.

### 4. Upload the data

```bash
bash deploy/upload_data.sh
```

This uploads to `gs://BUCKET/v1/`. Versions are write-once: to publish new data, set
`DATA_VERSION=v2`, upload, then deploy.

### 5. Build and deploy

```bash
bash deploy/deploy.sh
```

Cloud Build builds the image, so Docker isn't needed locally.

### 6. Give the team access and open the app

```bash
gcloud run services add-iam-policy-binding clio --region us-east1 \
    --member user:teammate@example.com --role roles/run.invoker

gcloud run services proxy clio --region us-east1 --port 8080
```

Then browse to http://localhost:8080. The first request after the service has been idle
takes 10–30 seconds while an instance starts.

## Day-to-day operations

| Task | Command |
| --- | --- |
| Deploy a code change | `bash deploy/deploy.sh` |
| Switch dataset | Set `NETCDF_FILE` in `config.env`, then `bash deploy/deploy.sh` |
| Publish new data | Set a new `DATA_VERSION`, `bash deploy/upload_data.sh`, `bash deploy/deploy.sh` |
| Change a limit without rebuilding | `gcloud run services update clio --region us-east1 --update-env-vars DAILY_QUERY_LIMIT=50` |
| **Emergency stop for Gemini spend** | `gcloud run services update clio --region us-east1 --update-env-vars DAILY_QUERY_LIMIT=0` |
| Roll back | `gcloud run revisions list --service clio --region us-east1`, then `gcloud run services update-traffic clio --region us-east1 --to-revisions REVISION=100` |
| Rotate the Gemini key | `printf '%s' "NEW_KEY" \| gcloud secrets versions add gemini-api-key --data-file -`, then redeploy |
| View logs | `gcloud run services logs read clio --region us-east1` |

## Known issues to watch

- **OpenStreetMap Nominatim:** each map selection reverse-geocodes 5 points through the
  public Nominatim service (`data_processor.py`), with a 1.1 s pause between calls, so a
  selection takes about 5–8 s. Nominatim's usage policy allows at most 1 request per second
  per application. Two people selecting at the same moment can exceed that, and Cloud Run's
  shared outbound IPs can get throttled. Failed lookups are skipped, so the app keeps working
  without place names. The 6-per-minute limit keeps this low for a small team; a public
  launch should serialize or cache these calls, or use a paid geocoder.
- **Error messages:** through `clio.wsgi`, 500 responses show "Internal server error."
  instead of the exception text. Use `gcloud run services logs read` to see the detail.
