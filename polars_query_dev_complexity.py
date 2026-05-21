"""
polars_query_dev_complexity.py
──────────────────────────────

Author : Francis Wolinski
Created: 2026-04-23
License: MIT

Scores the *authoring* complexity of a Polars LazyFrame query
from its unoptimised explain plan.

Complexity ≠ execution cost.  We measure how much mental effort and code
a developer had to write, using these signals from the explain string:

  Operation count    – each SELECT / FILTER / SORT / GROUP_BY / JOIN / …
  Filter depth       – deeply nested FILTERs multiply the score
  Expression chains  – dots in an expression proxy method-chain length
  Column references  – unique col("x") names referenced
  Aggregations       – .count(), .sum(), .mean(), … are explicit authoring steps
  Literal lists      – ["a","b"] inside predicates indicate hand-typed enumerations
  Joins              – flat bonus per JOIN (major authoring unit)

Usage
─────
    import polars as pl
    from polars_query_dev_complexity import score_complexity, complexity_collect

    # One-shot scoring
    lf = pl.scan_parquet("user.parquet").filter(...)
    result = score_complexity(lf)
    print(result)               # ComplexityResult with .total and .breakdown

    # Patch collect() for the duration of a block
    collected = []
    with complexity_collect(callback=collected.append, threshold=30):
        df = lf.collect()       # scores first, then collects normally
    # df is the real DataFrame; collected[0] is the ComplexityResult

    # JSON
    log_path = pathlib.Path(tmp) / "complexity.jsonl"

    handler = JSONLFileHandler(
        log_path,
        tz=timezone.utc,
        extra={"app": "demo", "env": "dev"},
    )

    with complexity_collect(callback=handler, log=False):
        lf.collect()

"""

import inspect
import re
from dataclasses import dataclass, field
from typing import Optional

import polars as pl


# ── Scoring weights ────────────────────────────────────────────────────────────

WEIGHTS = {
    "op_base":          1.0,   # per recognised operation node
    "filter_depth":     1.5,   # extra per nesting level beyond 1
    "expr_chain_hop":   0.5,   # per additional dot-method beyond the bare col()
    "unique_col":       0.5,   # per unique column name referenced
    "aggregation":      1.5,   # per aggregation function found
    "literal_value":    0.5,   # per scalar inside a list literal  ["a","b","c"]
    "join":             5.0,   # flat bonus per JOIN node
}

# Default thresholds
THRESHOLDS = {
    "trivial":      5,
    "simple":       12,
    "moderate":     22,
    "complex":      35,
    "very complex": float("inf"),
}

# Regex helpers
_RE_OP      = re.compile(
    r"^\s*(SELECT|FILTER|SORT|GROUP_BY|JOIN|AGGREGATE|EXPLODE|MELT|"
    r"WITH_COLUMNS|SLICE|LIMIT|CACHE|SCAN|PROJECT)\b",
    re.MULTILINE | re.IGNORECASE,
)
_RE_COL     = re.compile(r'col\("([^"]+)"\)')
_RE_AGG     = re.compile(
    r"\.(count|sum|mean|median|min|max|std|var|first|last|n_unique|"
    r"product|cumsum|cumprod|cummin|cummax)\s*\(",
    re.IGNORECASE,
)
_RE_LIST    = re.compile(r"\[([^\]]+)\]")  # captures content of [...] literals
_RE_STRING  = re.compile(r'"[^"]*"')


# ── Core data-class ────────────────────────────────────────────────────────────

@dataclass
class ComplexityResult:
    """Holds the total score and a named breakdown for each signal."""

    total: float
    thresholds: dict[str, float]
    breakdown: dict[str, float] = field(default_factory=dict)
    explain_plan: str = ""
    caller: str = ""

    def __post_init__(self):
        items = [(limit, label) for label, limit in self.thresholds.items()]
        items.sort(key=lambda x: x[0])

        # Validation (optional but recommended)
        last = -float("inf")
        for limit, label in items:
            if limit < last:
                raise ValueError("Thresholds must be increasing")
            last = limit

        self._normalized = items

    # Derived tier (purely qualitative)
    @property
    def tier(self) -> str:
        for limit, label in self._normalized:
            if self.total <= limit:
                return label
        return "unknown"

    def __str__(self) -> str:
        lines = [
            f"Authoring complexity : {self.total:.1f}  [{self.tier}]",
            "─" * 44,
        ]
        for k, v in self.breakdown.items():
            lines.append(f"  {k:<28} {v:+.1f}")
        lines.append("─" * 44)
        #lines.append("Plan:")
        #for ln in self.explain_plan.splitlines():
        #    lines.append("  " + ln)
        return "\n".join(lines)


# ── Main scorer ────────────────────────────────────────────────────────────────

