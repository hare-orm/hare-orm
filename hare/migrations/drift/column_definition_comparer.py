from __future__ import annotations

from copy import copy
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.text.char_field import CharField
from hare.fields.db_defaults.now import Now
from hare.fields.db_defaults.sql_default import SqlDefault
from hare.fields.enums import NowValueType
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.inspectdb.constants import AMBIGUOUS_REASON_SENTINEL_KWARG, BASE_FIELD_SENTINEL_KWARG
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.inspected_field_specification import InspectedFieldSpecification
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.drift.constants import CURRENT_TIME_DEFAULT_FINGERPRINT, SQL_FINGERPRINT_NOISE_RE
from hare.migrations.writer.migration_writer import MigrationWriter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect


class ColumnDefinitionComparer:
    """Compares a field's declared column - type, DEFAULT - with the introspected one. Every
    comparison errs on "equal": a spelling it can't normalize is never reported."""

    @staticmethod
    def get_declared_column_type(field: Field[Any], dialect: str | Dialect) -> str | None:
        """The type a field's single column is created with.

        Args:
            field: The declared field.
            dialect: The database dialect.

        Returns:
            The SQL type - a relation's is its target key's; None for a generated primary key
            (its serial/autoincrement column), a relation over several columns or one whose
            target isn't resolved, and a field without a column of its own.
        """
        if isinstance(field, ManyToManyFieldInstance):
            return None
        if isinstance(field, ForeignKeyFieldInstance):
            if len(field.db_column_names) != 1:
                return None
            try:
                key_field = field.to_field_instance
            except (AttributeError, ConfigurationError):
                return None
            return key_field.get_column_type(DialectRegistry.get_dialect(dialect))
        if isinstance(field, RelationalField) or (field.pk and field.generated) or not field.has_db_field:
            return None
        return field.get_column_type(DialectRegistry.get_dialect(dialect))

    @staticmethod
    def column_types_differ(dialect: str | Dialect, declared_type: str, column: ColumnInfo) -> bool:
        """Whether a column's type is not the one its field declares, by the rules of the dialect's
        introspector (``SchemaIntrospector.column_types_differ``).

        Args:
            dialect: The name of the database's dialect.
            declared_type: The field's SQL type.
            column: The introspected column.

        Returns:
            Whether they differ; False on a dialect without an introspector.
        """
        introspector_class = DialectRegistry.get_dialect(dialect).introspector_class
        return introspector_class is not None and introspector_class.column_types_differ(declared_type, column)

    @staticmethod
    def get_sql_fingerprint(sql: str, dialect: str | Dialect) -> str:
        """A default expression's text with what the database changes when it echoes one back
        left out - letter case, whitespace, parentheses, type casts.

        Args:
            sql: The expression.
            dialect: The database dialect, whose introspector reads a literal back.

        Returns:
            The fingerprint - a number or quoted string literal in its literal fingerprint.
        """
        introspector_class = DatabaseCatalog.get_dialect_introspector_class(dialect)
        fingerprint = SQL_FINGERPRINT_NOISE_RE.sub("", introspector_class.strip_type_casts(sql.lower()))
        literal_fingerprint = ColumnDefinitionComparer.get_literal_fingerprint(
            introspector_class.parse_db_default(fingerprint)
        )
        return literal_fingerprint if literal_fingerprint is not None else fingerprint

    @staticmethod
    def get_literal_fingerprint(value: Any) -> str | None:
        """A plain default value's fingerprint.

        Args:
            value: A boolean, number or string.

        Returns:
            ``1``/``0`` for a boolean (SQLite stores one as a number), a number without trailing
            zeros, a quoted string; None for any other value.
        """
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int | float | Decimal):
            try:
                return format(Decimal(str(value)).normalize(), "f")
            except InvalidOperation:
                return None
        if isinstance(value, str):
            return f"'{value}'"
        return None

    @staticmethod
    def get_declared_default_fingerprint(field: Field[Any], dialect: str | Dialect) -> str | None:
        """The fingerprint of a field's ``db_default``.

        Args:
            field: A field with a db_default.
            dialect: The database dialect.

        Returns:
            The fingerprint; None for a value it has no fingerprint for (a date, a JSON document).
        """
        db_default = field.db_default
        if isinstance(db_default, Now) and db_default.value_type == NowValueType.DATETIME:
            return CURRENT_TIME_DEFAULT_FINGERPRINT
        if hasattr(db_default, "get_sql"):
            fingerprint = ColumnDefinitionComparer.get_sql_fingerprint(
                db_default.get_sql(DialectRegistry.get_dialect(dialect)), dialect
            )
            if fingerprint == ColumnDefinitionComparer.get_sql_fingerprint(
                Now().get_sql(DialectRegistry.get_dialect(dialect)), dialect
            ):
                return CURRENT_TIME_DEFAULT_FINGERPRINT
            return fingerprint
        return ColumnDefinitionComparer.get_literal_fingerprint(db_default)

    @staticmethod
    def get_observed_default_fingerprint(db_default: Any, dialect: str | Dialect) -> str | None:
        """The fingerprint of an introspected column default.

        Args:
            db_default: The default as SchemaIntrospector parsed it.
            dialect: The database dialect.

        Returns:
            The fingerprint; None for a value it has no fingerprint for.
        """
        if isinstance(db_default, Now):
            return CURRENT_TIME_DEFAULT_FINGERPRINT
        if isinstance(db_default, SqlDefault):
            fingerprint = ColumnDefinitionComparer.get_sql_fingerprint(db_default.sql, dialect)
            if fingerprint == ColumnDefinitionComparer.get_sql_fingerprint(
                Now().get_sql(DialectRegistry.get_dialect(dialect)), dialect
            ):
                return CURRENT_TIME_DEFAULT_FINGERPRINT
            return fingerprint
        return ColumnDefinitionComparer.get_literal_fingerprint(db_default)

    @staticmethod
    def db_defaults_differ(field: Field[Any], column: ColumnInfo, dialect: str | Dialect) -> bool:
        """Whether a column lacks its field's ``db_default`` or has another one.

        Args:
            field: A field with a db_default.
            column: The introspected column.
            dialect: The database dialect.

        Returns:
            True when the column has no DEFAULT while the field declares a non-NULL one, or when
            both have one and their fingerprints differ.
        """
        if field.db_default is None:
            return False
        if column.db_default is None:
            return True
        declared_fingerprint = ColumnDefinitionComparer.get_declared_default_fingerprint(field, dialect)
        observed_fingerprint = ColumnDefinitionComparer.get_observed_default_fingerprint(column.db_default, dialect)
        return (
            declared_fingerprint is not None
            and observed_fingerprint is not None
            and declared_fingerprint != observed_fingerprint
        )

    @staticmethod
    def get_resized_field(field: Field[Any], column: ColumnInfo) -> Field[Any] | None:
        """A copy of a CharField/DecimalField sized like the column, when that is all that differs.

        Args:
            field: The declared field.
            column: The introspected column.

        Returns:
            The resized copy, or None when the column's type differs in more than its size.
        """
        if isinstance(field, CharField) and column.max_length is not None:
            if column.max_length == field.max_length:
                return None
            resized_char_field = copy(field)
            resized_char_field.max_length = column.max_length
            return resized_char_field
        if isinstance(field, DecimalField) and column.numeric_precision is not None:
            if (column.numeric_precision, column.numeric_scale) == (field.max_digits, field.decimal_places):
                return None
            resized_decimal_field = copy(field)
            resized_decimal_field.max_digits = column.numeric_precision
            resized_decimal_field.decimal_places = column.numeric_scale or 0
            return resized_decimal_field
        return None

    @staticmethod
    def get_field_of_column_type(dialect: str | Dialect, field: Field[Any], column: ColumnInfo) -> Field[Any] | None:
        """The field inspectdb reconstructs the column as, carrying the declared field's name,
        column, description and db_default.

        Args:
            dialect: The database dialect.
            field: The declared field.
            column: The introspected column.

        Returns:
            The field, or None when inspectdb has no confident mapping for the column's type or
            the mapping looks the same as the declared field.
        """
        path, kwargs, is_ambiguous = ColumnTypeMapper.map_column_type(dialect, column)
        if is_ambiguous or BASE_FIELD_SENTINEL_KWARG in kwargs:
            return None
        kwargs.pop(AMBIGUOUS_REASON_SENTINEL_KWARG, None)
        try:
            column_field = MigrationWriter.get_callable(path)(
                null=column.nullable, **InspectedFieldSpecification.build_arguments(kwargs)
            )
        except (ConfigurationError, TypeError, ValueError):
            return None
        column_field.model_field_name = field.model_field_name
        column_field.source_field = field.source_field
        column_field.description = field.description
        if field.has_db_default():
            column_field.db_default = field.db_default
        if StateSignatures.get_field_signature(column_field) == StateSignatures.get_field_signature(field):
            return None
        return column_field
