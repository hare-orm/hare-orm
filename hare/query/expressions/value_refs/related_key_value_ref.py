from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_refs.value_ref_types import ParameterValues, ValueRef


@dataclass(frozen=True)
class RelatedKeyValueRef:
    """The value of a lookup on a to-many relation's own name (``tags=obj``, ``books__in=[...]``):
    an instance stands for its primary key, which the reference recorded comparing the related key
    binds.
    """

    ref: ValueRef
    #: ``Model`` - an instance of it stands for its primary key.
    instance_class: type[Model]
    #: Whether the value is a list (``__in``/``__not_in``) whose instances stand for their keys too.
    converts_lists: bool

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
        if isinstance(value, instance_class):
            value = value.pk
        elif self.converts_lists and isinstance(value, (list, tuple, set)):
            value = [element.pk if isinstance(element, instance_class) else element for element in value]
        return self.ref.get_parameter_values(value, model, dialect)