def score_complexity(
    lf: pl.LazyFrame,
    *,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
    caller: Optional[str] = None,
) -> ComplexityResult:
    """
    Score the authoring complexity of *lf* from its unoptimised explain plan.

    Parameters
    ----------
    lf      : A Polars LazyFrame (any depth of operations).
    weights : Override any subset of the default WEIGHTS dict.
    thresholds : Override any subset of the default THRESHOLDS dict.
    caller: Optional caller of LazyFrame.collect().

    Returns
    -------
    ComplexityResult with .total, .breakdown, .tier, .explain_plan
    """
    w = WEIGHTS | (weights or {})
    t = THRESHOLDS | (thresholds or {})
    plan = lf.explain(optimized=False)
    return _score_plan(plan, w, t, caller)


def score_plan_string(
    plan: str,
    *,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
    caller: Optional[str] = None,
) -> ComplexityResult:
    """
    Score directly from an explain-plan string (useful for testing / caching).
    """
    w = WEIGHTS | (weights or {})
    t = THRESHOLDS | (thresholds or {})
    return _score_plan(plan, w, t, caller)


# ── Internal logic ─────────────────────────────────────────────────────────────

def _score_plan(plan: str, w: dict[str, float], t: dict[str, float], c: str) -> ComplexityResult:
    bd: dict[str, float] = {}

    # 1. Operation count
    ops = _RE_OP.findall(plan)
    bd["operations"] = len(ops) * w["op_base"]

    # 2. JOIN bonus
    join_count = sum(1 for op in ops if op.upper() == "JOIN")
    if join_count:
        bd["joins"] = join_count * w["join"]

    # 3. FILTER depth (count nested FILTER lines)
    filter_lines = [ln for ln in plan.splitlines()
                    if re.match(r"\s*FILTER\b", ln, re.IGNORECASE)]
    depth = len(filter_lines)
    if depth > 1:
        bd["filter_depth_penalty"] = (depth - 1) * w["filter_depth"]

    # 4. Expression method-chain hops
    #    Strip string literals so we don't count dots inside column names.
    bare_plan = _RE_STRING.sub('""', plan)
    # Count dots per FILTER / SELECT / WITH_COLUMNS expression block
    chain_score = 0.0
    for ln in bare_plan.splitlines():
        if re.match(r"\s*(FILTER|SELECT|WITH_COLUMNS)\b", ln, re.IGNORECASE):
            dots = ln.count(".")
            chain_score += max(0, dots - 1) * w["expr_chain_hop"]
    if chain_score:
        bd["expression_chains"] = chain_score

    # 5. Unique column references
    cols = set(_RE_COL.findall(plan))
    if cols:
        bd["unique_columns"] = len(cols) * w["unique_col"]

    # 6. Aggregations
    agg_matches = _RE_AGG.findall(plan)
    if agg_matches:
        bd["aggregations"] = len(agg_matches) * w["aggregation"]

    # 7. Literal list values (e.g. is_in([["a","b","c"]]))
    lit_count = 0
    for m in _RE_LIST.finditer(plan):
        content = m.group(1)
        # only count if the bracket contains quoted strings or numbers
        values = re.findall(r'"[^"]*"|\b\d+\.?\d*\b', content)
        lit_count += len(values)
    if lit_count:
        bd["literal_values"] = lit_count * w["literal_value"]

    total = round(sum(bd.values()), 2)
    return ComplexityResult(total=total, breakdown=bd, explain_plan=plan, thresholds=t, caller=c)


# ── Comparison helper ──────────────────────────────────────────────────────────

def compare(
    *frames_or_plans: "pl.LazyFrame | str",
    labels: Optional[list[str]] = None,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
) -> list[ComplexityResult]:
    """
    Score and rank multiple LazyFrames or plan strings.

    Example
    -------
    results = compare(lf_simple, lf_complex, labels=["baseline", "filtered"])
    for r in results:
        print(r)
    """
    results = []
    for i, item in enumerate(frames_or_plans):
        if isinstance(item, pl.LazyFrame):
            r = score_complexity(item, weights=weights, thresholds=thresholds)
        else:
            r = score_plan_string(item, weights=weights, thresholds=thresholds)
        if labels:
            r.breakdown["__label__"] = labels[i]   # store for display only
        results.append(r)

    results.sort(key=lambda r: r.total)
    return results


# ── Context manager ───────────────────────────────────────────────────────────

