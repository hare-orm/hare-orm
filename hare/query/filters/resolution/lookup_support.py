from __future__ import annotations

from hare.exceptions import FieldError, UnSupportedError
from hare.fields.field import Field
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.filters.constants import LOOKUP_REQUIRED_FEATURES
from hare.query.filters.resolution.annotation_filters import AnnotationFilters


class LookupSupport:
    """The checks that a filter key names a lookup the field and the connection's dialect support, and
    the error naming an unknown key."""

    @staticmethod
    def raise_if_lookup_unsupported(expression_context: ExpressionContext, key: str) -> None:
        """Rejects a lookup the query's dialect or connection doesn't run, before any SQL is built.
        Keys on an annotation are left to the annotation; so is everything while no connection is
        chosen.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Raises:
            FieldError: The key names no field or relation, or a lookup its field doesn't have.
            UnSupportedError: The dialect or the connection doesn't run the lookup.
        """
        if (
            expression_context.connection is None
            or AnnotationFilters.get_annotation_name(expression_context, key) is not None
        ):
            return
        lookup_info = expression_context.model._meta.get_lookup_info(key)
        dialect = expression_context.dialect
        lookup_name = f"__{lookup_info.lookup}" if lookup_info.lookup else "equality"
        if dialect.filter_operators.supports_lookup(lookup_info):
            required_feature = LOOKUP_REQUIRED_FEATURES.get(lookup_info.lookup)
            if required_feature is None and lookup_info.field_lookup is not None:
                required_feature = lookup_info.field_lookup.required_feature
            if required_feature is None and lookup_info.transforms and isinstance(lookup_info.field, Field):
                required_feature = lookup_info.field.path_required_feature
            if required_feature is None:
                return
            if getattr(expression_context.connection.features, required_feature):
                if not lookup_info.transforms and isinstance(lookup_info.field, Field):
                    lookup_info.field.raise_if_unsupported_by(expression_context.connection)
                return
            raise UnSupportedError(
                f"{expression_context.model.__name__}.objects.filter({key}=...) can't run on the "
                f"{expression_context.connection.connection_alias!r} connection: the {lookup_name} lookup needs "
                f"features.{required_feature}, which the connection doesn't have"
            )
        lookup_name = f"__{lookup_info.lookup}" if lookup_info.lookup else "equality"
        if lookup_info.dialects is not None and dialect.name not in lookup_info.dialects:
            reason = f"its field only exists on {', '.join(sorted(lookup_info.dialects))}"
        elif not dialect.filter_operators.has_columns_of(lookup_info):
            reason = "the dialect has no column type for its field"
        elif lookup_info.requires_extension is not None and not dialect.features.supports_extensions:
            reason = f"it needs the {lookup_info.requires_extension} extension"
        elif (dialect_reason := dialect.filter_operators.get_unsupported_reason(lookup_info)) is not None:
            reason = dialect_reason
        else:
            reason = f"the {dialect.name} dialect doesn't implement the {lookup_name} lookup"
        raise UnSupportedError(
            f"{expression_context.model.__name__}.objects.filter({key}=...) can't run on {dialect.name}: {reason}"
        )

    @staticmethod
    def get_unknown_key_error(expression_context: ExpressionContext, key: str) -> FieldError:
        """The error of a filter key naming nothing the model or the query has.

        Args:
            expression_context: The context the filter is resolved in.
            key: The filter key.

        Returns:
            The error.
        """
        meta = expression_context.model._meta
        allowed = sorted(meta.fields | meta.fetch_fields | set(expression_context.annotations))
        path_prefix = expression_context.select_related_path_prefix
        full_key = f"{path_prefix}__{key}" if path_prefix else key
        return FieldError(
            f"Unknown filter param '{full_key}': {expression_context.model.__name__} has no field or lookup "
            f"'{key}'. Allowed base values are {allowed}"
        )
