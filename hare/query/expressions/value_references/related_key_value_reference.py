from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues, ValueReference


@dataclass(frozen=True)
class RelatedKeyValueReference:
    """The value of a lookup on a to-many relation's own name (``tags=obj``, ``books__in=[...]``):
    an instance stands for its primary key, which the reference recorded comparing the related key
    binds.
    """

    reference: ValueReference
    #: ``Model`` - an instance of it stands for its primary key.
    instance_class: type[Model]
    #: Whether the value is a list (``__in``/``__not_in``) whose instances stand for their keys too.
    converts_lists: bool
    #: The attribute an instance stands for - the field a forward relation's ``to_field`` names.
    key_name: str = "pk"

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of a new value - each instance replaced by its primary key.

        Args:
            value: The new value - an instance, a key, or a list of either.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters the related key's reference gives, or None.
        """
        instance_class = self.instance_class
        key_name = self.key_name
        if isinstance(value, instance_class):
            value = getattr(value, key_name)
        elif self.converts_lists and isinstance(value, (list, tuple, set)):
            value = [
                getattr(element, key_name) if isinstance(element, instance_class) else element for element in value
            ]
        return self.reference.get_parameter_values(value, model, dialect)
