from __future__ import annotations

import functools
import operator
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import IntegrityError
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.query.enums import Lookup
from hare.query.expressions import Q
from hare.query.plans.description.hashable_values import HashableValues
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.constants import HIDDEN_PARAMETER_TEXT

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class UniqueChecks:
    """The uniqueness a model declares, checked by hare before its rows are written - on a database
    keeping none of it (``Features.checks_constraints_before_write``): a value already held by another
    row, or by another row of the same write, is refused as the database would refuse it. One query per
    declared uniqueness per batch; a row another process writes in between the check and the write
    isn't seen - the writes of this process hold their values (``UniqueKeyLocks``)."""

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
            if changed_attribute_names is not None and not uniqueness.reads_any(changed_attribute_names):
                continue
            values_by_instance = cls.get_values_by_object(
                model, connection, uniqueness, objs, series_keyed_ids if is_key else frozenset()
            )
            if not values_by_instance:
                continue
            existing = await cls.fetch_existing(
                model, connection, uniqueness, [values for values, _instance in values_by_instance.values()]
            )
            for value_key, existing_keys in existing.items():
                values, instance = values_by_instance[value_key]
                # A new row is no row stored already - a saved instance written again (force_create)
                # too; a table keeping versions of a row stores the saved one's earlier versions.
                saved_row_key = instance.pk if is_update or (keeps_row_versions and instance._saved_in_db) else None
                if any(key != saved_row_key for key in existing_keys):
                    raise cls.get_error(model, connection, uniqueness, values)

    @classmethod
    def get_values_by_object(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        uniqueness: DeclaredUniqueness,
        objs: Sequence[Model],
        skipped_object_ids: set[int] | frozenset[int],
    ) -> dict[Any, tuple[tuple[Any, ...], Model]]:
        """The values each obj holds in the fields of a uniqueness - but the values unique by
        themselves (a NULL among them) and the objs the uniqueness doesn't hold for - by their key
        (``get_value_key()``).

        Args:
            model: The model.
            connection: The connection.
            uniqueness: The uniqueness.
            objs: The objs.
            skipped_object_ids: The ``id()`` of the objs left out.

        Returns:
            The values and the obj holding them, by the values' key.

        Raises:
            IntegrityError: Two of the objs hold the same values.
        """
        values_by_object: dict[Any, tuple[tuple[Any, ...], Model]] = {}
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
            if values_by_object.setdefault(cls.get_value_key(values), (values, obj))[1] is not obj:
                raise cls.get_error(model, connection, uniqueness, values)
        return values_by_object

    @staticmethod
    def get_value_key(values: tuple[Any, ...]) -> Any:
        """The values of a uniqueness as a key of a dict - themselves where they hash, a key equal for
        equal values where one holds a list or a dict (an array, a map).

        Args:
            values: The values.

        Returns:
            The key.
        """
        try:
            hash(values)
        except TypeError:
            return HashableValues.get_hashable_value(values)
        return values

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
            The keys of the rows holding each of the values found, by the values' key
            (``get_value_key()``).
        """
        attribute_names = uniqueness.attribute_names
        queryset = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True)).using(
            connection
        )
        if uniqueness.condition is not None:
            queryset = queryset.filter(uniqueness.condition)
        key_attribute_names = model._meta.primary_key_attribute_names
        existing: dict[Any, list[Any]] = {}
        # The rows holding a value of the first field are read by one list of them - the database reads a
        # long one as data - and the rows holding the other fields' values too are kept.
        width = len(attribute_names)
        # The values of one field are each listed once already.
        get_value_key = UniqueChecks.get_value_key
        wanted = {get_value_key(values) for values in value_rows} if width > 1 else None
        first_values = (
            [values[0] for values in value_rows]
            if wanted is None
            else list({get_value_key((values[0],)): values[0] for values in value_rows}.values())
        )
        # A field taking no __in (an array) is matched by its values one by one.
        first_name = attribute_names[0]
        takes_in = Lookup.IN in model._meta.fields_map[first_name].get_lookups()
        # The key read once where it is the uniqueness itself.
        selected_names = [*attribute_names, *(name for name in key_attribute_names if name not in attribute_names)]
        key_positions = [selected_names.index(name) for name in key_attribute_names]
        chunk_size = connection.features.max_bind_parameters
        for start in range(0, len(first_values), chunk_size):
            chunk = first_values[start : start + chunk_size]
            matching = (
                queryset.filter(**{f"{first_name}__in": chunk})
                if takes_in
                else queryset.filter(functools.reduce(operator.or_, (Q(**{first_name: value}) for value in chunk)))
            )
            for row in await matching.values_list(*selected_names):
                values = tuple(row[:width])
                value_key = get_value_key(values)
                if wanted is not None and value_key not in wanted:
                    continue
                key = (
                    row[key_positions[0]]
                    if len(key_positions) == 1
                    else tuple(row[position] for position in key_positions)
                )
                existing.setdefault(value_key, []).append(key)
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
        sensitive_fields = model._meta.sensitive_fields
        values_text = ", ".join(
            f"{name}={HIDDEN_PARAMETER_TEXT if name in sensitive_fields else repr(value)}"
            for name, value in zip(uniqueness.attribute_names, values, strict=True)
        )
        return IntegrityError(
            f"{model.__name__}: another row holds {values_text} - {uniqueness.description} is unique, and the "
            f"{connection.dialect.name} database keeps no uniqueness: hare checked it"
        )
