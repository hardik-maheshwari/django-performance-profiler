"""
trace_capture.sources
---------------------
AST-based source & import extraction for the source-annotated (.py) strategy.

Mirrors the technique static_life_saver's visitors rely on: parse the file
once, locate the node, slice the exact segment with ast.get_source_segment. Because the slice starts at the node's
col_offset, a method comes out with its `def` dedented to column 0 while the
body keeps its (deeper) indentation, so the segment parses standalone -- no
manual textwrap.dedent, no read-file-and-guess.

Deliberately NOT importing static_life_saver.FileScanner: that drags in
static_life_saver.config, which loads the type cache + harvested sidecar at
import time. We only need two stdlib-ast primitives, so we keep them local.
"""

import ast
import functools


@functools.lru_cache(maxsize=None)
def _parse(abs_path):
    """Parse a file once, cache by path. Returns (source_text, tree) or (None, None).

    Cache is per-process, so it is refreshed on every branch switch (a branch
    switch is a new `python manage.py shell` process). Within one process the
    source can't change under us, so caching is safe and saves re-parsing a
    file once per method we pull out of it.
    """
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()
        return source, ast.parse(source)
    except Exception:
        return None, None


def extract_function_source(abs_path, lineno):
    """Exact source of the def whose header spans `lineno` (a co_firstlineno).

    Located by LINE, not by name: co_firstlineno points at the real definition
    site regardless of the runtime class the tracer stamped on the qualname, so
    this survives inherited / relocated methods that a name search would miss.

    Decorator gotcha: across Python versions co_firstlineno lands on either the
    first decorator or the `def`, and FunctionDef.lineno moved from the decorator
    (<3.8) to the `def` (>=3.8). We match if `lineno` falls anywhere in the
    header range [first-decorator .. def], which covers every convention without
    ambiguity (two functions' headers never overlap). The returned segment
    starts at the `def` (decorators dropped) -- fine for a reading aid.
    """
    source, tree = _parse(abs_path)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header_start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if header_start <= lineno <= node.lineno:
                return ast.get_source_segment(source, node)
    return None


def read_import_block(abs_path):
    """Verbatim text of every MODULE-LEVEL import statement, in file order.

    The "cheap header": these are exactly the names the file's functions bind
    to, so pasting them lets Pylance resolve (and hover) the imported symbols.
    Unused ones ride along -> a cosmetic "unused import" squiggle, never a
    resolution failure. We do NOT chase module-level constants or sibling
    helpers -- that unbounded recursion is static_life_saver's job, out of scope
    for a debug artifact.
    """
    source, tree = _parse(abs_path)
    if tree is None:
        return []
    lines = []
    for node in tree.body:  # module scope only -- ignore in-function imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(source, node)
            if seg:
                lines.append(seg)
    return lines