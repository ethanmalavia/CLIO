# Deploying CLIO to Google Cloud Run

This folder deploys CLIO as a **private** Cloud Run service. The data (NetCDF + Chroma DB)
lives in a Cloud Storage bucket that is mounted read-only into the container. CLIO's data
access, retrieval and agent behave exactly as they do locally, and local development still
runs with `python -m clio.app`.

```
browser ──(gcloud run services proxy)──> Cloud Run: clio (gunicorn + clio.wsgi)
                                            ├── /data      <- gs://BUCKET (read-only mount)
                                            ├── /tmp/chromadb (copied from /data at startup)
                                            ├── Gemini API  <- key from Secret Manager
                                            └── OpenStreetMap Nominatim (place names)
```

## Before you deploy: research constraints

- **The BISECT projections are unpublished** (Gebremedhin et al., in review). Keep the
  service private, keep the bucket private, and use a **paid-tier** Gemini key: free-tier
  prompts may be used by Google, and every prompt carries statistics from this data.
- **Deploy the model you benchmark.** Set `GEMINI_MODEL` to the exact model ID used for the
  agent benchmark and named in the paper, so the deployed agent is the one the evaluation
  describes. Each revision logs its model and dataset at startup (`clio.startup`).
- **Never commit manuscript drafts.** `*.docx` is ignored by git, Cloud Build and Docker.

## Usage and cost safeguards

| Layer | Safeguard | Where |
| --- | --- | --- |
| Access | Service is private (`--no-allow-unauthenticated`); only people granted `roles/run.invoker` can call it | `deploy.sh`, step 6 |
| Scaling | Max 1 instance per service, min 0 (no idle cost) | `config.env` |
| Per client | 6 questions/minute and 6 map selections/minute, per IP | `src/clio/wsgi.py` |
| Per day | 100 questions per instance per UTC day | `src/clio/wsgi.py` |
| Request size | Bodies over 256 KB and messages over 4,000 characters are rejected | `src/clio/wsgi.py` |
| CORS | Cross-origin browser calls are blocked unless listed in `ALLOWED_ORIGINS` | `src/clio/wsgi.py` |
| Errors | 500 responses return a generic message; details stay in Cloud Logging | `src/clio/wsgi.py` |
| Error display | Limit and server errors appear as warnings in the chat, without a provenance widget, and are left out of the history sent to the model | `frontend/index.html` |
| Model | A pinned `GEMINI_MODEL` is required (no `-latest` aliases) | `deploy.sh` |
| Hard Gemini cap | Daily request quota on the Generative Language API | step 3 (manual) |
| Budget | $50/month budget with email alerts at 50%, 90%, 100% | `setup.sh` |
| Secrets | Gemini key in Secret Manager, readable only by the runtime service account | `setup.sh` |
| Data | Private bucket (public access prevention on), mounted read-only | `setup.sh`, `deploy.sh` |
| Geocoding | Nominatim lookups serialized, spaced 1.1 s apart and cached | `src/clio/wsgi.py` |
| Storage | Artifact Registry keeps the 5 newest images, deletes older ones after 30 days | `cleanup-policy.json` |

Two of these need care:

- **The in-app daily limit is per instance and resets when an instance restarts.** It stops
  runaway loops, but the Gemini quota in step 3 is the real hard cap.
- **Budget alerts only send email; they don't stop spending.** The quota does.

## Prerequisites

- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), logged in with `gcloud auth login`
- A Google Cloud project with a billing account linked
- A paid-tier Gemini API key **created in that same project** (in Google AI Studio, pick the
  project when creating the key), so its usage is billed to the project, counts against the
  budget and can be capped with a quota
- Bash (Git Bash on Windows works)

## Steps

### 1. Prepare the data locally

- NetCDF files in `data/netcdf/` (or set `LOCAL_NETCDF_DIR` to where they are)
- The RAG source PDFs from the main README in `documents/rag/`
- Optionally build the Chroma DB locally: fill in `.env`, run `python rag/rag_manager.py` and
  use `add`. This creates `data/chromadb/`. If you can't, step 4 builds it in Cloud Build.

