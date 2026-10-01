from __future__ import annotations

from collections.abc import Sequence
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.citext import CitextField
from hare.exceptions import ConfigurationError
from hare.fields import CharField, Field, GeneratedField, TextField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.query.filters.field_lookup import FieldLookup


class TSVectorField(Field[str]):
    """A Postgres ``TSVECTOR`` column, optionally generated from other fields on the model.

    Args:
        source_fields: Field names to concatenate into the generated vector. Required if
            ``stored`` (or ``generated``) is true.
        config: The text search configuration to pass to ``TO_TSVECTOR`` (e.g. ``"english"``).
            Required whenever the column is generated/stored.
        weights: Per-source-field weight letters (``"A"``-``"D"``), same length as
            ``source_fields``.
        stored: Whether to generate the column as ``GENERATED ALWAYS ... STORED`` from
            ``source_fields``. Defaults to True, but is forced to False when no ``source_fields``
            are given.

    Raises:
        ConfigurationError: If ``generated`` conflicts with ``stored``, if ``weights`` are given
            without matching ``source_fields``, or if a generated/stored column has no ``config``
            (Postgres's 1-argument ``TO_TSVECTOR`` is only STABLE, not IMMUTABLE, so it can never
            satisfy a ``GENERATED ALWAYS ... STORED`` column).
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})

    SQL_TYPE = "TSVECTOR"
    field_type = str
    allows_generated = True

    def get_generated_from_field_names(self) -> tuple[str, ...]:
        return self.source_fields

    def with_renamed_generated_from_field(self, old_name: str, new_name: str) -> Field[Any]:
        if old_name not in self.source_fields:
            return self
        renamed_field = copy(self)
        renamed_field.source_fields = tuple(new_name if name == old_name else name for name in self.source_fields)
        return renamed_field

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups, with ``__search`` matching the stored vector as it is, in the
        field's text search configuration, rather than a ``to_tsvector()`` of it."""
        lookups = super().get_lookups()
        lookups["search"] = lookups["search"].with_changes(is_tsvector=True, search_config=self.config)
        return lookups

    def __init__(
        self,
        source_fields: Sequence[str] | str | None = None,
        config: str | None = None,
        weights: Sequence[str] | None = None,
        stored: bool = True,
        **kwargs: Any,
    ) -> None:
        if isinstance(source_fields, str):
            source_fields = (source_fields,)
        self.source_fields = tuple(source_fields or ())
        if not self.source_fields and stored:
            stored = False
        if "generated" in kwargs and kwargs["generated"] != stored:
            raise ConfigurationError("TSVectorField 'generated' must match 'stored' when provided.")
        generated = kwargs.pop("generated", stored)
        if generated and not self.source_fields:
            raise ConfigurationError("TSVectorField generated columns require source_fields.")
        if generated and config is None:
            # One-argument TO_TSVECTOR() isn't IMMUTABLE, and a stored generated column needs an
            # immutable expression.
            raise ConfigurationError("TSVectorField generated/stored columns require an explicit config.")
        super().__init__(generated=generated, **kwargs)
        self.config = config
        self.weights = tuple(weights) if weights is not None else None
        self.stored = stored

        if self.weights and not self.source_fields:
            raise ConfigurationError("TSVectorField weights require source_fields.")
        if self.weights and len(self.weights) != len(self.source_fields):
            raise ConfigurationError("TSVectorField weights must match source_fields length.")

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # No source fields is the default, and so is the stored flag their absence forces.
        if not self.source_fields:
            kwargs.pop("source_fields", None)
            kwargs.pop("stored", None)
        return path, args, kwargs

    def _quote_sql_literal(self, value: str) -> str:
        escaped = value.replace("'", "''")
        return f"'{escaped}'"

    @staticmethod
    def is_text_source_field(field: Field[Any] | None) -> bool:
        """Whether a vector source is a text column, usable by ``TO_TSVECTOR`` without a cast.

        Args:
            field: The source's field, or None when unknown.

        Returns:
            True for a CharField/TextField/CitextField source.
        """
        effective_field = field.output_field if isinstance(field, GeneratedField) else field
        return isinstance(effective_field, (CharField, TextField, CitextField))

    def _to_tsvector_sql(self, db_field: str, field: Field[Any]) -> str:
        column_sql = f'"{db_field}"' if self.is_text_source_field(field) else f'CAST("{db_field}" AS TEXT)'
        field_sql = f"COALESCE({column_sql}, '')"
        if self.config is not None:
            return f"TO_TSVECTOR({self._quote_sql_literal(self.config)},{field_sql})"
        return f"TO_TSVECTOR({field_sql})"

    def get_generated_sql(self, dialect: Dialect) -> str | None:
        if not self.stored:
            return None
        parts: list[str] = []
        for idx, field_name in enumerate(self.source_fields):
            field = self.model._meta.fields_map.get(field_name)
            if field is None:
                raise ConfigurationError(f"Unknown source field '{field_name}'.")
            if not field.has_db_field:
                raise ConfigurationError(f"Source field '{field_name}' does not map to a database column.")
            db_field = field.source_field or field.model_field_name
            vector_sql = self._to_tsvector_sql(db_field, field)
            if self.weights is not None:
                weight = self._quote_sql_literal(self.weights[idx])
                vector_sql = f"SETWEIGHT({vector_sql},{weight})"
            parts.append(vector_sql)
        expression = " || ".join(parts)
        return f"GENERATED ALWAYS AS ({expression}) STORED"
