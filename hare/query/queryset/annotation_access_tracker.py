from __future__ import annotations

from typing import Any


class AnnotationAccessTracker(dict[str, Any]):
    """Annotation mapping that remembers which keys expression resolution actually read."""

    def __init__(self, annotations: dict[str, Any]) -> None:
        super().__init__(annotations)
        self.accessed_keys: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.accessed_keys.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self:
            self.accessed_keys.add(key)
        return super().get(key, default)
