from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import FieldError, QueryError
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.fields.database_default import DatabaseDefault
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.write.instance_values import InstanceValues
from hare.models.write.rules.constants import (
    BLIND_INDEX_REASON,
    COMPOSITE_RELATION_REASON,
    CONFLICT_TENANT_REASON,
    GENERATED_REASON,
    PRIMARY_KEY_REASON,
    REFUSED_FIELD_MESSAGE,
    SOFT_DELETE_REASON,
    UNBOUND_VALUE_MESSAGE,
    UNCOMPUTED_BLIND_INDEX_REASON,
    UNKNOWN_FIELD_MESSAGE,
    VERSION_REASON,
)
from hare.models.write.rules.enums import WriteType
from hare.native.native_modules import NativeModules
from hare.query.expressions.expression import Expression
from hare.sql.terms.term import Term
from hare.time import Timezone

if TYPE_CHECKING:
    import datetime

    from hare.fields.data.temporal.datetime_field import DatetimeField
    from hare.fields.data.temporal.time_field import TimeField
    from hare.fields.encrypted.blind_index_field import BlindIndexField
    from hare.models import Model


TModel = TypeVar("TModel", bound="Model")


class RowWriteRules:
    """What one write of rows of a model writes - built once per model, write type and named fields, and kept:

    - the fields the caller names, as the fields holding columns (``pk``, a forward relation's key fields, a
      generic foreign key's branches), refused with one set of messages when the write can't set them;
    - the blind index of each encrypted field written, written with it;
    - the ``auto_now``/``auto_now_add`` stamps and the version bump the write makes itself;
    - the positions of the ``sensitive=True`` values among the written ones;
    - the fields a report of the write names as changed - the ones the caller named.

    Args:
        model: The written model.
        write_type: What the write does to its rows.
        field_names: The fields the caller names; None for a write of every field the write type writes.
        method_name: The writing method, named in the messages - ``.update()``, ``bulk_update()``.
        stamps_named_fields: Whether an ``auto_now`` field the caller names is stamped all the same - a
            write of objects, whose attribute is no value the caller gave; a write of given values keeps it.
        single_column_relations: Whether a relation is written as one column only - a relation to a
            composite key is refused.
        finds_rows_by_key: Whether the write finds its rows by their primary key - every column of the key is
            refused; a write finding them by a filter refuses the key as a whole, a column of a composite
            key is its to change.

    Raises:
        FieldError: A name is no field stored in the model's table.
        QueryError: The write type can't set a field.
    """

    #: The rules of each model, by write type, names and options.
    RULES_CACHE: ClassVar[ModelCache[dict[tuple[Any, ...], RowWriteRules]]] = ModelCache()
    #: The compiled ``rust.native.rows`` - sets the values of many objects in one call; None where it isn't
    #: built.
    native_rows: ClassVar[Any] = NativeModules.rows

    __slots__ = (
        "model",
        "write_type",
        "method_name",
        "finds_rows_by_key",
        "named_field_names",
        "field_names",
        "stamped_fields",
        "stamped_names",
        "shared_stamp_field",
        "always_stamped_names",
        "unset_stamped_names",
        "saved_attribute_names",
        "written_names",
        "updated_names",
        "version_field_name",
        "blind_indexes",
        "sets_directly",
    )

    def __init__(
        self,
        model: type[Model],
        write_type: WriteType,
        field_names: Sequence[str] | None,
        method_name: str,
        *,
        stamps_named_fields: bool,
        single_column_relations: bool,
        finds_rows_by_key: bool,
    ) -> None:
        meta = model._meta
        self.model = model
        self.write_type = write_type
        self.method_name = method_name
        self.finds_rows_by_key = finds_rows_by_key
        named_field_names: tuple[str, ...] | None = None
        if field_names is not None and write_type is not WriteType.INSERT and "pk" in field_names:
            raise QueryError(REFUSED_FIELD_MESSAGE.format(name="pk", method=method_name, reason=PRIMARY_KEY_REASON))
        if field_names is None:
            written = self.get_every_written_field_name(model, write_type)
        else:
            written = RowWriteRules.get_stored_field_names(
                model, field_names, method_name, single_column_relations=single_column_relations
            )
            for name in written:
                self.raise_if_refused(name)
            named_field_names = tuple(written)
        #: The fields the caller named, as fields holding columns - None for a write of every field.
        self.named_field_names = named_field_names
        blind_index_fields = meta.blind_index_fields
        #: ``(encrypted field, its blind index)`` of each encrypted field written.
        self.blind_indexes: tuple[tuple[str, str], ...] = tuple(
            (name, blind_index_fields[name]) for name in written if name in blind_index_fields
        )
        #: The fields written with the caller's values - the named ones and their blind indexes, in order.
        self.field_names: tuple[str, ...] = tuple(
            dict.fromkeys(
                name
                for written_name in written
                for name in (written_name, blind_index_fields.get(written_name))
                if name is not None
            )
        )
        #: ``(name, field, only_when_unset)`` of each ``auto_now``/``auto_now_add`` field the write stamps -
        #: an ``auto_now_add`` field only when it holds no value yet.
        self.stamped_fields: tuple[tuple[str, DatetimeField[Any] | TimeField[Any], bool], ...] = (
            self.get_stamped_fields(set(self.field_names), stamps_named_fields)
        )
        #: The fields the write stamps.
        self.stamped_names: tuple[str, ...] = tuple(name for name, _field, _only_when_unset in self.stamped_fields)
        #: The one stamped field whose value every stamp takes - the stamped fields are of one class, the value
        #: theirs alike; None when they are of several.
        self.shared_stamp_field: DatetimeField[Any] | TimeField[Any] | None = (
            self.stamped_fields[0][1]
            if self.stamped_fields and len({type(field) for _name, field, _only in self.stamped_fields}) == 1
            else None
        )
        #: The stamped fields set whatever they hold - every one but an ``auto_now_add`` field holding a value.
        self.always_stamped_names: tuple[str, ...] = tuple(
            name for name, _field, only_when_unset in self.stamped_fields if not only_when_unset
        )
        #: The stamped fields set only while they hold no value - an ``auto_now_add`` field.
        self.unset_stamped_names: tuple[str, ...] = tuple(
            name for name, _field, only_when_unset in self.stamped_fields if only_when_unset
        )
        #: The attributes ``stamp()`` sets - their values before it are kept for a failed write.
        self.saved_attribute_names: tuple[str, ...] = (
            *self.stamped_names,
            *(index for _source, index in self.blind_indexes),
        )
        #: ``Meta.optimistic_lock_field`` when the write bumps it - every write of existing rows.
        self.version_field_name: str | None = None if write_type is WriteType.INSERT else meta.optimistic_lock_field
        #: Every field a write of objects writes: the caller's ones, the blind indexes and the stamps.
        self.written_names: tuple[str, ...] = tuple(dict.fromkeys((*self.field_names, *self.stamped_names)))
        #: Every field an update of objects writes: the written ones and the version it bumps.
        self.updated_names: tuple[str, ...] = (
            self.written_names
            if self.version_field_name is None or self.version_field_name in self.written_names
            else (*self.written_names, self.version_field_name)
        )
        #: Whether the stamps and indexes are stored on an object directly - assigning them through the
        #: model's ``__setattr__`` does nothing more.
        self.sets_directly = RowWriteRules.are_plain_attributes(model, self.saved_attribute_names)

    @classmethod
    def get(
        cls,
        model: type[Model],
        write_type: WriteType,
        field_names: Iterable[str] | None,
        method_name: str,
        *,
        stamps_named_fields: bool = True,
        single_column_relations: bool = False,
        finds_rows_by_key: bool = True,
    ) -> RowWriteRules:
        """The rules of a write - built the first time and kept with the model.

        Args:
            model: The written model.
            write_type: What the write does to its rows.
            field_names: The fields the caller names; None for every field the write type writes.
            method_name: The writing method, named in the messages.
            stamps_named_fields: See the class.
            single_column_relations: See the class.
            finds_rows_by_key: See the class.

        Returns:
            The rules.

        Raises:
            FieldError, QueryError: See the class.
        """
        names = None if field_names is None else tuple(field_names)
        key = (write_type, names, method_name, stamps_named_fields, single_column_relations, finds_rows_by_key)
        rules_by_key = cls.RULES_CACHE.get(model)
        if rules_by_key is None:
            rules_by_key = cls.RULES_CACHE[model] = {}
        try:
            rules = rules_by_key.get(key)
            hashable = True
        except TypeError:
            # A name that can't be hashed is no field name - refused while the rules are built.
            rules = None
            hashable = False
        if rules is None:
            rules = cls(
                model,
                write_type,
                names,
                method_name,
                stamps_named_fields=stamps_named_fields,
                single_column_relations=single_column_relations,
                finds_rows_by_key=finds_rows_by_key,
            )
            if hashable:
                rules_by_key[key] = rules
        return rules

    @staticmethod
    def get_stored_field_names(
        model: type[Model],
        names: Iterable[Any],
        method_name: str,
        *,
        expands_generic_keys: bool = True,
        single_column_relations: bool = False,
    ) -> list[str]:
        """The fields holding the columns ``names`` name: ``pk`` the primary key's fields, a forward relation
        its key fields, a generic foreign key the key fields of every branch.

        Args:
            model: The model.
            names: The names as the caller gave them.
            method_name: The method, named in the messages.
            expands_generic_keys: Whether a generic foreign key stands for its branches - it names no column
                of a conflict target, each branch keeping its own uniqueness.
            single_column_relations: Whether a relation to a composite key is refused.

        Returns:
            The field names, each once, in the given order.

        Raises:
            FieldError: A name is no field stored in the model's table, or a generic foreign key not
                expanded.
            QueryError: A relation to a composite key where one column is written.
        """
        meta = model._meta
        fields_map = meta.fields_map
        stored: dict[str, None] = {}
        for name in names:
            field = fields_map.get(name) if isinstance(name, str) else None
            if name == "pk" and field is None:
                stored.update(dict.fromkeys(meta.primary_key_attribute_names))
            elif isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                if single_column_relations and len(field.source_fields) > 1:
                    raise QueryError(
                        REFUSED_FIELD_MESSAGE.format(
                            name=name,
                            method=method_name,
                            reason=COMPOSITE_RELATION_REASON.format(fields=", ".join(field.source_fields)),
                        )
                    )
                stored.update(dict.fromkeys(field.source_fields))
            elif isinstance(name, str) and (generic_field := meta.generic_foreign_key_fields.get(name)) is not None:
                if not expands_generic_keys:
                    raise FieldError(
                        f"{method_name} names {generic_field.get_label()} - each branch keeps its own uniqueness; "
                        f"name the branch instead ({', '.join(repr(branch) for branch in generic_field.branch_names)})"
                    )
                for branch_name in generic_field.branch_names:
                    branch = cast("ForeignKeyFieldInstance[Any]", fields_map[branch_name])
                    stored.update(dict.fromkeys(branch.source_fields))
            elif isinstance(name, str) and name in meta.fields_db_projection:
                stored[name] = None
            else:
                raise FieldError(UNKNOWN_FIELD_MESSAGE.format(name=name, method=method_name, model=model.__name__))
        return list(stored)

    @staticmethod
    def get_every_written_field_name(model: type[Model], write_type: WriteType) -> list[str]:
        """The fields a write of every field writes: an insert every stored field but a generated one; an
        update every field but the primary key, a generated field and ``Meta.soft_delete_field``.

        Args:
            model: The model.
            write_type: What the write does to its rows.

        Returns:
            The field names.
        """
        meta = model._meta
        fields_map = meta.fields_map
        if write_type is WriteType.INSERT:
            return [
                name for name in meta.fields_db_projection if not fields_map[name].generated or fields_map[name].pk
            ]
        primary_key_attribute_names = meta.primary_key_attribute_names
        return [
            name
            for name in meta.fields_db_projection
            if name not in primary_key_attribute_names
            and name != meta.soft_delete_field
            and not fields_map[name].generated
        ]

    def raise_if_refused(self, name: str) -> None:
        """Raises for a field the write type can't set.

        Args:
            name: A field holding a column.

        Raises:
            QueryError: The field is a blind index, the primary key or a generated field an update can't
                set, the version or the soft-delete field of an update, the tenant of a conflict update.
        """
        meta = self.model._meta
        field = meta.fields_map[name]
        write_type = self.write_type
        source_name = getattr(field, "blind_index_source_field_name", None)
        reason: str | None = None
        if source_name is not None and meta.blind_index_fields.get(source_name) == name:
            reason = BLIND_INDEX_REASON.format(source=source_name)
        elif write_type is not WriteType.INSERT and (
            name in meta.primary_key_attribute_names if self.finds_rows_by_key else field.pk
        ):
            reason = PRIMARY_KEY_REASON
        elif field.generated and not (write_type is WriteType.INSERT and field.pk):
            reason = GENERATED_REASON
        elif write_type in {WriteType.UPDATE, WriteType.CONFLICT_UPDATE} and name == meta.optimistic_lock_field:
            reason = VERSION_REASON
        elif write_type in {WriteType.UPDATE, WriteType.CONFLICT_UPDATE} and name == meta.soft_delete_field:
            reason = SOFT_DELETE_REASON
        elif write_type is WriteType.CONFLICT_UPDATE and meta.tenant_field is not None:
            tenant_names = RowWriteRules.get_stored_field_names(self.model, [meta.tenant_field], self.method_name)
            if name in tenant_names:
                reason = CONFLICT_TENANT_REASON
        if reason is not None:
            raise QueryError(REFUSED_FIELD_MESSAGE.format(name=name, method=self.method_name, reason=reason))

    def get_stamped_fields(
        self, written_names: set[str], stamps_named_fields: bool
    ) -> tuple[tuple[str, DatetimeField[Any] | TimeField[Any], bool], ...]:
        """The ``auto_now``/``auto_now_add`` fields the write stamps: an insert each, an update every
        ``auto_now`` field and a written ``auto_now_add`` one still without a value.

        Args:
            written_names: The fields written with the caller's values.
            stamps_named_fields: Whether a named field is stamped all the same.

        Returns:
            ``(name, field, only_when_unset)`` of each.
        """
        fields_map = self.model._meta.fields_map
        named = set() if stamps_named_fields or self.named_field_names is None else set(self.named_field_names)
        stamped: list[tuple[str, Any, bool]] = []
        for name, field in fields_map.items():
            auto_now = getattr(field, "auto_now", False)
            auto_now_add = getattr(field, "auto_now_add", False)
            if not (auto_now or auto_now_add) or name in named:
                continue
            if self.write_type is WriteType.INSERT or auto_now:
                stamped.append((name, field, not auto_now))
            elif name in written_names and stamps_named_fields:
                stamped.append((name, field, True))
        return tuple(stamped)

    @staticmethod
    def are_plain_attributes(model: type[Model], field_names: Iterable[str]) -> bool:
        """Whether assigning the fields on an object only stores the value - the model's ``__setattr__`` is
        ``Model.__setattr__`` and has nothing more to do for them.

        Args:
            model: The model.
            field_names: The fields.

        Returns:
            True when the values may be stored directly.
        """
        # Local import: the Model module imports the write package.
        from hare.models.model import Model

        if model.__setattr__ is not Model.__setattr__:
            return False
        meta = model._meta
        if meta.next_setattr is not object.__setattr__:
            return False
        hooks = meta.get_setattr_hooks()
        return not any(name in hooks for name in field_names)

    @property
    def changed_field_names(self) -> tuple[str, ...] | None:
        """The fields a report of the write names as changed - the ones the caller named; None when the
        caller named none (a write of every field)."""
        return self.named_field_names

    def get_written_names(self) -> list[str]:
        """Every field a write of objects writes: the caller's ones, the blind indexes and the stamps.

        Returns:
            The field names, each once.
        """
        return list(self.written_names)

    def get_updated_names(self) -> list[str]:
        """Every field an update of objects writes: the caller's ones, the blind indexes, the stamps and the
        version it bumps.

        Returns:
            The field names, each once.
        """
        return list(self.updated_names)

    def get_stamp_values(self, now: datetime.datetime) -> list[tuple[str, DatetimeField[Any] | TimeField[Any], Any]]:
        """The value each stamped field takes at ``now``, as a read gives it back.

        Args:
            now: The moment of the write.

        Returns:
            ``(name, field, value)`` of each stamped field.
        """
        values = self.get_stamped_values(now)
        return [(name, field, value) for (name, field, _only), value in zip(self.stamped_fields, values, strict=True)]

    def get_stamped_values(self, now: datetime.datetime | None) -> list[Any]:
        """The value each stamped field takes at ``now``, in the order of ``stamped_fields`` - converted once
        for every field of one class, all of them taking one moment.

        Args:
            now: The moment of the write - the current one when None.

        Returns:
            The values.
        """
        shared_stamp_field = self.shared_stamp_field
        if shared_stamp_field is not None:
            return [shared_stamp_field.get_stamp_value(now)] * len(self.stamped_fields)
        if now is None:
            now = Timezone.now()
        values_by_class: dict[type[Any], Any] = {}
        values = []
        for _name, field, _only_when_unset in self.stamped_fields:
            field_class = type(field)
            value = values_by_class.get(field_class, values_by_class)
            if value is values_by_class:
                value = values_by_class[field_class] = field.get_stamp_value(now)
            values.append(value)
        return values

    def stamp(
        self, objs: Sequence[Model], changed_values: InstanceValues, now: datetime.datetime | None = None
    ) -> None:
        """Gives the objects the stamps and the blind indexes the write makes, remembering what they held
        before (``changed_values``) - a failed write puts it back.

        Args:
            objs: The objects written.
            changed_values: Where the values before the write are remembered.
            now: The moment of the write - the current one by default.
        """
        if not objs or not self.saved_attribute_names:
            return
        set_value = object.__setattr__ if self.sets_directly else setattr
        native_rows = self.native_rows if self.sets_directly else None
        shared_stamp_field = self.shared_stamp_field
        if shared_stamp_field is not None and native_rows is not None:
            # The values before it remembered and the stamps set in one pass.
            native_rows.stamp_attribute_values(
                changed_values.old_values,
                objs,
                self.saved_attribute_names,
                self.always_stamped_names,
                self.unset_stamped_names,
                shared_stamp_field.get_stamp_value(now),
                ROLLBACK_RESTORE_UNSET,
            )
        else:
            changed_values.capture(objs, self.saved_attribute_names)
        if self.stamped_fields and (shared_stamp_field is None or native_rows is None):
            always_values: list[Any]
            unset_values: list[Any]
            if shared_stamp_field is not None:
                value = shared_stamp_field.get_stamp_value(now)
                always_values = [value] * len(self.always_stamped_names)
                unset_values = [value] * len(self.unset_stamped_names)
            else:
                always_values = []
                unset_values = []
                for (_name, _field, only_when_unset), value in zip(
                    self.stamped_fields, self.get_stamped_values(now), strict=True
                ):
                    (unset_values if only_when_unset else always_values).append(value)
            if unset_values:
                if native_rows is not None:
                    native_rows.set_missing_attribute_values(objs, self.unset_stamped_names, unset_values)
                else:
                    for obj in objs:
                        for name, value in zip(self.unset_stamped_names, unset_values, strict=True):
                            if getattr(obj, name, None) is None:
                                set_value(obj, name, value)
            if always_values:
                if native_rows is not None:
                    native_rows.set_attribute_values(objs, self.always_stamped_names, always_values)
                else:
                    for obj in objs:
                        for name, value in zip(self.always_stamped_names, always_values, strict=True):
                            set_value(obj, name, value)
        if self.blind_indexes:
            fields_map = self.model._meta.fields_map
            for source_name, index_name in self.blind_indexes:
                index_field = cast("BlindIndexField", fields_map[index_name])
                for obj in objs:
                    set_value(obj, index_name, index_field.get_blind_index(getattr(obj, source_name)))

    def get_stamped_copies(self, objs: Sequence[TModel]) -> list[TModel]:
        """Copies of the objects holding what the write would give them - for showing its statements without
        changing the objects themselves.

        Args:
            objs: The objects.

        Returns:
            The copies.
        """
        copies = [copy.copy(obj) for obj in objs]
        self.stamp(copies, InstanceValues())
        return copies

    def add_blind_index_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """The values of a write given as values, with the blind index of each encrypted field written: the
        plaintext, which the index field turns into its index, or ``F()`` of another encrypted field's index.

        Args:
            values: The written values, by field name.

        Returns:
            The values with the blind indexes.

        Raises:
            QueryError: An encrypted field is written from another expression - its index can't be computed.
        """
        if not self.blind_indexes:
            return dict(values)
        # Local import: the expressions import the models package.
        from hare.query.expressions import F

        blind_index_fields = self.model._meta.blind_index_fields
        written = dict(values)
        for source_name, index_name in self.blind_indexes:
            if source_name not in values:
                continue
            value = values[source_name]
            if type(value) is F and value.name in blind_index_fields:
                written[index_name] = F(blind_index_fields[value.name])
            elif isinstance(value, (Expression, Term)):
                raise FieldError(
                    REFUSED_FIELD_MESSAGE.format(
                        name=source_name, method=self.method_name, reason=UNCOMPUTED_BLIND_INDEX_REASON
                    )
                )
            else:
                written[index_name] = value
        return written

    def raise_if_unbound_values(self, objs: Iterable[Model], field_names: Iterable[str], error: Exception) -> None:
        """Explains a write of objects whose values failed to bind: a field holding an expression, which a
        write of values can't take, or the ``DatabaseDefault`` sentinel, which can't be left out for one
        object of a statement. Asked only once the values failed - a write of plain values pays nothing.

        Args:
            objs: The objects written.
            field_names: The fields their rows hold.
            error: The error the values failed with.

        Raises:
            QueryError: A field holds such a value - chained to ``error``.
        """
        names = list(field_names)
        expression_names: set[str] = set()
        default_names: set[str] = set()
        for obj in objs:
            values = obj.__dict__
            for name in names:
                value = values.get(name)
                if isinstance(value, Expression):
                    expression_names.add(name)
                elif isinstance(value, DatabaseDefault):
                    default_names.add(name)
        for found_names, value_type in (
            (expression_names, "an F()/expression"),
            (default_names, "their DatabaseDefault sentinel (never given a value)"),
        ):
            if found_names:
                raise QueryError(
                    UNBOUND_VALUE_MESSAGE.format(
                        method=self.method_name,
                        model=self.model.__name__,
                        fields=sorted(found_names),
                        value_type=value_type,
                    )
                ) from error

    def get_sensitive_positions(self, field_names: Sequence[str]) -> list[int]:
        """The positions of the ``sensitive=True`` fields among a row's fields.

        Args:
            field_names: The fields of a row, in order.

        Returns:
            The positions - none for a model without sensitive fields.
        """
        sensitive_fields = self.model._meta.sensitive_fields
        if not sensitive_fields:
            return []
        return [position for position, name in enumerate(field_names) if name in sensitive_fields]
