from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError
from hare.query.expressions import Expression, F, RawSQL
from hare.query.expressions.term_expression import TermExpression
from hare.query.filters.constants import DATETIME_CAST_SEGMENTS
from hare.query.functions.datetime.extract import Extract
from hare.query.functions.datetime.trunc import Trunc
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.lookup_info.value_paths import ValuePaths
from hare.query.query_connection import QueryConnection
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.queryset.queryset import QuerySet


class AnnotationArguments:
    """What annotate(), alias() and group_by() are given: expressions under names that collide with no
    field, the annotations a path inside a value adds, and the descriptions of annotations kept for
    their output fields."""

    @staticmethod
    def get_annotations(annotations: dict[str, Any]) -> dict[str, Any]:
        """The annotations as they are kept, each checked to be an expression - a ``hare.sql`` term
        built by hand as a ``TermExpression``, which a plan describes and binds; a ``RawSQL``
        fragment describes itself.

        Args:
            annotations: The annotations given, by name.

        Returns:
            The annotations kept - the mapping given when it holds no term built by hand.

        Raises:
            TypeError: A value is e.g. a bare field name string.
        """
        kept_annotations = annotations
        for key, annotation in annotations.items():
            if isinstance(annotation, Expression):
                continue
            if not isinstance(annotation, Term):
                hint = f" - use F({annotation!r}) to reference a field" if isinstance(annotation, str) else ""
                value_type_name = type(annotation).__name__
                raise TypeError(
                    f"The annotation {key!r} must be an expression, got {value_type_name} {annotation!r}{hint}"
                )
            if not isinstance(annotation, RawSQL):
                if kept_annotations is annotations:
                    kept_annotations = dict(annotations)
                kept_annotations[key] = TermExpression(annotation)
        return kept_annotations

    @staticmethod
    def raise_if_annotation_names_collide_with_fields(
        queryset: QuerySet[Any, Any], annotation_names: Iterable[str]
    ) -> None:
        """Rejects an annotation named after a field of the model - every other reference to
        that name (another annotation's F(), a filter, an ordering) would silently read the
        annotation instead of the field.

        Args:
            queryset: The queryset.
            annotation_names: The new annotation names.

        Raises:
            FieldError: A name is a field of the model or ``pk``.
            ValueError: A name contains quotes, a semicolon, whitespace or an SQL comment.
        """
        annotation_names = list(annotation_names)
        queryset._raise_if_annotation_names_are_unsafe(annotation_names)
        colliding_names = sorted(
            name for name in annotation_names if name in queryset.model._meta.fields_map or name == "pk"
        )
        if colliding_names:
            raise FieldError(
                f"Annotation name(s) {colliding_names} conflict with field(s) of model "
                f"{queryset.model.__name__} - references to the field would read the annotation instead. "
                "Use a different annotation name."
            )

    @staticmethod
    def get_path_annotations(queryset: QuerySet[Any, Any], names: Iterable[str]) -> dict[str, Expression]:
        """The selected names reading a path into a JSON field or annotation, through an array or range
        field, or a part of a date, time or datetime field - each as an annotation of its own name.

        Args:
            queryset: The queryset.
            names: The selected names.

        Returns:
            Each path name to its expression.
        """
        paths: dict[str, Expression] = {}
        for name in names:
            base_name, __, path = name.partition("__")
            if not path or name in queryset._annotations:
                continue
            if base_name in queryset._annotations or ValuePaths.get_value_path_split(queryset.model, name) is not None:
                paths[name] = F(name)
            elif (date_part_split := ValuePaths.get_date_part_split(queryset.model, name)) is not None:
                field_path, part = date_part_split
                paths[name] = Trunc(field_path, part) if part in DATETIME_CAST_SEGMENTS else Extract(field_path, part)
        return paths

    @staticmethod
    def get_annotation_descriptions(queryset: QuerySet[Any, Any]) -> dict[tuple[str, str, str], Any]:
        """The queryset's cached descriptions of keys starting with an annotation - by (description type, key,
        dialect name), the dialect the annotations' types were resolved with. Emptied once a
        model's fields, lookups or relations change (``LookupInfoBuilder.forget_descriptions()``).

        Args:
            queryset: The queryset.

        Returns:
            The cache, shared with the clones that have the same annotations.
        """
        descriptions = queryset._annotation_descriptions
        if descriptions is None:
            descriptions = queryset._annotation_descriptions = (
                LookupInfoBuilder.annotation_descriptions.new_shared_bucket()
            )
        return descriptions

    @staticmethod
    def get_annotation_description(
        queryset: QuerySet[Any, Any], description_type: str, key: str, build: Callable[[], Any]
    ) -> Any:
        """A description of a key starting with one of the queryset's annotations, built once per
        queryset annotations and dialect - an error is raised again on every call, never cached.

        Args:
            queryset: The queryset.
            description_type: What is described - ``lookup_info``, ``ordering_info``, ...
            key: The filter key, ordering name or path.
            build: Builds the description.

        Returns:
            The description.
        """
        descriptions = AnnotationArguments.get_annotation_descriptions(queryset)
        cache_key = (description_type, key, QueryConnection.get_analysis_dialect(queryset).name)
        description = descriptions.get(cache_key)
        if description is None:
            description = build()
            descriptions[cache_key] = description
        return description

    @staticmethod
    def detach_annotation_descriptions(queryset: QuerySet[Any, Any], names: Iterable[str]) -> None:
        """Gives a clone about to set the annotations ``names`` descriptions of its own - its
        original keeps seeing only descriptions of its own annotations. Replacing an annotation
        empties them: another annotation's type may depend on it.

        Args:
            queryset: The queryset.
            names: The annotation names about to be set.
        """
        descriptions = queryset._annotation_descriptions
        if descriptions is None:
            return
        if any(name in queryset._annotations for name in names) or not descriptions:
            queryset._annotation_descriptions = None
        else:
            own_descriptions = LookupInfoBuilder.annotation_descriptions.new_shared_bucket()
            own_descriptions.update(descriptions)
            queryset._annotation_descriptions = own_descriptions

    @staticmethod
    def get_annotation_output_fields(queryset: QuerySet[Any, Any]) -> dict[str, Field[Any] | None]:
        """The field whose type each annotation's value has, resolved once per queryset and dialect -
        with the neutral SQL dialect until a connection is chosen.

        Args:
            queryset: The queryset.

        Returns:
            Each annotation name to its value's field, None where that isn't known.
        """
        descriptions = AnnotationArguments.get_annotation_descriptions(queryset)
        dialect_name = QueryConnection.get_analysis_dialect(queryset).name
        missing_names = [
            name for name in queryset._annotations if ("output_field", name, dialect_name) not in descriptions
        ]
        if missing_names:
            expression_context = queryset._get_probe_expression_context(queryset._annotations)
            for name in missing_names:
                annotation = queryset._annotations[name]
                descriptions[("output_field", name, dialect_name)] = (
                    annotation.get_value_field(annotation.get_result(expression_context))
                    if isinstance(annotation, Expression)
                    else None
                )
        return {name: descriptions[("output_field", name, dialect_name)] for name in queryset._annotations}
