from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.classes.class_path import ClassPath

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.features import Features
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class TableOptions:
    """How one dialect stores a model's table - declared in ``Meta.table_options``, one entry per
    dialect::

        class Event(Model):
            class Meta:
                table_options = [
                    SqliteTableOptions(without_rowid=True),
                    PostgresqlTableOptions(tablespace="fast_disk", storage_parameters={"fillfactor": 70}),
                ]

    A connection uses the entry of its own dialect and ignores the others, so one model runs on
    every database it is routed to. A dialect package declares its own subclass: the options its
    ``CREATE TABLE`` takes, written before the word ``TABLE`` (``get_create_prefix_sql``) and after
    the column list (``get_create_suffix_sql``), and - in its schema editor - how a change of them
    is applied (by default the table is rebuilt with the new options).

    Attributes:
        dialect_name: The name of the dialect the options are for.
    """

    dialect_name: ClassVar[str]

    @classmethod
    def from_observed(cls, properties: Mapping[str, Any]) -> Self | None:
        """Builds the options an existing table was created with, from what the dialect's
        introspector read in its catalog - ``inspectdb`` writes them into ``Meta.table_options``
        and ``hare drift`` compares them with the declared ones.

        Args:
            properties: The table's storage properties by option name; a name the class doesn't
                declare is ignored.

        Returns:
            The options, None when every one of them has its default.
        """
        option_names = {option.name for option in dataclasses.fields(cls) if option.init}
        options = cls(**{name: value for name, value in properties.items() if name in option_names})
        return None if options == cls() else options

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects options the model's table can't be created with - checked before its DDL is
        written, so the mistake gets a clear message rather than the database's own error.

        Args:
            model: The model.
            features: The features of the connection the table is created on.

        Raises:
            ConfigurationError: The options don't fit the model.
            UnSupportedError: The connection can't create a table with the options.
        """

    def raise_if_unsampled(self, model: type[Model]) -> None:
        """Rejects a sample of the table (``sample()``) its options don't allow - nothing by default.

        Args:
            model: The model.

        Raises:
            QueryError: The table can't be read in a sample.
        """

    def get_column_type(self, column_type: str) -> str:
        """Returns the type a column of the table is declared with.

        Args:
            column_type: The dialect's column type of the column's field.

        Returns:
            The type - the field's own by default.
        """
        return column_type

    def get_column_clauses_sql(self, field_name: str) -> str:
        """Returns what the options add to a column's definition, after its type, default and inline
        comment - a compression codec, a time to live.

        Args:
            field_name: The column's field.

        Returns:
            The clauses, each with its leading space - none by default.
        """
        return ""

    def get_after_create_sqls(
        self, model: type[Model], table_sql: str, quote: Callable[[str], str], features: Features
    ) -> list[str]:
        """Returns the statements the options run right after the table's ``CREATE TABLE`` - what
        the table gets by a statement of its own.

        Args:
            model: The model the table is created for.
            table_sql: The created table, quoted - the model's own, or the copy a rebuild fills.
            quote: Quotes an identifier.
            features: The features of the connection the DDL runs on.

        Returns:
            The statements - none by default.
        """
        return []

    def keeps_row_versions(self) -> bool:
        """Whether the table keeps several rows of one key - versions of a row the database merges
        into one: the key is no uniqueness there.

        Returns:
            False by default.
        """
        return False

    def get_storage_table_name(self, table_name: str) -> str:
        """Returns the table the rows of a model are stored in - the one the DDL of the model's
        columns and storage is written for.

        Args:
            table_name: The model's table.

        Returns:
            The model's own table by default.
        """
        return table_name

    def get_companion_table_sqls(
        self, model: type[Model], quote: Callable[[str], str], client: DatabaseClient, *, replaces: bool
    ) -> list[str]:
        """Returns the statements creating the table the options put before the one storing the rows
        - a table spreading them over the servers of a cluster.

        Args:
            model: The model.
            quote: Quotes an identifier.
            client: The connection the DDL runs on.
            replaces: Whether the table exists and is made anew - after its model's columns changed.

        Returns:
            The statements - none by default.
        """
        return []

    def get_create_prefix_sql(self) -> str:
        """Returns what ``CREATE TABLE`` takes before the word ``TABLE``.

        Returns:
            The SQL with its trailing space, or an empty string.
        """
        return ""

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        """Returns what ``CREATE TABLE`` takes after the column list.

        Args:
            model: The model whose table is created.
            quote: Quotes an identifier.

        Returns:
            The SQL with its leading space, or an empty string.
        """
        return ""

    def get_partitions(self) -> dict[str, Any]:
        """The partitions of the table the migrations add (``AddPartition``) and remove
        (``RemovePartition``) one at a time, by name - none by default. A partition object names
        the dialect it belongs to as its ``dialect_name``.

        Returns:
            The partitions.
        """
        return {}

    def with_field_names(self, column_to_field_name: Mapping[str, str]) -> Self:
        """The options naming model fields where they name columns - options read from a database
        name columns, a model's declaration fields.

        Args:
            column_to_field_name: Column name -> the name of the field owning it.

        Returns:
            The options - themselves by default: they name no column.
        """
        return self

    def can_change_partitions_to(self, new_options: TableOptions) -> bool:
        """Whether the partitions of other options of the dialect are reached from these by adding
        and removing partitions one at a time - else the change is one of the table as a whole.

        Args:
            new_options: The options changed to.

        Returns:
            False by default - the options have no such partitions.
        """
        return False

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        """The options with another set of the partitions ``get_partitions()`` returns.

        Args:
            partitions: The partitions, by name.

        Returns:
            The options.
        """
        return self

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """Returns how a migration file rebuilds the options: the class path and the arguments that
        differ from the defaults.

        Returns:
            The path, positional arguments and keyword arguments.
        """
        kwargs = {}
        for option in dataclasses.fields(self):
            default = option.default
            if option.default_factory is not dataclasses.MISSING:
                default = option.default_factory()
            if getattr(self, option.name) != default:
                kwargs[option.name] = getattr(self, option.name)
        return ClassPath.get(type(self)), [], kwargs
