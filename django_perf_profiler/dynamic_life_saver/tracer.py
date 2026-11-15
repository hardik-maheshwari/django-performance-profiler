"""
dynamic_life_saver.tracer
-------------------------
Runtime call profiler. Hooks sys.setprofile for one request (middleware), one
block (context manager), or one call (decorator). Logs first-party calls in
execution order, times each (cumulative + self), flags the self-time bottleneck,
and aggregates by function so repeat calls (one helper hit 4x per request) surface.

First-party / boundary classification is delegated to core.boundaries.

FUSION: while a tracer runs it registers on core.active_tracer. If the SQL spy
or the ES spy is also active, it pushes each query's / op's cost onto the
tracer's current frame via note_sql() / note_es(), so the report shows
per-function SQL / ES / Python time. SQL milliseconds come from the spy's tight
per-query timing; ES milliseconds are the client wall time, which includes the
ES client library's own Python (setprofile inflates that part too).
Cumulative/self ms are setprofile-inflated, so the *fractions* are the honest
signal.

HARVEST (opt-in): with harvest_types=True the tracer also records the runtime
types of args flowing into LOCAL functions (dynamic_life_saver.harvest), keyed
in static_life_saver's cache format; stop() merges them into the harvest sidecar.

CAVEAT: single-thread only. setprofile is per-thread; the spy follows threadpool
workers via copy_context, so a query fired from a worker thread (which shares the
tracer via the copied contextvar but not its stack) is routed to an off-thread
bucket rather than misattributed to whatever frame the owner thread is in.
"""

import os
import re
import sys
import time
import logging
import functools
import threading
from dataclasses import dataclass

from ..core import boundaries
from ..core import active_tracer
from .harvest import TypeHarvester

# Django ORM bases for the manager-plumbing show-once gate (see _is_orm_manager).
# Guarded so importing the tracer never hard-depends on Django being importable
# at module-load time.
try:
    from django.db.models import Manager as _Manager, QuerySet as _QuerySet

    _ORM_BASES = (_Manager, _QuerySet)
except Exception:
    _ORM_BASES = ()

logger = logging.getLogger("django_perf_profiler.dynamic_life_saver")

BOTTLENECK_SELF_RATIO = 0.15  # self-time >= this fraction of traced wall -> HOG
REPEAT_CALL_THRESHOLD = 3  # called >= this many times -> repeat flag
LOG_FULL_TRACE = True  # also emit the full ordered trace to the logger
OUTPUT_FILE = os.environ.get("DLS_OUTPUT_FILE", "dynamic_life_saver_log.txt")

# Event kind for "another tracer ran here": the inner tracer takes over
# setprofile, so the outer one is blind for that span. The marker keeps the
# outer trace's order honest and points at the inner run's own report.
NESTED = "NESTED"


SUPPRESS_NAMES = {
    # Django DB-router hooks: fired by the ORM on every read, always trivial, pure noise.
    "db_for_read",
    "db_for_write",
    "allow_relation",
    "allow_migrate",
}

# SUPPRESS_NAMES = {}

# Project-specific noise: first-party utilities that fire on nearly every ORM
# call (a tenant/scope resolver, a permission helper) and add nothing the spy
# doesn't already show. Comma-separated function names, e.g.
#   DLS_SUPPRESS_NAMES=current_tenant,has_perm
# (mirrors the DLS_OUTPUT_FILE env idiom below).
SUPPRESS_NAMES = SUPPRESS_NAMES | {
    n.strip() for n in os.environ.get("DLS_SUPPRESS_NAMES", "").split(",") if n.strip()
}

SUPPRESS_QUALNAMES = {
    # Framework-invoked first-party plumbing the static model X-ray already documents.
    # NOTE: Django's own related managers (RelatedManager / the m2m manager) are
    # already 3rd-party (their get_queryset lives in site-packages -> EXTERNAL) and
    # never reach the qualname stage, so this entry is a belt-and-suspenders no-op;
    # the boundary is what enforces "3rd-party = ignored, be explicit to see them".
    "RelatedManager.get_queryset",
}

# SUPPRESS_QUALNAMES = {}


