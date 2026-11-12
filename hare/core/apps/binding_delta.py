from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.models import Model


class BindingDelta:
    """Restore or publish all class attributes changed by one synchronous binding attempt.

    The original metadata object stays untouched; preparation installs a separate graph.
    Capturing the complete class namespace also covers generated accessors and attributes
    masking relations inherited by subclasses, without maintaining a list of rollback names.
    """

    def __init__(self, model: type[Model]) -> None:
        self.model = model
        self.before = dict(vars(model))
        self.after: dict[str, Any] | None = None

    def capture(self) -> None:
        self.after = dict(vars(self.model))

    def restore(self) -> None:
        self.apply(self.before)

    def publish(self) -> None:
        if self.after is not None:
            self.apply(self.after)

    def apply(self, attributes: dict[str, Any]) -> None:
        namespace = vars(self.model)
        for name in namespace.keys() - attributes.keys():
            type.__delattr__(self.model, name)
        for name, value in attributes.items():
            if name not in namespace or namespace[name] is not value:
                type.__setattr__(self.model, name, value)
