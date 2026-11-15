"""
es_spy - a `sql_spy` for Elasticsearch.

WHY
---

`sql_spy` is blind to Elasticsearch: ES ops are HTTP calls to the cluster, not DB
queries through Django's cursor. This mirrors `sql_spy`'s shape for ES:
    monkeypatch the ONE chokepoint  ->  contextvar-gate it  ->  aggregate by
    fingerprint  ->  attribute the caller  ->  report.

CHOKEPOINT (verified against the pinned elasticsearch-py source)
--------------------------------------------------------------------------------

`elasticsearch._sync.client._base.BaseClient.perform_request`
    (elasticsearch 8.19.3 - elasticsearch/_sync/client/_base.py:255)

Why THIS method and not the transport layer (read from that source, not assumed):
    * Called ONCE per logical op. Retries live BELOW it, inside
      transport.perform_request (max_retries passed down at _base.py:321).
      So our count == logical operations, never retry-inflated.
    * Namespaced clients (es.indices.*, es.cluster.*) delegate to the ROOT client's
      perform_request (_base.py:409->422), endpoint_id preserved - so this
      single patch catches them too, exactly once.

HONESTY GUARANTEES (a tool that lies is worse than no tool)
--------------------------------------------------------------------------------

    * Failures RAISE, they don't return (ApiError, _base.py:351). We record
      in a `finally` and read status/body off the exception, so a failed ES op is
      NEVER silently dropped.
    * A 200 can still carry failures; all are surfaced (see _partials):
      `_shards.failed > 0` and `timed_out: true` (partial results), bulk item
      errors, by_query failures, msearch sub-search errors, mget doc errors.
    * FUSION: under the dynamic tracer, each op's client wall time is charged
      to the calling function (core.active_tracer.note_es), so the tracer
      report splits SQL / ES / Python time per function.
    * Three timings kept DISTINCT, never conflated:
        - wall      : perf_counter around perform_request (client-side, incl retries
                      + (de)serialization)
        - transport : resp.meta.duration (the transport's own measure)
        - took (ms) : resp.body["took"] (pure server-side ES exec; search/bulk only)
    * HEAD responses carry no body (exists()); took/_shards extraction is guarded.
    * The patch is global but a strict no-op unless inside `with es_spy():` - zero
      overhead and zero behavior change when idle.

KNOWN BLIND SPOT (named, not hidden)
--------------------------------------------------------------------------------

Only the SYNC client is patched. The async client
(`elasticsearch._async.client._base.BaseClient`) is a separate class and is NOT
covered: calls made through AsyncElasticsearch are not instrumented.

USAGE
-----

    from django_perf_profiler.es_spy.es_spy import es_spy
    with es_spy("search endpoint"):
       Example.function()
"""

import contextvars
import inspect
import json
import logging
import threading
import time
from contextlib import contextmanager

from elasticsearch._sync.client._base import BaseClient

from ..core import active_tracer, context_propagation

logger = logging.getLogger(__name__)

_spy_state = contextvars.ContextVar("es_spy_state", default=None)


# ==============================================================================
# 1. THREAD-POOL PROPAGATION (shared with sql_spy, idempotent)
# ==============================================================================
# Apps often fan ES searches out across a ThreadPoolExecutor, and
# helpers.parallel_bulk uses multiprocessing's ThreadPool. A contextvar does NOT
# cross into either pool's workers by default, so without this the spy would MISS
# every concurrent ES call. One patch covers both spies (copy_context snapshots
# ALL contextvars) and both pool families.
context_propagation.install()


# ==============================================================================
# 2. OP / INDEX / FINGERPRINT helpers
# ==============================================================================
def _op_from_path(method, path):
    """Fallback op-kind when endpoint_id is absent - derive from method + path."""
    p = (path or "").rstrip("/")
    for marker, name in (
        ("/_search", "search"),
        ("/_msearch", "msearch"),
        ("/_bulk", "bulk"),
        ("/_update_by_query", "update_by_query"),
        ("/_delete_by_query", "delete_by_query"),
        ("/_count", "count"),
        ("/_mget", "mget"),
    ):
        if p.endswith(marker):
            return name
    if "/_doc" in p or "/_create" in p:
        return {"PUT": "index", "POST": "index", "DELETE": "delete", "GET": "get"}.get(
            method, method.lower()
        )
    return (method or "?").lower()


def _index_from_path(path):
    """First path segment, unless it's an `_endpoint` (e.g. /_bulk has no index in
    the path - it lives per-action in the body)."""
    segs = [s for s in (path or "").split("/") if s]
    if segs and not segs[0].startswith("_"):
        return segs[0]
    return None


