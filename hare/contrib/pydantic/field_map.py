from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING, Any

from hare import (
    ForeignKeyFieldInstance,
)
from hare.contrib.pydantic.descriptions.computed_field_description import ComputedFieldDescription
from hare.contrib.pydantic.descriptions.pydantic_meta_data import PydanticMetaData
from hare.fields import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class FieldMap(MutableMapping[str, Field[Any] | ComputedFieldDescription]):
    def __init__(
        self,
        meta: PydanticMetaData,
        pk_fields: list[Field[Any]] | None = None,
        *,
        model_fields_map: dict[str, Field[Any]] | None = None,
        relations_as_ids: bool = False,
    ) -> None:
        self._field_map: dict[str, Field[Any] | ComputedFieldDescription] = {}
        #: model_field_name of every PK component (plural to also cover a composite PK) - a
        #: raw FK shadow column matching one of these is the PK itself, not a plain relation's
        #: raw column, so exclude_raw_fields must not strip it (see field_map_update below).
        self.pk_raw_fields: frozenset[str] = frozenset(field.model_field_name for field in pk_fields or ())
        #: The model's own fields_map - the source of a forward FK/O2O's shadow id field(s).
        self.model_fields_map: dict[str, Field[Any]] = model_fields_map or {}
        #: Replace every forward FK/O2O with its shadow ``<name>_id`` field(s).
        self.relations_as_ids: bool = relations_as_ids
        if pk_fields:
            self.field_map_update(pk_fields, meta)
        self.computed_fields: dict[str, ComputedFieldDescription] = {}

    def __delitem__(self, __key: str) -> None:
        self._field_map.__delitem__(__key)

    def __getitem__(self, __key: str) -> Field[Any] | ComputedFieldDescription:
        return self._field_map.__getitem__(__key)

    def __len__(self) -> int:  # pragma: no-coverage
        return self._field_map.__len__()

    def __iter__(self) -> Iterator[str]:
        return self._field_map.__iter__()

    def __setitem__(self, __key: str, __value: Field[Any] | ComputedFieldDescription) -> None:
        self._field_map.__setitem__(__key, __value)

    def sort_alphabetically(self) -> None:
        self._field_map = {k: self._field_map[k] for k in sorted(self._field_map)}

    def sort_definition_order(self, cls: type[Model], computed: tuple[str, ...]) -> None:
        ordered_names: list[str] = []
        for name in tuple(cls._meta.fields_map.keys()) + computed:
            field = cls._meta.fields_map.get(name)
            if self.relations_as_ids and isinstance(field, ForeignKeyFieldInstance):
                # A shadow id field takes its relation's place in definition order.
                ordered_names.extend(field.source_fields)
            ordered_names.append(name)
        self._field_map = {
            name: self._field_map[name] for name in dict.fromkeys(ordered_names) if name in self._field_map
        }

    def add_relation_id_fields(self, field: ForeignKeyFieldInstance[Any], meta: PydanticMetaData) -> None:
        """Puts a forward FK/O2O's shadow id field(s) into the map in place of the relation.

        Either the relation name or a shadow field name selects it in ``include``/``exclude``.

        Args:
            field: The forward FK/O2O relation.
            meta: The finalized PydanticMeta settings.
        """
        names = (field.model_field_name, *field.source_fields)
        excluded = any(name in meta.exclude for name in names)
        not_included = not any(meta.selects(name) for name in names)
        for source_field_name in field.source_fields:
            if excluded or not_included:
                # The shadow id of an O2O primary key stays unless excluded by its own name.
                if source_field_name in self.pk_raw_fields and source_field_name not in meta.exclude:
                    continue
                self.pop(source_field_name, None)
            else:
                self[source_field_name] = self.model_fields_map[source_field_name]

    def field_map_update(self, fields: list[Field[Any]], meta: PydanticMetaData) -> None:
        for field in fields:
            name = field.model_field_name
            if self.relations_as_ids and isinstance(field, ForeignKeyFieldInstance):
                self.add_relation_id_fields(field, meta)
                continue
            if not meta.selects(name):
                continue
            # Remove raw fields
            if isinstance(field, ForeignKeyFieldInstance) and meta.exclude_raw_fields:
                # Every key column of a relation to a composite key is popped.
                for raw_field in field.source_fields:
                    if raw_field not in self.pk_raw_fields:
                        self.pop(raw_field, None)
            self[name] = field

    def computed_field_map_update(self, computed: tuple[str, ...], cls: type[Model], meta: PydanticMetaData) -> None:
        for name in computed:
            # Same include/exclude contract as field_map_update above - a computed field must
            # honor exclude=/include= exactly like an ordinary one.
            if not meta.selects(name):
                continue
            self[name] = ComputedFieldDescription(
                function=getattr(cls, name),
                description=None,
            )
