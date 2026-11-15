"""
core.boundaries
===============

Single source of truth for "is this code first-party?", shared by the static
tracer, the SQL spy, and the dynamic call tracer.

Collapses three separate, drifting definitions into one:
    - static_life_saver: the curated LOCAL_APPS set + is_local_app()
    - sql_spy: the inspect.stack() framework blocklist in _record_query
    - dynamic_life_saver: _classify() (PROJECT_ROOT + IGNORE markers + boundaries)

The path-based approach (dynamic's) wins because it does not depend on a
hand-maintained app list.

Classification (precedence is deliberate):
    SELF       the profiler's own files -> never attribute the profiler to itself
    BOUNDARY   a library we surface as a leaf (boto3, cryptography, ...). Checked
               BEFORE the ignore/root rules so a lib living under site-packages is
               still surfaced rather than silently dropped.
    EXTERNAL   site-packages / migrations / frozen / stdlib, etc.
    LOCAL      under PROJECT_ROOT and none of the above.

Tools extend the boundary/ignore/self sets via the add_*/register_* helpers
rather than editing this file -- composition, not forking.
"""

import os

LOCAL = "LOCAL"
BOUNDARY = "BOUNDARY"
EXTERNAL = "EXTERNAL"

#Override with LIFESAVER_PROJECT_ROOT; defaults to the cwd the tools runs from.
PROJECT_ROOT = os.path.abspath(os.environ.get("LIFESAVER_PROJECT_ROOT", os.getcwd()))

# None by default: third-party code stays invisible unless you opt in, e.g.
#   add_boundary_lib("requests", "redis")
_DEFAULT_BOUNDARIES_LIBS = ()

# Opt-in extra boundary libs without code changes, comma-separated, e.g.
# LIFESAVER_BOUNDARY_LIBS=rest_framework,django/contrib/auth to see which dispatch()
# ran (APIView vs LoginRequiredMixin). A path fragment works: matched as /<lib>/.
_ENV_BOUNDARY_LIBS = tuple(
    lib.strip() for lib in os.environ.get("LIFESAVER_BOUNDARY_LIBS", "").split(",") if lib.strip()
)

_DEFAULT_IGNORE_MARKERS = (
    "/site-packages/",
    "/dist-packages/",
    "/.venv/",
    "/venv/",
    "/migrations/",
    "<frozen",
    "<string",
    "/lib/python",
)

_boundary_libs = set(_DEFAULT_BOUNDARIES_LIBS) | set(_ENV_BOUNDARY_LIBS)
_ignore_markers = set(_DEFAULT_IGNORE_MARKERS)
_self_paths = set()
_cache = {}


def set_project_root(path):
    """Point classification at a different root (and clear the cache)."""
    global PROJECT_ROOT
    PROJECT_ROOT = os.path.abspath(path)
    _cache.clear()

def add_boundary_lib(*libs):
    """Surface extra libraries as boundary root (and clear the cache)."""
    _boundary_libs.update(libs)
    _cache.clear()

def add_ignore_marker(*markers):
    _ignore_markers.update(markers)
    _cache.clear()

def register_self_path(*paths):
    """Mark a directory as the profiler's own code -> always EXTERNAL, so a tool
    never traces or attributes work to its own instrumentation frames."""
    for p in paths:
        _self_paths.add(os.path.abspath(p))
    _cache.clear()

def _auto_register_toolkit():
    """Treat the whole toolkit (core + sibling tool packages) as non-first-party.
    This single rule subsumes sql_spy's 'sql_spy/' blocklist entry and the
    dynamic tracer's _THIS_FILE self-exclusion."""
    core_dir = os.path.dirname(os.path.abspath(__file__))
    pkg_root = os.path.dirname(core_dir)  # django_perf_profiler/
    register_self_path(pkg_root)  # every tool package lives under it

def _is_boundary(filename):
    for lib in _boundary_libs:
        #Slash-delimited so a name that is a prefix of another doesn't collide:
        # '/redis/' never matches '/redislite/'. (Same matching insight orm_xray
        # uses for table names.)
        if ("/" + lib + "/") in filename or filename.endswith("/" + lib + ".py"):
            return True
    return False

def _under(filename, directory):
    #Separator-delimited so the toolkit's /core never swallows a first-party
    # /core_utils, and a root of /repo never claims /repo-other.
    return filename.startswith(directory.rstrip(os.sep) + os.sep)

def classify(filename):
    """Return LOCAL/ BOUNDARY / EXTERNAL for an absolute source filename."""
    if not filename:
        return EXTERNAL

    cached = _cache.get(filename)
    if cached is not None:
        return cached

    #Pre-calculate if the file lives in a virtualenv or system directory
    is_ignored = any(m in filename for m in _ignore_markers)
    in_root = _under(filename, PROJECT_ROOT)

    if any(_under(filename, p) for p in _self_paths):
        kind = EXTERNAL
    elif _is_boundary(filename) and (is_ignored or not in_root):
        # Only elevate to BOUNDARY if it's an external library or inside a venv.
        # This protects first-party files
        kind = BOUNDARY
    elif is_ignored:
        kind = EXTERNAL
    elif in_root:
        kind = LOCAL
    else:
        kind = EXTERNAL

    _cache[filename] = kind
    return kind

def is_local(filename):
    return classify(filename) == LOCAL

_auto_register_toolkit()
    