from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.caching.model_cache import ModelCache
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.enums import DeletionRoute

if TYPE_CHECKING:
    from hare.dialects.base.features import Features
    from hare.models.model_binding import ModelBinding


class DeletionRoutes:
    """How the rows of a deletion are removed - one decision for a queryset's and an instance's
    ``delete()``. Only the model graph's facts are cached; the connection's features are read on
    every call. A model's own ``delete()`` is called before this decision: an instance reaching it
    through ``super().delete()`` doesn't enter its override again."""

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def get_requirements(binding: ModelBinding) -> tuple[bool, bool, bool]:
        """What of the model graph the decision reads - a fact of the model.

        Args:
            binding: The model whose rows are deleted.

        Returns:
            Whether hare walks the cascade, whether the model is on a cycle of database-enforced
            CASCADE relations, and whether a PROTECT relation is inside its cascade.
        """
        return (
            DeletionGraph.needs_python_cascade(binding),
            DeletionGraph.has_self_cascading_constrained_relations(binding),
            DeletionGraph.has_protect_within_cascade(binding),
        )

    @staticmethod
    def get(binding: ModelBinding, features: Features, permanent: bool = False) -> DeletionRoute:
        """How the rows of ``model`` are removed.

        Args:
            binding: The model whose rows are deleted.
            features: The features of the connection the deletion runs on.
            permanent: Whether a soft-deleting model's rows are removed for good.

        Returns:
            The route.
        """
        if not permanent and binding.soft_delete_field is not None:
            return DeletionRoute.SOFT_UPDATE if DeletionGraph.is_unreferenced(binding) else DeletionRoute.SOFT_CASCADE
        python_cascade, cascade_cycle, protect_within_cascade = DeletionRoutes.get_requirements(binding)
        if python_cascade:
            return DeletionRoute.PYTHON_CASCADE
        if features.cascade_depth_limit is not None and cascade_cycle:
            return DeletionRoute.DEPTH_LIMITED
        if features.checks_foreign_keys_per_cascade_step and protect_within_cascade:
            return DeletionRoute.PROTECT_DEFERRED
        return DeletionRoute.STATEMENT
