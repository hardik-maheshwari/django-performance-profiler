import contextvars
import re
from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ReverseOneToOneDescriptor,
    ReverseManyToOneDescriptor,
)
from django.db.models.query_utils import DeferredAttribute

_pending = contextvars.ContextVar("orm_xray_pending", default=None)
_active = contextvars.ContextVar("orm_xray_active", default=False)

_installed = False
_orig = {}


def set_active(flag: bool):
    _active.set(bool(flag))


def consume_pending_access():
    ev = _pending.get()
    if ev is not None:
        _pending.set(None)
    return ev


def targets_sql(trigger, sql):
    """Stale-stamp guard. A genuine lazy-load always queries its relation's own
    table; a stamp left over from an earlier cache-hit access (see _lazy_wrapper)
    will not. Attribute the trigger ONLY if one of its candidate tables appears
    in the SQL that actually fired. Conservative: if we couldn't determine a
    table, we refuse to attribute rather than risk a wrong label."""
    if not trigger:
        return False
    tables = trigger.get("tables")
    if not tables:
        return False
    low = sql.lower()
    # Match table names at identifier boundaries, NOT as raw substrings.
    # Table names are frequently prefixes of one another
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in tables)


def first_id(params):
    if not params:
        return None
    try:
        return repr(tuple(params))
    except TypeError:
        return repr(params)


def classify(trigger):
    if not trigger:
        return None
    kind = trigger["kind"]
    if kind == "deferred":
        return "deferred_reload"
    prefetched = trigger.get("prefetched")
    if kind in ("reverse_fk", "m2m"):
        return "prefetched_but_busted" if prefetched else "never_prefetched"
    if kind in ("forward_fk", "reverse_o2o"):
        return "missing_select_related" if not prefetched else "prefetched_but_busted"
    return None


def fix_hint(kind, n_distinct_ids, n_calls=None):
    if kind == "prefetched_but_busted":
        return (
            "relation IS prefetched but a query still fired — a "
            ".first()/.filter()/.annotate()/.exclude() rebuilt the queryset and "
            "bypassed the cache. Use .all()[0] / iterate .all(), or push the "
            "annotation onto the queryset."
        )
    if kind == "never_prefetched":
        # Tell genuine fan-out (many distinct objects, each loaded once) apart
        # from recompute-dominated (few objects hit repeatedly). When calls
        # outnumber distinct objects ~2:1+, prefetch only masks the fan-out —
        # the redundant re-fetches are the real waste, so memoize / hoist.
        if not n_distinct_ids or n_distinct_ids <= 1:
            return (
                "same object hit repeatedly (one distinct id) — recomputation, "
                "not fan-out. Hoist it out of the loop / memoize. "
                "(prefetch would also mask it, but the call itself is redundant.)"
            )
        if n_calls and n_calls >= 2 * n_distinct_ids:
            return (
                f"recompute-dominated — {n_calls} calls over only {n_distinct_ids} "
                "distinct objects. Memoize / hoist the lookup; prefetch_related "
                "removes the fan-out but not the redundant re-fetches."
            )
        return "fan-out across rows — add prefetch_related('<relation>')."
    if kind == "missing_select_related":
        return "forward relation not joined — add select_related('<relation>')."
    if kind == "deferred_reload":
        return (
            "a field excluded by .only()/.defer() was read (often inside "
            "__init__/__str__/a property). Add it to .only(), or stop reading it."
        )
    return ""


def _ident(instance):
    try:
        return (type(instance).__name__, instance.pk)
    except Exception:
        return (type(instance).__name__, None)


def _safe_table(model):
    try:
        return model._meta.db_table.lower()
    except Exception:
        return None


def _tables(*models):
    """Candidate db_tables for the related model(s), Nones dropped, lowercased."""
    out = set()
    for m in models:
        t = _safe_table(m)
        if t:
            out.add(t)
    return frozenset(out)


def _is_prefetched(instance, *candidate_names):
    cache = getattr(instance, "_prefetched_objects_cache", None)
    if cache is None:
        return False
    for name in candidate_names:
        if name and name in cache:
            return True
    return False if not candidate_names else None


