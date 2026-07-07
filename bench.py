#!/usr/bin/env python3
"""Reproduce the README benchmarks on your own vault.

Usage:
  python3 bench.py --root <vault>                    # no-op update + search latency
  python3 bench.py --root <vault> --cold             # also time a from-scratch build
  python3 bench.py --root <vault> --queries q.tsv    # recall@5 (query<TAB>expected-path-substring per line)

--cold deletes <vault>/.wikimap first. The index is disposable by design,
but notes/edges live in it too — skip --cold if you have semantics saved.
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

WIKIMAP = str(Path(__file__).parent / "wikimap.py")


def run(root, *cmd):
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, WIKIMAP, "--root", str(root), *cmd],
        capture_output=True, text=True,
    )
    return time.time() - t0, r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--queries", help="TSV: query<TAB>expected-path-substring")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve()

    if args.cold:
        shutil.rmtree(root / ".wikimap", ignore_errors=True)
        dt, out = run(root, "update")
        print(f"cold build   : {dt:6.2f}s  {out.strip()}")
    dt, out = run(root, "update")
    print(f"no-op update : {dt:6.2f}s  {out.strip()}")

    queries = []
    if args.queries:
        for line in Path(args.queries).read_text(encoding="utf-8").splitlines():
            if line.strip() and "\t" in line:
                q, expected = line.split("\t", 1)
                queries.append((q.strip(), expected.strip()))
    else:
        queries = [("spec", ""), ("plan", ""), ("update", "")]

    hits, times = 0, []
    for q, expected in queries:
        dt, out = run(root, "search", q)
        times.append(dt)
        top5 = [l for l in out.splitlines() if ":" in l and not l.startswith(" ")][:5]
        hit = bool(expected) and any(expected in l for l in top5)
        hits += hit
        mark = ("HIT " if hit else "MISS") if expected else "----"
        print(f"{mark} {dt * 1000:6.0f}ms  {q!r} -> {top5[0].split('  ')[0] if top5 else '(no results)'}")
    print(f"\nsearch: avg {sum(times) / len(times) * 1000:.0f}ms, max {max(times) * 1000:.0f}ms"
          + (f" | recall@5 {hits}/{len(queries)}" if args.queries else ""))


if __name__ == "__main__":
    main()
