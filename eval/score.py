"""
Score a run of eval/run_eval.py.

  python eval/score.py eval/results/<run>            automatic checks + blinded review sheet
  python eval/score.py eval/results/<run> --grades   also merge grades.json and unblind

Automatic checks (per answer):
  ok              the request succeeded
  retrieved       the agent called retrieve_domain_context
  citations       parenthetical citations with a year and a page; a citation is
                  "traced" when its page is among the pages actually retrieved
  untraced nums   numbers that appear neither in the selection statistics, the
                  dataset bounds, the question nor the retrieved passages (rounding
                  allowed). These are candidates for fabrication; check them by hand.
  doi / markdown  the system prompt forbids both
  recap           for "recap" cases, how many of the 6 recap elements are present
  expected nums   for "numbers" cases, whether the named statistics are stated

Human grading: review.md shows each conversation from both arms side by side
as "X" and "Y" in random order, with the case's rubric. Fill grades.json
(created on the first run) with 0 = fails the rubric, 1 = partly, 2 = fully,
then run again with --grades.
"""
import argparse
import json
import random
import re
import statistics
from collections import defaultdict
from pathlib import Path

NUMBER_RE   = re.compile(r"(?<![\w.])[-−]?\d+(?:,\d{3})*(?:\.\d+)?")
PAREN_RE    = re.compile(r"\(([^()]*)\)")
YEAR_RE     = re.compile(r"\b(?:19|20)\d{2}\b")
PAGES_RE    = re.compile(r"\bpp?\.\s*([\w–\-, ]+)")
SCENARIO_RE = re.compile(r"\b(?:SSP\d-\d\.\d|RCP\s?\d\.\d)\b", re.IGNORECASE)
DOI_RE      = re.compile(r"\bdoi\b|\b10\.\d{4,9}/", re.IGNORECASE)
MARKDOWN_RE = re.compile(r"\*\*|__|^#{1,6}\s|`", re.MULTILINE)
CHUNK_RE    = re.compile(r"SOURCE:\s*(?P<source>[^\n]*)\nAUTHOR:\s*(?P<author>[^\n]*)\n"
                         r"(?:YEAR:\s*(?P<year>[^\n]*)\n)?(?:DOI:[^\n]*\n)?PAGE:\s*(?P<page>[^\n]*)\n"
                         r"CONTENT:\s*(?P<content>.*?)(?=--- DOCUMENT CHUNK ---|\Z)", re.DOTALL)


def to_float(token: str) -> float:
    return float(token.replace("−", "-").replace(",", ""))


