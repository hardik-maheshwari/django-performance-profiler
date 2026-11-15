# Django Performance Profiler

Find out **why** a Django/DRF endpoint is slow, not just that it is. The profiler wraps a
block of code (or a whole request) and reports every SQL query and Elasticsearch call it made,
which line of *your* code triggered each one, and what to change. It covers N+1 queries,
prefetches that silently stopped working, and search responses that returned HTTP 200 with
results missing.

It is a development tool: run it locally or against a copy of production data, not in production
request paths.

## What it catches

| Tool | What it reports |
|---|---|
| **SQL spy** (`sql_spy`) | Every query grouped by structure, with count, time, DB alias and the first-party line that caused it. N+1s are labelled with the relation and serializer field that triggered them, plus a fix hint: `missing_select_related`, `never_prefetched`, `prefetched_but_busted` (a `.filter()`/`.first()` rebuilt a prefetched queryset), `deferred_reload` (a field excluded by `.only()`/`.defer()` was read). Separates real fan-out from the same object fetched repeatedly. Against a remote database it also reads Postgres's own execution time from `pg_stat_statements`. |
| **Planner misestimate detector** | `EXPLAIN ANALYZE` on hot queries flags plan nodes whose row estimates miss by 10× or more, finds the source node where the misestimate starts, and names the fix (`ANALYZE`, extended statistics). A drift gate catches plan changes. |
| **ES spy** (`es_spy`) | Every Elasticsearch call grouped by shape, with client time, server `took` and caller. Flags responses that are HTTP 200 but incomplete: shard failures, `timed_out`, bulk item errors, `update_by_query`/`delete_by_query` failures, `msearch` sub-request errors and `mget` doc errors. |
| **Call tracer** (`dynamic_life_saver`) | First-party calls in execution order with cumulative and self time, the self-time hogs, and functions called many times per request. When a spy runs alongside it, each function also shows its own SQL and ES time, so you can see whether a slow function is waiting on the database, on search, or on Python. |
| **Run capture + drift** (`trace_capture`) | Runs the tracer and spies together and writes the output to a `.txt`, a readable `.py` with each traced function's source, or a JSON fingerprint. Comparing a `master` fingerprint with a branch fingerprint shows any change in queries, response or logs after a refactor, even when the call graph moved. |
| **QuerySet build X-ray** (`build_xray`) | The SQL after every chained `.filter()`/`.annotate()`/... step, so you can see which step added a join or subquery. |
| **Static crawler + model X-ray** (`static_life_saver`) | Starts from a view function and follows the AST call graph through first-party code, writing one context file with each function's source, the models it touches and their schema. Types it cannot infer can be filled from what the tracer saw at runtime, or through an interactive wizard. `inspect_model` explains a model's relations, managers, signals and what a delete would cascade to. |

Spies follow work into `ThreadPoolExecutor` and `multiprocessing.pool.ThreadPool` workers
(which `elasticsearch.helpers.parallel_bulk` uses), so queries made on worker threads are still
counted and attributed.

## Quick start (demo)

The repo includes a small DRF shop app on Postgres 16 + Elasticsearch 8.19, wired to the profiler.

```bash
docker compose up --build        # migrates, seeds, serves on :8000
docker compose logs -f web       # reports appear here
```

Each endpoint triggers one problem:

| Endpoint | Shows |
|---|---|
| `/books/` | N+1 on a forward FK (`missing_select_related`) and on an M2M (`never_prefetched`) |
| `/books/busted-prefetch/` | Tags are prefetched, but the serializer filters them, so the prefetch is bypassed (`prefetched_but_busted`) |
| `/books/fixed/` | The same serializer with `select_related` + `prefetch_related`: 2 queries instead of 41 |
| `/books/traced/` | Call tracer + spies together: SQL time per function |
| `/search/` | One search per author in a loop |
| `/search/traced/` | Per-function ES vs SQL vs Python time |
| `/search/parallel/` | Searches on a thread pool, still attributed |
| `/search/partials/` | Three HTTP 200 responses hiding failures: a shard failure, an `msearch` sub-error, an `mget` doc error |
| `/reindex/` | A bulk request where one document fails while the response is HTTP 200 |
| `/reindex/parallel/` | `parallel_bulk` across `multiprocessing`'s ThreadPool, both chunks recorded |

