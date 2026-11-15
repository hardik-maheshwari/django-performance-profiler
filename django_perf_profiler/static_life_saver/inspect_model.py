import inspect
from django.db import models
from django.db.models import signals
from django.contrib.contenttypes.fields import GenericForeignKey
import io
import sys
import base64
import functools
from contextlib import redirect_stdout


def get_defining_class(model_class, method_name):
    """
    Finds which class in the MRO actually defined the method.
    """
    if not hasattr(model_class, method_name):
        return None
    method = getattr(model_class, method_name)
    if not (inspect.isfunction(method) or inspect.ismethod(method)):
        return None
    for cls in inspect.getmro(model_class):
        if method_name in cls.__dict__:
            if cls in (models.Model, object):
                return None
            return cls.__name__
    return None


def copy_to_clipboard(func):
    """Decorator: print the function's stdout as usual and, when the caller passes
    clipboard=True, also copy it to the clipboard via the OSC 52 terminal escape.

    OSC 52 is understood by most modern terminals (iTerm2, kitty, WezTerm,
    Windows Terminal, tmux with set-clipboard on) and works over SSH / inside
    containers, because the TERMINAL does the copy, not the remote host. Opt-in,
    since a terminal without OSC 52 support may print the escape as junk."""

    @functools.wraps(func)
    def wrapper(*args, clipboard=False, **kwargs):
        if not clipboard:
            return func(*args, **kwargs)

        f = io.StringIO()
        with redirect_stdout(f):
            func(*args, **kwargs)

        output = f.getvalue()
        print(output, end="")  # still print to the shell

        b64_output = base64.b64encode(output.encode("utf-8")).decode("utf-8")
        sys.stdout.write(f"\033]52;c;{b64_output}\a")  # terminal copies this to the clipboard
        sys.stdout.flush()

        print("\n📋 [copied to clipboard (OSC 52)]\n")

    return wrapper


# ------------------------------------------------------------------------------
# SECTIONS
# Each section is a self-contained inspector: takes a model_class and prints
# exactly its own block. They print (not return) so copy_to_clipboard can
# capture the whole report in one go.
# To add a new section: write a _section_* function and register it in SECTIONS.
# ------------------------------------------------------------------------------


def _is_es_registered(model_class):
    """True if this model is mirrored into ElasticSearch via django-elasticsearch-dsl."""
    try:
        from django_elasticsearch_dsl.registries import registry

        return (
            model_class in registry._models or model_class in registry._related_models
        )
    except Exception:
        return False


def _section_meta(model_class):
    """--- 1. META CONFIGURATION ---"""
    meta = model_class._meta
    print(f"⚙️ META CONFIGURATION")
    print(f"{'-' * 10}")
    warnings = []
    if meta.abstract:
        warnings.append("ABSTRACT Model")
    if meta.proxy:
        warnings.append(f"PROXY Model (Parent: {meta.concrete_model.__name__})")
    if meta.parents:
        # Concrete (multi-table) inheritance: every query silently JOINs the parent table(s).
        parent_names = ", ".join(p.__name__ for p in meta.parents)
        warnings.append(
            f"🔴 MULTI-TABLE INHERITANCE (Parents: {parent_names}) -> Silent JOIN on EVERY query"
        )

    if meta.swapped:
        warnings.append(f"SWAPPED Out (Active: {meta.swapped})")
    if meta.ordering:
        warnings.append(
            f"⚠️ DEFAULT ORDERING: {meta.ordering} (Adds 'ORDER BY' to queries)"
        )
    if meta.select_on_save:
        warnings.append("⚠️ SELECT_ON_SAVE: True (Doubles DB hits on save)")

    if getattr(meta, "managed", True) is False:
        warnings.append(
            "⚠️ UNMANAGED TABLE (managed=False) - Django ignores this in migrations"
        )
    if _is_es_registered(model_class):
        warnings.append(
            "🔍 INDEXED IN ELASTICSEARCH -> save()/delete() fan out to ES (external side effect; "
            "QuerySet.update()/bulk_create() skip signals, so the index can drift from the DB)"
        )

    if warnings:
        for w in warnings:
            print(f"  -> {w}")
    else:
        print("  ✅ Standard Meta configuration.")


