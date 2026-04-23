# score_tpch.py  —  run from the root of polars-benchmark/
#
# Usage:
#   pip install polars
#   python score_tpch.py
#   python score_tpch.py --out results.jsonl
#
# No data generation needed: explain(optimized=False) works on
# unexecuted plans. We mock the scan functions so each query
# module can be imported and its LazyFrame extracted without
# reading any parquet files.

import argparse
import ast
import importlib.util
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from polars_query_dev_complexity import score_complexity

# ── Mock scan so imports don't fail on missing data files ──────────────────────
#    Each TPC-H query starts with pl.scan_parquet("...").
#    We replace it with a LazyFrame built from an empty DataFrame
#    whose schema matches the expected table columns.

_SCHEMAS = {
    "lineitem": {"l_orderkey":"Int64","l_partkey":"Int64","l_suppkey":"Int64",
                 "l_linenumber":"Int64","l_quantity":"Float64","l_extendedprice":"Float64",
                 "l_discount":"Float64","l_tax":"Float64","l_returnflag":"Utf8",
                 "l_linestatus":"Utf8","l_shipdate":"Date","l_commitdate":"Date",
                 "l_receiptdate":"Date","l_shipinstruct":"Utf8","l_shipmode":"Utf8","l_comment":"Utf8"},
    "orders":   {"o_orderkey":"Int64","o_custkey":"Int64","o_orderstatus":"Utf8",
                 "o_totalprice":"Float64","o_orderdate":"Date","o_orderpriority":"Utf8",
                 "o_clerk":"Utf8","o_shippriority":"Int32","o_comment":"Utf8"},
    "customer": {"c_custkey":"Int64","c_name":"Utf8","c_address":"Utf8","c_nationkey":"Int64",
                 "c_phone":"Utf8","c_acctbal":"Float64","c_mktsegment":"Utf8","c_comment":"Utf8"},
    "part":     {"p_partkey":"Int64","p_name":"Utf8","p_mfgr":"Utf8","p_brand":"Utf8",
                 "p_type":"Utf8","p_size":"Int32","p_container":"Utf8","p_retailprice":"Float64","p_comment":"Utf8"},
    "supplier": {"s_suppkey":"Int64","s_name":"Utf8","s_address":"Utf8","s_nationkey":"Int64",
                 "s_phone":"Utf8","s_acctbal":"Float64","s_comment":"Utf8"},
    "partsupp": {"ps_partkey":"Int64","ps_suppkey":"Int64","ps_availqty":"Int32",
                 "ps_supplycost":"Float64","ps_comment":"Utf8"},
    "nation":   {"n_nationkey":"Int64","n_name":"Utf8","n_regionkey":"Int64","n_comment":"Utf8"},
    "region":   {"r_regionkey":"Int64","r_name":"Utf8","r_comment":"Utf8"},
}

def _mock_scan(path: str, *args, **kwargs) -> pl.LazyFrame:
    """Return an empty but correctly-schemed LazyFrame for any TPC-H parquet path."""
    table = Path(path).stem.lower()          # e.g. "lineitem" from "lineitem.parquet"
    schema = _SCHEMAS.get(table, {})
    dtype_map = {"Int64": pl.Int64, "Int32": pl.Int32,
                 "Float64": pl.Float64, "Utf8": pl.Utf8, "Date": pl.Date}
    return pl.DataFrame({col: pl.Series(col, [], dtype=dtype_map[dt])
                         for col, dt in schema.items()}).lazy()

# Patch at module level before any query imports
pl.scan_parquet = _mock_scan
pl.scan_csv     = _mock_scan


# ── Load and score each query ──────────────────────────────────────────────────

def load_query_lf(query_file: Path) -> "pl.LazyFrame | None":
    """
    Import a TPC-H query module and return the LazyFrame it defines.
    The benchmark files expose either:
      - a function named q() or query() that returns a LazyFrame, or
      - a module-level variable named 'result' or 'q' that is a LazyFrame.
    """
    spec = importlib.util.spec_from_file_location("_tpch_q", query_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"  [skip] {query_file.name}: import error — {e}")
        return None

    # Try callable first
    for name in ("q", "query", "run", "main"):
        fn = getattr(mod, name, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, pl.LazyFrame):
                    return result
            except Exception:
                pass

    # Then module-level LazyFrame variable
    for name in ("result", "q", "lf", "query"):
        val = getattr(mod, name, None)
        if isinstance(val, pl.LazyFrame):
            return val

    return None


def score_all(queries_dir: Path) -> list[dict]:
    records = []
    for qfile in sorted(queries_dir.glob("q*.py")):
        lf = load_query_lf(qfile)
        if lf is None:
            continue
        result = score_complexity(lf)
        record = {
            "query":      qfile.stem,
            "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "complexity":  result.total,
            "tier":        result.tier,
            "breakdown":   result.breakdown,
            "explain":     result.explain_plan,
        }
        records.append(record)
        print(f"  {qfile.stem:<6}  {result.total:>6.1f}  [{result.tier}]")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", default="polars-benchmark/queries/polars")
    parser.add_argument("--out", default="tpch_complexity.jsonl")
    args = parser.parse_args()

    # my_project/ so polars_complexity is importable
    sys.path.insert(0, str(Path(__file__).parent))
    # polars-benchmark/ so `from queries.utils import ...` works inside query files
    sys.path.insert(0, str(Path(args.queries).resolve().parents[1]))

    queries_dir = Path(args.queries)
    if not queries_dir.exists():
        sys.exit(f"Query folder not found: {queries_dir}")

    print(f"Scoring queries in {queries_dir}/\n{'─'*40}")
    records = score_all(queries_dir)

    out = Path(args.out)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n{'─'*40}")
    print(f"Written {len(records)} records → {out}")
    print(f"Complexity range: {min(r['complexity'] for r in records):.1f}"
          f" – {max(r['complexity'] for r in records):.1f}")
