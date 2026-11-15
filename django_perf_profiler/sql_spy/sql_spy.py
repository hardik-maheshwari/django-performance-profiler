import datetime
import os
import textwrap
import time
import inspect
import logging
import re
from contextlib import contextmanager
import uuid
from django.db import connection, connections, DatabaseError, transaction
from django.apps import apps
import sqlparse
from sqlparse.sql import IdentifierList, Identifier, Token
from sqlparse.tokens import Keyword, Name, Wildcard
import threading
from ..core import active_tracer, context_propagation
from . import orm_xray
from . import drf_xray

logger = logging.getLogger(__name__)
HAS_SQLPARSE = True

# --- 1. THE SCHEMA CACHE ---
_TABLE_SCHEMA_CACHE = None


def get_table_schema_cache():
    global _TABLE_SCHEMA_CACHE
    if _TABLE_SCHEMA_CACHE is not None:
        return _TABLE_SCHEMA_CACHE

    _TABLE_SCHEMA_CACHE = {}
    for model in apps.get_models():
        table_name = model._meta.db_table
        columns = {field.column for field in model._meta.concrete_fields}

        # NEW: Store both the columns and the clean, human-readable name
        _TABLE_SCHEMA_CACHE[table_name] = {
            "columns": columns,
            "human_name": model._meta.model_name,
        }

    return _TABLE_SCHEMA_CACHE


def interpolate_params(raw_sql, params):
    """Safely injects truncated parameters into the SQL string for display."""
    if not params:
        return raw_sql

    safe_params = []
    for p in params:
        # 1. Handle Strings, Bytes, UUIDs, AND Datetimes/Dates!
        if isinstance(p, (str, bytes, uuid.UUID, datetime.date, datetime.datetime, datetime.time)):
            # Cast to string safely
            val = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
            if len(val) > 100:
                val = val[:100] + "... [TRUNCATED]"
            safe_params.append(f"'{val}'")  # Wrap in quotes for SQL readability

        # 2. Handle Nulls
        elif p is None:
            safe_params.append("NULL")

        # 3. Handle Numbers/Booleans/etc
        else:
            safe_params.append(str(p))

    try:
        return raw_sql % tuple(safe_params)
    except (TypeError, ValueError):
        return raw_sql + f"\n    /* Raw Params: {safe_params} */"


