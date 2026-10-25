from __future__ import annotations

from typing import Any

from hare.migrations.state.state_change_journal import StateChangeJournal


class TrackedDict(dict[Any, Any]):
    """A dict of a migration state that tells the state change journal recording it before it changes - the
    state's models, a model state's fields and options.

    Args:
        values: The items.
        owner: The state or model state the dict belongs to.
    """

    __slots__ = ("owner",)

    def __init__(self, values: Any = (), *, owner: Any = None) -> None:
        super().__init__(values)
        self.owner = owner

    def before_change(self, key: Any) -> None:
        """Tells the journal recording, if any, that the item of a key is about to change.

        Args:
            key: The key.
        """
        journal = StateChangeJournal.recording.get()
        if journal is not None and self.owner is not None:
            journal.record_change(self.owner, key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self.before_change(key)
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        self.before_change(key)
        super().__delitem__(key)

    def pop(self, key: Any, *default: Any) -> Any:
        self.before_change(key)
        return super().pop(key, *default)

    def popitem(self) -> tuple[Any, Any]:
        if self:
            self.before_change(next(reversed(self)))
        return super().popitem()

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self.before_change(key)
        return super().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        values = dict(*args, **kwargs)
        for key in values:
            self.before_change(key)
        super().update(values)

    def clear(self) -> None:
        for key in list(self):
            self.before_change(key)
        super().clear()

    def __ior__(self, other: Any) -> TrackedDict:  # type: ignore[misc]
        self.update(other)
        return self