def _section_columns(model_class):
    """--- 2. DATABASE COLUMNS & RELATIONS (The Skeleton) ---"""
    meta = model_class._meta
    print(f"\n🏗️ DATABASE COLUMNS & RELATIONS")
    print(f"{'-' * 10}")

    # Iterate over concrete fields defined on this model
    fields_to_inspect = (
        list(meta.local_fields)
        + list(meta.local_many_to_many)
        + list(meta.private_fields)
    )

    for field in fields_to_inspect:
        # GenericForeignKey is a virtual field: no DB column, no get_internal_type().
        if isinstance(field, GenericForeignKey):
            print(f"  ⭐ {field.name:<20} : GenericForeignKey (Complex)")
            continue

        try:
            field_type = field.get_internal_type()
        except AttributeError:
            # Other virtual/private fields (e.g. GenericRelation) may also lack it.
            field_type = field.__class__.__name__
        flags = []
        if getattr(field, "db_index", False):
            flags.append("INDEXED")
        if getattr(field, "unique", False):
            flags.append("UNIQUE")
        if getattr(field, "null", False):
            flags.append("NULL")

        if getattr(field, "auto_now", False):
            # auto_now fires in save()/update_or_create() only. QuerySet.update()
            # and bulk_update() issue raw SQL and SILENTLY skip it - if this field
            # is an expiry/staleness clock, those paths freeze it.
            flags.append("⚠️ AUTO_NOW (save() only - .update()/bulk_update() SKIP it)")
        if getattr(field, "auto_now_add", False):
            flags.append("AUTO_NOW_ADD")

        # Callable defaults (timezone.now, dict, list, uuid4...) are applied in Python
        # on INSERT via save()/create(). QuerySet.update()/bulk_update() never apply
        # defaults, so a bare .update() that means to "reset" this field is a no-op on it.
        if field.has_default() and callable(getattr(field, "default", None)):
            default_name = getattr(field.default, "__name__", "callable")
            flags.append(f"DEFAULT={default_name}() (insert only - .update() SKIPs)")

        if getattr(field, "validators", []):
            flags.append("VALIDATORS (full_clean() only)")

        flag_str = f"[{', '.join(flags)}]" if flags else ""

        # Handle Relationships
        if field.is_relation and field.related_model:
            target = (
                field.related_model.__name__
                if hasattr(field.related_model, "__name__")
                else str(field.related_model)
            )

            # Determine Type
            if field.many_to_one:
                rel_type = "Foreign Key (N:1)"
            elif field.one_to_one:
                rel_type = "OneToOne (1:1)"
            elif field.many_to_many:
                rel_type = "M2M (M:N)"
            else:
                rel_type = "Relation"

            # Determine Reverse Accessor. Ask Django (get_accessor_name) rather than
            # rebuilding the rule: the default is `<model>_set` for FK/M2M but just
            # `<model>` for OneToOne, and related_name='+' means no accessor at all.
            reverse_accessor = "Unknown"
            remote = getattr(field, "remote_field", None)
            if remote is not None:
                name = remote.get_accessor_name()
                # Django 5.1+ has the `hidden` property; older versions is_hidden().
                hidden = getattr(remote, "hidden", None)
                if hidden is None:
                    hidden = remote.is_hidden()
                if not name or hidden:
                    reverse_accessor = "(none, related_name='+')"
                elif getattr(remote, "related_name", None):
                    reverse_accessor = f"{name} (Explicit)"
                else:
                    reverse_accessor = f"{name} (Default)"

            print(
                f"  🔗 {field.name:<20} : {rel_type:<10} -> {target:<20} | Reverse: .{reverse_accessor:<20} {flag_str}"
            )

        # Handle Standard Fields
        else:
            # Highlight Heavy IO fields
            if isinstance(field, (models.FileField, models.ImageField)):
                print(
                    f"  💾 {field.name:<20} : {field_type} (Storage: {field.storage.__class__.__name__}) {flag_str}"
                )
            elif "JSON" in field_type:
                print(
                    f"  ✨ {field.name:<20} : {field_type} (Mutable, heavy - .only()/.defer() off hot reads) {flag_str}"
                )
            elif field_type in ("TextField", "BinaryField"):
                # Unbounded blobs. A fat SELECT * that drags these in bloats every
                # row on a hot read path - prefer .only()/.defer().
                print(
                    f"  ⚪ {field.name:<20} : {field_type} (heavy - .only()/.defer() off hot reads) {flag_str}"
                )
            else:
                print(f"  • {field.name:<20} : {field_type} {flag_str}")

    reverse_relations = list(meta.related_objects)
    if reverse_relations:
        print(f"  {'-' * 10} Reverse Relations (Implicit) {'-' * 10}")
        for rel in reverse_relations:
            target = (
                rel.related_model.__name__
                if hasattr(rel, "related_model")
                else "Unknown"
            )

            # This is the exact name you use in .prefetch_related() or .filter()
            accessor_name = (
                rel.get_accessor_name() if hasattr(rel, "get_accessor_name") else None
            )

            # If related_name='+', Django doesn't create a reverse accessor
            if not accessor_name:
                continue

            if rel.many_to_many:
                rel_type = "Rev M2M (N:M)"
            elif rel.one_to_many:
                rel_type = "Rev FK (1:N)"
            elif rel.one_to_one:
                rel_type = "Rev 1:1"
            else:
                rel_type = "Rev Rel"

            origin_field = rel.field.name if hasattr(rel, "field") else "unknown"

            print(
                f"  ⬅️ {accessor_name:<20} : {rel_type:<10} <- {target:<20} | Origin: {target}.{origin_field}"
            )