# Per-call labels: qualname -> fn(frame locals at call time) -> short text shown on
# that call's line. Answers "what did this call act on" without app changes, e.g.
# WHICH task an enqueue sent (otherwise every enqueue line reads the same).
#
# Hook the first-party wrapper your code already funnels external I/O through
# (a task enqueuer, a storage client, a search helper): that tags an existing
# line instead of turning on boundary libs, which add a frame per library call.
# Label with identifiers only (task names, keys, topics, index names), never
# payloads, message bodies or email addresses -- traces get pasted around.
# A failing labeller yields "".
#
#   from django_perf_profiler.dynamic_life_saver.tracer import register_annotation
#   register_annotation(
#       "TaskQueue.enqueue",
#       lambda loc: "task=%s" % loc["task_name"],
#   )
ANNOTATE = {}


def register_annotation(qualname, labeller):
    """Attach a labeller to every traced call of `qualname` (e.g. 'Cls.method')."""
    ANNOTATE[qualname] = labeller


def _cap(value, n=80):
    """Truncate a label value; handy inside labellers."""
    text = str(value)
    return text if len(text) <= n else text[:n] + "..."

# First-party manager plumbing shown ONCE per manager, then collapsed. get_queryset()
# on a custom Manager/QuerySet fires on every ORM
# access and only echoes what the spy's per-query output already shows -- but the
# first occurrence is a useful call-order breadcrumb, so we keep it and hide the rest
# (with a +N-hidden tag). isinstance-gated so a same-named View.get_queryset (real
# business logic) is never collapsed.
_ORM_PLUMBING_METHODS = {"get_queryset"}


def _is_orm_manager(frame):
    """True iff this frame's receiver is a Django Manager/QuerySet -- i.e. the call
    is ORM plumbing, not a View.get_queryset that merely shares the method name."""
    if not _ORM_BASES:
        return False
    try:
        return isinstance(frame.f_locals.get("self"), _ORM_BASES)
    except Exception:
        return False


def _rel(filename):
    # Path-relative-to-root display. Moves to core.frames in the next extraction.
    try:
        return os.path.relpath(filename, boundaries.PROJECT_ROOT)
    except ValueError:
        return filename


@dataclass
class _Call:
    idx: int
    depth: int
    qualname: str
    rel_file: str
    lineno: int
    kind: str
    frame_id: int
    t_start: float
    orm_plumbing: bool = False  # the shown-once occurrence of manager get_queryset
    note: str = ""  # ANNOTATE label, e.g. the task an enqueue sent
    elapsed: float = 0.0
    child_time: float = 0.0  # cumulative time of tracked DIRECT children
    sql_self: float = 0.0  # SQL fired while THIS frame was innermost
    sql_child: float = 0.0  # cumulative SQL of tracked DIRECT children
    sql_count: int = 0  # queries fired while THIS frame was innermost
    sql_count_child: int = 0  # cumulative query count of tracked DIRECT children
    es_self: float = 0.0  # ES client time while THIS frame was innermost
    es_child: float = 0.0  # cumulative ES time of tracked DIRECT children
    es_count: int = 0  # ES ops fired while THIS frame was innermost
    es_count_child: int = 0  # cumulative ES op count of tracked DIRECT children

    @property
    def self_time(self):
        return max(self.elapsed - self.child_time, 0.0)

    @property
    def sql_cummulative(self):  # SQL time anywhere in this frame's subtree
        return self.sql_self + self.sql_child

    @property
    def sql_count_cummulative(self):  # query count anywhere in this frame's subtree
        return self.sql_count + self.sql_count_child

    @property
    def es_cummulative(self):  # ES time anywhere in this frame's subtree
        return self.es_self + self.es_child

    @property
    def es_count_cummulative(self):  # ES op count anywhere in this frame's subtree
        return self.es_count + self.es_count_child