### Sample output

`/books/` (trimmed):

```text
--- [books: N+1] Wall time: 0.4969s | SQL (summed): 0.0581s (12% of wall) ---
--- [books: N+1] Total Queries: 41 ---
--- [books: N+1] Unique Query Structures: 3 ---

[2] 🚨 N+1 DETECTED 🎯 shop/serializers.py:16 in get_author_name() | 🔄 Count: 20 | ⏱️ 0.0175s | 🗄️ db=default (local)
      ↳ missing_select_related on 'author' during BookSerializer.author_name | 5 distinct (recompute-dominated)
        fix: forward relation not joined — add select_related('<relation>').
      SELECT "author".*
  FROM "shop_author" AS "author"
  WHERE "author".id = 1
  LIMIT 21 /* Note: Parameters shown are from the FIRST execution */
[3] 🚨 N+1 DETECTED 🎯 shop/serializers.py:19 in get_tag_labels() | 🔄 Count: 20 | ⏱️ 0.0218s | 🗄️ db=default (local)
      ↳ never_prefetched on 'tags' during BookSerializer.tag_labels | 20 distinct (fan-out)
        fix: fan-out across rows — add prefetch_related('<relation>').
```

`/search/partials/` (trimmed):

```text
── [search: 200s that hide failures] Wall: 0.4482s | ES client (summed): 0.4370s | ES server (Σ took): 219ms ──
[1] 🔴 SHARD FAILURES=1 (PARTIAL RESULTS) 📍 shop/views.py:101 in search_partials() | ⚙️ search on 'books,books_kw' | 🖥️ server 203ms | HTTP 200 | hits=1
[2] 🔴 ITEM FAILURES=1 [index_not_found_exceptionx1] (DESPITE HTTP 200) 📍 shop/views.py:103 in search_partials() | ⚙️ msearch
[3] 🔴 ITEM FAILURES=1 [index_not_found_exceptionx1] (DESPITE HTTP 200) 📍 shop/views.py:106 in search_partials() | ⚙️ mget
```

`/books/traced/`, call tracer section (trimmed):

```text
BOTTLENECKS (by self-time):
  43.2ms self 24.3% | SQL 13.6ms x20 | BookSerializer.get_tag_labels x20 (repeat)  (shop/serializers.py) <- HOG
  35.9ms self 20.2% | SQL 10.5ms x20 | BookSerializer.get_author_name x20 (repeat)  (shop/serializers.py) <- HOG
```

## Using it in your project

There is no PyPI package yet. Put `django_perf_profiler/` on your `PYTHONPATH` (or copy it in)
and install `sqlparse`; `elasticsearch` is only needed for the ES spy. Reports go to the
`django_perf_profiler` logger, so give it a handler:

```python
LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"django_perf_profiler": {"handlers": ["console"], "level": "INFO"}},
}
```

Set `LIFESAVER_PROJECT_ROOT` to your project's root (defaults to the current directory). Code
under it counts as first-party; everything else (site-packages, migrations, the profiler itself)
is skipped when attributing queries.

**Wrap a block:**

```python
from django_perf_profiler.sql_spy.sql_spy import sql_spy
from django_perf_profiler.es_spy.es_spy import es_spy

with sql_spy("order list"):
    data = OrderSerializer(qs, many=True).data

with es_spy("product search"):
    results = es.search(index="products", query=q)
```

**Everything at once, saved to files:**

```python
from django_perf_profiler.trace_capture import capture, BasicStrategy, SourceStrategy

with capture("order list", [BasicStrategy("out/orders.txt"), SourceStrategy("out/orders.py")]):
    response = view(request)
```

`capture()` runs the tracer, the SQL spy and (if `elasticsearch` is installed) the ES spy.
For class-based views, `capture_view(...)` is the decorator form and also records the response.

**Trace any request on demand** (only when `DEBUG = True`):

```python
MIDDLEWARE = [..., "django_perf_profiler.dynamic_life_saver.tracer.DynamicLifeSaverMiddleware"]
```

Then add `?dls=1` (or header `X-Dynamic-Life-Saver: 1`) to a request. `?dls_harvest=1` also
records the runtime types of arguments for the static crawler.

**Drift check between branches:**

