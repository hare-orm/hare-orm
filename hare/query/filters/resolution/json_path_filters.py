from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.fields.data.json.json_field import JSONField
from hare.fields.data.json.json_path_field import JSONPathField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.constants import (
    MIRRORED_COMPARISON_LOOKUPS,
)
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.f import F
from hare.query.filters import FieldLookups
from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups
from hare.query.filters.resolution.annotation_filters import AnnotationFilters
from hare.sql.functions.declarations import JsonComparand
from hare.sql.terms.term import Term
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class JsonPathFilters:
    """Filters on a path into a JSON field or a JSON-valued annotation: the path as an annotation of
    its own, the value compared with it, and the comparison mirrored for a JSON value on the right."""

    @staticmethod
    def get_json_comparand(term: Term, value_field: Field[Any] | None, dialect: Dialect) -> Term:
        """An expression value compared with the value at a JSON path, as a JSON value.

        Args:
            term: The value's resolved term.
            value_field: The value's field.
            dialect: The dialect the comparison renders for.

        Returns:
            The term as a JSON value, or itself when it holds one already or its type is unknown.
        """
        # Local import: hare.query.functions.json.json_object imports the expressions package this module is in.
        from hare.query.functions.json.json_object import JSONObject

        effective_field = GeneratedField.get_effective_field(value_field) if value_field is not None else None
        if effective_field is None or isinstance(effective_field, JSONField):
            return term
        json_term, value_type = JSONObject.get_json_value(term, effective_field, dialect)
        return JsonComparand(json_term, value_type, is_aware=Timezone.get_use_timezone())

    @staticmethod
    def get_json_path_mirrored_kwarg(expression_context: ExpressionContext, key: str, value: Any) -> tuple[str, Any]:
        """A comparison of a field with the value at a JSON path (``number__gt=F("data__rank")``)
        as the same comparison of the path with the field (``data__rank__lt=F("number")``), which
        compares JSON values.

        Args:
            expression_context: The node's context.
            key: The filter key.
            value: The filter value.

        Returns:
            The key and value to resolve.
        """
        if not isinstance(value, F):
            return key, value
        meta = expression_context.model._meta
        field_name, __, lookup = key.partition("__")
        field = meta.fields_map.get(field_name)
        if (
            field is None
            or field_name in meta.fetch_fields
            or isinstance(GeneratedField.get_effective_field(field), JSONField)
            or lookup not in MIRRORED_COMPARISON_LOOKUPS
            or value.name.partition("__")[0] in meta.fetch_fields
            or not isinstance(value.get_result(expression_context).output_field, JSONPathField)  # type: ignore[call-overload]
        ):
            return key, value
        mirrored_lookup = MIRRORED_COMPARISON_LOOKUPS[lookup]
        return (f"{value.name}__{mirrored_lookup}" if mirrored_lookup else value.name), F(field_name)

    @staticmethod
    def get_json_base_field(expression_context: ExpressionContext, name: str) -> JSONField[Any] | None:
        """The JSON field of a model field or annotation a filter key starts with.

        Args:
            expression_context: The node's context.
            name: The key's first segment.

        Returns:
            The field, or None when the name isn't a JSON field or JSON-valued annotation.
        """
        annotation = expression_context.annotations.get(name)
        if annotation is not None:
            if not isinstance(annotation, Expression):
                return None
            # Resolved only to read its field - its values are recorded where it is built.
            field = annotation.get_result(  # type: ignore[call-overload]
                dataclasses.replace(expression_context, value_wrapper_references=None)
            ).output_field
        else:
            field = expression_context.model._meta.fields_map.get(name)
        effective_field = GeneratedField.get_effective_field(field) if field is not None else None
        return effective_field if isinstance(effective_field, JSONField) else None

    @staticmethod
    def get_json_path_expression_context(
        expression_context: ExpressionContext, key: str
    ) -> tuple[ExpressionContext, str | None]:
        """The context a filter key resolves in, and the annotation it starts with. A key reading a
        path into a JSON field or JSON-valued annotation (``data__owner__name``,
        ``data__score__gt``) resolves as a filter on ``F()`` of that path.

        Args:
            expression_context: The node's context.
            key: The filter key.

        Returns:
            The context - with the path as an annotation for such a key - and the annotation the key
            starts with, None for a key of the model.
        """
        annotation_name = AnnotationFilters.get_annotation_name(expression_context, key)
        field_name, __, path = key.partition("__")
        if not path or (json_field := JsonPathFilters.get_json_base_field(expression_context, field_name)) is None:
            return expression_context, annotation_name
        if path in FieldLookups.get(json_field):
            # A lookup of the whole value.
            return expression_context, annotation_name
        path_segments = path.split("__")
        path_lookup_names = JsonPathLookups.get_lookup_names()
        if len(path_segments) == 1 and path_segments[0] in path_lookup_names:
            # A lookup name right after the field is that lookup of the whole value, never a key.
            return expression_context, annotation_name
        path_name = key
        if len(path_segments) > 1 and path_segments[-1] in path_lookup_names:
            path_name = key.rpartition("__")[0]
        return (
            dataclasses.replace(
                expression_context, annotations={**expression_context.annotations, path_name: F(path_name)}
            ),
            path_name,
        )