class CallTracer:
    def __init__(self, label="", harvest_types=False):
        self.label = label
        self.events = []  # ordered _Call records (true execution order)
        self._stack = []  # active tracked _Call records
        self._counter = 0
        self._t0 = 0.0
        self._t1 = 0.0
        self._prev = None
        self._token = None  # core.active_tracer registration token
        self._nested_in = (
            None  # (outer tracer, marker _Call) when started under another
        )
        self._owner_thread = None
        self._orphan_sql = 0.0  # SQL with no owning frame (worker threads / pre-frame)
        self._orphan_count = 0
        self._orphan_es = 0.0  # ES ops with no owning frame
        self._orphan_es_count = 0
        self._orphan_lock = threading.Lock()
        # Manager plumbing show-once: qualname -> count of collapsed repeats. The
        # first get_queryset() per manager is recorded; the rest bump the count.
        self._mgr_seen = set()
        self._mgr_hidden = {}
        # HARVEST: opt-in runtime type observation, flushed to the sidecar at stop().
        self._harvester = TypeHarvester() if harvest_types else None

    # ---- lifecycle ----
    def start(self):
        self._t0 = time.perf_counter()
        self._owner_thread = threading.get_ident()
        outer = active_tracer.get()
        self._nested_in = (
            (outer, outer._open_nested(self.label))
            if isinstance(outer, CallTracer)
            else None
        )
        self._token = active_tracer.set_active(self)
        self._prev = sys.getprofile()
        sys.setprofile(self._profile)

    def stop(self):
        sys.setprofile(self._prev)
        if self._token is not None:
            active_tracer.clear(self._token)
            self._token = None
        self._t1 = time.perf_counter()
        now = self._t1
        if self._nested_in and self._nested_in[1] is not None:
            outer, rec = self._nested_in
            rec.elapsed = now - rec.t_start
            if outer._stack:  # count it as the caller's child time, not self time
                outer._stack[-1].child_time += rec.elapsed
        while self._stack:  # defensive: close anything still open
            rec = self._stack.pop()
            if not rec.elapsed:
                rec.elapsed = now - rec.t_start
        # HARVEST: persist this run's observations (merge-on-flush; never raises).
        if self._harvester is not None:
            self._harvester.flush()

    def _open_nested(self, label):
        """An inner tracer is starting under us: drop a marker at the current
        position (not pushed on the stack -- it gets no return event)."""
        if self._token is None or threading.get_ident() != self._owner_thread:
            return None
        self._counter += 1
        rec = _Call(
            idx=self._counter,
            depth=len(self._stack),
            qualname="nested trace: %s" % label,
            rel_file="",
            lineno=0,
            kind=NESTED,
            frame_id=0,
            t_start=time.perf_counter(),
        )
        self.events.append(rec)
        return rec

    # ---- seam API (called by the spy via core.active_tracer) ----
    def current_location(self):
        if threading.get_ident() != self._owner_thread:
            return None  # worker thread: not our stack
        if not self._stack:
            return None
        top = self._stack[-1]
        return (top.qualname, top.rel_file, top.lineno)

    def note_sql(self, duration, count=1):
        if threading.get_ident() == self._owner_thread and self._stack:
            top = self._stack[-1]
            top.sql_self += duration
            top.sql_count += count
            return
        # worker thread, or query with no first-party frame -> off-thread bucket
        with self._orphan_lock:
            self._orphan_sql += duration
            self._orphan_count += count

    def note_es(self, duration, count=1):
        if threading.get_ident() == self._owner_thread and self._stack:
            top = self._stack[-1]
            top.es_self += duration
            top.es_count += count
            return
        with self._orphan_lock:
            self._orphan_es += duration
            self._orphan_es_count += count

    # ---- profiler hook (Python call/return only; c_* and exceptions ignored) ----
    def _profile(self, frame, event, arg):
        if event == "call":
            self._on_call(frame)
        elif event == "return":
            self._on_return(frame)

    def _on_call(self, frame):
        code = frame.f_code
        if code.co_name in SUPPRESS_NAMES:  # framework plumbing -> skip, like EXTERNAL
            return
        kind = boundaries.classify(code.co_filename)
        if kind == boundaries.EXTERNAL:  # core already excludes the toolkit itself
            return
        # HARVEST: first-party frame with live args -> observe runtime types.
        # One dict lookup per LOCAL call once the per-code sample cap is hit.
        if kind == boundaries.LOCAL and self._harvester is not None:
            self._harvester.observe(frame, code)
        if (
            kind == boundaries.BOUNDARY
            and self._stack
            and self._stack[-1].kind == boundaries.BOUNDARY
        ):
            return  # already inside a surfaced lib -> collapse internals

        if (
            kind == boundaries.BOUNDARY
            and code.co_name.startswith("__")
            and code.co_name.endswith("__")
        ):
            return  # boundary-lib dunder (Pipeline.__del__) -> GC-timing noise
        qualname = self._qualname(frame, code)
        if (
            qualname in SUPPRESS_QUALNAMES
        ):  # framework plumbing the static X-ray already covers
            return
        # Show-once for first-party manager get_queryset: record the first, collapse
        # (and count) the rest. View.get_queryset is excluded by the isinstance gate.
        is_plumbing = code.co_name in _ORM_PLUMBING_METHODS and _is_orm_manager(frame)
        if is_plumbing:
            if qualname in self._mgr_seen:
                self._mgr_hidden[qualname] = self._mgr_hidden.get(qualname, 0) + 1
                return
            self._mgr_seen.add(qualname)
        note = ""
        labeller = ANNOTATE.get(qualname)
        if labeller is not None:
            try:
                note = labeller(frame.f_locals)
            except Exception:
                pass
        self._counter += 1
        rec = _Call(
            idx=self._counter,
            depth=len(self._stack),
            qualname=qualname,
            rel_file=_rel(code.co_filename),
            lineno=code.co_firstlineno,
            kind=kind,
            frame_id=id(frame),
            t_start=time.perf_counter(),
            orm_plumbing=is_plumbing,
            note=note,
        )
        self.events.append(rec)
        self._stack.append(rec)

    def _on_return(self, frame):
        if not self._stack:
            return
        top = self._stack[-1]
        if top.frame_id != id(frame):  # return of an untracked frame -> ignore
            return
        top.elapsed = time.perf_counter() - top.t_start
        self._stack.pop()
        if self._stack:
            self._stack[-1].child_time += top.elapsed
            self._stack[-1].sql_child += top.sql_cummulative
            self._stack[-1].sql_count_child += top.sql_count_cummulative
            self._stack[-1].es_child += top.es_cummulative
            self._stack[-1].es_count_child += top.es_count_cummulative

    @staticmethod
    def _qualname(frame, code):
        name = code.co_name
        try:
            loc = frame.f_locals
            if "self" in loc:
                mro = type(loc["self"]).__mro__
            elif isinstance(loc.get("cls"), type):
                mro = loc["cls"].__mro__
            else:
                return name
            for klass in mro:
                fn = klass.__dict__.get(name)
                if fn is None:
                    continue
                fn = getattr(fn, "__func__", fn)  # unwrap classmethod / staticmethod
                if getattr(fn, "__code__", None) is code:
                    return klass.__name__ + "." + name
            return (
                mro[0].__name__ + "." + name
            )  # nothing matched -> runtime type (old behavior)
        except Exception:
            return name

    # ---- reporting ----
    def report(self):
        wall = (self._t1 - self._t0) or 1e-9
        # qualname -> [count, self, cummulative, rel, sql_self, sql_cummulative, sql_count,
        #              es_cummulative, es_count]
        agg = {}
        for e in self.events:
            if (
                e.kind == NESTED
            ):  # a marker, not a function -- its time is in its own report
                continue
            a = agg.setdefault(e.qualname, [0, 0.0, 0.0, e.rel_file, 0.0, 0.0, 0, 0.0, 0])
            a[0] += 1
            a[1] += e.self_time
            a[2] += e.elapsed
            a[4] += e.sql_self
            a[5] += e.sql_cummulative
            a[6] += e.sql_count_cummulative
            a[7] += e.es_cummulative
            a[8] += e.es_count_cummulative
        ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)

        out = [
            "",
            "=" * 72,
            "[DynamicLifeSaver: %s]" % self.label,
            "   traced wall %.1fms | %d tracked calls | %d unique functions"
            % (wall * 1000, len(self.events), len(agg)),
            "   (setprofile inflates absolute ms, ES client time included; SQL ms are tight"
            " -- trust the SQL/ES shares & counts)",
            "=" * 72,
            "",
            "BOTTLENECKS (by self-time):",
        ]

        any_hog = False
        for qn, (cnt, slf, cummulative, rf, sqs, sqc, sqn, esc, esn) in ranked[:6]:
            hog = " <- HOG" if slf >= BOTTLENECK_SELF_RATIO * wall else ""
            any_hog = any_hog or bool(hog)
            rep = (
                (" x%d (repeat)" % cnt)
                if cnt >= REPEAT_CALL_THRESHOLD
                else (" x%d" % cnt if cnt > 1 else "")
            )
            sql_pct = (sqc / cummulative * 100) if (sqn and cummulative > 0) else 0.0
            es_pct = (esc / cummulative * 100) if (esn and cummulative > 0) else 0.0
            sql_seg = (
                " | SQL %6.1fms x%d (%.0f%% cummulative)" % (sqc * 1000, sqn, sql_pct)
                if sqn
                else ""
            )
            if esn:
                sql_seg += " | ES %6.1fms x%d (%.0f%% cummulative)" % (esc * 1000, esn, es_pct)
            if sqn or esn:
                if sql_pct >= 50:
                    verdict = " [SQL-bound]"
                elif es_pct >= 50:
                    verdict = " [ES-bound]"
                elif sql_pct + es_pct >= 50:
                    verdict = " [IO-bound]"
                else:
                    verdict = " [mixed]"
            else:
                verdict = " [CPU]" if hog else ""
            out.append(
                "   %8.1fms self %5.1f%% | %8.1fms cummulative%s%s | %s%s  (%s)%s"
                % (
                    slf * 1000,
                    slf / wall * 100,
                    cummulative * 1000,
                    sql_seg,
                    verdict,
                    qn,
                    rep,
                    rf,
                    hog,
                )
            )
        if not any_hog:
            out.append(
                "   (no single function dominates -- cost is spread or in untracked IO)"
            )

        if self._orphan_count:
            out.append("")
            out.append(
                "   off-thread / no-frame SQL: %.1fms over %d queries "
                "(worker threads, or queries with no first-party frame)"
                % (self._orphan_sql * 1000, self._orphan_count)
            )
        if self._orphan_es_count:
            out.append(
                "   off-thread / no-frame ES: %.1fms over %d ops "
                "(worker threads, or ops with no first-party frame)"
                % (self._orphan_es * 1000, self._orphan_es_count)
            )

        repeats = sorted(
            [kv for kv in agg.items() if kv[1][0] >= REPEAT_CALL_THRESHOLD],
            key=lambda kv: kv[1][0],
            reverse=True,
        )
        if repeats:
            out.append("")
            out.append(
                "REPEAT CALLS (>= %dx -- same function, many calls):"
                % REPEAT_CALL_THRESHOLD
            )
            for qn, (cnt, slf, cummulative, rf, sqs, sqc, sqn, esc, esn) in repeats[:12]:
                sqlbit = " | SQL %.1fms x%d" % (sqc * 1000, sqn) if sqn else ""
                if esn:
                    sqlbit += " | ES %.1fms x%d" % (esc * 1000, esn)
                out.append(
                    "   x%-3d | %8.1fms cummulative total%s | %s  (%s)"
                    % (cnt, cummulative * 1000, sqlbit, qn, rf)
                )

        # HARVEST: surface run-local type observations for human verification.
        if self._harvester is not None:
            out.extend(self._harvester.report_lines())

        ordered = ["", "ORDERED CALL TRACE (depth-indented, execution order):"]
        for e in self.events:
            sql_seg = (" sql=%.2fms" % (e.sql_self * 1000)) if e.sql_count else ""
            if e.es_count:
                sql_seg += " es=%.2fms" % (e.es_self * 1000)
            plumb_seg = ""
            if e.orm_plumbing:
                hidden = self._mgr_hidden.get(e.qualname, 0)
                plumb_seg = (
                    "  [manager plumbing: shown once, +%d hidden]" % hidden
                    if hidden
                    else "  [manager plumbing: shown once]"
                )
            ordered.append(
                "   [%5d] %s%s %s %s  self=%.2f cummulative=%.2fms%s%s%s"
                % (
                    e.idx,
                    "  " * e.depth,
                    _mark(e),
                    e.qualname,
                    _where(e),
                    e.self_time * 1000,
                    e.elapsed * 1000,
                    sql_seg,
                    plumb_seg,
                    "  [%s]" % e.note if e.note else "",
                )
            )

        collapsed = [
            "",
            "COLLAPSED CALL TRACE (repeats folded to xN, alternating loop bodies"
            " under '~ loop'; [idx] = first call, see full trace below):",
        ]
        _render_collapsed(_blocks(_build_tree(self.events)), 0, collapsed)

        summary_text = "\n".join(out) + "\n" + "\n".join(collapsed)
        ordered_text = "\n".join(ordered)
        logger.info(summary_text + (("\n" + ordered_text) if LOG_FULL_TRACE else ""))

        try:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                f.write(summary_text + "\n" + ordered_text + "\n")
            if not LOG_FULL_TRACE:
                logger.info("full ordered trace -> %s" % OUTPUT_FILE)
        except Exception as exc:
            logger.warning(
                "DynamicLifeSaver: could not write %s: %s" % (OUTPUT_FILE, exc)
            )

        return summary_text