def numbers_in(value) -> list:
    """Every number in a value: dict/list contents, numbers, and digits inside strings."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in numbers_in(v)]
    return [to_float(t) for t in NUMBER_RE.findall(str(value).replace("-", " "))]


def matches(token: str, value: float) -> bool:
    """True if `token` is `value` rounded to the token's precision (sign ignored:
    answers often say 'decreasing by 0.02' for a trend of -0.02)."""
    decimals = len(token.split(".")[1]) if "." in token else 0
    number = abs(to_float(token))
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    return abs(number - abs(value)) <= tolerance or (
        abs(value) >= 100 and abs(number - abs(value)) <= 0.005 * abs(value))


def retrieved_chunks(tool_log: list) -> list:
    chunks = []
    for entry in tool_log or []:
        if entry.get("type") == "result":
            chunks += [m.groupdict() for m in CHUNK_RE.finditer(str(entry.get("content", "")))]
    return chunks


def citations(text: str) -> list:
    found = []
    for inner in PAREN_RE.findall(text or ""):
        pages = PAGES_RE.search(inner)
        if YEAR_RE.search(inner) or pages:
            found.append({"text": inner, "year": bool(YEAR_RE.search(inner)),
                          "pages": [p.strip() for p in re.split(r"[,–\-]", pages.group(1)) if p.strip()]
                          if pages else []})
    return found


def strip_for_numbers(text: str) -> str:
    text = SCENARIO_RE.sub(" ", text or "")
    return PAREN_RE.sub(lambda m: " " if (YEAR_RE.search(m.group(1)) or PAGES_RE.search(m.group(1)))
                        else m.group(0), text)


def check_answer(record: dict, case: dict, stats: dict | None, info: dict, prior_logs: list) -> dict:
    text = record.get("text") or ""
    log = record.get("toolLog") or []
    chunks = retrieved_chunks(log + prior_logs)
    retrieved = any(e.get("type") == "call" and e.get("tool") == "retrieve_domain_context" for e in log)

    allowed = numbers_in(stats) + numbers_in(info.get("lat_lon_bounds")) + \
        numbers_in(info.get("time_range")) + numbers_in(record["question"]) + \
        [n for c in chunks for n in numbers_in(c["content"])]
    untraced = []
    for token in NUMBER_RE.findall(strip_for_numbers(text)):
        number = abs(to_float(token))
        if "." not in token and number <= 10:
            continue  # counts and ordinals ("three", "2 scenarios") are too noisy to check
        if not any(matches(token, v) for v in allowed):
            untraced.append(token)

    cites = citations(text)
    pages = {c["page"].strip() for c in chunks}
    traced = [c for c in cites if c["pages"] and all(p in pages for p in c["pages"])]

    result = {
        "ok": record["status"] == 200,
        "retrieved": retrieved,
        "citations": len(cites),
        "citations_with_year_and_page": sum(1 for c in cites if c["year"] and c["pages"]),
        "citations_traced": len(traced),
        "untraced_numbers": untraced,
        "doi_leak": bool(DOI_RE.search(text)),
        "markdown": bool(MARKDOWN_RE.search(text)),
        "seconds": record["seconds"],
        "chars": len(text),
    }

    checks = case.get("checks", {})
    if record["turn"] == 0 and stats:
        stated = lambda key: stats.get(key) is not None and any(
            matches(t, stats[key]) for t in NUMBER_RE.findall(strip_for_numbers(text)))
        if checks.get("numbers"):
            result["expected_numbers"] = {k: stated(k) for k in checks["numbers"]}
        if checks.get("recap"):
            location = stats.get("max_value_location") or {}
            years = {s[:4] for s in stats.get("time_range", [])}
            result["recap"] = {
                "long_name": stats.get("long_name", "").lower() in text.lower(),
                "period": all(y in text for y in years),
                "trend": stated("trend_per_year") or "trend" in text.lower(),
                "min": stated("min"),
                "max": stated("max"),
                "max_location": bool(location) and all(
                    any(matches(t, location[k]) for t in NUMBER_RE.findall(text) if "." in t)
                    for k in ("lat", "lon")),
            }
    if checks.get("retrieval") is True and record["turn"] == 0:
        result["retrieval_as_expected"] = retrieved
    if checks.get("no_retrieval"):
        result["retrieval_as_expected"] = not retrieved
    return result


def load_run(run_dir: Path):
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text(encoding="utf-8").splitlines() if line]
    records.sort(key=lambda r: (r["case"], r["repeat"], r["arm"], r["turn"]))
    return run, records


def selection_for(case: dict, run: dict):
    if not case.get("region"):
        return None
    variables = run["datasetInfo"]["variables"]
    variable = case["variable"] if case["variable"] in variables else \
        next(v for v in variables if case["variable"].lower() in v.lower())
    return run["selections"][f"{case['region']}|{variable}"]


def pct(values) -> str:
    values = list(values)
    return f"{100 * sum(values) / len(values):.0f}% ({sum(values)}/{len(values)})" if values else "n/a"


def summarize(run, records, checked, grades_by_arm) -> str:
    arms = list(run["arms"])
    rows = []

    def row(label, fn):
        rows.append(f"| {label} | " + " | ".join(fn(a) for a in arms) + " |")

    by_arm = {a: [(r, c) for r, c in zip(records, checked) if r["arm"] == a] for a in arms}
    ok = lambda a: [c for _, c in by_arm[a] if c["ok"]]
    row("Requests succeeded", lambda a: pct(c["ok"] for _, c in by_arm[a]))
    row("Median / p90 latency (s)", lambda a: "{:.1f} / {:.1f}".format(
        statistics.median([c["seconds"] for c in ok(a)]),
        sorted(c["seconds"] for c in ok(a))[int(0.9 * (len(ok(a)) - 1))]) if ok(a) else "n/a")
    row("Median answer length (chars)", lambda a: f"{statistics.median([c['chars'] for c in ok(a)]):.0f}" if ok(a) else "n/a")
    row("Retrieved when the case needs it", lambda a: pct(
        c["retrieval_as_expected"] for c in ok(a) if "retrieval_as_expected" in c))
    row("Expected statistics stated", lambda a: pct(
        v for c in ok(a) for v in c.get("expected_numbers", {}).values()))
    row("Recap elements present", lambda a: pct(
        v for c in ok(a) for v in c.get("recap", {}).values()))
    row("Citations with year and page", lambda a: pct(
        [1] * sum(c["citations_with_year_and_page"] for c in ok(a)) +
        [0] * sum(c["citations"] - c["citations_with_year_and_page"] for c in ok(a))))
    row("Cited pages that were retrieved", lambda a: pct(
        [1] * sum(c["citations_traced"] for c in ok(a)) +
        [0] * sum(c["citations"] - c["citations_traced"] for c in ok(a))))
    row("Answers with untraced numbers", lambda a: pct(bool(c["untraced_numbers"]) for c in ok(a)))
    row("Answers stating a DOI", lambda a: pct(c["doi_leak"] for c in ok(a)))
    row("Answers using markdown", lambda a: pct(c["markdown"] for c in ok(a)))

    lines = [f"# CLIO model comparison: {' vs '.join(arms)}", "",
             "Arms: " + ", ".join(f"{a} = {u}" for a, u in run["arms"].items()),
             f"Repeats per case: {run['repeats']}", "",
             "## Automatic checks", "", "| Check | " + " | ".join(arms) + " |",
             "| --- |" + " --- |" * len(arms), *rows, ""]

    if grades_by_arm:
        lines += ["## Blinded rubric grades (0 fails, 1 partly, 2 fully)", "",
                  "| Category | " + " | ".join(arms) + " |", "| --- |" + " --- |" * len(arms)]
        categories = sorted({cat for a in arms for cat, _ in grades_by_arm[a]})
        for cat in categories + ["all"]:
            cells = []
            for a in arms:
                scores = [s for c, s in grades_by_arm[a] if cat in (c, "all")]
                cells.append(f"{statistics.mean(scores):.2f} (n={len(scores)}, {sum(s == 0 for s in scores)} fails)"
                             if scores else "n/a")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")
        lines.append("")

    lines += ["## Untraced numbers (check by hand)", ""]
    for r, c in zip(records, checked):
        if c["untraced_numbers"]:
            lines.append(f"- {r['arm']} {r['case']} r{r['repeat']} t{r['turn']}: {', '.join(c['untraced_numbers'])}")
    return "\n".join(lines) + "\n"


def write_review(run_dir: Path, run, records, cases):
    """Blinded side-by-side sheet: arms appear as X/Y in a random order per item."""
    rng = random.Random(run_dir.name)
    groups = defaultdict(lambda: defaultdict(list))
    for r in records:
        groups[(r["case"], r["repeat"])][r["arm"]].append(r)
    key, out = {}, ["# Blinded review", "",
                    "Grade each response 0 (fails the rubric), 1 (partly meets it) or 2 (fully meets it) in grades.json.",
                    "Check numbers against the selection statistics and citations against the retrieved pages.", ""]
    for (case_id, repeat), by_arm in sorted(groups.items()):
        item = f"{case_id}|r{repeat}"
        arms = list(by_arm)
        rng.shuffle(arms)
        labels = dict(zip("XYZW", arms))
        key[item] = labels
        case = cases[case_id]
        out += [f"## {item}  ({case['category']})", "", f"Rubric: {case['rubric']}", ""]
        stats = selection_for(case, run)
        out += ["Selection statistics:", "```", json.dumps(stats, indent=1) if stats else "none (no selection)", "```", ""]
        for label, arm in labels.items():
            out.append(f"### {label}")
            for r in by_arm[arm]:
                sources = sorted({f"{c['source'][:50]} p.{c['page']}" for c in retrieved_chunks(r["toolLog"])})
                out += [f"**User:** {r['question']}", "",
                        f"**Status {r['status']}, retrieved:** {'; '.join(sources) or 'nothing'}", "",
                        r["text"] or f"(error: {r['error']})", ""]
    (run_dir / "review.md").write_text("\n".join(out), encoding="utf-8")
    (run_dir / "blind_key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    grades_path = run_dir / "grades.json"
    if not grades_path.exists():
        grades_path.write_text(json.dumps(
            {item: {label: {"score": None, "note": ""} for label in labels} | {"preferred": None}
             for item, labels in key.items()}, indent=1), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--grades", action="store_true", help="merge grades.json into the report")
    args = parser.parse_args()

    run, records = load_run(args.run_dir)
    cases = {c["id"]: c for c in run["cases"]["cases"]}

    checked, prior = [], defaultdict(list)
    for r in records:
        case = cases[r["case"]]
        conversation = (r["case"], r["repeat"], r["arm"])
        checked.append(check_answer(r, case, selection_for(case, run), run["datasetInfo"], prior[conversation]))
        prior[conversation] += r.get("toolLog") or []
    (args.run_dir / "checks.jsonl").write_text(
        "".join(json.dumps({"case": r["case"], "arm": r["arm"], "repeat": r["repeat"], "turn": r["turn"], **c}) + "\n"
                for r, c in zip(records, checked)), encoding="utf-8")

    write_review(args.run_dir, run, records, cases)

    grades_by_arm = {}
    if args.grades:
        grades = json.loads((args.run_dir / "grades.json").read_text(encoding="utf-8"))
        key = json.loads((args.run_dir / "blind_key.json").read_text(encoding="utf-8"))
        grades_by_arm = {a: [] for a in run["arms"]}
        preferred = defaultdict(int)
        for item, labels in key.items():
            category = cases[item.split("|")[0]]["category"]
            for label, arm in labels.items():
                score = grades.get(item, {}).get(label, {}).get("score")
                if score is not None:
                    grades_by_arm[arm].append((category, score))
            choice = grades.get(item, {}).get("preferred")
            preferred[labels.get(choice, "tie") if choice else "ungraded"] += 1
        print("Preferred:", dict(preferred))

    report = summarize(run, records, checked, grades_by_arm)
    (args.run_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Blinded review sheet: {args.run_dir / 'review.md'}")


if __name__ == "__main__":
    main()
