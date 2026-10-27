# Model comparison (Pro vs Flash)

Tests whether moving CLIO's agent to a cheaper Gemini model changes answer quality.
Two private Cloud Run services run the same image, data and limits, and differ only in
`GEMINI_MODEL`. The same questions, with the same selection statistics, go to both.

## Run it

Needs a deployment set up through `deploy/README.md` steps 1–4, with the Chroma DB built
(`bash deploy/build_chromadb.sh` builds it in Cloud Build if you can't build it locally).
Use a paid-tier key for the test: it carries statistics from unpublished data.

```bash
bash eval/deploy_arms.sh <pro-model-id> <flash-model-id>   # prints the two URLs
python eval/run_eval.py --arm pro=<url> --arm flash=<url> --repeats 2
python eval/score.py eval/results/<run>                     # checks + blinded review.md
# grade review.md into grades.json (0/1/2 per answer, plus "preferred"), then:
python eval/score.py eval/results/<run> --grades
bash eval/teardown_arms.sh
```

`run_eval.py` and `score.py` use only the Python standard library. Results go to
`eval/results/`, which is gitignored.

## What it measures

`cases.json` holds 20 cases (22 questions) across the manuscript's layers:

| Category | Tests |
| --- | --- |
| access | Stating the selection statistics correctly: recap, mean/std, trend, max and its location |
| understanding | Retrieving and citing the BISECT, NOAA and IPCC documents |
| translation | Plain-language explanation, follow-up questions |
| governance | Holding to the statistics when challenged; no estimates outside the selection, domain or time range; no recommendations; no answers from memory for documents outside the knowledge base; no DOI; rectangle-only selection |

Automatic checks (`score.py`) flag: failed requests, latency, retrieval when a case needs
it, recap completeness, whether expected statistics are stated, citation format and
whether cited pages were actually retrieved, numbers that can't be traced to the
statistics or the retrieved passages, DOI leaks and markdown.

Numbers flagged as untraced are candidates, not verdicts: a model may legitimately
convert units or round. The blinded review settles them.

## Cost

About 44 questions per arm with `--repeats 2`. On Gemini Pro that is roughly $2–4 and on
Flash under $1. Cloud Run time stays within the free tier.