# ---------------------------------------------------------------------------
# collapsed trace: the hand-traceable view. Repeating siblings fold into
# blocks: a run of same-shape calls and an alternating loop body of calls prints once under a
# "loop xN" header. Shape = def site + the block patterns of its children
# (repeat counts ignored), so outer iterations whose inner loops differ only in
# length still fold together. Order is never changed: non-consecutive repeats
# stay in place. Node = [call, kids, shape].
# ---------------------------------------------------------------------------
_MAX_LOOP_BODY = 4


def _build_tree(events):
    roots, stack = [], []
    for e in events:
        del stack[e.depth :]
        node = [e, [], None]
        (stack[-1][1] if stack else roots).append(node)
        stack.append(node)
    interned = {}
    for node in roots:
        _shape(node, interned)
    return roots


def _runs(nodes):
    groups = []
    for n in nodes:
        if groups and groups[-1][-1][2] == n[2]:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def _blocks(nodes):
    """Siblings -> [(pattern, reps, cols)]. pattern = tuple of 1..4 shapes that
    repeats `reps` times back to back; cols[p] = every node at position p across
    all repetitions. A plain run is a 1-shape pattern."""
    groups = _runs(nodes)
    shapes = [g[0][2] for g in groups]
    out, i = [], 0
    while i < len(groups):
        best_k, best_r = 1, 1
        for k in range(2, _MAX_LOOP_BODY + 1):
            pat = shapes[i : i + k]
            if len(pat) < k:
                break
            r = 1
            while shapes[i + r * k : i + r * k + k] == pat:
                r += 1
            if r >= 2 and k * r > best_k * best_r:
                best_k, best_r = k, r
        k, r = best_k, best_r
        cols = [
            [n for rep in range(r) for n in groups[i + rep * k + p]] for p in range(k)
        ]
        out.append((tuple(shapes[i : i + k]), r, cols))
        i += k * r
    return out