def _norm(v):
    """Collapse a value to its STRUCTURE: keys preserved (field names & query
    clauses matter), scalar leaves -> '?', lists -> shape of their first element.
    So two searches differing only in the token/ids share one fingerprint, while
    a search on `name` vs `description` stay distinct."""
    if isinstance(v, dict):
        return {
            k: _norm(val) for k, val in sorted(v.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(v, (list, tuple)):
        return [_norm(v[0])] if v else []
    return "?"


def _fingerprint(op, index, body):
    try:
        if body is None:
            shape = ""
        elif isinstance(body, (bytes, str)):
            shape = "<ndjson>"  # bulk body; grouped by op+index only
        else:
            shape = json.dumps(_norm(body), sort_keys=True)[:4000]
    except Exception:
        shape = "<unfingerprintable>"
    return f"{op}|{index}|{shape}"


def _bulk_doc_count(body):
    """A bulk body is alternating action/source lines (NDJSON str, or a list).
    Action lines are the odd ones; doc count ~= len/2. Best-effort, display only."""
    try:
        if isinstance(body, (bytes, str)):
            text = body.decode() if isinstance(body, bytes) else body
            lines = [ln for ln in text.splitlines() if ln.strip()]
            return len(lines) // 2 or len(lines)
        if isinstance(body, (list, tuple)):
            return len(body) // 2 or len(body)
    except Exception:
        pass
    return None


# ==============================================================================
# 3. CALLER ATTRIBUTION  (same idea as sql_spy's stack walk)
# ==============================================================================
_SKIP = (
    "elasticsearch/",  # ES client lib (NOTE: does NOT match "elasticsearch_dsl/")
    "elasticsearch_dsl/",  # DSL internals - we want the APP frame that called .save()/.search()
    "elastic_transport/",
    "django/db",
    "django/core",
    "/contextlib",
    "concurrent/futures",
    "multiprocessing/pool",
    "/threading",  # pool-worker entry (run()); the real app caller is the fn submitted
    "django_perf_profiler/",  # the profiler's own frames (spies, core)
)

# App-level pass-through wrappers. Codebases often route every ES call through
# one shim (e.g. a decorator that rewrites the index name to an alias, then
# calls through). It's app code, so the path-skip above won't catch it, and it
# sits on nearly EVERY ES op - attributing to it would mask every real caller.
# Register such frames by (short_file, func), short_file = last two path parts.
_SKIP_WRAPPERS = set()


def add_skip_wrapper(short_file, func_name):
    """Skip a pass-through frame, e.g. add_skip_wrapper("search/client.py", "wrapped")."""
    _SKIP_WRAPPERS.add((short_file, func_name))


def _caller():
    # FUSION: if the dynamic tracer is live, its current frame IS the caller
    # (O(1), and it skips the inspect.stack walk) -- same as sql_spy.
    loc = active_tracer.current_location()
    if loc is not None:
        qualname, rel_file, lineno = loc
        return (rel_file, lineno, qualname, False)
    for fi in inspect.stack():
        fn = fi.filename.replace("\\", "/")
        if any(s in fn for s in _SKIP):
            continue
        parts = fn.split("/")
        short = "/".join(parts[-2:]) if len(parts) >= 2 else fn
        if (short, fi.function) in _SKIP_WRAPPERS:
            continue
        return (short, fi.lineno, fi.function, False)
    # Pool worker whose stack is all library frames (e.g. helpers.parallel_bulk):
    # fall back to the first-party line that submitted the work.
    site = context_propagation.submit_site()
    if site is not None:
        short, lineno, func = site
        return (short, lineno, func, True)
    return ("Unknown", 0, "Unknown", False)


def _partials(body):
    """Everything a 2xx response can hide. None of these raise, so without this
    the spy would show a green 200 over lost writes or incomplete results.

    Returns {"items": n, "item_types": {error_type: n}, "shard_fail": n,
             "timed_out": n}:

      items      per-item / per-request errors:
                   bulk            `errors: true` + an `error` on each failed item
                   by_query        per-doc `failures`
                   msearch         a sub-search's own `error` in `responses[]`
                   mget            a doc's own `error` in `docs[]`
      shard_fail `_shards.failed`: top level, plus each msearch sub-response
      timed_out  `timed_out: true` (search hit its `timeout`; hits are partial),
                 top level plus each msearch sub-response
    """
    out = {"items": 0, "item_types": {}, "shard_fail": 0, "timed_out": 0}

    def _add(err):
        out["items"] += 1
        t = err.get("type", "error") if isinstance(err, dict) else "error"
        out["item_types"][t] = out["item_types"].get(t, 0) + 1

    def _search_level(b):
        shards = b.get("_shards")
        if isinstance(shards, dict) and shards.get("failed"):
            out["shard_fail"] += shards["failed"]
        if b.get("timed_out") is True:
            out["timed_out"] += 1

    _search_level(body)

    if body.get("errors") and isinstance(body.get("items"), list):
        for item in body["items"]:
            # each item is {"<action>": {..., "status": 4xx, "error": {...}}}
            if not isinstance(item, dict):
                continue
            for result in item.values():
                if isinstance(result, dict) and result.get("error"):
                    _add(result["error"])

    failures = body.get("failures")
    if isinstance(failures, list):
        for f in failures:
            _add(f.get("cause") if isinstance(f, dict) else None)

    for key in ("responses", "docs"):  # msearch / mget
        subs = body.get(key)
        if not isinstance(subs, list):
            continue
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            if sub.get("error"):
                _add(sub["error"])
            elif key == "responses":
                _search_level(sub)

    return out


# ==============================================================================
# 4. RECORD  (never raises; never triggers an ES call)
# ==============================================================================
def _record_op(state, method, path, endpoint_id, body, resp, exc, wall):
    op = endpoint_id or _op_from_path(method, path)
    index = _index_from_path(path)

    status = took = transport_dur = hits_total = None
    error = None
    partial = {"items": 0, "item_types": {}, "shard_fail": 0, "timed_out": 0}

    if exc is not None:
        error = type(exc).__name__
        meta = getattr(exc, "meta", None)
        status = getattr(meta, "status", None)
        b = getattr(exc, "body", None)
        if isinstance(b, dict):
            took = b.get("took")
            shards = b.get("_shards")
            if isinstance(shards, dict) and shards.get("failed"):
                partial["shard_fail"] = shards["failed"]
    elif resp is not None:
        meta = getattr(resp, "meta", None)
        status = getattr(meta, "status", None)
        transport_dur = getattr(meta, "duration", None)
        b = getattr(resp, "body", None)  # HeadApiResponse has none -> guard below
        if isinstance(b, dict):
            took = b.get("took")
            partial = _partials(b)
            h = b.get("hits")
            if isinstance(h, dict):
                t = h.get("total")
                hits_total = t.get("value") if isinstance(t, dict) else t

    shard_fail = partial["shard_fail"]
    item_fail, item_types = partial["items"], partial["item_types"]
    timed_out = partial["timed_out"]
    fp = _fingerprint(op, index, body)
    loc = _caller()

    with state["lock"]:
        state["total_ops"] += 1
        state["total_wall"] += wall
        if took is not None:  # 0 is a real answer (sub-ms op), not "missing"
            state["total_took_ms"] += took

        q = state["ops"].get(fp)
        if q is None:
            q = state["ops"][fp] = {
                "op": op,
                "index": index,
                "count": 0,
                "wall": 0.0,
                "took_ms": 0.0,
                "took_seen": False,
                "transport_s": 0.0,
                "status": status,
                "hits": hits_total,
                "shard_fail": 0,
                "item_fail": 0,
                "item_errors": {},
                "timed_out": 0,
                "errors": {},
                "file": loc[0],
                "line": loc[1],
                "func": loc[2],
                "via_pool": loc[3],
                "body": body,
                "ndocs": _bulk_doc_count(body) if op == "bulk" else None,
            }
        q["count"] += 1
        q["wall"] += wall
        if took is not None:
            q["took_ms"] += took
            q["took_seen"] = True
        if transport_dur:
            q["transport_s"] += transport_dur
        if hits_total is not None:
            q["hits"] = hits_total
        if shard_fail:
            q["shard_fail"] += shard_fail
        if item_fail:
            q["item_fail"] += item_fail
            for t, n in item_types.items():
                q["item_errors"][t] = q["item_errors"].get(t, 0) + n
        if timed_out:
            q["timed_out"] += timed_out
        if error:
            q["errors"][error] = q["errors"].get(error, 0) + 1


# ==============================================================================
# 5. THE CHOKEPOINT PATCH  (gated; perfect passthrough when idle)
# ==============================================================================
_perform_orig = BaseClient.perform_request


def _patched_perform(self, method, path, *args, **kwargs):
    state = _spy_state.get()
    if state is None:
        return _perform_orig(self, method, path, *args, **kwargs)

    endpoint_id = kwargs.get("endpoint_id")
    body = kwargs.get("body")
    start = time.perf_counter()
    resp = None
    exc = None
    try:
        resp = _perform_orig(self, method, path, *args, **kwargs)
        return resp
    except Exception as e:  # ApiError on non-2xx (_base.py:351)
        exc = e
        raise
    finally:
        wall = time.perf_counter() - start
        # FUSION: charge the op's client wall time to the tracer's current frame
        # (that's the time the function spent waiting); no-op without a tracer.
        active_tracer.note_es(wall)
        try:
            _record_op(state, method, path, endpoint_id, body, resp, exc, wall)
        except Exception as rec_err:  # the instrument must never break the request
            logger.warning("es_spy: record failed (%s)", rec_err)


BaseClient.perform_request = _patched_perform


# ==============================================================================
# 6. REPORT
# ==============================================================================
def _pretty_body(body, max_list=4):
    """Readable query DSL: collapse long arrays to `[a, b, …+N]`, then indent."""
    if body is None:
        return "    (no body)"
    if isinstance(body, (bytes, str)):
        n = _bulk_doc_count(body)
        return f"    <ndjson body ~{n} docs>" if n else "    <raw body>"

    def collapse(v):
        if isinstance(v, bytes):  # helpers.*bulk pre-serialize each NDJSON line
            try:
                v = json.loads(v)
            except Exception:
                return v.decode("utf-8", "replace")
        if isinstance(v, dict):
            return {k: collapse(val) for k, val in v.items()}
        if isinstance(v, list):
            if len(v) > max_list:
                return [collapse(x) for x in v[:max_list]] + [f"…+{len(v) - max_list}"]
            return [collapse(x) for x in v]
        return v

    try:
        text = json.dumps(collapse(body), indent=2, default=str)
    except Exception:
        text = str(body)[:2000]
    return "\n".join("    " + ln for ln in text.splitlines())


def _report(label, state, wall):
    ops = list(state["ops"].values())
    total_ops = state["total_ops"]
    total_wall = state["total_wall"]
    total_took = state["total_took_ms"]

    overlap = ""
    if wall > 0 and total_wall > wall:
        overlap = " - client times overlapped (concurrent)"

    logger.info(
        f"\n── [{label}] Wall: {wall:.4f}s | ES client (summed): {total_wall:.4f}s{overlap} "
        f"| ES server (Σ took): {total_took:.0f}ms ──"
    )
    logger.info(
        f"── [{label}] Total ES ops: {total_ops} | Unique op shapes: {len(ops)} ──\n"
    )

    # Server time is the honest cost signal; fall back to client wall when a
    # write has no `took`.
    ops.sort(key=lambda q: (q["took_ms"], q["wall"]), reverse=True)

    for i, q in enumerate(ops):
        flags = ""
        if q["count"] > 2:
            flags += f"🔁 REPEATED x{q['count']} "  # same op shape many times - fan-out / N+1
        if q["shard_fail"]:
            flags += f"🔴 SHARD FAILURES={q['shard_fail']} (PARTIAL RESULTS) "
        if q["timed_out"]:
            flags += f"🔴 TIMED OUT x{q['timed_out']} (PARTIAL RESULTS) "
        if q["item_fail"]:
            kinds = ", ".join(f"{k}x{v}" for k, v in q["item_errors"].items())
            flags += f"🔴 ITEM FAILURES={q['item_fail']} [{kinds}] (DESPITE HTTP {q['status']}) "
        if q["errors"]:
            errs = ", ".join(f"{k}x{v}" for k, v in q["errors"].items())
            flags += f"🔴 ERRORS[{errs}] "

        idx = q["index"] or "-"
        hits = f" | hits={q['hits']}" if q["hits"] is not None else ""
        docs = f" | ~{q['ndocs']} docs" if q.get("ndocs") else ""
        took = f"{q['took_ms']:.0f}ms" if q["took_seen"] else "n/a"

        logger.info(
            f"[{i + 1}] {flags}📍 {q['file']}:{q['line']} in {q['func']}(){' via thread pool' if q['via_pool'] else ''} "
            f"| ⚙️ {q['op']} on '{idx}' | 🔢 {q['count']} | "
            f"🖥️ server {took} | 💻 client {q['wall']:.4f}s | HTTP {q['status']}{hits}{docs}"
        )
        logger.info(_pretty_body(q["body"]))

    logger.info("────────────────────────────────────────────────────────────\n")


# ==============================================================================
# 7. CONTEXT MANAGER  (the entry point - mirrors sql_spy)
# ==============================================================================
@contextmanager
def es_spy(label="ES Spy"):
    state = {
        "ops": {},
        "total_ops": 0,
        "total_wall": 0.0,
        "total_took_ms": 0.0,
        "lock": threading.Lock(),
    }
    token = _spy_state.set(state)
    wall_start = time.perf_counter()
    try:
        yield
    finally:
        wall = time.perf_counter() - wall_start
        _spy_state.reset(token)
        try:
            _report(label, state, wall)
        except Exception as e:
            logger.warning("es_spy: report failed (%s)", e)