```python
from django_perf_profiler.trace_capture import capture, FingerprintStrategy

with capture("order list", [FingerprintStrategy("out/orders.master.json")]):
    view(request)
```

Run once on `master`, once on your branch, then:

```bash
python -m django_perf_profiler.trace_capture.drift out/orders.master.json out/orders.branch.json
```

Every difference is listed; deciding whether it is a feature or a bug is left to you.

**Static crawler** (run from the project root):

```bash
python -m django_perf_profiler.static_life_saver.main shop/views.py books_traced
# options: --include=<file>:<func>[:depth],...  --deep-dive=<name>:<depth>,...
#          --emit-blindspots=<file.json>  /  --ingest-blindspots=<file.json>
```

Output goes to `life_saver_llm.txt`. Schema stubs and model inspection run through
`LIFESAVER_SHELL_CMD`, which defaults to `python manage.py shell -c`. In the demo:

```bash
docker compose exec -w /app/demo \
  -e LIFESAVER_SHELL_CMD="python /app/demo/manage.py shell -c" \
  web python -m django_perf_profiler.static_life_saver.main shop/views.py books_traced
```

**Model X-ray** (in `manage.py shell`):

```python
from django_perf_profiler.static_life_saver.inspect_model import inspect_model
inspect_model(Book)
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LIFESAVER_PROJECT_ROOT` | current directory | What counts as first-party code |
| `LIFESAVER_BOUNDARY_LIBS` | none | Comma-separated libraries to show in traces anyway, e.g. `rest_framework` |
| `LIFESAVER_SHELL_CMD` | `python manage.py shell -c` | How the crawler runs Django-aware helper scripts |
| `LIFESAVER_HARVEST_FILE` | `.type_cache.harvested.json` at repo root | Where runtime type observations are stored |
| `LIFESAVER_TYPE_CACHE` | `.type_cache.json` next to the harvest file | Types confirmed by you through the wizard |
| `DLS_OUTPUT_FILE` | `dynamic_life_saver_log.txt` | Call tracer's full report file |
| `DLS_SUPPRESS_NAMES` | none | Comma-separated first-party function names to hide from traces |
| `PROFILER_LOCAL_DB_HOSTS` | `postgres,localhost,127.0.0.1` | Extra DB hosts to treat as local (no `pg_stat_statements` server timing) |

## How it works

- **One patch point per backend.** The SQL spy wraps Django's `CursorWrapper.execute`/`executemany`;
  the ES spy wraps `BaseClient.perform_request` in elasticsearch-py, which every API method goes through.
- **Scoped by `contextvars`,** so only code inside a `with` block is recorded, and concurrent
  requests don't leak into each other's reports. A small patch copies the context into thread-pool
  tasks so worker threads are covered.
- **ORM and DRF hooks** (`orm_xray`, `drf_xray`) record which relation access and which serializer
  field were active when a query fired. That is how an N+1 gets its relation, its kind and its fix.
- **The tracer and the spies don't import each other.** While the tracer runs it registers on
  `core.active_tracer`; the spies push each query's cost onto the tracer's current frame through it.
- **One rule for "first-party"** (`core.boundaries`), shared by the spies, the tracer and the crawler.

## Supported versions

- Python 3.12 (demo image)
- Django **5.2 LTS** and **6.x** (tested on 5.2 and 6.1). Also works on 4.2 (end of life
  April 2026, not tested in CI). Version differences are handled with feature detection.
- PostgreSQL for the server-side timing and EXPLAIN features; query capture works on any backend Django supports.
- elasticsearch-py **8.x** (pinned 8.19.3; 9.x untested).

## Limitations

- The tracer uses `sys.setprofile`, which inflates absolute Python times. Trust the SQL/ES
  shares and call counts more than raw milliseconds.
- `pg_stat_statements` numbers are aggregated per query structure across the whole instance, not
  per execution. Server timing runs only against hosts not listed as local, so it does not show in the demo.
- Async views and the async Elasticsearch client are not instrumented.
- The crawler cannot yet infer types through chains such as `.filter(...).first()`.
- No automated test suite or CI yet; checks have been run by hand against the demo stack.

## Roadmap

- CI matrix for Django 5.2 and 6.x.
- MySQL support via a compose override file.