def _shape(node, interned):
    for kid in node[1]:
        _shape(kid, interned)
    e = node[0]
    key = (
        e.qualname,
        e.rel_file,
        e.lineno,
        _note_shape(e.note),
        tuple(b[0] for b in _blocks(node[1])),
    )
    node[2] = interned.setdefault(key, len(interned))


def _child_blocks(col):
    # same shape => every node in col has the same block patterns, aligned
    per = [_blocks(n[1]) for n in col]
    return [
        (
            per[0][b][0],
            sum(x[b][1] for x in per),
            [[c for x in per for c in x[b][2][p]] for p in range(len(per[0][b][0]))],
        )
        for b in range(len(per[0]))
    ]


def _render_collapsed(blocks, depth, out):
    for pattern, reps, cols in blocks:
        d = depth
        if len(pattern) > 1:
            out.append(
                "   [%5d] %s~ loop x%d (%d-call body)  [%d..%d]"
                % (
                    cols[0][0][0].idx,
                    "  " * depth,
                    reps,
                    len(pattern),
                    cols[0][0][0].idx,
                    cols[-1][-1][0].idx,
                )
            )
            d += 1
        for col in cols:
            out.append(_collapsed_line([n[0] for n in col], d))
            _render_collapsed(_child_blocks(col), d + 1, out)


