"""
polars_query_dev_complexity.py
──────────────────────────────

Author : Francis Wolinski
Created: 2026-04-23
License: MIT

Full walkthrough of polars_query_dev_complexity features:
   1. Scoring from raw plan strings  (no Polars install needed)
   2. Scoring live LazyFrames
   3. complexity_collect() context manager — accumulate & threshold
   4. JSONLFileHandler — write, read, tail, analyse

Usage:
─────
    uv run demo.py
"""

import json
import pathlib
import tempfile
from datetime import timezone

# Rename polars_query_dev_complexity.py first; until then import as-is:
from polars_query_dev_complexity import (
    ComplexityThresholdExceeded,
    JSONLFileHandler,
    complexity_collect,
    score_plan_string,
)

W = 60  # section header width


def section(title: str) -> None:
    print()
    print("=" * W)
    print(title)
    print("=" * W)


# ── 1. Score from raw plan strings ────────────────────────────────────────────
#    Useful when you have cached explain output, or want to test without
#    building real LazyFrames.

PLAN_SIMPLE = (
    'SELECT [col("_id").count()] '
    'SELECT [col("_id")] '
    "Parquet SCAN [user.parquet] PROJECT */9 COLUMNS ESTIMATED ROWS: 1000"
)

PLAN_COMPLEX = "\n".join([
    'SELECT [col("_id").count()]',
    '  SELECT [col("_id")]',
    '    FILTER col("createdAt").dt.to_string().is_between(["2026-01-01", "2026-04-01"])',
    '      FROM',
    '        FILTER col("position").is_in([["engineer"]])',
    '          FROM',
    '            FILTER [(col("role")) == ("TECH")]',
    '              FROM',
    '                Parquet SCAN [user.parquet] PROJECT */9 COLUMNS ESTIMATED ROWS: 1000',
])

section("1. SCORING FROM PLAN STRINGS")
r_simple  = score_plan_string(PLAN_SIMPLE)
r_complex = score_plan_string(PLAN_COMPLEX)
print(r_simple)
print()
print(r_complex)
print()
print(f"  simple  → {r_simple.total:.1f}  [{r_simple.tier}]")
print(f"  complex → {r_complex.total:.1f}  [{r_complex.tier}]")


# ── 2. Score live LazyFrames ──────────────────────────────────────────────────

section("2. LIVE LAZYFRAME SCORES")
try:
    import polars as pl

    df = pl.DataFrame({
        "_id":       range(12),
        "department":      ["TECH", "HR", "COM"] * 4,
        "position":  ["officer", "manager", "engineer", "employee"] * 3,
        "createdAt": ["2026-01-15"] * 12,
    })
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        parquet_path = pathlib.Path(f.name)
    df.write_parquet(parquet_path)

    lf_simple = pl.scan_parquet(parquet_path).select(pl.col("_id").count())

    lf_complex = (
        pl.scan_parquet(parquet_path)
        .filter(pl.col("department") == "TECH")
        .filter(pl.col("position").is_in(["engineer"]))
        .filter(pl.col("createdAt").str.to_date().dt.year() >= 2026)
        .select(pl.col("_id").count())
    )

    from polars_query_dev_complexity import score_complexity
    for label, lf in [("simple", lf_simple), ("complex", lf_complex)]:
        r = score_complexity(lf)
        print(f"  {label:<10} → {r.total:.1f}  [{r.tier}]")

    POLARS_AVAILABLE = True

except ImportError:
    print("  (skipped — polars not installed)")
    POLARS_AVAILABLE = False


# ── 3. complexity_collect() — accumulate results ──────────────────────────────

section("3a. CONTEXT MANAGER: accumulate results")
if POLARS_AVAILABLE:
    captured = []
    with complexity_collect(callback=captured.append, log=False):
        df_s = lf_simple.collect()
        df_c = lf_complex.collect()
    print(f"  collect() calls intercepted : {len(captured)}")
    print(f"  scores captured             : {[r.total for r in captured]}")
    print(f"  DataFrames returned intact  : shapes {[df_s.shape, df_c.shape]}")
else:
    print("  (skipped — polars not installed)")


# ── 3b. complexity_collect() — threshold guard ────────────────────────────────

section("3b. CONTEXT MANAGER: threshold guard")
if POLARS_AVAILABLE:
    try:
        with complexity_collect(threshold=10.0, log=False):
            lf_simple.collect()   # passes — score is low
            lf_complex.collect()  # blocked — score exceeds 10.0
    except ComplexityThresholdExceeded as exc:
        print(f"  collect() blocked: {exc.result.total:.1f} > {exc.threshold}")
        print(f"  tier    : {exc.result.tier}")
        print(f"  breakdown:")
        for k, v in exc.result.breakdown.items():
            print(f"    {k:<28} {v:+.2f}")
else:
    print("  (skipped — polars not installed)")


# ── 4. JSONLFileHandler ───────────────────────────────────────────────────────

section("4a. JSONL LOG: write via complexity_collect callback")

with tempfile.TemporaryDirectory() as tmp:
    log_path = pathlib.Path(tmp) / "complexity.jsonl"

    handler = JSONLFileHandler(
        log_path,
        tz=timezone.utc,
        extra={"app": "demo", "env": "dev"},
    )

    if POLARS_AVAILABLE:
        with complexity_collect(callback=handler, log=False, log_caller=True):
            lf_simple.collect()
            lf_complex.collect()
    else:
        # Feed the handler directly from plan strings when Polars unavailable
        handler(r_simple)
        handler(r_complex)

    # ── 4b. Read back and inspect ─────────────────────────────────────────
    section("4b. JSONL LOG: read_all()")
    records = handler.read_all()
    print(f"  {len(records)} record(s) in {log_path.name}\n")
    for rec in records:
        print(json.dumps(rec, indent=2))

    # ── 4c. tail() ────────────────────────────────────────────────────────
    section("4c. JSONL LOG: tail(1)")
    for rec in handler.tail(1):
        print(f"  timestamp : {rec['timestamp']}")
        print(f"  complexity: {rec['complexity']}  [{rec['tier']}]")
        print(f"  breakdown : {rec['breakdown']}")

    # ── 4d. Simple analysis without pandas ───────────────────────────────
    section("4d. JSONL LOG: simple analysis")
    all_records = handler.read_all()
    scores = [r["complexity"] for r in all_records]
    tiers  = [r["tier"] for r in all_records]
    print(f"  count  : {len(scores)}")
    print(f"  min    : {min(scores):.1f}")
    print(f"  max    : {max(scores):.1f}")
    print(f"  avg    : {sum(scores) / len(scores):.1f}")
    print(f"  tiers  : {dict((t, tiers.count(t)) for t in sorted(set(tiers)))}")

    above_threshold = [r for r in all_records if r["complexity"] > 10.0]
    print(f"\n  queries above threshold 10.0:")
    if above_threshold:
        for r in above_threshold:
            print(f"    {r['timestamp']}  {r['complexity']:.1f}  [{r['tier']}]")
    else:
        print("    none")

# ── cleanup ───────────────────────────────────────────────────────────────────
if POLARS_AVAILABLE:
    parquet_path.unlink(missing_ok=True)

print()
print("─" * W)
print("Demo complete.")
