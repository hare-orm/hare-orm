from __future__ import annotations

import importlib
import re
from types import MappingProxyType

from hare.core.extensions.constants import URL_SCHEME_PATTERN
from hare.core.extensions.declarations import ExtensionContribution
from hare.core.extensions.messages import (
    DIALECT_CONFLICT_MESSAGE,
    DRIVER_CONFLICT_MESSAGE,
    IDENTIFIER_LIMIT_MESSAGE,
    METHOD_CONDITION_MESSAGE,
    METHOD_NAME_MESSAGE,
    URL_SCHEME_MESSAGE,
)
from hare.core.extensions.registry_snapshot import RegistrySnapshot
from hare.dialects.constants import BUILTIN_DIALECTS_BY_NAME, BUILTIN_DRIVER_MODULES_BY_NAME
from hare.exceptions import ConfigurationError
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT


class RegistryBuilder:
    """Stage one extension without modifying the published snapshot or clearing its caches."""

    def __init__(self, snapshot: RegistrySnapshot) -> None:
        self.previous = snapshot
        self.dialects = dict(snapshot.dialects)
        self.drivers = dict(snapshot.drivers)
        self.schemes = dict(snapshot.schemes)
        self.lookups = {key: dict(value) for key, value in snapshot.lookups.items()}
        self.transforms = {key: dict(value) for key, value in snapshot.transforms.items()}
        self.methods = {key: dict(value) for key, value in snapshot.methods.items()}
        self.components = dict(snapshot.components)
        self.changed = False

    def add(self, contribution: ExtensionContribution) -> None:
        """Validate every component, including the dependencies of newly declared drivers."""
        for dialect in contribution.dialects:
            previous = self.dialects.get(dialect.name)
            if previous is dialect:
                continue
            if previous is not None:
                raise ConfigurationError(DIALECT_CONFLICT_MESSAGE.format(name=dialect.name))
            if dialect.name in BUILTIN_DIALECTS_BY_NAME:
                module, attribute = BUILTIN_DIALECTS_BY_NAME[dialect.name]
                if dialect is not getattr(importlib.import_module(module), attribute):
                    raise ConfigurationError(DIALECT_CONFLICT_MESSAGE.format(name=dialect.name))
            limit = dialect.features.max_identifier_length
            if limit is not None and limit < IDENTIFIER_LENGTH_LIMIT:
                raise ConfigurationError(
                    IDENTIFIER_LIMIT_MESSAGE.format(name=dialect.name, limit=limit, required=IDENTIFIER_LENGTH_LIMIT)
                )
            self.dialects[dialect.name] = dialect
            self.changed = True
            self.add(dialect.get_contribution())
        for driver in contribution.drivers:
            if self.drivers.get(driver.name) is driver:
                continue
            for name in (driver.name, *driver.url_schemes):
                reserved_module = BUILTIN_DRIVER_MODULES_BY_NAME.get(name)
                if reserved_module is not None and type(driver).__module__ != reserved_module:
                    raise ConfigurationError(DRIVER_CONFLICT_MESSAGE.format(name=name))
                if name in self.drivers or name in self.schemes:
                    raise ConfigurationError(DRIVER_CONFLICT_MESSAGE.format(name=name))
            for scheme in driver.url_schemes:
                if re.fullmatch(URL_SCHEME_PATTERN, scheme) is None:
                    raise ConfigurationError(URL_SCHEME_MESSAGE.format(scheme=scheme))
            self.add(ExtensionContribution(dialects=(driver.dialect,)))
            self.drivers[driver.name] = driver
            self.schemes.update((scheme, driver) for scheme in driver.url_schemes)
            self.changed = True
        for registration in contribution.lookups:
            self.lookups.setdefault(registration.field_class, {})[registration.name] = registration.lookup
            self.changed = True
        for registration in contribution.transforms:
            self.transforms.setdefault(registration.field_class, {})[registration.name] = registration.transform
            self.changed = True
        for registration in contribution.methods:
            from hare.query.queryset.queryset import QuerySet

            if registration.name.startswith("_") or hasattr(QuerySet, registration.name):
                raise ConfigurationError(METHOD_NAME_MESSAGE.format(name=registration.name))
            implementations = self.methods.setdefault(registration.name, {})
            if any(
                method.takes_condition != registration.method.takes_condition
                for dialect, method in implementations.items()
                if dialect is not registration.dialect_class
            ):
                raise ConfigurationError(METHOD_CONDITION_MESSAGE.format(name=registration.name))
            implementations[registration.dialect_class] = registration.method
            self.changed = True
        for registration in contribution.components:
            values = self.components.get(registration.mapping, registration.mapping.defaults)
            self.components[registration.mapping] = MappingProxyType({**values, registration.key: registration.value})
            self.changed = True

    def build(self) -> RegistrySnapshot:
        """Freeze the staged generation; an idempotent registration keeps its exact snapshot."""
        if not self.changed:
            return self.previous
        return RegistrySnapshot(
            self.previous.generation + 1,
            MappingProxyType(self.dialects),
            MappingProxyType(self.drivers),
            MappingProxyType(self.schemes),
            MappingProxyType({key: MappingProxyType(value) for key, value in self.lookups.items()}),
            MappingProxyType({key: MappingProxyType(value) for key, value in self.transforms.items()}),
            MappingProxyType({key: MappingProxyType(value) for key, value in self.methods.items()}),
            MappingProxyType(self.components),
        )
