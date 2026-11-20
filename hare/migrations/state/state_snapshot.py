from __future__ import annotations

from dataclasses import dataclass

from hare.migrations.state.model_state_map import ModelStateMap
from hare.migrations.state.state import State


@dataclass(frozen=True, slots=True, init=False)
class StateSnapshot:
    """Private declarations of one boundary, without a retained rendering graph.

    Shared model declarations never escape through a working state. A materialized
    state detaches accessed models; subsequent publication protects retained custom
    references through the existing ModelState/Field snapshot protocols.
    """

    _state: State

    def __init__(self, state: State) -> None:
        models = (
            state.models._snapshot_models()
            if isinstance(state.models, ModelStateMap)
            else {key: model.clone() for key, model in state.models.items()}
        )
        object.__setattr__(self, "_state", state._copy_with_models(models))

    def materialize(self) -> State:
        """Return an independent working view and its own lazy rendering."""
        return self._state._copy_with_models(ModelStateMap(self._state.models))
