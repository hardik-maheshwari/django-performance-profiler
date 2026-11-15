"""
trace_capture.capture
---------------------
The single collection point. `capture()` runs a block under the dynamic tracer
+ SQL spy, tees their log output into a buffer, then hands ONE CapturedRun to
each strategy. All strategies read from CapturedRun, so collection lives here
exactly once (DRY) and the strategies stay pure renderers.

Why log capture rather than re-rendering from events: the "output" a human
copy-pastes is what the tools LOG -- the tracer's report + the spy's per-query
dump (format_sql_for_humans, orm_xray fix hints, drf serializer context). All
of that already exists; recomputing it would duplicate a lot of stable code.
So we tee the loggers and reuse their rendering verbatim. The one thing logs
can't give us -- which functions ran, and where -- comes from tracer.events.

Phase 3 additions (all backward-compatible):
  - the buffer retains each record's LOGGER NAME, so behaviour logs (the app's own
    lines a refactor must preserve) can be separated from the tools' own output;
  - `capture()` yields a CaptureSession the harness attaches the response to,
    so the drift fingerprint can read the outcome (status / redirect / messages).

Only one change to stable code backs all this: sql_spy now `yield state`.
"""

import contextlib
import dataclasses
import functools
import logging
import os

from ..core import boundaries
from ..dynamic_life_saver.tracer import trace_calls
from ..sql_spy.sql_spy import sql_spy

try:  # ES is optional: SQL-only projects needn't install the client
    from ..es_spy.es_spy import es_spy
except ImportError:  # elasticsearch not installed
    es_spy = None

logger = logging.getLogger(__name__)

# Never trace or extract our own instrumentation frames -- the same rule the
# tracer applies to sql_spy / dynamic_life_saver / core via
# boundaries._auto_register_toolkit (which predates this package).
boundaries.register_self_path(os.path.dirname(os.path.abspath(__file__)))

# The loggers whose output IS the profiling tools' output. Every tool logs
# under the package ("django_perf_profiler.sql_spy.sql_spy",
# "django_perf_profiler.dynamic_life_saver", ...), so one handler on the parent
# catches them all. These are EXCLUDED from the drift fingerprint -- they
# differ by design (call counts, timings, file paths).
_TOOL_LOGGERS = ("django_perf_profiler",)


@dataclasses.dataclass(frozen=True)
class FunctionRef:
    """One first-party function that ran, located by its definition site."""

    qualname: str  
    rel_file: str  # relative to core.boundaries.PROJECT_ROOT
    lineno: int  # code.co_firstlineno -- the real def site

    @property
    def abs_file(self):
        return os.path.normpath(os.path.join(boundaries.PROJECT_ROOT, self.rel_file))

    @property
    def class_name(self):
        return self.qualname.split(".")[0] if "." in self.qualname else None

    @property
    def short_name(self):
        return self.qualname.split(".")[-1]


@dataclasses.dataclass
class CapturedRun:
    """Everything a strategy needs from one traced block."""

    label: str
    log_text: str  # ALL captured log output, verbatim (phases 1/2)
    functions: list  # list[FunctionRef]: deduped, first-appearance order, LOCAL only
    spy_state: dict = None  # raw sql_spy state -> the drift fingerprint's SQL input
    domain_logs: list = dataclasses.field(default_factory=list)  # behaviour-logger lines only
    response: object = None  # HttpResponse the harness handed back (drift outcome)
    request: object = None  # the request (for flash messages)
    exception: object = None  # exception the harness recorded, if the block raised


class CaptureSession:
    """Yielded by `capture()`. Carries the live tracer and lets the harness hand
    back the outcome so the drift fingerprint can read it:

        with capture(label, [...]) as cap:
            resp = view(request)
            cap.record_response(request, resp)      # success path
            # or, in an except block:
            # cap.record_exception(err)
    """

    def __init__(self):
        self.tracer = None
        self.response = None
        self.request = None
        self.exception = None

    def record_response(self, request, response):
        self.request = request
        self.response = response

    def record_exception(self, exc):
        self.exception = exc


class _BufferHandler(logging.Handler):
    """Collects (logger_name, formatted_message) so behaviour logs can later be
    split from tool output. Joined into text at teardown."""

    def __init__(self):
        super().__init__()
        self.records = []  # list[(name, message)]

    def emit(self, record):
        try:
            self.records.append((record.name, self.format(record)))
        except Exception:
            pass

    def text(self):
        return "\n".join(msg for _, msg in self.records)

    def lines_from(self, names):
        names = tuple(names)
        return [msg for name, msg in self.records if _name_matches(name, names)]


def _name_matches(record_name, names):
    """A record belongs to a logger group if its name equals a group name or is a
    dotted child of one (so 'sql_spy.sql_spy' matches 'sql_spy')."""
    for n in names:
        if record_name == n or record_name.startswith(n + "."):
            return True
    return False


