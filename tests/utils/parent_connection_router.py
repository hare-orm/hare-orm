"""A router whose parent-model choices can change in an isolated task context."""

from contextvars import ContextVar


class ParentConnectionRouter:
    routed_models: ContextVar[frozenset[type]] = ContextVar("routed_parent_models", default=frozenset())

    def db_for_read(self, model):
        return "models" if model in self.routed_models.get() else None
