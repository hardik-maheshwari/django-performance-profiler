import ast
import os
import re
import sys

from ..core import boundaries, keys
from .config import (
    MAX_DEPTH,
    OUTPUT_FILE,
    GLOBAL_BLIND_SPOTS,
    NOISE_CLASSES,
    GLOBAL_SCHEMA_STUBS,
)
from .visitors import FileScanner, FunctionDetailsVisitor
from .inspector import resolve_blind_spots


visited_symbols = set()
GLOBAL_CLASS_INDEX = {}
GLOBAL_INDEX_BUILT = False


def _is_first_party(path):
    """Crawl only first-party code: the same core.boundaries rule the tracer and
    spies use (under PROJECT_ROOT, not a venv / site-packages / migrations)."""
    return boundaries.classify(os.path.abspath(str(path))) == boundaries.LOCAL


def find_class_globally(class_name):
    """
    Lightning-fast regex scanner that maps every class to its file path.
    Runs only once per crawl.
    """
    global GLOBAL_INDEX_BUILT
    if not GLOBAL_INDEX_BUILT:
        # Regex to find 'class ClassName'
        class_regex = re.compile(r"^[ \t]*class\s+([a-zA-Z0-9_]+)\b", re.MULTILINE)

        for root, dirs, files in os.walk(boundaries.PROJECT_ROOT):
            # Ignore hidden folders, virtual envs, and migrations to keep it instant
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("migrations", "venv", "env", "node_modules", "__pycache__")
            ]

            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            content = f.read()
                            for match in class_regex.finditer(content):
                                found_class = match.group(1)
                                if found_class not in GLOBAL_CLASS_INDEX:
                                    GLOBAL_CLASS_INDEX[found_class] = full_path
                    except Exception:
                        pass
        GLOBAL_INDEX_BUILT = True

    return GLOBAL_CLASS_INDEX.get(class_name)


def trace_inheritance_chain(
    base_class_name,
    context_file_path,
    method_name,
    context_scanner,
    depth,
    prefix,
    branch_max_depth,
    deep_dive_config,
):
    """
    Recursively hunts for 'method_name' in the hierarchy of 'base_class_name'.
    Returns True if the method was found and crawled.
    """
    # 1. Resolve where 'base_class_name' lives relative to the current file
    target_path = None
    target_class_real_name = base_class_name

    # Check Imports in the context file
    if base_class_name in context_scanner.import_map:
        path, original_name = context_scanner.import_map[base_class_name]
        target_path = path
        if original_name:
            target_class_real_name = original_name
    # Check if defined in the same file (Sibling class)
    elif base_class_name in context_scanner.definition_map:
        target_path = context_file_path

    if not target_path:
        return False

    # 2. First-party only: never crawl into Django or other libraries
    if not _is_first_party(target_path):
        return False

    # 3. Inspect the Parent File
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            source = f.read()
            tree = ast.parse(source)

        # We need a scanner for this new file to check ITS bases/methods
        parent_scanner = FileScanner(target_path)
        parent_scanner.visit(tree)

        if target_class_real_name in parent_scanner.class_metadata:
            meta = parent_scanner.class_metadata[target_class_real_name]

            # A. Found the method explicitly defined here?
            if method_name in meta["methods"]:
                symbol = f"{target_class_real_name}.{method_name}"
                crawl(
                    target_path,
                    symbol,
                    depth,
                    prefix,
                    branch_max_depth,
                    deep_dive_config,
                )
                return True

            # B. Not found? Recurse up to Grandparents.
            for grand_base in meta["bases"]:
                # RECURSIVE STEP
                # We now search relative to 'target_path' (the parent file) using 'parent_scanner'
                found = trace_inheritance_chain(
                    grand_base,
                    target_path,
                    method_name,
                    parent_scanner,
                    depth,
                    prefix,
                    branch_max_depth,
                    deep_dive_config,
                )
                if found:
                    return True

    except Exception:
        pass

    return False


