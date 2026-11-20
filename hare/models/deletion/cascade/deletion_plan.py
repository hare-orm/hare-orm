from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hare.models.enums import DeletionAction

if TYPE_CHECKING:
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
    from hare.models import Model
    from hare.models.model_binding import ModelBinding

#: A row: its model and primary key.
RowKey = tuple["type[Model]", Any]


@dataclass(slots=True)
class DeletionPlan:
    """Every row a delete reaches from its root rows through ``on_delete=CASCADE``, wave by wave,
    with what the delete does to each - built by ``DeletionCollector.collect()``, read by the
    PROTECT/RESTRICT checks, ``CascadeDeletion`` and ``DeletePreviewBuilder``.

    Attributes:
        root_model: The model of the root rows.
        root_pks: The root rows' primary keys.
        waves: The rows of each wave by model, ``{pk: action}`` - wave 0 holds the root rows; a
            row a wave reaches is listed in the first wave that reaches it only.
        roots_reached_again: Root rows the walk led back to through a cascade cycle - the
            database's cascade may then have removed them already.
        row_versions: The ``Meta.optimistic_lock_field`` value each soft-deleted row was read with.
        rows_without_key: Rows of a model without a primary key that a ``CASCADE`` reaches - deleted
            by the relation's own condition, per backward relation the target values it matches.
    """

    root_binding: ModelBinding
    root_pks: list[Any]
    waves: list[dict[type[Model], dict[Any, DeletionAction | None]]] = field(default_factory=list)
    roots_reached_again: set[Any] = field(default_factory=set)
    row_versions: dict[RowKey, Any] = field(default_factory=dict)
    rows_without_key: list[tuple[BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any], list[Any]]] = field(
        default_factory=list
    )

    @property
    def root_model(self) -> type[Model]:
        """The root's Python class; structural traversal uses root_binding.graph."""
        return self.root_binding._model

    def iterate_rows(self) -> Iterator[tuple[type[Model], Any, DeletionAction | None]]:
        """Every reached row, wave by wave.

        Yields:
            ``(model, pk, action)``.
        """
        for wave in self.waves:
            for model, rows in wave.items():
                for pk, action in rows.items():
                    yield model, pk, action

    def get_keys(self, *actions: DeletionAction | None) -> frozenset[RowKey]:
        """The rows the delete does one of ``actions`` to - every row without ``actions``.

        Args:
            actions: The actions.

        Returns:
            ``(model, pk)`` of each row.
        """
        return frozenset((model, pk) for model, pk, action in self.iterate_rows() if not actions or action in actions)

    def get_pks_by_model(
        self, *actions: DeletionAction | None, include_roots: bool = True
    ) -> dict[type[Model], list[Any]]:
        """Group the selected rows by model in wave order."""
        excluded = None if include_roots else {(self.root_model, pk) for pk in self.root_pks}
        pks_by_model: dict[type[Model], list[Any]] = {}
        for model, pk, action in self.iterate_rows():
            if (not actions or action in actions) and (excluded is None or (model, pk) not in excluded):
                pks_by_model.setdefault(model, []).append(pk)
        return pks_by_model
