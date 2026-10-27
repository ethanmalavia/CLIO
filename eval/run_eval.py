"""
Run the same questions against two deployed CLIO services and record the answers.

Each case selects its region once (on the first arm), then sends the same
conversation, with the same selection statistics, to every arm. The arms
differ only in GEMINI_MODEL (see eval/deploy_arms.sh), so differences in the
answers come from the model.

Standard library only, so it runs anywhere Python 3.10+ and gcloud do:

  python eval/run_eval.py --arm pro=https://clio-eval-pro-....run.app \
                          --arm flash=https://clio-eval-flash-....run.app \
                          --repeats 2

The services are private: requests carry an identity token from
`gcloud auth print-identity-token` (or from the CLIO_ID_TOKEN env var).
Output goes to eval/results/<timestamp>/ (gitignored: it holds statistics
from unpublished data).
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent


class Client:
    """Authenticated JSON client for one CLIO service."""

    _token, _token_time, _token_lock = None, 0.0, threading.Lock()

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @classmethod
    def token(cls) -> str:
        with cls._token_lock:
            if os.getenv("CLIO_ID_TOKEN"):
                return os.environ["CLIO_ID_TOKEN"]
            # Identity tokens last an hour; refresh well before that.
            if cls._token is None or time.time() - cls._token_time > 40 * 60:
                gcloud = shutil.which("gcloud")
                if not gcloud:
                    raise SystemExit("gcloud not found; set CLIO_ID_TOKEN instead.")
                cls._token = subprocess.run(
                    [gcloud, "auth", "print-identity-token"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                cls._token_time = time.time()
            return cls._token

    def call(self, path: str, body: dict | None = None, timeout: int = 320):
        """Return (status, json, seconds). Waits and retries on 429 and cold-start 5xx."""
        for attempt in range(6):
            request = urllib.request.Request(
                self.base_url + path,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {self.token()}",
                         "Content-Type": "application/json"},
                method="GET" if body is None else "POST",
            )
            start = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, json.load(response), time.monotonic() - start
            except urllib.error.HTTPError as error:
                elapsed = time.monotonic() - start
                try:
                    data = json.load(error)
                except Exception:
                    data = {"error": error.reason}
                if error.code == 429 and attempt < 5:
                    time.sleep(int(error.headers.get("Retry-After", "60")))
                    continue
                if error.code in (502, 503) and attempt < 5:
                    time.sleep(10)
                    continue
                return error.code, data, elapsed
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < 5:
                    time.sleep(10)
                    continue
                return 0, {"error": str(error)}, time.monotonic() - start
        raise AssertionError("unreachable")


def resolve_variable(wanted: str, variables: list) -> str:
    if wanted in variables:
        return wanted
    matches = [v for v in variables if wanted.lower() in v.lower()]
    if not matches:
        raise SystemExit(f"No dataset variable matches '{wanted}'. Variables: {variables}")
    return matches[0]


def select_regions(client: Client, spec: dict, info: dict) -> dict:
    """Run /region/select once per (region, variable) used by the cases."""
    selections = {}
    time_range = [info["time_range"]["start"], info["time_range"]["end"]]
    for case in spec["cases"]:
        if not case.get("region"):
            continue
        variable = resolve_variable(case["variable"], info["variables"])
        key = f"{case['region']}|{variable}"
        if key in selections:
            continue
        status, data, _ = client.call("/region/select", {
            "bbox":       spec["regions"][case["region"]],
            "time_range": case.get("time_range", time_range),
            "variable":   variable,
        })
        if status != 200:
            raise SystemExit(f"/region/select failed for {key}: {status} {data}")
        selections[key] = data["stats"]
        print(f"selected {key}: mean={data['stats'].get('mean')} {data['stats'].get('units')}")
    return selections


def run_conversation(arm: str, client: Client, case: dict, stats: dict | None, repeat: int) -> list:
    """Send a case's turns to one arm, carrying that arm's own answers forward as history."""
    history, records = [], []
    for turn, message in enumerate(case["turns"]):
        status, data, seconds = client.call("/query", {
            "message":          message,
            "history":          history,
            "selectionContext": stats,
        })
        text = data.get("textResponse")
        records.append({
            "case": case["id"], "category": case["category"], "arm": arm,
            "repeat": repeat, "turn": turn, "question": message,
            "status": status, "seconds": round(seconds, 2),
            "text": text, "error": data.get("error"), "toolLog": data.get("toolLog", []),
        })
        if status != 200:
            break  # mirror the frontend: a failed turn is not added to the history
        history += [{"role": "user", "content": message},
                    {"role": "assistant", "content": text or "No response."}]
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", required=True, metavar="NAME=URL",
                        help="a service to test; give two or more")
    parser.add_argument("--cases", default=str(EVAL_DIR / "cases.json"))
    parser.add_argument("--repeats", type=int, default=2,
                        help="times to run each case per arm (answers vary even at temperature 0)")
    parser.add_argument("--only", nargs="*", help="run only these case ids")
    parser.add_argument("--workers", type=int, default=2, help="concurrent conversations per arm")
    args = parser.parse_args()

    arms = dict(a.split("=", 1) for a in args.arm)
    clients = {name: Client(url) for name, url in arms.items()}
    spec = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        spec["cases"] = [c for c in spec["cases"] if c["id"] in args.only]

    first = next(iter(clients.values()))
    status, info, _ = first.call("/dataset/info")
    if status != 200:
        raise SystemExit(f"/dataset/info failed: {status} {info}")
    print(f"dataset variables: {info['variables']}, {info['time_range']}")
    selections = select_regions(first, spec, info)

    out_dir = EVAL_DIR / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True)
    (out_dir / "run.json").write_text(json.dumps({
        "arms": arms, "repeats": args.repeats, "datasetInfo": info,
        "selections": selections, "cases": spec,
    }, indent=2), encoding="utf-8")

    jobs = []
    for case in spec["cases"]:
        stats = None
        if case.get("region"):
            stats = selections[f"{case['region']}|{resolve_variable(case['variable'], info['variables'])}"]
        for repeat in range(args.repeats):
            for arm in arms:
                jobs.append((arm, case, stats, repeat))

    lock = threading.Lock()
    with open(out_dir / "responses.jsonl", "w", encoding="utf-8") as sink, \
         ThreadPoolExecutor(max_workers=args.workers * len(arms)) as pool:
        def job(arm, case, stats, repeat):
            for record in run_conversation(arm, clients[arm], case, stats, repeat):
                with lock:
                    sink.write(json.dumps(record) + "\n")
                    sink.flush()
                    print(f"{record['arm']:>6} {record['case']:<26} r{repeat} t{record['turn']} "
                          f"{record['status']} {record['seconds']:6.1f}s")
        for future in [pool.submit(job, *j) for j in jobs]:
            future.result()

    print(f"\nDone. Score with: python eval/score.py {out_dir}")


if __name__ == "__main__":
    main()
