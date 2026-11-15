"""
trace_capture.strategies
------------------------
Renderers over a CapturedRun. Three distinct use cases, one shared input:

  BasicStrategy   (Phase 1) convenience -- tee the tool output to a .txt so you
                  stop copy-pasting from the terminal. No interpretation.
  SourceStrategy  (Phase 2) debugging -- a parse-valid, non-runnable .py: the
                  trace in the module docstring, a best-effort import header, and
                  each traced first-party function's source once, grouped by
                  class. Read what ran without the IDE round-trip.
  (drift)         (Phase 3) regression gate -- lands here later; reads
                  run.spy_state, not the trace or source.
"""

import abc
import logging
import os
import textwrap

from ..core import boundaries
from .sources import extract_function_source, read_import_block

logger = logging.getLogger(__name__)


class Strategy(abc.ABC):
    @abc.abstractmethod
    def render(self, run):
        """Consume a CapturedRun and emit this strategy's artifact."""


class BasicStrategy(Strategy):
    """Phase 1: dump the captured tool output verbatim to `out_path` (.txt)."""

    def __init__(self, out_path):
        self.out_path = out_path

    def render(self, run):
        _ensure_parent(self.out_path)
        with open(self.out_path, "w", encoding="utf-8") as f:
            f.write("# %s\n\n" % run.label)
            f.write(run.log_text)
            f.write("\n")
        logger.info("BasicStrategy -> %s", self.out_path)


class SourceStrategy(Strategy):
    """Phase 2: a parse-valid, NON-runnable `.py` reading aid.

    Layout: module docstring (the captured trace) -> best-effort import header
    (so Pylance resolves/hovers the imported symbols) -> one source block per
    traced first-party function, grouped by owning class. Bodies come from
    ast.get_source_segment, so method indentation just works.

    Write it INSIDE the repo tree -- Pylance only resolves the imports if the
    file sits on its path. 100% squiggle-free is explicitly a non-goal: on a
    monkey-patched, duck-typed codebase that's a fool's errand (which is why the
    static + dynamic life savers exist). Best-effort hover is the win.
    """

    def __init__(self, out_path):
        self.out_path = out_path

    def render(self, run):
        chunks = [self._docstring(run), self._imports(run), self._bodies(run)]
        _ensure_parent(self.out_path)
        with open(self.out_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(c for c in chunks if c))
            f.write("\n")
        logger.info("SourceStrategy -> %s", self.out_path)

    @staticmethod
    def _docstring(run):
        # Neutralise any triple-quote in the trace so it can't close the docstring.
        body = run.log_text.replace('"""', r'\"\"\"')
        return '"""\nTRACE CAPTURE (generated, not runnable): %s\n\n%s\n"""' % (run.label, body)

    @staticmethod
    def _imports(run):
        # Union each involved file's module-level import block, verbatim, dedup
        # identical lines, preserving first-seen order.
        seen = set()
        lines = ["# --- imports (best-effort; unused ones are harmless) ---"]
        for rel_file in dict.fromkeys(fr.rel_file for fr in run.functions):
            abs_file = os.path.normpath(os.path.join(boundaries.PROJECT_ROOT, rel_file))
            for stmt in read_import_block(abs_file):
                if stmt not in seen:
                    seen.add(stmt)
                    lines.append(stmt)
        return "\n".join(lines) if len(lines) > 1 else ""

    def _bodies(self, run):
        # Group by (rel_file, class). Free functions land under class None.
        # dict insertion order keeps groups in first-appearance order.
        groups = {}
        for fr in run.functions:
            groups.setdefault((fr.rel_file, fr.class_name), []).append(fr)

        out = []
        for (rel_file, class_name), refs in groups.items():
            block = self._class_block(rel_file, class_name, refs) if class_name else self._free_block(refs)
            if block:
                out.append(block)
        return "\n\n".join(out)

    @staticmethod
    def _banner(fr):
        # Line number is as-of-capture -- a point-in-time snapshot, not a live index.
        return "# %s:%d  ·  %s" % (fr.rel_file, fr.lineno, fr.qualname)

    def _free_block(self, refs):
        parts = []
        for fr in refs:
            src = extract_function_source(fr.abs_file, fr.lineno)
            parts.append("%s\n%s" % (self._banner(fr), src or "# <source not found>"))
        return "\n\n".join(parts)

    def _class_block(self, rel_file, class_name, refs):
        # Stub-class wrapper: keeps `self` meaningful, namespaces method names so
        # same-named methods across classes can't collide, and mirrors the real
        # structure. Indenting one level under `class X:` stays valid because the
        # extracted body is already deeper than its dedented `def` line.
        parts = ["class %s:  # %s" % (class_name, rel_file)]
        for fr in refs:
            src = extract_function_source(fr.abs_file, fr.lineno)
            body = src or ("def %s(self, *a, **k): ...  # <source not found>" % fr.short_name)
            parts.append(textwrap.indent("%s\n%s" % (self._banner(fr), body), "    "))
        return "\n\n".join(parts)


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)