import contextvars
import dataclasses
import functools
import inspect
import logging
import threading

from django.core.exceptions import EmptyResultSet
from django.db import DEFAULT_DB_ALIAS
from django.db.models import Manager, Q, QuerySet

# Reuse the existing readability pipeline verbatim — same formatting on every
# side so a future diff strategy compares apples to apples. We deliberately do
# NOT reuse verify_query_logic here: it runs EXPLAIN against the DB per query,
# far too heavy to do on every step of a build.
from .sql_spy import format_sql_for_humans, interpolate_params, process_ast_readability

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# State: an explicit on/off bool (mirrors orm_xray.set_active), the current
# strategy, and a re-entry suppression flag. Counter/lock are module-level
# (shared), not per-context, so manual set_active(True) works standalone.
# Under concurrent fan-out the step numbers interleave — accepted: the on/off
# bool is the firehose control, and interactive use is one chain at a time.
# --------------------------------------------------------------------------
_active = contextvars.ContextVar("build_xray_active", default=False)
_suppress = contextvars.ContextVar("build_xray_suppress", default=False)
_strategy = contextvars.ContextVar("build_xray_strategy", default=None)

_lock = threading.Lock()
_counter = [0]

_installed = False
_generation = [0]
_orig = {}


def set_active(flag: bool):
    if flag:
        with _lock:
            _counter[0] = 0  # fresh step numbers per "on" window
            _generation[0] += 1
    _active.set(bool(flag))


def set_strategy(strategy):
    _strategy.set(strategy)


def get_strategy():
    return _strategy.get() or _DEFAULT_STRATEGY


# --------------------------------------------------------------------------
# The step event handed to the strategy. before/after are (sql, params) tuples
# straight off the compiler — rendering is the strategy's job.
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class BuildStep:
    node_id: int  # id assigned to the queryset this op produced (the child)
    parent_id: int  # id of the queryset this op was called on (self). When
    # is_origin, this is self's own freshly-assigned id.
    is_origin: bool  # True when self had never been traced — a new build starts
    op: str
    args: str
    call_site: str
    before: tuple
    after: tuple


# --------------------------------------------------------------------------
# Strategy: the seam. on_step per builder call; flush at context teardown.
# --------------------------------------------------------------------------
class BuildTraceStrategy:
    def on_step(self, event: BuildStep):
        raise NotImplementedError

    def flush(self):
        pass


class PrintStrategy(BuildTraceStrategy):
    """Streams the build to the log: origin once, then the result after each
    op that actually changed the SQL. To get strict 'before each change'
    instead (origin gap and all), print `event.before` here and drop the
    step-0 special-case."""

    def __init__(self):
        # Per-thread. Under ThreadPoolExecutor the SAME instance is shared across
        # workers (sql_spy's submit-patch copies the contextvar's reference, not
        # the object), so a shared _last_node would race and emit spurious branch
        # markers. Tracking it per thread keeps each worker's lineage correct.
        self._tl = threading.local()

    def on_step(self, ev: BuildStep):
        b_sql, b_params = ev.before
        a_sql, a_params = ev.after
        last = getattr(self._tl, "last_node", None)

        if ev.is_origin:
            logger.info("\n┌─ BUILD ── origin #%d @ %s", ev.parent_id, ev.call_site)
            logger.info("%s", _fmt(b_sql, b_params))
            label = f"[#{ev.node_id}]"
        elif ev.parent_id != last:
            label = f"[#{ev.node_id} ↰ branches from #{ev.parent_id}]"
        else:
            label = f"[#{ev.node_id}]"

        if a_sql != b_sql:
            logger.info("\n%s %s(%s)  @ %s", label, ev.op, ev.args, ev.call_site)
            logger.info("%s", _fmt(a_sql, a_params))
        else:
            logger.info(
                "\n%s %s(%s)  @ %s   %s",
                label,
                ev.op,
                ev.args,
                ev.call_site,
                _unchanged_reason(a_sql),
            )

        self._tl.last_node = ev.node_id

    def flush(self):
        self._tl.last_node = None
        logger.info("└─ END BUILD TRACE ──\n")


_DEFAULT_STRATEGY = PrintStrategy()


def _unchanged_reason(sql):
    s = sql.lstrip()
    if s.startswith("/* could not compile"):
        return "(could not compile — subquery)"
    if s.startswith("/* EmptyResultSet"):
        return "(empty result set)"
    return "(no SQL change)"