def crawl(
    file_path,
    symbol_name,
    depth,
    parent_prefix="",
    branch_max_depth=None,
    deep_dive_config=None,
):
    # --- 1. Basic Setup ---

    base_symbol = symbol_name.split(".")[-1]
    if base_symbol in NOISE_CLASSES:
        return

    if deep_dive_config is None:
        deep_dive_config = {}

    if branch_max_depth is None:
        branch_max_depth = MAX_DEPTH

    # 3. Dynamic Zoom Override: If this symbol is in our config, expand the budget!
    # Use split(".")[-1] so you can target 'random_function' or 'Order.random_function'
    if symbol_name in deep_dive_config:
        branch_max_depth = deep_dive_config[symbol_name]
    elif symbol_name.split(".")[-1] in deep_dive_config:
        branch_max_depth = deep_dive_config[symbol_name.split(".")[-1]]

    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return
    rel_path = keys.rel_to_root(abs_path)

    if depth > branch_max_depth:
        return
    ident = (abs_path, symbol_name)
    if ident in visited_symbols:
        is_class = symbol_name.split(".")[-1][0].isupper()

        if is_class:
            return  # Silent exit for repeated classes

        else:
            print(f"{parent_prefix}├── [{depth}] {symbol_name} [Already Visited ♻️]")
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n# === DEPTH {depth} | FILE: {rel_path} | SYMBOL: {symbol_name} ===\n")
                f.write(
                    f"# [Already Visited - Source code omitted to prevent cyclic duplication]\n"
                )
            return
    visited_symbols.add(ident)

    # --- 2. Parse Source ---
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            source_text = f.read()
            tree = ast.parse(source_text)
    except Exception as e:
        print(f"{parent_prefix} [!] Error parsing {rel_path}: {e}")
        return

    # --- 3. Extract Details (Source Code + Local Imports + Usages) ---
    detail_visitor = FunctionDetailsVisitor(symbol_name, source_text, abs_path)
    detail_visitor.visit(tree)

    if not detail_visitor.found:
        # Fallback: It might be an inherited method we are trying to resolve directly
        # If we can't find it, we just return.
        return

    # --- 5. Scan File for Imports & Class Metadata ---
    scanner = FileScanner(abs_path)
    scanner.visit(tree)

    # --- 4. Output to File ---
    if detail_visitor.target_class:
        # Don't duplicate if the user passed 'Class.method' via CLI
        if not symbol_name.startswith(f"{detail_visitor.target_class}."):
            display_name = f"{detail_visitor.target_class}.{symbol_name}"
        else:
            display_name = symbol_name
    else:
        display_name = symbol_name

    if symbol_name in scanner.class_metadata:
        if depth > 0:
            # Sub-dependency: Shallow Stub only!
            print(
                f"{parent_prefix}│   [Shallow Read: Captured schema stub for Class '{symbol_name}'. Preventing fan-out.]"
            )
            GLOBAL_SCHEMA_STUBS.add(symbol_name)
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n# === DEPTH {depth} | FILE: {rel_path} | SYMBOL: {symbol_name} ===\n")
                f.write(
                    f"# [Class Reference] {symbol_name} (Source code omitted to save context window)\n"
                )
            return
        else:
            # Root target (depth 0): write the class, then crawl each method
            with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n# === DEPTH {depth} | FILE: {rel_path} | SYMBOL: {symbol_name} ===\n")
                f.write("```python\n")
                f.write(detail_visitor.source_code or "# Source not found")
                f.write("\n```\n")

            # Spawn a crawl for every method in the class
            class_info = scanner.class_metadata[symbol_name]
            for method in sorted(class_info["methods"]):
                method_symbol = f"{symbol_name}.{method}"
                crawl(
                    abs_path,
                    method_symbol,
                    depth + 1,
                    parent_prefix + "│   ",
                    branch_max_depth,
                    deep_dive_config,
                )
            return

    print(f"{parent_prefix}├── [{depth}] {display_name} ({rel_path})")

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n# === DEPTH {depth} | FILE: {rel_path} | SYMBOL: {symbol_name} ===\n")

        f.write("```python\n")
        f.write(detail_visitor.source_code or "# Source not found")
        f.write("\n```\n")

    # --- 6. Helper for Resolving Dependencies ---
    def resolve_and_crawl(name, current_depth):
        # 6.1 Check Local Imports (Inside Function)
        if name in detail_visitor.local_imports:
            dep_path, dep_symbol = detail_visitor.local_imports[name]
            if _is_first_party(dep_path):
                crawl(
                    dep_path,
                    dep_symbol or name,
                    current_depth,
                    parent_prefix + "│   ",
                    branch_max_depth,
                    deep_dive_config,
                )
                return True

        # 6.2 Check Global Imports (Top of File)
        elif name in scanner.import_map:
            dep_path, dep_symbol = scanner.import_map[name]
            if _is_first_party(dep_path):
                crawl(
                    dep_path,
                    dep_symbol or name,
                    current_depth,
                    parent_prefix + "│   ",
                    branch_max_depth,
                    deep_dive_config,
                )
                return True

        # 6.3 Check Sibling Definitions (Same File)
        # Note: If it's a class method, we handle it in OOP section below,
        # unless it's a static utility in the same file.
        elif name in scanner.definition_map and name != symbol_name:
            crawl(
                abs_path,
                name,
                current_depth,
                parent_prefix + "│   ",
                branch_max_depth,
                deep_dive_config,
            )
            return True

        return False

    for used_var in sorted(detail_visitor.used_names):
        resolve_and_crawl(used_var, depth + 1)

    # --- 7. Resolve OOP Dependencies (Class Members) ---
    current_class = detail_visitor.target_class

    # 7b. Iterate over ALL captured member uses (Strings = self.X, Tuples = var.X)
    for member in sorted(detail_visitor.class_member_uses, key=lambda x: str(x)):
        # --- CASE 1: Implicit 'self.method' (String) ---
        if isinstance(member, str):
            if current_class and current_class in scanner.class_metadata:
                class_info = scanner.class_metadata[current_class]

                # Check Local Methods
                if member in class_info["methods"]:
                    normalized_name = f"{current_class}.{member}"
                    crawl(
                        abs_path,
                        normalized_name,
                        depth + 1,
                        parent_prefix + "│   ",
                        branch_max_depth,
                        deep_dive_config,
                    )

                # Check Inherited Methods
                else:
                    found_in_chain = False
                    for base_name in class_info["bases"]:
                        if trace_inheritance_chain(
                            base_name,
                            abs_path,
                            member,
                            scanner,
                            depth + 1,
                            parent_prefix + f"│   [Base: {base_name}] ",
                            branch_max_depth,
                            deep_dive_config,
                        ):
                            found_in_chain = True
                            break  # Found it in the hierarchy

                    if not found_in_chain:
                        # Fallback: If we couldn't find the method definition in any local parent,
                        # we assume it might be on the immediate base but we failed to parse it.
                        # We just show the base class itself as a hint.
                        for base_name in class_info["bases"]:
                            path = None
                            if base_name in scanner.import_map:
                                path, _ = scanner.import_map[base_name]
                            elif base_name in scanner.definition_map:
                                path = abs_path

                            if path:
                                crawl(
                                    path,
                                    base_name,
                                    depth + 1,
                                    parent_prefix + "│   ",
                                    branch_max_depth,
                                    deep_dive_config,
                                )

        elif isinstance(member, tuple) and member[0] == "__SUPER__":
            _, method_name = member
            if current_class and current_class in scanner.class_metadata:
                class_info = scanner.class_metadata[current_class]

                # Skip the local check and go straight to the inherited methods
                found_in_chain = False
                for base_name in class_info["bases"]:
                    if trace_inheritance_chain(
                        base_name,
                        abs_path,
                        method_name,
                        scanner,
                        depth + 1,
                        parent_prefix + f"│   [Base: {base_name}] ",
                        branch_max_depth,
                        deep_dive_config,
                    ):
                        found_in_chain = True
                        break

        # --- CASE 2: Typed Object 'a.random_function' (Tuple: ('A', 'random_function')) ---
        elif isinstance(member, tuple):
            class_name, method_name = member
            dep_path = None

            # 2.1 Find where ClassName is defined (Local Import or Global Import)
            # Check Local Imports inside function first
            if class_name in detail_visitor.local_imports:
                dep_path, _ = detail_visitor.local_imports[class_name]
            # Check Global Imports
            elif class_name in scanner.import_map:
                dep_path, _ = scanner.import_map[class_name]
            # Check if defined in THIS file (Sibling Class)
            elif class_name in scanner.definition_map:
                dep_path = abs_path

            else:
                # Not imported here: fall back to a project-wide class index
                dep_path = find_class_globally(class_name)

            if not dep_path:
                continue

            # 2.2 Load Metadata from THAT file
            # We need to scan the dependency file to know its methods
            try:
                # Optimization: Reuse global index or parse on-demand
                with open(dep_path, "r", encoding="utf-8") as f:
                    dep_tree = ast.parse(f.read())
                    dep_scanner = FileScanner(dep_path)
                    dep_scanner.visit(dep_tree)

                if class_name in dep_scanner.class_metadata:
                    target_class_info = dep_scanner.class_metadata[class_name]

                    # Check Method in THAT Class
                    if method_name in target_class_info["methods"]:
                        normalized_name = f"{class_name}.{method_name}"
                        crawl(
                            dep_path,
                            normalized_name,
                            depth + 1,
                            parent_prefix + "│   ",
                            branch_max_depth,
                            deep_dive_config,
                        )

                    # Check Inherited Methods in THAT Class
                    else:
                        # We need to resolve the PARENT of 'A' (e.g., 'models.Model')
                        # Problem: 'models.Model' is imported in 'dep_path', not here.
                        # Recursion Trick: We crawl the PARENT CLASS itself to find the method.
                        for base in target_class_info["bases"]:
                            # Resolve base in the context of dep_path
                            # This is tricky without fully recursive resolution,
                            # but usually sufficient to just list the base class.
                            crawl(
                                dep_path,
                                base,
                                depth + 1,
                                parent_prefix + "│   ",
                                branch_max_depth,
                                deep_dive_config,
                            )
            except Exception:
                pass


