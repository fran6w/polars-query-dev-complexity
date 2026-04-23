# polars-query-dev-complexity

Measures the **authoring complexity** of [Polars](https://pola.rs) `LazyFrame` queries — i.e. how much effort a developer put into *writing* a query — by parsing the unoptimised explain plan produced by `LazyFrame.explain(optimized=False)`.

> **Authoring complexity ≠ execution complexity.**
> A query can be trivial to write yet expensive to run, or vice versa.
> This tool measures the former: the cognitive and editorial effort visible in the query plan.

---

## Motivation & concept

When reviewing data pipelines or tracking query drift over time, it is useful to have an objective, reproducible measure of how elaborate a query is at the authoring level. Git diffs and line counts are poor proxies — a single `.filter()` call can hide a deeply chained expression, while a multi-step pipeline may be straightforward.

`polars-query-dev-complexity` extracts the following signals from the unoptimised explain plan:

| Signal | Weight | What it captures |
|---|---|---|
| Operation count | ×1.0 | Each `SELECT`, `FILTER`, `SORT`, `JOIN`, `GROUP_BY`, … node |
| Filter depth | ×1.5 | Stacked `FILTER` nesting — each level beyond the first |
| Expression chains | ×0.5 | Dot-method hops per expression, e.g. `.dt.to_string().is_between()` |
| Unique columns | ×0.5 | Distinct `col("x")` references |
| Aggregations | ×1.5 | `.count()`, `.sum()`, `.mean()`, `.min()`, `.max()`, … |
| Literal list values | ×0.5 | Items inside `["a", "b", …]` predicates |
| Join bonus | ×5.0 | Flat bonus per `JOIN` node |

Scores map to a **tier**:

| Score | Tier |
|---|---|
| ≤ 5 | trivial |
| ≤ 12 | simple |
| ≤ 22 | moderate |
| ≤ 35 | complex |
| > 35 | very complex |

---

## Installation

No packaging yet — copy the files you need into your project:

```
polars_query_dev_complexity.py   ← scorer + context manager + JSONL handler
demo.py                          ← runnable walkthrough of all features
score_tpch.py                    ← validate against pola-rs/polars-benchmark
```

The scorer itself has **no dependencies beyond the Python standard library**.
`polars` is only imported at call time inside `score_complexity()` and `complexity_collect()`,
so `score_plan_string()` and `JSONLFileHandler` work without Polars installed.

```bash
# with uv
uv add polars

# with pip
pip install polars
```

---

## Usage

### One-shot scoring

```python
import polars as pl
from polars_query_dev_complexity import score_complexity, score_plan_string

# From a live LazyFrame
lf = (
    pl.scan_parquet("user.parquet")
    .filter(pl.col("role") == "MEDIA")
    .filter(pl.col("position").is_in(["officer"]))
    .filter(pl.col("createdAt").dt.year() >= 2026)
    .select(pl.col("_id").count())
)
result = score_complexity(lf)
print(result)
# Authoring complexity : 16.0  [moderate]
# ────────────────────────────────────────────
#   operations                   +5.00
#   filter_depth_penalty         +3.00
#   expression_chains            +1.00
#   unique_columns               +2.00
#   aggregations                 +1.50
#   literal_values               +3.50

# From a cached plan string
result = score_plan_string(lf.explain(optimized=False))
print(result.total, result.tier)
# 16.0  moderate
```

### Context manager — intercept every `collect()`

Temporarily patches `LazyFrame.collect()` for the duration of the block.
The original `collect()` is always restored, even if an exception is raised.

```python
from polars_query_dev_complexity import complexity_collect, ComplexityThresholdExceeded

# Accumulate results
captured = []
with complexity_collect(callback=captured.append, log=False):
    df1 = lf_simple.collect()   # scores silently, returns DataFrame normally
    df2 = lf_complex.collect()

for r in captured:
    print(r.total, r.tier)

# Hard gate — block execution above a threshold
try:
    with complexity_collect(threshold=20.0):
        df = lf_very_complex.collect()  # raises before collecting
except ComplexityThresholdExceeded as e:
    print(e.result.breakdown)
```

### JSONL logging — one record per `collect()`

```python
from pathlib import Path
from datetime import timezone
from polars_query_dev_complexity import complexity_collect, JSONLFileHandler

handler = JSONLFileHandler(
    Path("logs/complexity.jsonl"),
    tz=timezone.utc,
    extra={"app": "my_dash_app", "env": "dev"},
)

with complexity_collect(callback=handler, log=False):
    df = lf.collect()
```

Each line in `complexity.jsonl`:

```json
{
  "timestamp": "2026-04-23 10:05:25.595",
  "complexity": 16.0,
  "tier": "moderate",
  "breakdown": {"operations": 5.0, "filter_depth_penalty": 3.0, "...": "..."},
  "explain": "SELECT [col(\"_id\").count()]\n  FILTER ...",
  "app": "my_dash_app",
  "env": "dev"
}
```

Read back without pandas:

```python
records = handler.read_all()   # list[dict]
records = handler.tail(20)     # last 20 records

# or with polars
import polars as pl
df = pl.read_ndjson("logs/complexity.jsonl")
df.top_k(10, by="complexity")
```

### Integrating in a Plotly Dash app

Add at the **top of `app.py`**, before layout and callbacks, so the patch is active
for the lifetime of the worker process regardless of Dash's reloader:

```python
import os
from pathlib import Path
from datetime import timezone

PROD = os.getenv("ENV", "dev") != "prod"

if not PROD:
    from polars_query_dev_complexity import complexity_collect, JSONLFileHandler

    _handler = JSONLFileHandler(
        Path(__file__).parent / "logs" / "complexity.jsonl",
        tz=timezone.utc,
        extra={"app": "my_dash_app"},
    )
    _ctx = complexity_collect(callback=_handler, log=False)
    _ctx.__enter__()
```

> **Note on Dash's reloader:** with `debug=True`, Dash forks the process.
> Guard with `os.environ.get("WERKZEUG_RUN_MAIN") == "true"` if you want
> the patch to apply only in the worker process and not the watcher.

---

## TPC-H benchmark

`score_tpch.py` validates the scorer against the 22 real-world TPC-H queries
from [pola-rs/polars-benchmark](https://github.com/pola-rs/polars-benchmark),
covering a wide range of authoring complexity — from simple date filters (Q6)
to multi-join aggregations (Q8, Q21).

No data generation is required: `LazyFrame.explain(optimized=False)` operates
on the unexecuted plan. Parquet scans are mocked with empty but correctly-typed
`LazyFrame`s so each query module can be imported without any `.parquet` files.

```bash
uv pip install linetimer
git clone https://github.com/pola-rs/polars-benchmark.git
uv run score_tpch.py
```

Expected output shape:

```
q1        29.0  [complex]
q10       43.0  [very complex]
q11       43.0  [very complex]
q12       34.0  [complex]
q13       21.0  [moderate]
...
```

---

## License

MIT — see [LICENSE](LICENSE).
