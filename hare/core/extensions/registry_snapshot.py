from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.core.extensions.snapshot_mapping import SnapshotMapping
    from hare.dialects.base.connection.driver import Driver
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.fields.registrations.registered_lookup import RegisteredLookup
    from hare.fields.registrations.registered_transform import RegisteredTransform
    from hare.query.queryset.extensions.query_set_extension import QuerySetExtension


@dataclass(frozen=True, slots=True, eq=False)
class RegistrySnapshot:
    """One published generation; nested registration maps are read-only as well."""

    generation: int = 0
    dialects: Mapping[str, Dialect] = field(default_factory=lambda: MappingProxyType({}))
    drivers: Mapping[str, Driver] = field(default_factory=lambda: MappingProxyType({}))
    schemes: Mapping[str, Driver] = field(default_factory=lambda: MappingProxyType({}))
    lookups: Mapping[type[Field[Any]], Mapping[str, RegisteredLookup]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    transforms: Mapping[type[Field[Any]], Mapping[str, RegisteredTransform]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    methods: Mapping[str, Mapping[type[Dialect], QuerySetExtension]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    components: Mapping[SnapshotMapping[Any, Any], Mapping[Any, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
