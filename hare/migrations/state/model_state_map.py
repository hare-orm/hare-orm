from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping

from hare.migrations.state.model_state import ModelState


class ModelStateMap(MutableMapping[tuple[str, str], ModelState]):
    """Detach a model on first access, preserving nested mutation of working states.

    Unread models belong to a private snapshot. Values obtained by a caller are
    independent working declarations. Publication copies detached values again,
    so references a custom operation retains cannot change a published snapshot.
    """

    def __init__(self, models: Mapping[tuple[str, str], ModelState]) -> None:
        self._models = dict(models)
        self._detached: set[tuple[str, str]] = set()

    def __getitem__(self, key: tuple[str, str]) -> ModelState:
        if key not in self._detached:
            self._models[key] = self._models[key].clone()
            self._detached.add(key)
        return self._models[key]

    def __setitem__(self, key: tuple[str, str], value: ModelState) -> None:
        self._models[key] = value
        self._detached.add(key)

    def __delitem__(self, key: tuple[str, str]) -> None:
        del self._models[key]
        self._detached.discard(key)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._models)

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, key: object) -> bool:
        return key in self._models

    def copy(self) -> dict[tuple[str, str], ModelState]:
        return dict(self.items())

    def _snapshot_models(self) -> dict[tuple[str, str], ModelState]:
        """Publish private values, cloning only declarations exposed to the working state."""
        return {key: model.clone() if key in self._detached else model for key, model in self._models.items()}