### 2. Configure

```bash
cp deploy/config.env.example deploy/config.env
```

Fill in `PROJECT_ID`, `BILLING_ACCOUNT`, `BUCKET` and `GEMINI_MODEL`. Setting
`NOMINATIM_USER_AGENT` to something like `"CLIO/0.1 (contact: you@university.edu)"` is
recommended by Nominatim's usage policy. `deploy/*.env` files are gitignored.

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

This uploads every `.nc` file, and a local Chroma DB if there is one, to `gs://BUCKET/v1/`.
Versions are write-once: to publish new data, set `DATA_VERSION=v2`, upload, then deploy.

Without a local Chroma DB, build it in Cloud Build:

```bash
bash deploy/build_chromadb.sh
```

It embeds the PDFs with `rag/rag_manager.py`'s own code, taking each document's title,
author, year and DOI from `deploy/rag_metadata.json`, and paces requests to fit the Gemini
free tier (100 requests and 30,000 tokens per minute, 1,000 requests per day). The current
documents need about 900 requests, so run it once per day at most on a free-tier key.
To deploy before the Chroma DB exists, set `RAG_ENABLED=false`: the app then starts with an
empty knowledge base.

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

## Comparing scenarios: one service per dataset

CLIO serves one dataset per process. To compare the historical baseline with SSP5-8.5,
run two services from the same image and bucket, each with its own config file:

```bash
cp deploy/config.env deploy/ssp585.env
# in ssp585.env: SERVICE=clio-ssp585, NETCDF_FILE=farfuture585_surface.nc
CLIO_CONFIG=deploy/ssp585.env bash deploy/deploy.sh
```

With two services running, set `NOMINATIM_MIN_INTERVAL=2.2` in both files, so the two
together stay within Nominatim's 1 request/second.

## Audit log (benchmarking and human-on-the-loop review)

Set `AUDIT_LOG=true` to write one structured Cloud Logging entry per question with the
model, the dataset, the question, the selection statistics the agent was given, its answer,
and the source and page of every chunk it retrieved. That lets a reviewer check numbers in
an answer against the statistics, and citations against the retrieved pages, after the fact.

It records what users type, so it is off by default. Tell testers before turning it on.

```bash
gcloud logging read 'jsonPayload.message="clio.query"' --limit 20 --format json
```

## Day-to-day operations

| Task | Command |
| --- | --- |
| Deploy a code change | `bash deploy/deploy.sh` |
| Switch dataset | Set `NETCDF_FILE` in the service's config, then `bash deploy/deploy.sh` |
| Publish new data | Set a new `DATA_VERSION`, `bash deploy/upload_data.sh`, `bash deploy/deploy.sh` |
| Change a limit without rebuilding | `gcloud run services update clio --region us-east1 --update-env-vars DAILY_QUERY_LIMIT=50` |
| Turn the audit log on or off | `gcloud run services update clio --region us-east1 --update-env-vars AUDIT_LOG=true` |
| **Emergency stop for Gemini spend** | `gcloud run services update clio --region us-east1 --update-env-vars DAILY_QUERY_LIMIT=0` |
| Roll back | `gcloud run revisions list --service clio --region us-east1`, then `gcloud run services update-traffic clio --region us-east1 --to-revisions REVISION=100` |
| Rotate the Gemini key | `printf '%s' "NEW_KEY" \| gcloud secrets versions add gemini-api-key --data-file -`, then redeploy |
| View logs | `gcloud run services logs read clio --region us-east1` |

## Known issues to watch

- **OpenStreetMap Nominatim:** each map selection reverse-geocodes 5 points, so a selection
  takes about 5–8 s, longer if another selection is waiting for its lookups. Failed lookups
  are skipped, so the app keeps working without place names. A public launch should move to
  a geocoder licensed for that volume.
- **Error messages:** through `clio.wsgi`, 500 responses show "Internal server error."
  instead of the exception text. Use `gcloud run services logs read` to see the detail.
