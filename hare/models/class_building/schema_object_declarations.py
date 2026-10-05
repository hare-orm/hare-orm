from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.enums import RowLevelSecurity
from hare.ddl.table_options import TableOptions
from hare.exceptions import ConfigurationError
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class SchemaObjectDeclarations:
    """The schema objects a model's Meta declares beside its table - views, materialized views,
    functions, sequences, policies, grants - with its row level security and its table options per
    dialect."""

    @staticmethod
    def get_schema_objects(
        meta: type[Model.Meta] | None,
        option: ModelOption,
        declaration_class: type[Any],
        excluded_class: type[Any] | None = None,
    ) -> tuple[Any, ...]:
        """Reads and checks a ``Meta`` list of declared database objects - views, functions,
        sequences, policies, grants.

        Args:
            meta: The model's Meta.
            option: The option.
            declaration_class: The class of its entries.
            excluded_class: A subclass declared in another option.

        Returns:
            The entries.

        Raises:
            ConfigurationError: The option isn't a list or tuple, an entry is of another class, or
                two entries share a name (two grants are the same).
        """
        declared = getattr(meta, option, ())
        if not isinstance(declared, (list, tuple)):
            raise ConfigurationError(f"Meta.{option} must be a list, got {declared!r}")
        names: set[str] = set()
        entries: list[Any] = []
        for entry in declared:
            if not isinstance(entry, declaration_class) or (
                excluded_class is not None and isinstance(entry, excluded_class)
            ):
                raise ConfigurationError(f"Meta.{option} entries must be {declaration_class.__name__}, got {entry!r}")
            if entry.name is None:
                if entry in entries:
                    raise ConfigurationError(f"Meta.{option} declares {entry!r} twice")
            elif entry.name in names:
                raise ConfigurationError(f"Meta.{option} declares two objects named {entry.name!r}")
            else:
                names.add(entry.name)
            entries.append(entry)
        return tuple(entries)

    @staticmethod
    def get_row_level_security(meta: type[Model.Meta] | None) -> RowLevelSecurity | None:
        """Reads and checks ``Meta.row_level_security``.

        Args:
            meta: The model's Meta.

        Returns:
            The setting; None when row level security is off.

        Raises:
            ConfigurationError: The value isn't a ``RowLevelSecurity`` or None.
        """
        value = getattr(meta, ModelOption.ROW_LEVEL_SECURITY, None)
        if value is None:
            return None
        if value not in set(RowLevelSecurity):
            raise ConfigurationError(
                f"Meta.row_level_security must be one of {', '.join(RowLevelSecurity)} or None, got {value!r}"
            )
        return RowLevelSecurity(value)

    @staticmethod
    def get_declared_table_options(meta: type[Model.Meta] | None) -> tuple[TableOptions, ...]:
        """Reads and checks ``Meta.table_options``.

        Args:
            meta: The model's Meta.

        Returns:
            The options, one entry per dialect.

        Raises:
            ConfigurationError: An entry isn't a ``TableOptions``, or two are for one dialect.
        """
        table_options = tuple(getattr(meta, ModelOption.TABLE_OPTIONS, ()))
        dialect_names: set[str] = set()
        for options in table_options:
            if not isinstance(options, TableOptions):
                raise ConfigurationError(
                    f"Meta.table_options entries must be TableOptions of a dialect, got {options!r}"
                )
            if options.dialect_name in dialect_names:
                raise ConfigurationError(f"Meta.table_options has two entries for the {options.dialect_name} dialect")
            dialect_names.add(options.dialect_name)
        return table_options
