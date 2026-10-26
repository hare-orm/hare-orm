from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.fields.relations.swappable_model_reference import SwappableModelReference
    from hare.models import Model

#: A model as it is named - its class, its ``"app.Model"`` label or a ``swappable()`` setting.
type ModelReference = type[Model] | str | SwappableModelReference
