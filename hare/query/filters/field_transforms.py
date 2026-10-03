"""Transforms of a field's value in a lookup path - what a path segment after a field's name reads
inside its value (an array's item, a range's bound)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.fields.base.field import Field
from hare.fields.generated import GeneratedField
from hare.sql.terms.base.term import Term

#: A function building the term a path segment reads from the term before it.
TermTransform = Callable[[Term], Term]


class FieldTransforms:
    """Reads a path of transforms inside a field's value (``tags__0``, ``tags__len``,
    ``during__startswith``, ``name__unaccent``, ``attributes__color``), chained. A field reads
    inside its own value through ``Field.get_path_transform()``; a dialect adds a segment to a field
    class with ``Field.register_transform()``.
    """

    @staticmethod
    def get_required_extension(field: Field[Any], segment: str) -> str | None:
        """The database extension a path segment registered on the field's class needs.

        Args:
            field: The field the segment reads inside.
            segment: The path segment.

        Returns:
            The extension, None when the segment needs none or isn't registered.
        """
        for klass in type(field).__mro__:
            registered_transform = klass.__dict__.get("registered_transforms", {}).get(segment)
            if registered_transform is not None:
                return registered_transform.required_extension
        return None

    @classmethod
    def get_path_required_extension(cls, field: Field[Any], segments: list[str]) -> str | None:
        """The first database extension a path of segments needs.

        Args:
            field: The field the path starts at.
            segments: The path's transform segments.

        Returns:
            The extension, None when no segment needs one.
        """
        for segment in segments:
            required_extension = cls.get_required_extension(cls.get_effective_field(field), segment)
            if required_extension is not None:
                return required_extension
            transform = cls.get_transform(field, segment)
            if transform is None:
                return None
            field = transform[1]
        return None

    @staticmethod
    def get_effective_field(field: Field[Any]) -> Field[Any]:
        """A field, or the field a GeneratedField computes."""
        return field.output_field if isinstance(field, GeneratedField) else field

    @classmethod
    def get_transform(cls, field: Field[Any], segment: str) -> tuple[TermTransform, Field[Any]] | None:
        """The transform one path segment applies to a value of ``field``.

        Args:
            field: The field of the value so far.
            segment: The path segment.

        Returns:
            The term transform and the field of its result, or None when the segment isn't one.
        """
        return cls.get_effective_field(field).get_path_transform(segment)

    @classmethod
    def get_path(cls, field: Field[Any], segments: list[str]) -> tuple[list[TermTransform], Field[Any], list[str]]:
        """Reads as many transform segments as the path starts with.

        Args:
            field: The field the path starts at.
            segments: The path segments after the field's name.

        Returns:
            The transforms in order, the field of the value they end at, and the segments left.
        """
        transforms: list[TermTransform] = []
        while segments and (transform := cls.get_transform(field, segments[0])) is not None:
            transforms.append(transform[0])
            field = transform[1]
            segments = segments[1:]
        return transforms, field, segments

    @staticmethod
    def apply(transforms: list[TermTransform], term: Term) -> Term:
        """``term`` through every transform."""
        for transform in transforms:
            term = transform(term)
        return term
