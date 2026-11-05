from __future__ import annotations

from collections.abc import Sequence
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.dialects.postgresql.fields.constants import TEXT_SEARCH_VECTOR_VALUE_TYPE
from hare.dialects.postgresql.search.messages import VECTOR_EXPRESSION_CONFLICT
from hare.exceptions import ConfigurationError
from hare.fields import CharField, Field, GeneratedField, TextField
from hare.fields.generation.generated_column import GeneratedColumn

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.expression import Expression
    from hare.query.filters.lookups.field_lookup import FieldLookup


class TSVectorField(Field[str]):
    """A writable search-vector column or a computed vector expression.

    Args:
        source_fields: The short generated form: each source becomes a SearchVector.
        config: Default configuration of nested SearchVector expressions and string searches.
            An explicit SearchVector or SearchQuery configuration takes precedence.
        weights: Per-source A-D weights in the short form.
        stored: Store the computed column. A plain column has no generated storage clause.
        expression: A vector expression, including SearchVector combined with +. Cannot be
            combined with source_fields or weights. Without a common config, searches require
            an explicit configured SearchQuery; each generated vector must have a configuration.

    A supplied expression is never mutated. Both generated forms use the normal computed-column
    declaration, dependency, renaming and dialect rendering paths. TSVectorField() remains an
    ordinary writable column.
    """

    argument_value_type = TEXT_SEARCH_VECTOR_VALUE_TYPE

    COLUMN_TYPE_FROM_DIALECT = True

    SQL_TYPE = "TSVECTOR"
    field_type = str
    allows_generated = True

    def get_generation_expression(self) -> Expression | None:
        """Normalize either public declaration into one expression without changing its inputs."""
        from hare.search.vector.combined_search_vector import CombinedSearchVector
        from hare.search.vector.search_vector import SearchVector

        if self.expression is not None:
            return self.expression
        expression: SearchVector | CombinedSearchVector | None = None
        for position, name in enumerate(self.source_fields):
            vector = SearchVector(name, weight=None if self.weights is None else self.weights[position])
            expression = vector if expression is None else expression + vector
        return expression

    def get_generated_from_field_names(self) -> tuple[str, ...]:
        return GeneratedColumn(self.get_generation_expression(), self.stored).get_field_names()

    def with_renamed_generated_from_field(self, old_name: str, new_name: str) -> Field[Any]:
        if self.expression is None:
            if old_name not in self.source_fields:
                return self
            renamed = copy(self)
            renamed.source_fields = tuple(new_name if name == old_name else name for name in self.source_fields)
            return renamed
        declaration = GeneratedColumn(self.expression, self.stored)
        renamed_declaration = declaration.with_renamed_field(old_name, new_name)
        if renamed_declaration is declaration:
            return self
        renamed = copy(self)
        renamed.expression = renamed_declaration.expression
        return renamed

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups, with ``__search`` matching the stored vector as it is, in the
        field's text search configuration, rather than a ``to_tsvector()`` of it."""
        lookups = super().get_lookups()
        lookups["search"] = lookups["search"].with_changes(
            is_tsvector=True,
            search_config=self.config,
            requires_search_query_config=self.expression is not None and self.config is None,
        )
        return lookups

    def __init__(
        self,
        source_fields: Sequence[str] | str | None = None,
        config: str | None = None,
        weights: Sequence[str] | None = None,
        stored: bool = True,
        *,
        expression: Expression | None = None,
        **kwargs: Any,
    ) -> None:
        if expression is not None and (source_fields is not None or weights is not None):
            raise ConfigurationError(VECTOR_EXPRESSION_CONFLICT)
        if expression is not None and not callable(getattr(expression, "get_result", None)):
            raise ConfigurationError("TSVectorField.expression must provide get_result().")
        if isinstance(source_fields, str):
            source_fields = (source_fields,)
        self.source_fields = tuple(source_fields or ())
        self.expression = expression
        self.config = config
        self.weights = tuple(weights) if weights is not None else None
        if kwargs.get("generated") and expression is None and not self.source_fields:
            raise ConfigurationError("TSVectorField generated columns require source_fields or expression.")
        if self.weights is not None and len(self.weights) != len(self.source_fields):
            raise ConfigurationError("TSVectorField weights must match source_fields length.")
        if expression is None and not self.source_fields:
            stored = False
        generated = expression is not None or (bool(self.source_fields) and stored)
        if "generated" in kwargs and kwargs.pop("generated") != generated:
            raise ConfigurationError("TSVectorField generated conflicts with its expression/storage declaration.")
        if generated and expression is None and config is None:
            raise ConfigurationError("TSVectorField generated/stored columns require an explicit config.")
        super().__init__(generated=generated, **kwargs)
        self.stored = stored

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # generated= follows stored.
        kwargs.pop("generated", None)
        # No source fields is the default, and so is the stored flag their absence forces.
        if not self.source_fields:
            kwargs.pop("source_fields", None)
            if self.expression is None:
                kwargs.pop("stored", None)
        return path, args, kwargs

    @staticmethod
    def is_text_source_field(field: Field[Any] | None) -> bool:
        """Whether a vector source is a text column, usable by ``TO_TSVECTOR`` without a cast.

        Args:
            field: The source's field, or None when unknown.

        Returns:
            True for a CharField/TextField/CitextField source.
        """
        effective_field = GeneratedField.get_effective_field(field) if field is not None else None
        return isinstance(effective_field, (CharField, TextField, CitextField))

    def get_generated_sql(self, dialect: Dialect) -> str | None:
        if not self.generated:
            return None
        from hare.dialects.postgresql.search.configured_search_vector import ConfiguredSearchVector

        expression = self.get_generation_expression()
        # generated is derived from an explicit expression or nonempty stored source_fields.
        assert expression is not None  # nosec B101
        configured = ConfiguredSearchVector(expression, self.config)
        return GeneratedColumn(configured, self.stored).get_sql(self, dialect)