def _sync_wrapper(orig, build_event):
    def __get__(self, instance, cls=None):
        if instance is None or not _active.get():
            return orig(self, instance, cls)
        token = _pending.set(build_event(self, instance))
        try:
            return orig(self, instance, cls)
        finally:
            _pending.reset(token)

    return __get__


def _lazy_wrapper(orig, build_event):
    def __get__(self, instance, cls=None):
        if instance is None or not _active.get():
            return orig(self, instance, cls)
        _pending.set(build_event(self, instance))
        return orig(self, instance, cls)

    return __get__


def _ev_forward_fk(self, instance):
    name = self.field.name
    return {
        "kind": "forward_fk",
        "name": name,
        "obj": _ident(instance),
        "prefetched": self.field.is_cached(instance),
        "tables": _tables(getattr(self.field, "related_model", None)),
    }


def _ev_reverse_o2o(self, instance):
    name = self.related.get_accessor_name()
    return {
        "kind": "reverse_o2o",
        "name": name,
        "obj": _ident(instance),
        "prefetched": self.related.is_cached(instance),
        "tables": _tables(getattr(self.related, "related_model", None)),
    }


def _ev_reverse_or_m2m(self, instance):
    is_m2m = hasattr(self, "reverse")
    rel = getattr(self, "rel", None)
    accessor = field_name = None
    try:
        accessor = self.rel.get_accessor_name()
    except Exception:
        pass
    try:
        field_name = self.field.name
    except Exception:
        pass
    # Forward M2M (ManyToManyDescriptor, reverse=False): field.name is what the
    # code wrote; get_accessor_name() is the OTHER side and misleads.
    # Reverse FK / reverse M2M: the accessor is the correct, code-facing name.
    is_forward_m2m = is_m2m and not getattr(self, "reverse", True)
    ordered = [field_name, accessor] if is_forward_m2m else [accessor, field_name]
    names = [n for n in ordered if n]
    name = names[0] if names else "<relation>"
    # The fetched rows belong to the related model; for M2M the through table is
    # also in the SQL, but matching the target model's table is enough.
    tables = _tables(
        getattr(rel, "related_model", None),
        getattr(rel, "model", None),
        getattr(getattr(self, "field", None), "related_model", None),
    )
    return {
        "kind": "m2m" if is_m2m else "reverse_fk",
        "name": name,
        "obj": _ident(instance),
        "prefetched": _is_prefetched(instance, *names),
        "tables": tables,
    }


def _ev_deferred(self, instance):
    # A deferred reload re-queries the instance's own table.
    return {
        "kind": "deferred",
        "name": self.field.attname,
        "obj": _ident(instance),
        "prefetched": None,
        "tables": _tables(type(instance)),
    }


def install():
    global _installed
    if _installed:
        return
    _orig["fwd"] = ForwardManyToOneDescriptor.__get__
    _orig["ro2o"] = ReverseOneToOneDescriptor.__get__
    _orig["rev"] = ReverseManyToOneDescriptor.__get__
    _orig["def"] = DeferredAttribute.__get__

    ForwardManyToOneDescriptor.__get__ = _sync_wrapper(_orig["fwd"], _ev_forward_fk)
    ReverseOneToOneDescriptor.__get__ = _sync_wrapper(_orig["ro2o"], _ev_reverse_o2o)
    ReverseManyToOneDescriptor.__get__ = _lazy_wrapper(_orig["rev"], _ev_reverse_or_m2m)
    DeferredAttribute.__get__ = _sync_wrapper(_orig["def"], _ev_deferred)
    _installed = True


def uninstall():
    global _installed
    if not _installed:
        return
    ForwardManyToOneDescriptor.__get__ = _orig["fwd"]
    ReverseOneToOneDescriptor.__get__ = _orig["ro2o"]
    ReverseManyToOneDescriptor.__get__ = _orig["rev"]
    DeferredAttribute.__get__ = _orig["def"]
    _installed = False