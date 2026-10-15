from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_type_hints

from hare import Hare
from hare.core.log import logger

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ModelAnnotations:
    """The type annotations of a model class and its methods."""

    @staticmethod
    def get(model: type[Model], method: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Gets all annotations including base classes.

        Builds a namespace from the Hare apps registry so that forward references (string
        annotations) to models defined elsewhere can be resolved by ``get_type_hints``.

        Args:
            model: The model class we need annotations from.
            method: If specified, we try to get the annotations for the callable.

        Returns:
            The annotations dict.
        """
        localns: dict[str, Any] = {}
        try:
            if Hare.apps:
                for app_models in Hare.apps.values():
                    localns.update(app_models)
        except Exception:  # nosec B110
            logger.debug("Failed to build forward-ref namespace from Hare.apps for %s", model, exc_info=True)
        try:
            return get_type_hints(method or model, localns=localns)
        except NameError:
            # An unresolvable forward reference - most often a typo'd annotation - should surface as
            # a clear error, not silently fall back to the raw (unresolved) annotation string below.
            raise
        except Exception:
            return getattr(method or model, "__annotations__", {})