def _collapsed_line(calls, depth):
    e = calls[0]
    cum = sum(c.elapsed for c in calls) * 1000
    sql_n = sum(c.sql_count for c in calls)
    sql_seg = (
        " sql=%.2fms x%d" % (sum(c.sql_self for c in calls) * 1000, sql_n)
        if sql_n
        else ""
    )
    es_n = sum(c.es_count for c in calls)
    if es_n:
        sql_seg += " es=%.2fms x%d" % (sum(c.es_self for c in calls) * 1000, es_n)
    if len(calls) == 1:
        count_seg, time_seg = "", "cummulative=%.2fms" % cum
    else:
        count_seg = "x%d " % len(calls)
        time_seg = "total=%.2fms avg=%.3fms  [%d..%d]" % (
            cum,
            cum / len(calls),
            e.idx,
            calls[-1].idx,
        )
    return "   [%5d] %s%s %s%s %s  %s%s%s%s" % (
        e.idx,
        "  " * depth,
        _mark(e),
        count_seg,
        e.qualname,
        _where(e),
        time_seg,
        sql_seg,
        "  [manager plumbing]" if e.orm_plumbing else "",
        _group_note(calls),
    )


def _note_shape(note):
    # ids differ per iteration -- they must not break a fold,
    # while a different job/topic/step must.
    return re.sub(r"\d+", "N", note)


