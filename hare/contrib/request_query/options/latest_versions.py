from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.options.constants import OPTIMISTIC_LOCK_FIELD, VERSIONED_RECORD_FIELD
from hare.contrib.versioning.versioned_model import VersionedModel
from hare.exceptions import ConfigurationError
from hare.query.expressions import Q
from hare.query.expressions.subqueries.exists import Exists
from hare.query.expressions.subqueries.outer_reference import OuterReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset import QuerySet


@dataclasses.dataclass(frozen=True, slots=True)
class LatestVersions:
    """Keeps only the latest version of each record of a ``VersionedModel``: the rows no newer
    version of the same record the request may see exists for - one correlated subquery, not a
    list of ids. ``Meta.versions = LatestVersions()``."""

    def get_condition(self, versions: QuerySet[Any], access_condition: Q | None) -> Q:
        """The condition keeping the latest version of each record.

        Args:
            versions: Every version of the model the request may see - with its deleted rows as the
                request asks for them.
            access_condition: The rows the request may see - a newer version it may not see
                doesn't hide the one it may.

        Returns:
            The condition.
        """
        newer_versions = versions.filter(
            **{
                VERSIONED_RECORD_FIELD: OuterReference(VERSIONED_RECORD_FIELD),
                f"{OPTIMISTIC_LOCK_FIELD}__gt": OuterReference(OPTIMISTIC_LOCK_FIELD),
            }
        )
        if access_condition is not None:
            newer_versions = newer_versions.filter(access_condition)
        return Q(~Exists(newer_versions))

    def check_model(self, owner: str, model: type[Model]) -> None:
        """Checks that the model has versions.

        Args:
            owner: The request query class, for the error.
            model: The model.

        Raises:
            ConfigurationError: The model isn't a ``VersionedModel``.
        """
        if not issubclass(model, VersionedModel):
            raise ConfigurationError(
                f"{owner}.Meta.versions: {model.__name__} isn't a VersionedModel - it has no versions"
            )
