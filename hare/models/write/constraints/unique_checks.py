from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import IntegrityError
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class UniqueChecks:
    """The uniqueness a model declares, checked by hare before its rows are written - on a database
    keeping none of it (``Features.checks_constraints_before_write``): a value already held by another
    row, or by another row of the same write, is refused as the database would refuse it. One query per
    declared uniqueness per batch; a row written in between the check and the write isn't seen."""

    @classmethod
    async def check_rows(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        objs: Sequence[Model],
        changed_attribute_names: set[str] | None = None,
        series_keyed_ids: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        """Refuses rows breaking a declared uniqueness.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be written.
            changed_attribute_names: The attributes an update changes - the uniqueness of the others
                holds already; None for new rows.
            series_keyed_ids: The ``id()`` of the objs keyed by a series - their keys are unique by
                themselves.

        Raises:
            IntegrityError: A row holds values of another row in a set of fields declared unique.
        """
        is_update = changed_attribute_names is not None
        key_attribute_names = tuple(
            DeclaredUniqueness.get_attribute_names(model, model._meta.primary_key_attribute_names)
        )
        table_options = model._meta.get_table_options(connection.dialect)
        keeps_row_versions = table_options is not None and table_options.keeps_row_versions()
        for uniqueness in DeclaredUniqueness.get_declared(model):
            is_key = uniqueness.attribute_names == key_attribute_names
            # A key never changes in an update; a table keeping versions of a row keeps several rows of
            # one key.
            if is_key and (is_update or keeps_row_versions):
                continue
            if changed_attribute_names is not None and changed_attribute_names.isdisjoint(uniqueness.attribute_names):
                continue
            values_by_instance = cls.get_values_by_object(
                model, connection, uniqueness, objs, series_keyed_ids if is_key else frozenset()
            )
            if not values_by_instance:
                continue
            existing = await cls.fetch_existing(model, connection, uniqueness, list(values_by_instance))
            for values, existing_keys in existing.items():
                instance = values_by_instance[values]
                own_key = instance.pk if is_update or instance._saved_in_db else None
                if any(key != own_key for key in existing_keys):
                    raise cls.get_error(model, connection, uniqueness, values)

    @classmethod
    def get_values_by_object(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        uniqueness: DeclaredUniqueness,
        objs: Sequence[Model],
        skipped_object_ids: set[int] | frozenset[int],
    ) -> dict[tuple[Any, ...], Model]:
        """The values each obj holds in the fields of a uniqueness - but the values unique by
        themselves (a NULL among them) and the objs the uniqueness doesn't hold for.

        Args:
            model: The model.
            connection: The connection.
            uniqueness: The uniqueness.
            objs: The objs.
            skipped_object_ids: The ``id()`` of the objs left out.

        Returns:
            The obj holding each of the values.

        Raises:
            IntegrityError: Two of the objs hold the same values.
        """
        values_by_object: dict[tuple[Any, ...], Model] = {}
        read_values = uniqueness.read_values
        nulls_distinct = uniqueness.nulls_distinct
        checks_condition = uniqueness.condition is not None
        is_single = len(uniqueness.attribute_names) == 1
        for obj in objs:
            if (skipped_object_ids and id(obj) in skipped_object_ids) or (
                checks_condition and not uniqueness.holds_for(obj)
            ):
                continue
            values = read_values(obj)
            # By identity - `None in values` asks each value's __eq__.
            if nulls_distinct and (values[0] is None if is_single else any(value is None for value in values)):
                continue
            if values_by_object.setdefault(values, obj) is not obj:
                raise cls.get_error(model, connection, uniqueness, values)
        return values_by_object

    @staticmethod
    async def fetch_existing(
        model: type[Model],
        connection: DatabaseClient,
        uniqueness: DeclaredUniqueness,
        value_rows: list[tuple[Any, ...]],
    ) -> dict[tuple[Any, ...], list[Any]]:
        """The stored rows holding any of the values in the fields - of every tenant, deleted ones too.

        Args:
            model: The model.
            connection: The connection.
            uniqueness: The uniqueness.
            value_rows: The values looked for.

        Returns:
            The keys of the rows holding each of the values found.
        """
        attribute_names = uniqueness.attribute_names
        queryset = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True)).using(
            connection
        )
        if uniqueness.condition is not None:
            queryset = queryset.filter(uniqueness.condition)
        key_attribute_names = model._meta.primary_key_attribute_names
        existing: dict[tuple[Any, ...], list[Any]] = {}
        # The rows holding a value of the first field are read by one list of them - the database reads a
        # long one as data - and the rows holding the other fields' values too are kept.
        width = len(attribute_names)
        # The values of one field are each listed once already.
        wanted = set(value_rows) if width > 1 else None
        first_values = (
            [values[0] for values in value_rows]
            if wanted is None
            else list(dict.fromkeys(values[0] for values in value_rows))
        )
        # The key read once where it is the uniqueness itself.
        selected_names = [*attribute_names, *(name for name in key_attribute_names if name not in attribute_names)]
        key_positions = [selected_names.index(name) for name in key_attribute_names]
        chunk_size = connection.features.max_bind_parameters
        for start in range(0, len(first_values), chunk_size):
            matching = queryset.filter(**{f"{attribute_names[0]}__in": first_values[start : start + chunk_size]})
            for row in await matching.values_list(*selected_names):
                values = tuple(row[:width])
                if wanted is not None and values not in wanted:
                    continue
                key = (
                    row[key_positions[0]]
                    if len(key_positions) == 1
                    else tuple(row[position] for position in key_positions)
                )
                existing.setdefault(values, []).append(key)
        return existing

    @staticmethod
    def get_error(
        model: type[Model], connection: DatabaseClient, uniqueness: DeclaredUniqueness, values: tuple[Any, ...]
    ) -> IntegrityError:
        """The refusal of a value held by another row.

        Args:
            model: The model.
            connection: The connection.
            uniqueness: The uniqueness broken.
            values: The values.

        Returns:
            The error.
        """
        values_text = ", ".join(
            f"{name}={value!r}" for name, value in zip(uniqueness.attribute_names, values, strict=True)
        )
        return IntegrityError(
            f"{model.__name__}: another row holds {values_text} - {uniqueness.description} is unique, and the "
            f"{connection.dialect.name} database keeps no uniqueness: hare checked it"
        )