def format_sql_for_humans(raw_sql):
    """
    Step 2a: Basic De-noising, Semantic Indentation, and Smart Wrapping.
    """
    if not HAS_SQLPARSE:
        return raw_sql[:2000]

    try:
        # 1. Parse & Format via AST
        formatted_sql = sqlparse.format(
            raw_sql,
            reindent=True,
        )

        # 2. Compress vertically first (removes the 60-line stack)
        compressed_sql = re.sub(r",\n\s+", ", ", formatted_sql)

        # 3. Smart Wrap horizontally (Prevents the 4,000-character line)
        wrapped_lines = []
        for line in compressed_sql.splitlines():
            if len(line) > 100:
                # Capture the AST's exact leading indentation
                indent = " " * (len(line) - len(line.lstrip()))

                # Wrap the text at 100 chars, pushing overflow to the next line with a slight indent
                wrapped = textwrap.fill(
                    line.strip(),
                    width=100,
                    initial_indent=indent,
                    subsequent_indent=indent + "    ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                wrapped_lines.append(wrapped)
            else:
                wrapped_lines.append(line)

        # 4. Add visual indentation for the terminal log
        return "\n".join(f"  {line}" for line in wrapped_lines)

    except Exception as e:
        return f"  /* Formatting failed: {e} */\n  {raw_sql[:2000]}"


def process_ast_readability(raw_sql):
    """
    Step 3 & 5: Safely translates aliases and applies the Binary SELECT rule per table.
    """
    if not HAS_SQLPARSE or not raw_sql.lstrip().upper().startswith("SELECT"):
        return raw_sql

    try:
        clean_sql = raw_sql.replace('"', "")
        stmt = sqlparse.parse(clean_sql)[0]
        schema_cache = get_table_schema_cache()

        alias_map = {}
        human_name_counts = {}
        human_to_cols = {}

        import sqlparse.sql as sql
        from sqlparse.tokens import Name, Whitespace, Wildcard

        def traverse_and_mutate(group, current_alias_map):
            # First, find and mutate table declarations in this exact scope
            for t in group.tokens:
                if isinstance(t, sql.Identifier):
                    real_name = t.get_real_name()
                    parent = t.get_parent_name()

                    if real_name in schema_cache and parent is None:
                        base_human = schema_cache[real_name]["human_name"]
                        alias = t.get_alias()

                        if base_human not in human_name_counts:
                            human_name_counts[base_human] = 1
                            final_human = base_human
                        else:
                            # Only increment if it's a completely new alias for this table
                            if alias and alias not in current_alias_map:
                                human_name_counts[base_human] += 1
                                final_human = f"{base_human}_{human_name_counts[base_human]}"
                            else:
                                # Strip quotes here so final_human doesn't end up double-quoted
                                final_human = current_alias_map.get(alias, base_human).strip('"')

                        quoted_human = f'"{final_human}"'

                        # Register the mapping in the LOCAL scope
                        if alias:
                            current_alias_map[alias] = final_human
                        if real_name not in current_alias_map:
                            current_alias_map[real_name] = final_human
                        human_to_cols[final_human] = schema_cache[real_name]["columns"]

                        # Mutate the declaration to "real_table AS human_alias"
                        t.tokens = [
                            sql.Token(Name, f'"{real_name}"'),
                            sql.Token(Whitespace, " "),
                            sql.Token(Keyword, "AS"),
                            sql.Token(Whitespace, " "),
                            sql.Token(Name, quoted_human),
                        ]
                        t._is_table_decl = True

            # Next, rename standard usages, and recurse into sub-groups
            for t in group.tokens:
                if getattr(t, "_is_table_decl", False):
                    continue

                if t.ttype in Name and t.value in current_alias_map:
                    clean_alias = current_alias_map[t.value].strip('"')
                    t.value = f'"{clean_alias}"'
                elif t.is_group:
                    # 🚀 THE SCOPE FIX: If we enter a subquery, clone the dictionary
                    # so inner aliases (U0, U1) don't leak out or corrupt siblings!
                    if isinstance(t, sql.Parenthesis):
                        traverse_and_mutate(t, current_alias_map.copy())
                    else:
                        traverse_and_mutate(t, current_alias_map)

        # Kick off the traversal with an empty global scope
        traverse_and_mutate(stmt, {})

        for t in stmt.tokens:
            if isinstance(t, sql.IdentifierList):
                alias_to_cols = {}

                for idx, tok in enumerate(t.tokens):
                    if isinstance(tok, sql.Identifier) and not isinstance(tok, sql.Function):
                        parent = tok.get_parent_name()
                        col = tok.get_real_name()
                        if parent and col:
                            if parent not in alias_to_cols:
                                alias_to_cols[parent] = []
                            alias_to_cols[parent].append((idx, col))

                for parent, cols_info in alias_to_cols.items():
                    if parent in human_to_cols:
                        schema_cols = human_to_cols[parent]
                        queried_cols = {c[1] for c in cols_info}

                        if schema_cols.issubset(queried_cols):
                            # 1. COMPLETELY REPLACE the first TokenList with a new Wildcard Token
                            first_idx = cols_info[0][0]
                            t.tokens[first_idx] = sql.Token(Wildcard, f'"{parent}".*')

                            # 2. COMPLETELY REPLACE the rest of the columns with Whitespace Tokens
                            for idx, col in cols_info[1:]:
                                t.tokens[idx] = sql.Token(Whitespace, "")

                                # Look backwards to erase the preceding comma
                                c_idx = idx - 1
                                while c_idx > 0:
                                    if t.tokens[c_idx].value == ",":
                                        t.tokens[c_idx] = sql.Token(Whitespace, "")
                                        break
                                    c_idx -= 1

        final_sql = str(stmt)

        # 1. Remove trailing spaces BEFORE commas (e.g., "book.* , " -> "book.*, ")
        final_sql = re.sub(r"\s+,", ",", final_sql)

        # 2. Collapse multiple empty commas (e.g., "a,,,,b" -> "a,b")
        final_sql = re.sub(r",+", ",", final_sql)

        # 3. Strip dangling comma directly after SELECT
        final_sql = re.sub(r"SELECT\s+,", "SELECT ", final_sql, flags=re.IGNORECASE)

        return final_sql

    except Exception as e:
        return raw_sql


def verify_query_logic(original_sql, mutated_sql):
    """
    Executes EXPLAIN (FORMAT JSON) on both queries securely.
    Verifies that the relational algebra (the execution tree structure)
    is perfectly identical, ignoring intentional alias renaming.
    """
    if not original_sql.lstrip().upper().startswith("SELECT"):
        return True

    explain_prefix = "EXPLAIN (FORMAT JSON) "

    # 1. Protect the Django connection from aborted transaction poisoning
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Fetch Original Plan
                cursor.execute(explain_prefix + original_sql)
                original_plan = cursor.fetchone()[0][0]["Plan"]

                # Fetch Mutated Plan
                cursor.execute(explain_prefix + mutated_sql)
                mutated_plan = cursor.fetchone()[0][0]["Plan"]

                # 2. Extract ONLY the logical operation tree (e.g., ['Limit', 'Sort', 'Hash Join', 'Seq Scan'])
                def extract_node_types(node):
                    types = [node.get("Node Type")]
                    for child in node.get("Plans", []):
                        types.extend(extract_node_types(child))
                    return types

                orig_nodes = extract_node_types(original_plan)
                mut_nodes = extract_node_types(mutated_plan)

                # 3. If the sequence of DB operations matches perfectly, our syntactic sugar is safe.
                return orig_nodes == mut_nodes

    except DatabaseError:
        # The AST mutator generated invalid Postgres syntax (e.g., swallowed a comma).
        return False
    except Exception:
        # Catch-all for any other parsing anomalies
        return False


import contextvars
from django.db.backends.utils import CursorWrapper

_spy_state = contextvars.ContextVar("sql_spy_state", default=None)

# ==========================================
# 1. THREAD POOLS: carry the spy state into pool workers
# (ThreadPoolExecutor + multiprocessing ThreadPool; see core.context_propagation)
# ==========================================
context_propagation.install()


# ==========================================
# 2. THE DATABASE CURSOR INTERCEPTOR
# ==========================================
_original_execute = CursorWrapper.execute
_original_executemany = CursorWrapper.executemany


def _record_query(state, sql, params, duration, alias="default"):
    active_tracer.note_sql(duration)  # FUSION: attribute to the dynamic tracer's
    # current frame if one is active; no-op otherwise.
    trigger = orm_xray.consume_pending_access()
    # Stale-stamp guard: a lazy reverse-FK/m2m stamp lingers until *some* query
    # fires (a prefetched `.all()` that hits cache fires none). If this SQL
    # doesn't touch the relation's table, the stamp is stale — drop it rather
    # than mislabel an unrelated query.
    if trigger is not None and not orm_xray.targets_sql(trigger, str(sql)):
        trigger = None
    kind = orm_xray.classify(trigger)
    ser = drf_xray.current_context()
    pid = orm_xray.first_id(params)
    query_hash = str(sql)

    query_hash = re.sub(
        r"IN\s*\(\s*(%s\s*,\s*)*%s\s*\)", "IN (%s, ...)", query_hash, flags=re.IGNORECASE
    )
    query_hash = re.sub(r"(%s\s*,\s*)+%s", "%s, ...", query_hash)

    with state["lock"]:
        state["total_time"] += duration
        state["total_queries"] += 1

        if query_hash in state["tracked_queries"]:
            q = state["tracked_queries"][query_hash]
            q["count"] += 1
            q["time"] += duration
            q["distinct_ids"].add(pid)
            q["aliases"].add(alias)
            # Retain the first valid trigger we saw
            if q.get("kind") is None and kind:
                q["kind"], q["trigger"], q["ser"] = kind, trigger, ser
        else:
            # FUSION: if the dynamic tracer is live, take attribution straight from
            # its current frame (O(1), and it skips the inspect.stack walk entirely).
            loc = active_tracer.current_location()
            via_pool = False
            if loc is not None:
                caller_func, caller_file, caller_line = loc  # (qualname, rel_file, lineno)
            else:
                caller_file, caller_line, caller_func = "Unknown", 0, "Unknown"

                for frame_info in inspect.stack():
                    filename = frame_info.filename
                    func_name = frame_info.function

                    if func_name in [
                        "_patched_execute",
                        "_patched_executemany",
                        "_record_query",
                        "sql_spy",
                        "__enter__",
                        "__exit__",
                    ]:
                        continue
                    if any(
                        x in filename
                        for x in [
                            "django/db",
                            "django/core",
                            "contextlib",
                            "concurrent/futures",
                            "multiprocessing/pool",
                            "/threading",
                            "rest_framework",
                            "django_perf_profiler/",
                        ]
                    ):
                        continue

                    path_parts = filename.replace("\\", "/").split("/")
                    caller_file = (
                        f"{path_parts[-2]}/{path_parts[-1]}" if len(path_parts) >= 2 else filename
                    )
                    caller_line = frame_info.lineno
                    caller_func = func_name
                    break
                else:
                    # Pool worker with only library frames: use the submit site.
                    site = context_propagation.submit_site()
                    if site is not None:
                        caller_file, caller_line, caller_func = site
                        via_pool = True

            state["tracked_queries"][query_hash] = {
                "sql": str(sql),
                "count": 1,
                "time": duration,
                "file": caller_file,
                "line": caller_line,
                "func": caller_func,
                "via_pool": via_pool,
                "params": params,
                "trigger": trigger,
                "kind": kind,
                "ser": ser,
                "distinct_ids": {pid},
                "aliases": {alias},
            }


def _patched_execute(self, sql, params=None):
    state = _spy_state.get()
    if not state:
        return _original_execute(self, sql, params)

    start = time.perf_counter()
    try:
        return _original_execute(self, sql, params)
    finally:
        _record_query(state, sql, params, time.perf_counter() - start, alias=self.db.alias)


def _patched_executemany(self, sql, param_list):
    state = _spy_state.get()
    if not state:
        return _original_executemany(self, sql, param_list)

    start = time.perf_counter()
    try:
        return _original_executemany(self, sql, param_list)
    finally:
        _record_query(state, sql, param_list, time.perf_counter() - start, alias=self.db.alias)


CursorWrapper.execute = _patched_execute
CursorWrapper.executemany = _patched_executemany


# ==========================================
# 2b. SERVER-SIDE TIMING (remote databases ONLY)
# ==========================================
# Client-side perf_counter times include the network round trip plus the
# profiler's own overhead. Against a local/loopback Postgres (or a docker
# service, conventionally named 'postgres') that tax is negligible, so client
# times are honest and nothing below runs. Against a REMOTE database (e.g. a
# realistic-size copy in a cloud region far from you) the round trip can dwarf
# the query itself, so we also read Postgres's OWN execution time from
# pg_stat_statements, which is network- and profiler-free.
# Extend the local set with PROFILER_LOCAL_DB_HOSTS=db,pg-dev (comma-separated).
LOCAL_DB_HOSTS = {"postgres", "localhost", "127.0.0.1", ""} | {
    h.strip() for h in os.environ.get("PROFILER_LOCAL_DB_HOSTS", "").split(",") if h.strip()
}


def _server_timing_active(conn):
    # Fail toward server-side truth: an unrecognised host is treated as remote.
    return conn.settings_dict.get("HOST") not in LOCAL_DB_HOSTS


def _pgss_reset(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_stat_statements_reset()")
    except Exception:
        pass  # extension absent / no priv -> _pgss_report will announce the fallback


def _pgss_report(conn):
    # pg_stat_statements renamed total_time->total_exec_time, mean_time->mean_exec_time in
    # 1.8 (PG13). Each attempt in its
    # own savepoint so a failed column ref doesn't poison the connection's transaction.
    q = (
        "SELECT calls, {m}, {t}, query FROM pg_stat_statements "
        "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "AND query NOT LIKE '%pg_stat_statements%' ORDER BY {t} DESC LIMIT 15"
    )
    rows = last = None
    for m, t in (("mean_exec_time", "total_exec_time"), ("mean_time", "total_time")):
        try:
            with transaction.atomic(using=conn.alias):
                with conn.cursor() as cur:
                    cur.execute(q.format(m=m, t=t))
                    rows = cur.fetchall()
            break
        except Exception as e:
            last = e
    if rows is None:
        logger.info(
            f"--- [server-side] pg_stat_statements read failed ({type(last).__name__}: "
            f"{str(last)[:160]}); wall times above are NETWORK-TAXED -- do not trust magnitude ---\n"
        )
        return
    logger.info(
        "\n--- SERVER-SIDE (network-free, pg_stat_statements) -- TRUST THESE for a remote DB, "
        "not the wall times above ---"
    )
    for calls, mean_ms, total_ms, query in rows:
        logger.info(
            f"  {total_ms:9.1f}ms total | {mean_ms:8.3f}ms mean x{calls:<6} | {query[:120]}"
        )
    logger.info("------------------------------------------------\n")


# ==========================================
# 3. THE CONTEXT MANAGER
# ==========================================
@contextmanager
def sql_spy(label="SQL Spy", trace=False, trace_strategy=None):
    state = {"tracked_queries": {}, "total_time": 0.0, "total_queries": 0, "lock": threading.Lock()}

    orm_xray.install()
    drf_xray.install()

    token = _spy_state.set(state)
    orm_xray.set_active(True)
    drf_xray.set_active(True)

    if trace_strategy is not None:
        trace = True

    if trace:
        from .build_xray import build_trace

    srv = _server_timing_active(connection)  # False on local -> everything below is a no-op
    if srv:
        _pgss_reset(connection)

    wall_start = time.perf_counter()
    try:
        if trace:
            with build_trace(trace_strategy):
                yield state  # trace_capture reads this for the drift fingerprint
        else:
            yield state
    finally:
        wall_time = time.perf_counter() - wall_start
        orm_xray.set_active(False)
        drf_xray.set_active(False)
        _spy_state.reset(token)

        sql_time = state["total_time"]
        # total_time is the SUM of per-query durations. Under concurrency queries
        # overlap, so this can exceed wall_time — when it does, that's parallelism,
        # not a bad measurement. wall_time is server-side dispatch only (not the
        # browser's end-to-end), but it tells you whether SQL is even the problem.
        if wall_time > 0:
            pct = sql_time / wall_time * 100
            overlap = " — queries overlapped (concurrent)" if sql_time > wall_time else ""
            sql_summary = f"SQL (summed): {sql_time:.4f}s ({pct:.0f}% of wall{overlap})"
        else:
            sql_summary = f"SQL (summed): {sql_time:.4f}s"

        logger.info(f"\n--- [{label}] Wall time: {wall_time:.4f}s | {sql_summary} ---")
        logger.info(f"--- [{label}] Total Queries: {state['total_queries']} ---")
        logger.info(f"--- [{label}] Unique Query Structures: {len(state['tracked_queries'])} ---\n")

        sorted_queries = sorted(state["tracked_queries"].values(), key=lambda x: x["count"])
        n_struct = len(state["tracked_queries"]) or 1
        hog_cut = max(0.30, 2.0 / n_struct)

        for i, q in enumerate(sorted_queries):
            alert = "🚨 N+1 DETECTED " if q["count"] > 2 else ""
            share = (q["time"] / sql_time) if sql_time else 0.0

            if q["time"] >= 0.05 and share >= hog_cut:
                alert += f"🐖 TIME HOG {share * 100:.0f}% of SQL "

            interpolated_sql = interpolate_params(q["sql"], q["params"])
            reduced_sql = process_ast_readability(interpolated_sql)

            if not verify_query_logic(interpolated_sql, reduced_sql):
                alert += "⚠️ [AST PARSE FAILED - RAW FALLBACK] "
                reduced_sql = interpolated_sql

            if q["count"] > 1:
                reduced_sql += "\n/* Note: Parameters shown are from the FIRST execution */"

            sql_preview = format_sql_for_humans(reduced_sql)

            aliases = q.get("aliases") or {"default"}
            db_tag = "/".join(sorted(aliases))
            if len(aliases) > 1:
                db_tag = f"⚠️ MIXED: {db_tag}"
            db_tag = f"{db_tag} ({'remote' if srv else 'local'})"  # srv = non-local host

            logger.info(
                f"[{i + 1}] {alert}🎯 {q['file']}:{q['line']} in {q['func']}(){' via thread pool' if q.get('via_pool') else ''} | 🔄 Count: {q['count']} | ⏱️ {q['time']:.4f}s | 🗄️ db={db_tag}"
            )
            if q.get("kind"):
                n = len(q["distinct_ids"])
                ser = q.get("ser") or {}
                where = ""
                if ser.get("phase") == "serialization":
                    where = f" during {ser.get('serializer')}.{ser.get('field')}"
                    if ser.get("page_size"):
                        where += f" (page={ser['page_size']})"

                tname = (q["trigger"] or {}).get("name", "?")
                if q["count"] > 2:
                    # spread + fix_hint share the `count >= 2*n` threshold — keep in sync.
                    if n <= 1:
                        spread = "same id -> recompute"
                    elif q["count"] >= 2 * n:
                        spread = "recompute-dominated"
                    else:
                        spread = "fan-out"
                    logger.info(
                        f"      ↳ {q['kind']} on '{tname}'{where} | {n} distinct ({spread})"
                    )
                    logger.info(f"        fix: {orm_xray.fix_hint(q['kind'], n, q['count'])}")

            logger.info(f"    {sql_preview}")

        if srv:
            # Report on a connection that actually served queries: `default` may have been
            # dropped while idle during a long replica query. pgss is instance-wide, so any
            # live connection to the same instance sees the reset + recorded stats.
            used = {a for q in state["tracked_queries"].values() for a in (q.get("aliases") or ())}
            _pgss_report(connections[next(iter(used))] if used else connection)

        logger.info("------------------------------------------------\n")