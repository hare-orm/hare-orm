from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.fields.constants import CASCADE
from hare.fields.enums import OnDelete, RelationLoadStrategy
from hare.fields.relations.fields.generic_foreign_key_field_instance import (
    BranchTarget,
    GenericForeignKeyFieldInstance,
)
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


def GenericForeignKeyField(
    to: dict[str, BranchTarget] | SwappableModelReference,
    related_name: str | None = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    *,
    null: bool = False,
    default: Model | Callable[[], Model] | None = None,
    lazy: RelationLoadStrategy | None = None,
    **kwargs: Any,
) -> GenericForeignKeyFieldInstance[Any]:
    """A relation to a row of one of several models - an exclusive arc of real foreign keys, one
    branch per target::

        class Comment(Model):
            target = fields.GenericForeignKeyField(
                {"post": Post, "photo": "media.Photo"}, related_name="comments", on_delete=CASCADE
            )

    Args:
        to: Branch name to model - a class, ``"app.Model"`` or ``swappable("SETTING")`` - or
            ``swappable("SETTING")`` of a setting holding such a dict. The branch name names the
            branch's column (``post_id``), attribute, filter path and the ``type`` the field reports.
        related_name: The backward relation on every target.
        on_delete: What happens when the row of a branch is deleted - see
            ``GenericForeignKeyFieldInstance``.
        db_constraint: Whether the branches get database foreign keys.
        null: Whether a row may have no branch set.
        default: A saved instance of a target - or a callable returning one - for a new instance
            given no branch.
        lazy: The loading strategy of every branch.
        kwargs: Passed to every branch's ``ForeignKeyField``.

    Returns:
        The field.
    """
    return GenericForeignKeyFieldInstance(
        to, related_name, on_delete, db_constraint, null=null, default=default, lazy=lazy, **kwargs
    )