def parse_include_args(include_str):
    """
    Parses strings like: 'path/to/file.py:SymbolName:Depth,another/file.py:OtherSymbol'
    Returns a list of (file_path, symbol_name, depth)
    """
    roots = []
    if not include_str:
        return roots

    parts = include_str.split(",")
    for part in parts:
        segments = part.split(":")
        if len(segments) >= 2:
            f_path = segments[0]
            s_name = segments[1]
            # Default to MAX_DEPTH if not specified in the include string
            d_val = int(segments[2]) if len(segments) > 2 else MAX_DEPTH
            roots.append((f_path, s_name, d_val))
    return roots


def emit_blindspots(blind_spots, out_path):
    """Agent mode: dump blind spots as JSON with the exact scoped_key precomputed,
    instead of the interactive wizard. Fill each "value", then --ingest-blindspots."""
    import json

    loc_re = re.compile(r"\[File:\s*(.*?)\s*\|\s*Func:\s*(.*?)\s*\]")
    grouped = {}
    for spot_key, locations in blind_spots.items():
        # spot_key is "base_name.attr"; base_name is a single ast.Name id (dot-free)
        # and attr is one segment -> rpartition splits them cleanly.
        var, _, attr = spot_key.rpartition(".")
        for loc in locations:
            m = loc_re.match(loc)
            if not m:
                continue
            rel_path, func = m.group(1), m.group(2)
            sk = keys.scoped_key(rel_path, func, var)  # SAME key the reader looks up
            rec = grouped.setdefault(
                sk,
                {"key": sk, "file": rel_path, "func": func, "var": var,
                 "attrs": set(), "locations": set(), "value": None},
            )
            if attr:
                rec["attrs"].add(attr)
            rec["locations"].add(loc)

    records = []
    for rec in grouped.values():
        rec["attrs"] = sorted(rec["attrs"])
        rec["locations"] = sorted(rec["locations"])
        records.append(rec)
    records.sort(key=lambda r: r["key"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    print(f"\n📝 Emitted {len(records)} blind-spot records -> {out_path}")
    print('   Fill "value": a Model name to map, "__BLACKLIST__" to ignore, null to defer.')
    print(f"   Then: python -m django_perf_profiler.static_life_saver.main --ingest-blindspots={out_path}")


def ingest_blindspots(in_path):
    """Write filled blind-spot JSON back through config's own writers, so the key
    and merge format are always the tool's (never hand-built)."""
    import json
    from . import config

    with open(in_path, encoding="utf-8") as f:
        records = json.load(f)
    mapped = blacklisted = skipped = 0
    for rec in records:
        val = rec.get("value")
        if val is None or val == "":
            skipped += 1
            continue
        rel, func, var = rec["file"], rec["func"], rec["var"]
        if str(val).strip().upper() in ("__BLACKLIST__", "__SKIP__", "BLACKLIST"):
            config.update_blacklist(rel, func, var)
            blacklisted += 1
        else:
            config.update_type_cache(rel, func, var, val)
            mapped += 1
    print(f"✅ Ingested {in_path}: {mapped} mapped, {blacklisted} blacklisted, {skipped} deferred.")


if __name__ == "__main__":
    sys.setrecursionlimit(5000)  # deep AST recursion on large call graphs

    ingest_path = next(
        (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--ingest-blindspots=")),
        None,
    )
    if ingest_path:
        ingest_blindspots(ingest_path)
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Usage: python -m django_perf_profiler.static_life_saver.main <file_path> <function_name>")
        sys.exit(1)

    primary_file = sys.argv[1]
    primary_func = sys.argv[2]

    extra_roots = []

    deep_dive_config = {}
    emit_path = None
    for arg in sys.argv[3:]:
        if arg.startswith("--include="):
            extra_roots = parse_include_args(arg.split("=")[1])
        if arg.startswith("--deep-dive="):
            # Example: --deep-dive=a:10,b:5
            pairs = arg.split("=")[1].split(",")
            for pair in pairs:
                if ":" in pair:
                    k, v = pair.split(":")
                    deep_dive_config[k] = int(v)
        if arg.startswith("--emit-blindspots="):
            emit_path = arg.split("=", 1)[1]

    # Clean the output file for a fresh run
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("# Multi-Root Dependency Trace\n")
        f.write(f"# Primary Root: {primary_func}\n")
        if extra_roots:
            f.write(f"# Included Roots: {', '.join([r[1] for r in extra_roots])}\n")
        f.write(f"# Config: Max Depth={MAX_DEPTH}\n\n")

    print(f"🚀 Starting Primary Trace: {primary_func}")
    crawl(primary_file, primary_func, 0, deep_dive_config=deep_dive_config)

    for f_path, s_name, d_limit in extra_roots:
        print(f"\n🌿 Starting Included Trace: {s_name} (Max Depth: {d_limit})")
        # Note: crawl() uses the global visited_symbols,
        # so it will skip anything already found in the primary trace!
        crawl(f_path, s_name, 0, branch_max_depth=d_limit, deep_dive_config=deep_dive_config)

    print(f"\n✅ Done. Context saved to {OUTPUT_FILE}")

    if GLOBAL_BLIND_SPOTS and emit_path:
        emit_blindspots(GLOBAL_BLIND_SPOTS, emit_path)
    elif GLOBAL_BLIND_SPOTS:
        print("\n" + "=" * 10)
        print("⚠️ BLIND SPOTS DETECTED (Missing Type Inference)")
        print("=" * 10)
        for spot in sorted(GLOBAL_BLIND_SPOTS.keys()):
            print(f"  -> {spot}")
            for loc in sorted(GLOBAL_BLIND_SPOTS[spot]):
                print(f"       📍 {loc}")
        print("=" * 10)

        # Resolve them interactively (ORM inspection + runtime priors)
        resolve_blind_spots(GLOBAL_BLIND_SPOTS)

    # emit mode is headless; skip the shell-backed schema-stub inspection too
    if GLOBAL_SCHEMA_STUBS and not emit_path:
        from .inspector import inspect_schema_stubs

        inspect_schema_stubs(GLOBAL_SCHEMA_STUBS)