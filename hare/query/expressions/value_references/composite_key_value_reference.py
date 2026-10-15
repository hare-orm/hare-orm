from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues, ValueReference


@dataclass(frozen=True)
class CompositeKeyValueReference:
    """The value of a lookup on a relation to a composite key (``edition=obj``,
    ``edition__in=[...]``), compared column by column: a reference per key column of each compared
    key, in order - an instance stands for its key fields, a tuple is the key itself.
    """

    component_references: tuple[ValueReference, ...]
    #: The instance's fields holding the key, in key order.
    key_names: tuple[str, ...]
    #: ``Model`` - an instance of it stands for its key.
    instance_class: type[Model]
    #: Whether the value is a list of keys (``__in``/``__not_in``).
    is_list: bool

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of a new key or list of keys - each component through its column's reference.

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None for a value of another shape.
        """
        if self.is_list:
            if not isinstance(value, (list, tuple, set)):
                return None
            items = list(value)
        else:
            items = [value]
        key_names = self.key_names
        components: list[Any] = []
        for item in items:
            if isinstance(item, self.instance_class):
                components.extend(getattr(item, key_name) for key_name in key_names)
            elif isinstance(item, tuple) and len(item) == len(key_names):
                components.extend(item)
            else:
                return None
        if len(components) != len(self.component_references):
            return None
        parameters: ParameterValues = []
        for component_reference, component in zip(self.component_references, components, strict=True):
            component_parameters = component_reference.get_parameter_values(component, model, dialect)
            if component_parameters is None:
                return None
            parameters.extend(component_parameters)
        return parameters
