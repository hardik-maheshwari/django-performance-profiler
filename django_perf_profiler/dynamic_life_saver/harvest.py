"""
dynamic_life_saver.harvest
--------------------------
Runtime type harvesting: while the dynamic tracer runs, observe the ACTUAL
types of arguments flowing into first-party functions, keyed in the exact
static_life_saver cache format (core.keys). This is what closes the
static/dynamic gap: static can only guess an untyped arg's type (CamelCase
fallback, wizard), dynamic sees the ground truth — and shared utilities called
from cheap endpoints are the same functions static hits when crawling
expensive ones.

Phase 1 scope (deliberate):
  - args only, from frame.f_locals at call time. No locals-at-return, no
    loop variables, no return types.
  - up to SAMPLES_PER_FUNC calls sampled per code object per run, so a
    function called 400x pays ~3 harvests + one dict lookup per call, and
    within-run polymorphism (strategy dispatch) is still caught.

Persistence (Phase 2): at tracer stop(), flush() merges this run's
observations into the sidecar via core.harvest_store (evidence file, separate
from the wizard-owned .type_cache.json; conflicts merged, never resolved).
A flush failure is logged and surfaced in the report but never propagates —
harvesting must never be able to break a traced request.

Attribution rules (false labels are worse than gaps):
  - value is None            -> record nothing. None proves nothing.
  - lazy/proxy wrappers      -> record nothing. SimpleLazyObject is "really"
    a User; recording the wrapper would prime a wrong blacklist entry,
    and unwrapping (_wrapped) is exactly the cleverness that mislabels.
    Detected by MRO class-name match, so django never needs importing here.
  - type's defining module classifies LOCAL (core.boundaries)
                             -> a type observation: key -> ClassName.
    A class object passed as an arg (serializer_class=OrderSerializer) records the class's
    own name — static's (ClassName, attr) member-use resolution is equally
    correct for classmethod access.
  - anything else (builtins, stdlib, site-packages, BOUNDARY libs)
                             -> a blacklist CANDIDATE observation. These map
    to the wizard's "-1 ignore forever" answer, but per the design they stay
    candidates (wizard priors), never silent auto-blacklisting.
  - unresolvable module/file -> record nothing.
"""

import logging
import os
import sys

from ..core import boundaries, harvest_store, keys

logger = logging.getLogger("django_perf_profiler.dynamic_life_saver")

SAMPLES_PER_FUNC = 3

# Wrapper types whose runtime class lies about the payload. Name-based so this
# module stays import-free of django/lazy libs. Extend via add_proxy_names().
_PROXY_CLASS_NAMES = {
    "SimpleLazyObject",
    "LazyObject",
    "Promise",  # django lazy translation strings
    "lazy",
}

_SKIP_ARG_NAMES = {"self", "cls"}


def add_proxy_names(*names):
    _PROXY_CLASS_NAMES.update(names)


