from __future__ import annotations

import types
from typing import Any

from hare.contrib.factories import LazyFunction, ManyToMany, ModelFactory, Sequence, SubFactory
from hare.models import Model
from tests.contracts.constants import SAMPLED_FIELD_NAME
from tests.contracts.field_sample import FieldSample


class SampleFactories:
    """The factories of a field sample's models."""

    @staticmethod
    def make_target_factory(target_model: type[Model]) -> type[ModelFactory[Any]]:
        """The factory of the model a relation sample targets.

        Args:
            target_model: The model.

        Returns:
            A factory numbering the keys from 1.
        """
        return types.new_class(
            "ContractTargetFactory",
            (ModelFactory[target_model],),  # type: ignore[valid-type]
            exec_body=lambda namespace: namespace.update(id=Sequence(lambda number: number + 1)),
        )

    @staticmethod
    def make_row_factory(
        sample: FieldSample, row_model: type[Model], target_model: type[Model] | None, value: Any = None
    ) -> type[ModelFactory[Any]]:
        """The factory of a sample's row, declaring the sampled field as the field is used.

        Args:
            sample: The sample.
            row_model: The sample's row model.
            target_model: The model its relation targets, None for a field with none.
            value: What the sampled field takes - a plain value of a column field, ignored by a relation.

        Returns:
            A factory numbering the keys from 1.
        """
        declarations: dict[str, Any] = {"id": Sequence(lambda number: number + 1)}
        if target_model is not None:
            target_factory = SampleFactories.make_target_factory(target_model)
            if SAMPLED_FIELD_NAME in row_model._meta.many_to_many_fields:
                declarations[SAMPLED_FIELD_NAME] = ManyToMany(target_factory, size=1)
            else:
                declarations[SAMPLED_FIELD_NAME] = SubFactory(target_factory)
        elif sample.values:
            declarations[SAMPLED_FIELD_NAME] = LazyFunction(lambda: value)
        return types.new_class(
            "ContractRowFactory",
            (ModelFactory[row_model],),  # type: ignore[valid-type]
            exec_body=lambda namespace: namespace.update(declarations),
        )
