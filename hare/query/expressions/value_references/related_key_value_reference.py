from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    #: The related model - an instance of it stands for its primary key.
    instance_class: type[Model]
    #: Whether the value is a list (``__in``/``__not_in``) whose instances stand for their keys too.
    converts_lists: bool
    #: The filter's key, for the error of an instance that didn't load the attribute it stands for.
    filter_key: str
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
        # Deferred import: the models package imports the query expressions.
        from hare.models import Model

        if isinstance(value, Model):
            value = self.get_instance_key(value)
        elif self.converts_lists and isinstance(value, (list, tuple, set)):
            value = [self.get_instance_key(element) if isinstance(element, Model) else element for element in value]
        return self.reference.get_parameter_values(value, model, dialect)

    def get_instance_key(self, obj: Model) -> Any:
        """The key an instance stands for.

        Args:
            obj: The instance.

        Returns:
            Its attribute ``key_name``.

        Raises:
            QueryError: The instance is of another model - a concrete subclass's too, whose rows live in a
                table of their own - or didn't load the attribute (``.only()``/``.defer()``).
        """
        # Deferred import: the relation fields import the query expressions.
        from hare.fields.relations.relation_values import RelationValues

        RelationValues.raise_if_not_of_model(obj, self.instance_class, self.filter_key)
        (key,) = RelationValues.get_relation_key_values(obj, (self.key_name,), f"Filter '{self.filter_key}'")
        return key