class TypeHarvester:
    """Owner-thread only (lives inside the setprofile hook), so no locking."""

    def __init__(self, samples_per_func=SAMPLES_PER_FUNC):
        self.samples_per_func = samples_per_func
        self._samples = {}  # code object -> calls sampled (holds a ref: no id reuse)
        self._type_cache = {}  # type -> ("local"|"non_local"|None, name)
        self.local_types = {}  # key -> {TypeName: count}   (static-cache candidates)
        self.non_local = {}  # key -> {TypeName: count}     (blacklist candidates)
        self.last_flush = None  # None | ("ok", path, total_keys) | ("error", msg)

    # ---- persistence (called from CallTracer.stop) ----
    def flush(self):
        """Merge this run's observations into the sidecar. Never raises."""
        if not self.has_observations():
            self.last_flush = None
            return
        try:
            path, total_keys = harvest_store.merge_and_save(self.local_types, self.non_local)
            self.last_flush = ("ok", path, total_keys)
        except Exception as e:
            self.last_flush = ("error", str(e))
            logger.warning("TypeHarvester: sidecar flush failed: %s", e)

    # ---- called from CallTracer._on_call for LOCAL frames only ----
    def observe(self, frame, code):
        taken = self._samples.get(code, 0)
        if taken >= self.samples_per_func:
            return
        self._samples[code] = taken + 1

        name = code.co_name
        if name.startswith("<"):  # <lambda>, <listcomp>, ... — static can't target these
            return

        n_args = code.co_argcount + code.co_kwonlyargcount
        if not n_args:
            return

        arg_names = code.co_varnames[:n_args]
        try:
            local_vars = frame.f_locals
        except Exception:
            return
        rel_path = keys.rel_to_root(code.co_filename)

        for arg_name in arg_names:
            if arg_name in _SKIP_ARG_NAMES:
                continue
            if arg_name not in local_vars:
                continue
            verdict = self._classify_value(local_vars[arg_name])
            if verdict is None:
                continue
            bucket_name, type_name = verdict
            key = keys.scoped_key(rel_path, name, arg_name)
            bucket = self.local_types if bucket_name == "local" else self.non_local
            per_key = bucket.setdefault(key, {})
            per_key[type_name] = per_key.get(type_name, 0) + 1

    def _classify_value(self, value):
        if value is None:
            return None
        # A class passed as a value is classified by ITSELF, not its metaclass.
        t = value if isinstance(value, type) else type(value)

        cached = self._type_cache.get(t)
        if cached is not None or t in self._type_cache:
            return cached

        verdict = self._classify_type(t)
        self._type_cache[t] = verdict
        return verdict

    @staticmethod
    def _classify_type(t):
        try:
            if any(c.__name__ in _PROXY_CLASS_NAMES for c in t.__mro__):
                return None  # wrapper lies about the payload — refuse to attribute
            mod = sys.modules.get(t.__module__)
            filename = getattr(mod, "__file__", None)
            if not filename:
                # builtins / frozen / __main__ oddities: builtins are honest
                # blacklist candidates; anything else is unresolvable — skip.
                if t.__module__ == "builtins":
                    return ("non_local", t.__name__)
                return None
            kind = boundaries.classify(os.path.abspath(filename))
            if kind == boundaries.LOCAL:
                return ("local", t.__name__)
            return ("non_local", t.__name__)
        except Exception:
            return None  # silence over speculation

    # ---- reporting (consumed by CallTracer.report) ----
    def has_observations(self):
        return bool(self.local_types or self.non_local)

    def report_lines(self):
        out = ["", "HARVESTED TYPES:"]
        if not self.has_observations():
            out.append("   (no observations — no sampled LOCAL calls had classifiable args)")
            return out

        clean = {k: v for k, v in self.local_types.items() if len(v) == 1}
        conflicted = {k: v for k, v in self.local_types.items() if len(v) > 1}

        if clean:
            out.append("   static-cache candidates (single observed type):")
            for key in sorted(clean):
                ((tname, cnt),) = clean[key].items()
                out.append("      %s -> %s  x%d" % (key, tname, cnt))
        if conflicted:
            out.append(
                "   POLYMORPHIC within this run (would merge in sidecar, never auto-resolve):"
            )
            for key in sorted(conflicted):
                seen = ", ".join(
                    "%s x%d" % (tn, c)
                    for tn, c in sorted(conflicted[key].items(), key=lambda kv: -kv[1])
                )
                out.append("      %s -> {%s}" % (key, seen))
        if self.non_local:
            out.append(
                "   blacklist candidates (non-first-party arg types), %d keys:"
                % len(self.non_local)
            )
            for key in sorted(self.non_local):
                seen = ", ".join(
                    "%s x%d" % (tn, c)
                    for tn, c in sorted(self.non_local[key].items(), key=lambda kv: -kv[1])
                )
                out.append("      %s -> {%s}" % (key, seen))

        if self.last_flush is None:
            out.append("   (not yet flushed)")
        elif self.last_flush[0] == "ok":
            _, path, total_keys = self.last_flush
            out.append("   persisted -> %s (%d keys total in sidecar)" % (path, total_keys))
        else:
            out.append(
                "   SIDECAR FLUSH FAILED: %s — observations from this run were NOT persisted"
                % self.last_flush[1]
            )
        return out