def _unique_local_functions(tracer):
    """tracer.events -> deduped FunctionRef list.

    Key is (rel_file, qualname, lineno); first appearance wins the order. LOCAL
    frames only: we can only cleanly extract source for first-party files, and
    BOUNDARY libs live under site-packages. `<lambda>`/`<listcomp>` frames are
    dropped -- they have no FunctionDef node to slice and no name worth a banner.
    """
    seen = set()
    out = []
    if tracer is None:
        return out
    for e in tracer.events:
        if e.kind != boundaries.LOCAL:
            continue
        if e.qualname.split(".")[-1].startswith("<"):
            continue
        key = (e.rel_file, e.qualname, e.lineno)
        if key in seen:
            continue
        seen.add(key)
        out.append(FunctionRef(qualname=e.qualname, rel_file=e.rel_file, lineno=e.lineno))
    return out


@contextlib.contextmanager
def capture(label, strategies=(), *, harvest_types=False, domain_loggers=()):
    """Drop-in for `with trace_calls(x), sql_spy(x), es_spy(x):` that also feeds
    strategies. es_spy joins only when the elasticsearch client is installed.

        from django_perf_profiler.trace_capture import capture, BasicStrategy, SourceStrategy
        with capture(sim_name, [BasicStrategy("out/x.txt"), SourceStrategy("out/x.py")]):
            response = view(request)

    `domain_loggers` are additionally attached AND flagged as behaviour loggers,
    so their lines land in CapturedRun.domain_logs (the drift fingerprint's log
    signal) while staying out of the tool-vs-tool noise. Pass the names of
    the loggers your feature under test writes to, e.g. ("billing",).

    Strategies run in a `finally`, so artifacts are written even when the traced
    block raises. A strategy that itself fails is logged and skipped -- it can never mask the
    block's real exception.
    """
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger_names = tuple(_TOOL_LOGGERS) + tuple(domain_loggers)

    saved_levels = {}
    for name in logger_names:
        lg = logging.getLogger(name)
        saved_levels[name] = lg.level
        lg.setLevel(logging.DEBUG)  # ensure .info records reach our handler
        lg.addHandler(handler)

    session = CaptureSession()
    spy_state = None
    try:
        es_cm = es_spy(label) if es_spy is not None else contextlib.nullcontext()
        with trace_calls(label, harvest_types=harvest_types) as _tracer, sql_spy(
            label
        ) as _spy, es_cm:
            session.tracer, spy_state = _tracer, _spy
            yield session  # harness runs the view here (and calls record_response)
        # both context managers have exited -> report() + spy summary already
        # logged (handler still attached), so log_text is complete below.
    finally:
        for name in logger_names:
            lg = logging.getLogger(name)
            lg.removeHandler(handler)
            lg.setLevel(saved_levels[name])

        run = CapturedRun(
            label=label,
            log_text=handler.text(),
            functions=_unique_local_functions(session.tracer),
            spy_state=spy_state,
            domain_logs=handler.lines_from(domain_loggers),
            response=session.response,
            request=session.request,
            exception=session.exception,
        )
        for strat in strategies:
            try:
                strat.render(run)
            except Exception as e:
                logger.warning("strategy %r failed: %s", strat, e)


def _find_request(args):
    """Best-effort locate the HttpRequest in a view's positional args. Works for
    both `get(self, request, ...)` (method_decorator) and `view(request, ...)`
    (function-based) -- duck-typed on the two attributes every Django/DRF request
    carries. Returns None if none looks like a request (flash messages just stay
    empty in the fingerprint)."""
    for a in args:
        if hasattr(a, "META") and hasattr(a, "method"):
            return a
    return None


def capture_view(label, strategies=(), *, harvest_types=False, domain_loggers=(), record_outcome=True):
    """Decorator form of `capture()` -- one decorator that replaces the old
    `@method_decorator(sql_spy(...))` + `@method_decorator(dynamic_life_saver(...))`
    stack, and (unlike the free contextmanager-as-decorator) AUTO-RECORDS the
    outcome, so it drives the drift fingerprint without a manual record_response.

        @method_decorator(
            capture_view("Example.get", [FingerprintStrategy("out/x.json")],
                         domain_loggers=EXAMPLE_DOMAIN_LOGGERS),
            name="get",
        )
        def get(self, request, ...): ...

    The wrapped call's return value becomes the recorded response and its request
    arg is auto-detected; a raised exception is recorded (record_exception) and
    re-raised. Set record_outcome=False for a pure Basic/Source decoration where
    the outcome is irrelevant (then plain `capture(...)` works as a decorator too).
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with capture(
                label, strategies, harvest_types=harvest_types, domain_loggers=domain_loggers
            ) as cap:
                try:
                    response = func(*args, **kwargs)
                except Exception as err:
                    if record_outcome:
                        cap.record_exception(err)
                    raise
                if record_outcome:
                    cap.record_response(_find_request(args), response)
                return response

        return wrapper

    return decorate