def _section_indexes(model_class):
    """--- 2.5 PHYSICAL INDEXES & CONSTRAINTS (live pg_catalog, + _meta drift) ---

    Reads the ACTUAL indexes from Postgres (pg_index), not just model _meta.
    _meta only knows Meta.indexes / constraints / unique_together - it is BLIND to
    expression/opclass indexes created via raw-SQL migrations (e.g.
    `gin (upper(value) gin_trgm_ops)`) and to anything added out-of-band. These are
    exactly the indexes that decide query plans on hot search paths, so the old
    _meta-only view could report "no index" on a column that has three.
    """
    import re
    from django.db import connections, router, transaction

    meta = model_class._meta
    print(f"\n🗂️ PHYSICAL INDEXES & CONSTRAINTS (live catalog)")
    print(f"{'-' * 10}")

    try:
        alias = router.db_for_read(model_class) or "default"
        # Savepoint: on a non-Postgres DB this query fails, and inside an open
        # transaction (ATOMIC_REQUESTS, a test case) a bare failure would poison it.
        with transaction.atomic(using=alias), connections[alias].cursor() as cur:
            cur.execute(
                """
                SELECT i.relname,
                       ix.indisprimary,
                       ix.indisunique,
                       am.amname,
                       ix.indexprs IS NOT NULL AS has_expression,
                       ix.indpred  IS NOT NULL AS is_partial,
                       ix.indisvalid,
                       pg_get_indexdef(ix.indexrelid)
                FROM pg_index ix
                JOIN pg_class i     ON i.oid = ix.indexrelid
                JOIN pg_class t     ON t.oid = ix.indrelid
                JOIN pg_am am       ON am.oid = i.relam
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE t.relname = %s AND n.nspname = ANY(current_schemas(false))
                ORDER BY i.relname
                """,
                [meta.db_table],
            )
            physical = cur.fetchall()
    except (
        Exception
    ) as e:  # noqa: BLE001 - dev tool: degrade to _meta view on any DB error
        print(
            f"  ⚠️ live catalog read failed ({e!r}); falling back to _meta-only view."
        )
        _section_indexes_from_meta(model_class)
        return

    physical_names = set()
    # Strip the "CREATE [UNIQUE] INDEX <name> ON <table> USING <method> " prefix so we
    # print just the (columns/expression)[ WHERE ...] body. Robust to nested parens
    # (expression indexes) because we only trim a fixed prefix, never parse the body.
    _prefix = re.compile(r"^CREATE (?:UNIQUE )?INDEX \S+ ON \S+ USING \w+ ")
    if not physical:
        print("  (no rows in pg_index - table not present/migrated on this DB)")
    for name, is_pk, is_unique, method, has_expr, is_partial, is_valid, ddl in physical:
        physical_names.add(name)
        tags = []
        if is_pk:
            tags.append("PK")
        elif is_unique:
            tags.append("UNIQUE")
        if is_partial:
            tags.append("PARTIAL")
        if has_expr:
            # THE blind spot: functional/opclass index, underivable from _meta.fields.
            tags.append("🔴 EXPRESSION (invisible to _meta)")
        if not is_valid:
            # A CONCURRENTLY build that failed midway leaves an invalid index: it EXISTS
            # (name matches, so a name-only drift check passes) but the planner IGNORES it.
            tags.append("🔴 INVALID (build failed - planner IGNORES it, REINDEX/drop)")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        body = _prefix.sub("", ddl)
        print(f"  🔍 {name:<42} USING {method:<6} {body}{tag_str}")

    # Drift the OTHER way: declared in _meta but physically absent (faked/reverted
    # migration). Only index-backing declarations qualify - CheckConstraint is not an
    # index, so it would false-positive here; it's listed separately below.
    declared = {}
    for idx in meta.indexes:
        if getattr(idx, "name", None):
            declared[idx.name] = "Meta.indexes"
    for c in meta.constraints:
        if c.__class__.__name__ in ("UniqueConstraint", "ExclusionConstraint"):
            declared[c.name] = f"Meta.constraints ({c.__class__.__name__})"
    absent = [(n, src) for n, src in declared.items() if n not in physical_names]
    if absent:
        print(f"  {'-' * 10}")
        for n, src in absent:
            print(
                f"  🟠 DRIFT: '{n}' declared in {src} but NOT in the DB "
                f"(migration faked/reverted?)"
            )

    # CheckConstraints are real but never surface as indexes - list them so we don't
    # regress the old section's constraint coverage.
    checks = [c for c in meta.constraints if c.__class__.__name__ == "CheckConstraint"]
    if checks:
        print(f"  {'-' * 10}")
        for c in checks:
            print(f"  ✏️ CHECK CONSTRAINT    : {c.name}")


