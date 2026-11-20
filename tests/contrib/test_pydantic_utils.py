import functools
import sys
from enum import StrEnum
from types import FunctionType, ModuleType
from typing import Annotated, ClassVar, get_type_hints, no_type_check

import pytest

from hare.contrib.pydantic.creation.model_annotations import ModelAnnotations

pytestmark = pytest.mark.database_independent


@pytest.fixture
def annotation_namespaces(monkeypatch):
    base = ModuleType("tests._annotations_base")
    child = ModuleType("tests._annotations_child")
    secondary = ModuleType("tests._annotations_secondary")
    for module, label in ((base, "base"), (child, "child"), (secondary, "secondary")):
        module.Status = StrEnum("Status", {"VALUE": label})
        monkeypatch.setitem(sys.modules, module.__name__, module)
    remote = type("Remote", (), {})
    registry = {name: type(name, (), {}) for name in ("Status", "list", "int", "dict")}
    registry["Remote"] = remote
    monkeypatch.setattr(ModelAnnotations, "get_registry_namespace", lambda binding: registry)
    return base, child, secondary, remote


def test_annotation_names_follow_each_declaring_class_module_and_mro(annotation_namespaces):
    base, child, secondary, remote = annotation_namespaces
    base_class = type(
        "Base",
        (),
        {
            "__module__": base.__name__,
            "__annotations__": {"base_status": "Status", "shared": "Status", "items": "list[Status]"},
        },
    )
    other_class = type(
        "Secondary",
        (),
        {"__module__": secondary.__name__, "__annotations__": {"secondary_status": "Status", "shared": "Status"}},
    )
    child_class = type(
        "Child",
        (base_class, other_class),
        {
            "__module__": child.__name__,
            "__annotations__": {"child_status": "Status", "remote": "Remote | None", "count": "int"},
        },
    )
    expected = {
        "base_status": base.Status,
        "secondary_status": secondary.Status,
        "shared": base.Status,
        "items": list[base.Status],
        "child_status": child.Status,
        "remote": remote | None,
        "count": int,
    }
    assert get_type_hints(child_class, localns={"Remote": remote}) == expected
    assert ModelAnnotations.get(child_class) == expected


def test_annotation_fallback_does_not_hide_class_aliases_or_builtin_names(annotation_namespaces):
    base, child, __, remote = annotation_namespaces
    base_class = type(
        "Base",
        (),
        {
            "__module__": base.__name__,
            "Alias": list[base.Status],
            "Remote": bytes,
            "__annotations__": {"items": "Alias", "local": "Remote", "status": "Status", "mapping": "dict[str, int]"},
        },
    )
    child.__dict__.pop("Status")
    child_class = type(
        "Child", (base_class,), {"__module__": child.__name__, "__annotations__": {"related": "Remote | None"}}
    )
    annotations = ModelAnnotations.get(child_class)
    assert annotations["items"] == list[base.Status]
    assert annotations["local"] is bytes
    assert annotations["status"] is base.Status
    assert annotations["mapping"] == dict[str, int]
    assert annotations["related"] == remote | None


def test_computed_annotation_uses_the_unwrapped_functions_module(annotation_namespaces):
    base, child, __, ___ = annotation_namespaces

    def original(self):
        return None

    function = FunctionType(original.__code__, base.__dict__, "computed")
    if hasattr(function, "__annotate__"):
        function.__annotate__ = None
    function.__annotations__ = {"return": "list[Status]"}
    wrapper = FunctionType(original.__code__, child.__dict__, "wrapper")
    if hasattr(wrapper, "__annotate__"):
        wrapper.__annotate__ = None
    wrapper = functools.wraps(function, assigned=(*functools.WRAPPER_ASSIGNMENTS, "__annotations__"))(wrapper)
    model = type("Child", (), {"__module__": child.__name__})
    expected = {"return": list[base.Status]}
    assert get_type_hints(wrapper) == expected
    assert ModelAnnotations.get(model, wrapper) == expected


def test_annotation_resolution_preserves_standard_typing_shapes_and_no_type_check(annotation_namespaces):
    class Description:
        value: Annotated[int | None, "label"]
        shared: ClassVar[str]

    assert ModelAnnotations.get(Description) == get_type_hints(Description)

    @no_type_check
    class Ignored:
        unknown: "DefinitelyNotRegistered"  # noqa: F821 - ignored by the standard typing contract

    assert ModelAnnotations.get(Ignored) == {}


def test_get_annotations_propagates_unresolvable_forward_reference():
    """get_annotations()'s get_type_hints() call was wrapped in a broad `except Exception`,
    silently falling back to the class's raw (unresolved) __annotations__ dict for ANY failure -
    including a genuine typo'd forward reference, which should surface as a clear NameError
    instead of masquerading as "field just didn't resolve"."""

    class BadAnnotations:
        broken: "ThisNameDoesNotExistAnywhere"  # noqa: F821 - deliberately unresolvable

    with pytest.raises(NameError):
        ModelAnnotations.get(BadAnnotations)
