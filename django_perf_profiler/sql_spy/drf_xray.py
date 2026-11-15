import contextvars
from rest_framework.serializers import Serializer, ListSerializer
from rest_framework.fields import SerializerMethodField
from rest_framework.relations import RelatedField, ManyRelatedField

_active = contextvars.ContextVar("drf_xray_active", default=False)
_stack = contextvars.ContextVar("drf_xray_stack", default=None)
_page = contextvars.ContextVar("drf_xray_page_size", default=None)

_installed = False
_orig = {}


def set_active(flag: bool):
    _active.set(bool(flag))
    if not flag:
        _stack.set(None)
        _page.set(None)


def current_context():
    stack = _stack.get()
    if not stack:
        return {"phase": None, "serializer": None, "field": None, "page_size": _page.get()}
    top = stack[-1]
    return {
        "phase": "serialization",
        "serializer": top["serializer"],
        "field": top["field"],
        "page_size": _page.get(),
    }


def _push(serializer_name, field_name):
    stack = _stack.get() or []
    _stack.set(stack + [{"serializer": serializer_name, "field": field_name}])


def _pop():
    stack = _stack.get()
    if stack:
        _stack.set(stack[:-1])


def _parent_name(field):
    parent = getattr(field, "parent", None)
    return type(parent).__name__ if parent is not None else "?"


def _wrap_list_to_repr(orig):
    def to_representation(self, data):
        if not _active.get():
            return orig(self, data)
        # Page size is for labelling only and must NEVER trigger a query.
        # Read length from already-materialized data; for an unevaluated
        # queryset/Manager, skip (forcing .all()/len() would inject a phantom
        # query attributed to the spy itself).
        try:
            if isinstance(data, (list, tuple)):
                _page.set(len(data))
            else:
                cache = getattr(data, "_result_cache", None)
                if cache is not None:
                    _page.set(len(cache))
        except Exception:
            pass
        return orig(self, data)

    return to_representation


def _wrap_serializer_to_repr(orig):
    def to_representation(self, instance):
        if not _active.get():
            return orig(self, instance)
        _push(type(self).__name__, None)
        try:
            return orig(self, instance)
        finally:
            _pop()

    return to_representation


def _wrap_field_to_repr(orig):
    def to_representation(self, value):
        if not _active.get():
            return orig(self, value)
        _push(_parent_name(self), self.field_name)
        try:
            return orig(self, value)
        finally:
            _pop()

    return to_representation


def install():
    global _installed
    if _installed:
        return
    _orig["list"] = ListSerializer.to_representation
    _orig["ser"] = Serializer.to_representation
    _orig["method"] = SerializerMethodField.to_representation
    _orig["related"] = RelatedField.to_representation
    _orig["many"] = ManyRelatedField.to_representation

    ListSerializer.to_representation = _wrap_list_to_repr(_orig["list"])
    Serializer.to_representation = _wrap_serializer_to_repr(_orig["ser"])
    SerializerMethodField.to_representation = _wrap_field_to_repr(_orig["method"])
    RelatedField.to_representation = _wrap_field_to_repr(_orig["related"])
    ManyRelatedField.to_representation = _wrap_field_to_repr(_orig["many"])
    _installed = True


def uninstall():
    global _installed
    if not _installed:
        return
    ListSerializer.to_representation = _orig["list"]
    Serializer.to_representation = _orig["ser"]
    SerializerMethodField.to_representation = _orig["method"]
    RelatedField.to_representation = _orig["related"]
    ManyRelatedField.to_representation = _orig["many"]
    _installed = False