from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.core.extensions.snapshot_mapping import SnapshotMapping
    from hare.dialects.base.connection.driver import Driver
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.fields.registrations.registered_lookup import RegisteredLookup
    from hare.fields.registrations.registered_transform import RegisteredTransform
    from hare.query.queryset.extensions.query_set_extension import QuerySetExtension


@dataclass(frozen=True, slots=True)
class LookupRegistration:
    field_class: type[Field[Any]]
    name: str
    lookup: RegisteredLookup


@dataclass(frozen=True, slots=True)
class TransformRegistration:
    field_class: type[Field[Any]]
    name: str
    transform: RegisteredTransform


@dataclass(frozen=True, slots=True)
class MethodRegistration:
    name: str
    dialect_class: type[Dialect]
    method: QuerySetExtension


@dataclass(frozen=True, slots=True)
class MappingRegistration:
    mapping: SnapshotMapping[Any, Any]
    key: Any
    value: Any


@dataclass(frozen=True, slots=True)
class ExtensionContribution:
    """Declarations published together after every component passes registry validation.

    Entry points return this value from a factory. Importing a module defines declarations;
    it must not register them. A dialect's get_contribution() supplies its field and query
    methods when that dialect first enters the registry.
    """

    dialects: tuple[Dialect, ...] = ()
    drivers: tuple[Driver, ...] = ()
    lookups: tuple[LookupRegistration, ...] = ()
    transforms: tuple[TransformRegistration, ...] = ()
    methods: tuple[MethodRegistration, ...] = ()
    components: tuple[MappingRegistration, ...] = ()