import contextlib
import logging

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def complexity_collect(
    *,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, float]] = None,
    callback=None,
    log: bool = True,
    log_level: int = logging.INFO,
    threshold: Optional[float] = None,
    log_caller: bool = False,
):
    """
    Context manager that temporarily patches ``LazyFrame.collect`` so that
    every call first scores the query's authoring complexity, then proceeds
    with the real collect and returns its result unchanged.

    Parameters
    ----------
    weights   : Override default scoring weights (passed straight to scorer).
    thresholds: Override default thresholds (passed straight to scorer).
    callback  : Optional callable ``(result: ComplexityResult) -> None`` called
                for every collect.  Use it to accumulate results, push metrics,
                raise custom alerts, etc.  Defaults to None.
    log       : Whether to log each score via the module logger (default True).
    log_level : Logging level used when ``log=True`` (default logging.INFO).
    threshold : If set, raises ``ComplexityThresholdExceeded`` when
                ``result.total`` exceeds this value *before* executing collect.

    Example
    -------
    results = []

    with complexity_collect(callback=results.append, threshold=30):
        df1 = lf_simple.collect()   # scores, then collects normally
        df2 = lf_complex.collect()  # raises if total > 30

    for r in results:
        print(r)
    """
    _original_collect = pl.LazyFrame.collect

    def _instrumented_collect(self, *args, **kwargs):

        caller = inspect.stack()[1].function if log_caller else None

        result = score_complexity(self, weights=weights, thresholds=thresholds, caller=caller)

        if log:
            logger.log(
                log_level,
                "collect() complexity: %.1f [%s]\n%s%s",
                result.total,
                result.tier,
                result.explain_plan,
                f"\n(caller: {result.caller})" if log_caller else "",
            )

        if threshold is not None and result.total > threshold:
            raise ComplexityThresholdExceeded(result, threshold)

        if callback is not None:
            callback(result)

        return _original_collect(self, *args, **kwargs)

    pl.LazyFrame.collect = _instrumented_collect
    try:
        yield
    finally:
        pl.LazyFrame.collect = _original_collect


class ComplexityThresholdExceeded(Exception):
    """Raised by ``complexity_collect`` when a query exceeds the threshold."""

    def __init__(self, result: ComplexityResult, threshold: float):
        self.result = result
        self.threshold = threshold
        super().__init__(
            f"Query complexity {result.total:.1f} [{result.tier}] "
            f"exceeds threshold {threshold}.\n{result}"
        )


# ── JSONL file handler ────────────────────────────────────────────────────────

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class JSONLFileHandler:
    """
    Writes one JSON record per ``LazyFrame.collect()`` call to a .jsonl file
    (one JSON object per line — safe to append, easy to stream with jq or
    pandas.read_json(..., lines=True)).

    Intended as the ``callback`` argument of ``complexity_collect()``:

        handler = JSONLFileHandler("complexity.jsonl")
        with complexity_collect(callback=handler, log=False):
            app.run(...)

    Record format
    -------------
    {
        "timestamp": "2026-04-23 10:05:25.595 UTC",
        "complexity": 9.0,
        "tier":       "simple",
        "breakdown":  {"operations": 2.0, "unique_columns": 1.0, ...},
        "explain":    "SELECT [col(\\"_id\\").count()] ..."
    }

    Parameters
    ----------
    path      : File path.  Created (including parent dirs) if absent.
                Existing file is appended to, never overwritten.
    encoding  : File encoding (default utf-8).
    tz        : Timezone for timestamps.  Defaults to local time.
                Pass ``timezone.utc`` for UTC.
    extra     : Optional dict of static fields merged into every record
                (e.g. ``{"app": "my_dash_app", "env": "dev"}``).
    """

    def __init__(
        self,
        path: "str | Path",
        *,
        encoding: str = "utf-8",
        tz: "timezone | None" = None,
        extra: "dict | None" = None,
    ):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._encoding = encoding
        self._tz = tz
        self._extra = extra or {}
        self._lock = threading.Lock()   # safe for Dash's threaded request handling

    def __call__(self, result: ComplexityResult) -> None:
        record = {
            "timestamp": datetime.now(self._tz).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "complexity": result.total,
            "tier": result.tier,
            "breakdown": result.breakdown,
            "explain": result.explain_plan,
            **({"caller": result.caller} if result.caller else {}),
            **self._extra,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding=self._encoding) as fh:
                fh.write(line + "\n")

    # ── Convenience readers ──────────────────────────────────────────────────

    def read_all(self) -> "list[dict]":
        """Return all records as a list of dicts (empty list if file absent)."""
        if not self._path.exists():
            return []
        with self._path.open(encoding=self._encoding) as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def tail(self, n: int = 20) -> "list[dict]":
        """Return the last *n* records without loading the whole file."""
        if not self._path.exists():
            return []
        with self._path.open(encoding=self._encoding) as fh:
            lines = fh.readlines()
        return [json.loads(ln) for ln in lines[-n:] if ln.strip()]


if __name__ == "__main__":
    print("Run demo.py for a full walkthrough of all features.")
    print("Run score_tpch.py to validate the scorer against the official polars-benchmark TPC-H queries.")
