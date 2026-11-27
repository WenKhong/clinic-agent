"""Retrieval eval: hit@3 of vector search() vs hybrid_search() on evals/retrieval.yaml.

Run:  uv run python -m evals.run_retrieval            # summary
      uv run python -m evals.run_retrieval --show 7   # top 3 of both methods for question 7

A hit = any expected FAQ id is in the top 3. Ids are compared as strings: the
FAQ id comes from the chunk source ("clinic.yaml#fees" -> "fees"), because
the database id means nothing in the YAML.

Needs GEMINI_API_KEY (one embedding call per question) and the Compose
Postgres with embedded FAQ chunks (uv run python -m app.embed).
"""

import argparse
import os
from collections import defaultdict
from pathlib import Path

import yaml

from app.db import get_conn  # importing app.db also loads .env into os.environ
from app.embed import Embedder, gemini_embedder
from app.load_clinic import FAQ_SOURCE_PREFIX
from app.search import Hit, hybrid_search, search

CASES_PATH = Path(__file__).with_name("retrieval.yaml")
K = 3


def faq_id(hit: Hit) -> str:
    return str(hit.source.removeprefix(FAQ_SOURCE_PREFIX))


def is_hit(expected: list, top: list[str]) -> bool:
    return any(str(e) in top for e in expected)


def cached(embed: Embedder) -> Embedder:
    """Embed each text once: hybrid_search() calls search() again with the same query."""
    seen: dict[str, list[float]] = {}

    def wrapped(texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in seen]
        if missing:
            seen.update(zip(missing, embed(missing), strict=True))
        return [seen[t] for t in texts]

    return wrapped


def pct(hits: int, n: int) -> str:
    return f"{hits}/{n} ({hits / n:.0%})"


def print_case(n: int, case: dict, vector: list[str], hybrid: list[str]) -> None:
    expected = [str(e) for e in case["expected"]]
    print(f"#{n} {case['id']} [{case['lang']}] {case['question']!r}")
    print(f"   expected: {', '.join(expected)}")
    for name, top in (("vector", vector), ("hybrid", hybrid)):
        verdict = "HIT " if is_hit(expected, top) else "miss"
        print(f"   {name}  {verdict}  {', '.join(top)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--show", type=int, metavar="N", help="print the top 3 for question N")
    args = parser.parse_args()

    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    numbered = list(enumerate(cases, start=1))
    if args.show is not None:
        if not 1 <= args.show <= len(cases):
            raise SystemExit(f"--show must be between 1 and {len(cases)}")
        numbered = [numbered[args.show - 1]]

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is empty: paste your key into .env (see .env.example).")
    embed = cached(gemini_embedder(api_key))

    results = []
    with get_conn() as conn:
        for n, case in numbered:
            q = case["question"]
            vector = [faq_id(h) for h in search(conn, embed, q, K)]
            hybrid = [faq_id(h) for h in hybrid_search(conn, embed, q, K)]
            results.append((n, case, vector, hybrid))

    if args.show is not None:
        print_case(*results[0])
        return

    total = len(results)
    v_hits = sum(is_hit(c["expected"], v) for _, c, v, _ in results)
    h_hits = sum(is_hit(c["expected"], h) for _, c, _, h in results)
    print(f"hit@{K}   vector {pct(v_hits, total)}   hybrid {pct(h_hits, total)}")

    by_lang: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for _, c, v, h in results:
        by_lang[c["lang"]].append((is_hit(c["expected"], v), is_hit(c["expected"], h)))
    print(f"\n{'type':<10} {'n':>2}   {'vector':<12} hybrid")
    for lang, rows in by_lang.items():
        n = len(rows)
        print(
            f"{lang:<10} {n:>2}   {pct(sum(v for v, _ in rows), n):<12} "
            f"{pct(sum(h for _, h in rows), n)}"
        )

    disagree = [r for r in results if is_hit(r[1]["expected"], r[2]) != is_hit(r[1]["expected"], r[3])]
    print(f"\nDisagreements (one method hits, the other misses): {len(disagree)}")
    for r in disagree:
        print_case(*r)
    differ = sum(v != h for _, _, v, h in results)
    print(f"\nTop-3 lists differ (any order or id) on {differ}/{total} questions.")


if __name__ == "__main__":
    main()
