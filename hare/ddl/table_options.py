from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.utils.class_path import ClassPath

if TYPE_CHECKING:  # pragma: nocoverage
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

    def raise_if_unsupported(self, model: type[Model]) -> None:
        """Rejects options the model's table can't be created with - checked before its DDL is
        written, so the mistake gets a clear message rather than the database's own error.

        Args:
            model: The model.

        Raises:
            ConfigurationError: The options don't fit the model.
        """

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
