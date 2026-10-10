from hare.core.hare_context import HareContext
from hare.models import ModelMeta


class ContextAppsReset:
    """Drops the models registry of the current context between tests that set Hare up by hand."""

    @staticmethod
    def reset_apps() -> None:
        """Forgets the current context's models and their default connections."""
        context = HareContext.get_current()
        if context is None or context._apps is None:
            return
        for model in context._apps.get_models_iterable():
            if isinstance(model, ModelMeta):
                model._meta.default_connection = None
        context._apps.clear()
        context._apps = None