# --------------------------------------------------------------------------
# Compile a queryset to (sql, params) WITHOUT executing it. Honors qs.db for
# multi-DB (sql_with_params would force DEFAULT_DB_ALIAS).
# --------------------------------------------------------------------------
def _compile(qs):
    try:
        compiler = qs.query.get_compiler(using=qs.db or DEFAULT_DB_ALIAS)
        sql, params = compiler.as_sql()
        return sql, params
    except EmptyResultSet:
        # .none() / a provably-empty filter short-circuits at compile time.
        return (
            "/* EmptyResultSet: query short-circuited (.none() / impossible filter) */",
            (),
        )
    except Exception as e:
        return f"/* could not compile: {e} */", ()


def _fmt(sql, params):
    try:
        interpolated = interpolate_params(sql, list(params) if params else [])
        return format_sql_for_humans(process_ast_readability(interpolated))
    except Exception:
        return "  " + " ".join(sql.split())[:2000]


def _trunc(s, n=100):
    return s if len(s) <= n else s[:n] + "... [TRUNCATED]"


# --------------------------------------------------------------------------
# Safe arg repr. THE footgun: repr() on a QuerySet
# evaluates it, and a queryset passed as an arg (e.g. filter(id__in=other_qs))
# would fire a phantom query that sql_spy then counts as real. So QuerySet/Manager args are compiled, never evaluated; dict/list/Q are
# walked so an embedded queryset can't sneak a repr() in.
# --------------------------------------------------------------------------
def _safe_arg_repr(v, depth=0):
    if isinstance(v, QuerySet):
        sql, _ = _compile(v)  # compile, never evaluate
        model = getattr(v, "model", None)
        name = model.__name__ if model else "?"
        return f"<QuerySet {name}: {_trunc(' '.join(sql.split()))}>"
    if isinstance(v, Manager):
        model = getattr(v, "model", None)
        return f"<Manager {model.__name__ if model else '?'}>"
    if isinstance(v, Q):
        return _q_repr(v)
    if isinstance(v, dict):
        inner = ", ".join(
            f"{k!r}: {_safe_arg_repr(val, depth + 1)}" for k, val in v.items()
        )
        return "{" + inner + "}"
    if isinstance(v, (list, tuple, set)) and depth < 3:
        return "[" + ", ".join(_safe_arg_repr(x, depth + 1) for x in v) + "]"
    try:
        return _trunc(repr(v))
    except Exception:
        return f"<{type(v).__name__}>"


def _q_repr(q):
    # Walk Q.children rather than repr(q): repr would repr an embedded queryset
    # (e.g. Q(id__in=qs)) and evaluate it.
    bits = []
    for child in q.children:
        if isinstance(child, Q):
            bits.append(_q_repr(child))
        elif isinstance(child, tuple) and len(child) == 2:
            k, val = child
            bits.append(f"{k}={_safe_arg_repr(val, 1)}")
        else:
            bits.append(_safe_arg_repr(child, 1))
    joined = f" {q.connector} ".join(bits)
    return ("NOT " if q.negated else "") + f"({joined})"


def _render_args(args, kwargs):
    parts = [_safe_arg_repr(a) for a in args]
    parts += [f"{k}={_safe_arg_repr(v)}" for k, v in kwargs.items()]
    return ", ".join(parts)


# --------------------------------------------------------------------------
# Caller attribution (same idea as sql_spy's inspect.stack walk). Skips our own
# frames + django internals and lands on the first app frame. For the manager's
# internal annotates that's the custom Manager's get_queryset; for your ops it's you.
# Duplicated rather than imported to keep this a pure addition (zero edits to
# the working spy); could be DRY'd into a shared _caller_site later.
# --------------------------------------------------------------------------
_SKIP_SUBSTR = (
    "django/db",
    "django/core",
    "/contextlib",
    "concurrent/futures",
    "sql_spy/",
    "build_xray",
)


def _caller_site():
    for fi in inspect.stack():
        fn = fi.filename.replace("\\", "/")
        if any(s in fn for s in _SKIP_SUBSTR):
            continue
        parts = fn.split("/")
        short = "/".join(parts[-2:]) if len(parts) >= 2 else fn
        return f"{short}:{fi.lineno} {fi.function}()"
    return "?"


