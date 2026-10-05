import pytest

from hare.contrib.pydantic.creation.model_annotations import ModelAnnotations


def test_get_annotations_propagates_unresolvable_forward_reference():
    """get_annotations()'s get_type_hints() call was wrapped in a broad `except Exception`,
    silently falling back to the class's raw (unresolved) __annotations__ dict for ANY failure -
    including a genuine typo'd forward reference, which should surface as a clear NameError
    instead of masquerading as "field just didn't resolve"."""

    class BadAnnotations:
        broken: "ThisNameDoesNotExistAnywhere"  # noqa: F821 - deliberately unresolvable

    with pytest.raises(NameError):
        ModelAnnotations.get(BadAnnotations)