def _section_indexes_from_meta(model_class):
    """--- 2.5 COMPOSITE INDEXES & CONSTRAINTS (Meta Class) ---"""
    meta = model_class._meta
    print(f"\n🧩 COMPOSITE INDEXES & CONSTRAINTS")
    print(f"{'-' * 10}")
    found_meta_indexes = False

    # 1. Explicit Indexes (models.Index, GinIndex, etc.)
    if meta.indexes:
        for idx in meta.indexes:
            found_meta_indexes = True
            fields = ", ".join(idx.fields)
            idx_type = idx.__class__.__name__
            # Resolve the actual Postgres access method (btree vs gin matters a LOT).
            # PostgresIndex subclasses set .suffix to the method (GinIndex -> 'gin');
            # base models.Index has suffix='idx' (a naming artifact) and is always btree.
            known_methods = {"btree", "gin", "gist", "hash", "brin", "spgist"}
            access_method = getattr(idx, "suffix", "")
            access_method = access_method if access_method in known_methods else "btree"
            # Check for specific index configurations like includes or conditions
            extras = []
            if getattr(idx, "condition", None):
                extras.append("Partial/Conditional")
            if getattr(idx, "include", None):
                extras.append(f"Includes: {idx.include}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""

            print(
                f"  🔍 {idx.name:<20} : {idx_type} (USING {access_method}) | Fields: ({fields}){extra_str}"
            )

    # 2. Unique Together
    if meta.unique_together:
        for ut in meta.unique_together:
            found_meta_indexes = True
            fields = ", ".join(ut)
            print(f"  🛡️ UNIQUE TOGETHER    : ({fields})")

    # 3. Index Together (Legacy Django)
    if getattr(meta, "index_together", None):
        for it in meta.index_together:
            found_meta_indexes = True
            fields = ", ".join(it)
            print(f"  🔍 INDEX TOGETHER     : ({fields}) (Legacy)")

    # 4. Constraints (UniqueConstraint, CheckConstraint, etc.)
    if meta.constraints:
        for constraint in meta.constraints:
            found_meta_indexes = True
            c_type = constraint.__class__.__name__
            fields = getattr(constraint, "fields", [])
            fields_str = f" | Fields: ({', '.join(fields)})" if fields else ""

            extras = []
            if getattr(constraint, "condition", None):
                extras.append("Conditional")
            extra_str = f" [{', '.join(extras)}]" if extras else ""

            print(f"  🟠 {constraint.name:<20} : {c_type}{fields_str}{extra_str}")

    if not found_meta_indexes:
        print("  ✅ No composite indexes or constraints defined.")


def _section_managers(model_class):
    """--- 3. MANAGER AUDIT ---

    Enumerates every manager and surfaces the .objects vs _base_manager split:
    forward FK access (book.author), one-to-one access in either direction, and
    cascade deletes go through _base_manager, which is a PLAIN unfiltered Manager
    unless Meta.base_manager_name is set - so a custom .objects filter is
    silently bypassed there. (Reverse-FK / M2M sets like author.books.all() are
    NOT affected: they're built from the related model's DEFAULT manager.)
    """
    meta = model_class._meta
    print(f"\n🔍 READ PATH (Managers)")
    print(f"{'-' * 10}")
    standard_managers = {"Manager", "BaseManager", "BaseManagerFromQuerySet"}

    def _filters_rows(mgr):
        """(overrides get_queryset in user code?, defining class name)."""
        definer = get_defining_class(mgr.__class__, "get_queryset")
        return bool(definer and definer not in standard_managers), definer

    default_mgr = model_class._default_manager
    base_mgr = model_class._base_manager
    base_name = getattr(meta, "base_manager_name", None)

    # 1. List every declared manager, tagging default/base roles + custom filters.
    any_custom = False
    for mgr in meta.managers:
        roles = []
        if mgr.name == getattr(default_mgr, "name", None):
            roles.append("DEFAULT = .objects / read path")
        if base_name and mgr.name == getattr(base_mgr, "name", None):
            roles.append("BASE = related lookups + cascades")
        role_str = f" [{', '.join(roles)}]" if roles else ""
        custom, definer = _filters_rows(mgr)
        if custom:
            any_custom = True
            print(
                f"  🔴 {mgr.name:<18} : {mgr.__class__.__name__} - get_queryset() overridden in '{definer}'{role_str}"
            )
        else:
            print(f"  • {mgr.name:<18} : {mgr.__class__.__name__} (standard){role_str}")

    # 2. The divergence check - the actual footgun.
    default_filters, default_definer = _filters_rows(default_mgr)
    base_filters, _ = _filters_rows(base_mgr)
    print(f"  {'-' * 10}")
    base_desc = (
        f"Meta.base_manager_name='{base_name}'"
        if base_name
        else "plain Manager (base_manager_name unset)"
    )
    print(f"  ⚪ _base_manager -> {base_mgr.__class__.__name__} ({base_desc})")
    if default_filters and not base_filters:
        print(
            f"  🔴 [HIGH RISK] '.objects' filters rows (get_queryset in '{default_definer}') "
            f"but _base_manager does NOT."
        )
        print(
            "    -> other_obj.<fk to this model>, one-to-one access and cascade deletes go "
            "through _base_manager and SEE ROWS that .objects hides "
            "(reverse-FK/M2M sets do apply the filter)."
        )
        print(
            "    -> Route those reads through .objects, or a base_objects that re-applies the filter."
        )

    # 3. Source of the default filter, if any (the implicit WHERE on every read).
    if default_filters:
        try:
            import textwrap

            qs_method = getattr(default_mgr.__class__, "get_queryset")
            source = textwrap.indent(inspect.getsource(qs_method), "      ")
            print(f"    -> .objects.get_queryset source:\n{source}")
        except Exception:
            print("    -> [Source code unavailable]")

    if not any_custom:
        print("  ✅ All managers standard (no implicit row filters).")


def _section_hooks(model_class):
    """--- 4. LIFECYCLE HOOKS ---"""
    print(f"\n🪝 LIFECYCLE HOOKS (Overrides)")
    print(f"{'-' * 10}")
    hooks = [
        ("__init__", "HIGH"),
        ("from_db", "CRITICAL"),
        ("save", "HIGH"),
        ("delete", "MED"),
        ("__str__", "N+1 RISK"),
    ]
    safe_mixins = {
        "TimeStampedModel",
        "UUIDModel",
        "TitleSlugDescriptionModel",
        "TimeFramedModel",
    }
    found_hook = False
    for method, risk in hooks:
        definer = get_defining_class(model_class, method)
        if definer:
            found_hook = True
            if definer in safe_mixins:
                print(
                    f"  ℹ️ [INFO] {method}() overridden in '{definer}' (Safe Library Mixin)"
                )
            else:
                print(f"  🔴 [{risk}] {method}() overridden in '{definer}'")

            if method == "__str__":
                try:
                    import textwrap

                    str_method = getattr(model_class, method)
                    source = textwrap.indent(inspect.getsource(str_method), "      ")
                    print(f"    -> Source:\n{source}")
                except Exception:
                    print("    -> [Source code unavailable]")
    if not found_hook:
        print("  ✅ No lifecycle methods overridden.")


def _section_signals(model_class):
    """--- 5. SIGNAL MAP ---"""
    print(f"\n📡 SIGNAL MAP")
    print(f"{'-' * 10}")

    es_registered = _is_es_registered(model_class)

    active_signals = {
        "pre_init": signals.pre_init,
        "post_init": signals.post_init,
        "pre_save": signals.pre_save,
        "post_save": signals.post_save,
        "pre_delete": signals.pre_delete,
        "post_delete": signals.post_delete,
        "m2m_changed": signals.m2m_changed,
    }
    found_signal = False
    for name, sig in active_signals.items():
        # Private API. Django >= 5.0 returns (sync_receivers, async_receivers);
        # older versions return one flat list.
        live = sig._live_receivers(model_class)
        if isinstance(live, tuple) and len(live) == 2 and all(isinstance(x, list) for x in live):
            live = [*live[0], *live[1]]
        for receiver in live:
            func = getattr(receiver, "__name__", str(receiver))
            mod = getattr(receiver, "__module__", "unknown")

            # ES-DSL attaches its handlers to EVERY model, but they're a pure
            # no-op unless the model is registered. Skip
            # them entirely on unregistered models
            if "django_elasticsearch_dsl" in mod and not es_registered:
                continue

            found_signal = True
            print(f"  📡 {name.upper()}: {mod}.{func}")
    if not found_signal:
        print("  ✅ No signals attached.")


def _section_hazards(model_class):
    """--- 6. VIRTUAL & M2M HAZARDS ---"""
    meta = model_class._meta
    print(f"\n⚠️ LOGIC HAZARDS")
    print(f"{'-' * 10}")
    # Properties
    props = list(getattr(meta, "_property_names", []))
    if not props:
        props = [n for n, v in model_class.__dict__.items() if isinstance(v, property)]
    if props:
        more = "..." if len(props) > 5 else ""
        print(f"  🧠 {len(props)} Virtual Properties: {', '.join(props[:5])}{more}")

    # M2M Through Logic (Safe Check)
    for field in meta.many_to_many:
        if (
            field.remote_field.through
            and not field.remote_field.through._meta.auto_created
        ):
            print(
                f"  🔀 [M2M] '{field.name}' uses custom table '{field.remote_field.through.__name__}' -> CHECK IT!"
            )


def _section_cascades(model_class):
    """--- 7. DELETE IMPACT (full on_delete map) ---"""
    meta = model_class._meta
    print(f"\n💣 DELETE IMPACT (on_delete map)")
    print(f"{'-' * 10}")

    # (emoji, label, description) per on_delete handler, ordered by severity.
    handlers = [
        (models.CASCADE, "🔥", "CASCADE", "WIPES rows in"),
        (models.PROTECT, "🛡️", "PROTECT", "BLOCKS delete (raises) from"),
        (models.RESTRICT, "⛔", "RESTRICT", "BLOCKS delete (raises) from"),
        (models.SET_NULL, "⚠️", "SET_NULL", "orphans (FK -> NULL) in"),
        (models.SET_DEFAULT, "🔧", "SET_DEFAULT", "resets FK -> default in"),
        (models.DO_NOTHING, "👻", "DO_NOTHING", "no action (DB integrity risk) in"),
    ]
    # Django 6.1+: the DATABASE enforces these (ON DELETE ... in the FK
    # constraint). No Python runs: no signals, no delete() overrides, and the
    # affected rows never pass through the Collector.
    for name, emoji, desc in (
        ("DB_CASCADE", "🔥", "WIPES rows (in the DB: no signals) in"),
        ("DB_SET_NULL", "⚠️", "orphans (FK -> NULL, in the DB: no signals) in"),
        ("DB_SET_DEFAULT", "🔧", "resets FK -> default (in the DB: no signals) in"),
    ):
        handler = getattr(models, name, None)
        if handler is not None:
            handlers.append((handler, emoji, name, desc))

    # Bucket reverse relations by their on_delete handler.
    buckets = {}
    for rel in meta.related_objects:
        buckets.setdefault(rel.on_delete, []).append(
            f"{rel.related_model.__name__}.{rel.field.name}"
        )

    # This model's OWN M2M fields: deleting a row also deletes its link rows in
    # each through table (the related rows themselves survive).
    own_m2m = [
        f"{model_class.__name__}.{f.name} (via {f.remote_field.through.__name__})"
        for f in meta.local_many_to_many
    ]

    if not buckets and not own_m2m:
        print("  ✅ No relations touched by a delete - safe delete.")
        return

    def _emit(origins, emoji, label, desc):
        shown = ", ".join(origins[:8])
        more = " ..." if len(origins) > 8 else ""
        print(f"  {emoji} {label:<11} {desc}: {shown}{more}")

    printed = set()
    for handler, emoji, label, desc in handlers:
        if handler in buckets:
            printed.add(handler)
            _emit(buckets[handler], emoji, label, desc)

    # M2M reverse relations carry no direct on_delete (it's None): deleting this
    # row removes the through-table LINK rows, not the related model's rows.
    m2m = (buckets.get(None) or []) + own_m2m
    if None in buckets:
        printed.add(None)
    if m2m:
        _emit(m2m, "🔗", "M2M", "unlinks (through-table rows) for")

    # Anything else (e.g. models.SET(value) closures / genuine custom callables).
    for handler, origins in buckets.items():
        if handler in printed:
            continue
        name = getattr(handler, "__name__", str(handler)).upper()
        _emit(origins, "❓", name, "custom handler on")


# ------------------------------------------------------------------------------
# REGISTRY + DRIVER
# Order here == order printed. Add a feature = append one (key, fn) pair.
# ------------------------------------------------------------------------------

SECTIONS = [
    ("meta", _section_meta),
    ("columns", _section_columns),
    ("indexes", _section_indexes),
    ("managers", _section_managers),
    ("hooks", _section_hooks),
    ("signals", _section_signals),
    ("hazards", _section_hazards),
    ("cascades", _section_cascades),
]


def _core_inspect_model(model_class, only=None, skip=None):
    """
    X-ray a single model. Prints the banner + selected sections + footer.

    only / skip filter by section key (see SECTIONS). Default = everything.
      only=["meta", "columns"]  -> just those two
      skip=["signals"]          -> everything except signals
    """
    meta = model_class._meta
    print(f"\n{'=' * 10}")
    print(f"🔬 MODEL X-RAY: {model_class.__name__}")
    print(f"📦 App: {meta.app_label} | 🗄️ Table: {meta.db_table}")
    print(f"{'=' * 10}\n")

    enabled = set(only) if only else {key for key, _ in SECTIONS}
    if skip:
        enabled -= set(skip)

    for key, section_fn in SECTIONS:
        if key in enabled:
            section_fn(model_class)

    print(f"\n{'=' * 20}\n")


@copy_to_clipboard
def inspect_model(model_class, only=None, skip=None):
    """X-ray one model. Add clipboard=True to also copy the report (OSC 52)."""
    _core_inspect_model(model_class, only=only, skip=skip)


@copy_to_clipboard
def inspect_model_multiple(*model_classes, only=None, skip=None):
    """
    X-ray several models in one shot; with clipboard=True all output lands in a
    single clipboard payload.
      inspect_model_multiple(A,B,C,D)
      inspect_model_multiple(A,B, only=["meta", "columns"], clipboard=True)
    Loops the *undecorated* core so we don't copy once per model.
    """
    for model_class in model_classes:
        _core_inspect_model(model_class, only=only, skip=skip)
