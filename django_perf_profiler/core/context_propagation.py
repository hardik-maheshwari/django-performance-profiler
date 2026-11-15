"""
core.context_propagation
------------------------
Make the spies' contextvars follow work into thread pools.

A contextvar does NOT cross into a pool worker by default: the worker thread
runs with its own (empty) context, so a spy gated on a contextvar goes silent
for every query or ES call made there. The fix is to snapshot the submitting
thread's context and run the task inside it.

Two pool families are covered, because they are separate code paths:
    * concurrent.futures.ThreadPoolExecutor  (submit; map goes through submit)
    * multiprocessing.pool.ThreadPool        (apply_async, _map_async, imap,
      imap_unordered; every other method funnels into these). Used by
      elasticsearch.helpers.parallel_bulk, among others.

Process pools are deliberately NOT patched: a context can't cross a process
boundary, and wrapping the task in a closure would break pickling.

Each task runs in its OWN copy of the snapshot (ctx.copy().run): a single
Context can't be entered by two threads at once, and a map call hands one
function to many workers concurrently.

Submit site: a task can run with nothing first-party on its worker stack
(parallel_bulk's chunks are all library frames), so a spy's stack walk finds no
caller. _bind therefore also records the first first-party frame at SUBMIT time
(core.boundaries LOCAL), readable in the worker via submit_site().

install() is idempotent; both spies call it at import.
"""

import concurrent.futures
import contextvars
import functools
import multiprocessing.pool
import sys

from . import boundaries

_installed = False
_submit_site = contextvars.ContextVar("submit_site", default=None)
_MAX_SITE_DEPTH = 40


def submit_site():
    """(short_file, lineno, func) of the first-party code that submitted the
    current pool task, or None outside a pool task / with no first-party frame."""
    return _submit_site.get()


def _first_local_frame(frame):
    for _ in range(_MAX_SITE_DEPTH):
        if frame is None:
            return None
        code = frame.f_code
        if boundaries.classify(code.co_filename) == boundaries.LOCAL:
            parts = code.co_filename.replace("\\", "/").split("/")
            return ("/".join(parts[-2:]), frame.f_lineno, code.co_name)
        frame = frame.f_back
    return None


def _run_at_site(site, fn, args, kwargs):
    _submit_site.set(site)
    return fn(*args, **kwargs)


def _bind(fn):
    """fn -> wrapper that runs fn inside a copy of the CURRENT context."""
    ctx = contextvars.copy_context()
    site = _first_local_frame(sys._getframe(2))  # skip _bind + the patched method

    @functools.wraps(fn)
    def run_in_context(*args, **kwargs):
        return ctx.copy().run(_run_at_site, site, fn, args, kwargs)

    return run_in_context


def install():
    global _installed
    if _installed:
        return

    executor_submit = concurrent.futures.ThreadPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        return executor_submit(self, _bind(fn), *args, **kwargs)

    concurrent.futures.ThreadPoolExecutor.submit = submit

    TP = multiprocessing.pool.ThreadPool
    for name in ("apply_async", "_map_async", "imap", "imap_unordered"):
        orig = getattr(TP, name)

        def method(self, func, *args, _orig=orig, **kwargs):
            return _orig(self, _bind(func), *args, **kwargs)

        functools.update_wrapper(method, orig)
        setattr(TP, name, method)

    _installed = True
