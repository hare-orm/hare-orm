from __future__ import annotations

from collections.abc import Callable

from hare.core.extensions.declarations import ExtensionContribution
from hare.core.extensions.messages import EXTENSION_CYCLE_MESSAGE
from hare.core.registries import Registries
from hare.exceptions import ConfigurationError


class ExtensionLoader:
    """Load results are lifecycle state: failed imports are retried only by explicit refresh."""

    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.active: set[str] = set()
        self.failures: dict[str, Exception] = {}

    def load(self, name: str, factory: Callable[[], ExtensionContribution]) -> None:
        """Evaluate a pure factory and publish its entire contribution, or remember its failure."""
        with Registries.lock:
            if name in self.completed:
                return
            if name in self.failures:
                raise self.failures[name]
            if name in self.active:
                raise ConfigurationError(EXTENSION_CYCLE_MESSAGE.format(name=name))
            self.active.add(name)
            try:
                token = Registries.collecting.set(True)
                try:
                    contribution = factory()
                finally:
                    Registries.collecting.reset(token)
                Registries.register(contribution)
                self.completed.add(name)
            except Exception as error:
                self.failures[name] = error.with_traceback(None)
                raise
            finally:
                self.active.remove(name)

    def clear_failures(self) -> None:
        """Allow a new attempt without reinstalling contributions that already succeeded."""
        with Registries.lock:
            self.failures.clear()