# --------------------------------------------------------------------------
# Wrappers.
#
# Builder: record before -> apply (with nested builder calls suppressed) ->
# record after -> hand to strategy. Suppressing during orig() is what makes
# composite builders behave: dates()/datetimes() internally call annotate/
# values_list/distinct/filter/order_by — without suppression each would emit a
# phantom step. Chained calls aren't nested (filter returns, THEN exclude runs
# on the result), so this never swallows a real user step.
#
# Terminal: get()/first()/last()/get_or_create()/... call builders internally
# (get -> filter, first -> order_by('pk')). Suppress those so they don't show
# as steps you never wrote. Also covers _fetch_all — the single evaluation
# chokepoint — so prefetch_related's eval-time .all()/.filter() calls don't
# emit phantom steps if you evaluate inside an active block.
# --------------------------------------------------------------------------
def _wrap_builder(orig, op_name):
    @functools.wraps(orig)
    def method(self, *args, **kwargs):
        if not _active.get() or _suppress.get():
            return orig(self, *args, **kwargs)

        before = _compile(self)
        call_site = _caller_site()
        args_repr = _render_args(args, kwargs)

        gen = _generation[0]
        stamp = getattr(self, "_xray_node", None)
        parent_id = stamp[1] if (stamp is not None and stamp[0] == gen) else None
        is_origin = parent_id is None
        if is_origin:
            with _lock:
                parent_id = _counter[0]
                _counter[0] += 1
            try:
                self._xray_node = (gen, parent_id)
            except (AttributeError, TypeError):
                pass

        tok = _suppress.set(True)  # suppress nested (composite) builders
        try:
            result = orig(self, *args, **kwargs)
        finally:
            _suppress.reset(tok)

        after = _compile(result)

        with _lock:
            node_id = _counter[0]
            _counter[0] += 1

        try:
            result._xray_node = (gen, node_id)
        except (AttributeError, TypeError):
            pass

        try:
            get_strategy().on_step(
                BuildStep(
                    node_id=node_id,
                    parent_id=parent_id,
                    is_origin=is_origin,
                    op=op_name,
                    args=args_repr,
                    call_site=call_site,
                    before=before,
                    after=after,
                )
            )
        except Exception as e:
            logger.warning("build_xray strategy.on_step failed: %s", e)

        return result

    return method


def _wrap_terminal(orig):
    @functools.wraps(orig)
    def method(self, *args, **kwargs):
        if not _active.get():
            return orig(self, *args, **kwargs)
        tok = _suppress.set(True)
        try:
            return orig(self, *args, **kwargs)
        finally:
            _suppress.reset(tok)

    return method


# Full chainable builder set (Django 5.2 + 6.1's fetch_mode). Each returns a new
# QuerySet. install() skips names the running Django doesn't have, so newer
# builders can be listed without breaking older versions.
_BUILDERS = [
    "filter",
    "exclude",
    "complex_filter",
    "annotate",
    "alias",
    "order_by",
    "reverse",
    "distinct",
    "values",
    "values_list",
    "select_related",
    "prefetch_related",
    "defer",
    "only",
    "extra",
    "using",
    "none",
    "all",
    "union",
    "intersection",
    "difference",
    "select_for_update",
    "dates",
    "datetimes",
    "fetch_mode",  # Django 6.1+
]

# Terminals + the eval chokepoint — wrapped only to SUPPRESS their internal
# builder calls (they never emit steps themselves).
_TERMINALS = [
    "get",
    "first",
    "last",
    "earliest",
    "latest",
    "exists",
    "contains",
    "count",
    "aggregate",
    "in_bulk",
    "iterator",
    "explain",
    "get_or_create",
    "update_or_create",
    "_fetch_all",
]


def install():
    global _installed
    if _installed:
        return
    for name in _BUILDERS:
        orig = getattr(QuerySet, name, None)
        if orig is None:
            continue
        _orig[name] = orig
        setattr(QuerySet, name, _wrap_builder(orig, name))
    for name in _TERMINALS:
        orig = getattr(QuerySet, name, None)
        if orig is None:
            continue
        _orig[name] = orig
        setattr(QuerySet, name, _wrap_terminal(orig))
    _installed = True


def uninstall():
    global _installed
    if not _installed:
        return
    for name, orig in _orig.items():
        setattr(QuerySet, name, orig)
    _orig.clear()
    _installed = False


# --------------------------------------------------------------------------
# Entry points.
# --------------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402


@contextmanager
def build_trace(strategy=None):
    install()  # idempotent; mirrors orm_xray (install once, gate by the bool)
    set_strategy(strategy or PrintStrategy())
    set_active(True)
    try:
        yield
    finally:
        set_active(False)
        try:
            get_strategy().flush()
        except Exception as e:
            logger.warning("build_xray flush failed: %s", e)


def trace_build(strategy=None):
    """Decorator form, for method_decorator on a CBV (same as sql_spy)."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            with build_trace(strategy):
                return fn(*a, **k)

        return wrapper

    return deco