def _group_note(calls):
    notes = {c.note for c in calls}
    if len(notes) == 1:
        note = calls[0].note
        return "  [%s]" % note if note else ""
    return "  [%s (varies)]" % _note_shape(calls[0].note)


def _mark(e):
    return {boundaries.BOUNDARY: ">>", NESTED: "=>"}.get(e.kind, "|")


def _where(e):
    return "(own report)" if e.kind == NESTED else "(%s:%d)" % (e.rel_file, e.lineno)


# ---------------------------------------------------------------------------
# decorator factory  (mirrors sql_spy(label="...") -- use with method_decorator)
# ---------------------------------------------------------------------------
def dynamic_life_saver(label=None, harvest_types=False):
    """A fresh tracer per call, so repeated invocations don't accumulate.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tracer = CallTracer(
                label or getattr(func, "__qualname__", func.__name__),
                harvest_types=harvest_types,
            )
            tracer.start()
            try:
                return func(*args, **kwargs)
            finally:
                tracer.stop()
                tracer.report()

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------
class trace_calls:
    def __init__(self, label="", harvest_types=False):
        self.tracer = CallTracer(label, harvest_types=harvest_types)

    def __enter__(self):
        self.tracer.start()
        return self.tracer

    def __exit__(self, exc_type, exc, tb):
        self.tracer.stop()
        self.tracer.report()
        return False


# ---------------------------------------------------------------------------
# Django middleware  (top of MIDDLEWARE; hit any endpoint with ?dls=1, DEBUG only;
# add ?dls_harvest=1 to also harvest runtime types - harvest implies tracing)
# ---------------------------------------------------------------------------
class DynamicLifeSaverMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        harvest = self._harvest_requested(request)
        if not (self._enabled(request) or harvest):
            return self.get_response(request)
        tracer = CallTracer(
            label="%s %s" % (request.method, request.path),
            harvest_types=harvest,
        )
        tracer.start()
        try:
            return self.get_response(request)
        finally:
            tracer.stop()
            tracer.report()

    @staticmethod
    def _debug_on():
        try:
            from django.conf import settings

            return bool(getattr(settings, "DEBUG", False))
        except Exception:
            return False

    @classmethod
    def _enabled(cls, request):
        if not cls._debug_on():
            return False
        return (
            request.GET.get("dls") == "1"
            or request.headers.get("X-Dynamic-Life-Saver") == "1"
        )

    @classmethod
    def _harvest_requested(cls, request):
        if not cls._debug_on():
            return False
        return (
            request.GET.get("dls_harvest") == "1"
            or request.headers.get("X-Dynamic-Life-Saver-Harvest") == "1"
        )
