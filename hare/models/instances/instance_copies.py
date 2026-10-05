from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.models.instances.instance_initialization import InstanceInitialization

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class InstanceCopies:
    """The copy clone() makes of an instance: each component of a composite primary key the copy leaves
    unset gets its default again."""

    @staticmethod
    def apply_clone_pk_component_default(obj: Model, field_name: str, field: Field[Any]) -> None:
        """Assign one composite-PK component its cloned value, mirroring the single-column PK
        default/db_default/async-default resolution in ``clone()`` above.

        Args:
            obj: The model obj.
            field_name: the model attribute name for this PK component.
            field: the Field object for this PK component.

        Raises:
            QueryError: if the field has neither a default nor a db_default to fall back on.
        """
        # generated=True is never possible here - CompositePrimaryKey's own validation forbids a
        # composite PK member from being DB-generated, so unlike the single-column PK path above,
        # there's no generated=True case to handle.
        if field.default is None and field.has_db_default():
            setattr(obj, field_name, field.get_db_default_value())
        elif field.default is None:
            raise QueryError(
                f"{obj._meta.full_name} requires an explicit value for composite primary key "
                f"component '{field_name}'. Please use .clone(pk=(<value>, ...))"
            )
        elif field._default_is_coroutine:
            # After the assignment: __setattr__ pops the pending default of an assigned field.
            setattr(obj, field_name, None)
            InstanceInitialization.add_pending_default(obj, field_name, field.default)
        elif callable(field.default):
            setattr(obj, field_name, field.default())
        else:
            setattr(obj, field_name, field.default)
