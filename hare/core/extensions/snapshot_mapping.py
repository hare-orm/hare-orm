from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Generic, TypeVar

from hare.core.extensions.declarations import ExtensionContribution, MappingRegistration
from hare.core.registries import Registries

TKey = TypeVar("TKey")
TValue = TypeVar("TValue")


class SnapshotMapping(Mapping[TKey, TValue], Generic[TKey, TValue]):
    """A component's declarations, extended through the execution's registry snapshot.

    Construction is local: a dialect builds its defaults before exposing the component.
    Once sealed, registrations publish immutable overlays together with mirror changes.
    The baseline remains available even to an execution predating lazy construction.
    This object stores declarations, never memoized lookup results.
    """

    def __init__(self, defaults: Mapping[TKey, TValue] | None = None) -> None:
        self.defaults = MappingProxyType(dict(defaults or ()))
        self.sealed = False

    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def current(self) -> Mapping[TKey, TValue]:
        self.seal()
        return Registries.current().components.get(self, self.defaults)

    def __getitem__(self, key: TKey) -> TValue:
        return self.current()[key]

    def __iter__(self) -> Iterator[TKey]:
        return iter(self.current())

    def __len__(self) -> int:
        return len(self.current())

    def seal(self) -> None:
        """Finish local construction; subsequent changes are registry publications."""
        self.sealed = True

    def register(self, key: TKey, value: TValue, mirrors: tuple[SnapshotMapping[TKey, TValue], ...] = ()) -> None:
        """Register one declaration, atomically including all dialect variants."""
        if not self.sealed:
            self.defaults = MappingProxyType({**self.defaults, key: value})
            return
        Registries.register(
            ExtensionContribution(
                components=tuple(MappingRegistration(mapping, key, value) for mapping in (self, *mirrors))
            )
        )
