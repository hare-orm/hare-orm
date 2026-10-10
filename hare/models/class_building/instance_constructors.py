from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.models.enums import DefaultAssignmentType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class InstanceConstructors:
    """Builds the ``rust.native.rows.ModelConstructor`` setting up a model's new instances."""

    @staticmethod
    def build(model: type[Model]) -> Any:
        """The constructor of ``model``'s instances.

        Args:
            model: The model.

        Returns:
            The constructor; False when the instances are set up by ``Model.__init__`` alone -
            the native extension isn't built, or the model has a ``__setattr__`` of its own; None
            while a relation's model isn't known yet or the extension has no constructor.
        """
        # Imported here: hare.query imports the models package.
        from hare.models.instances.instance_initialization import InstanceInitialization
        from hare.models.model import Model
        from hare.query.expressions import Expression
        from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

        module = HydrateAccelerator.module
        if module is None or model.__setattr__ is not Model.__setattr__:
            return False
        constructor_class = getattr(module, "ModelConstructor", None)
        if constructor_class is None:
            # A build of the extension from before the constructor - worked out again next time.
            return None
        meta = model._meta
        fields_map = meta.fields_map
        fields_db_projection = meta.fields_db_projection
        generated_pk_field_name = meta.generated_pk_field_name
        columns = []
        for name in fields_db_projection:
            field = fields_map[name]
            columns.append(
                (
                    name,
                    list(field.get_assign_normalized_types()),
                    field,
                    field.null,
                    field.pk,
                    name == generated_pk_field_name,
                )
            )
        relations = []
        for name in (*meta.foreign_key_fields, *meta.one_to_one_fields):
            relation_field = cast("RelationalField[Any]", fields_map[name])
            related_model = getattr(relation_field, "related_model", None)
            if not isinstance(related_model, type):
                return None
            relations.append(
                (
                    name,
                    related_model,
                    relation_field.null,
                    list(relation_field.source_fields),
                    [to_field.model_field_name for to_field in relation_field.to_field_instances],
                    f"_{name}",
                )
            )
        defaults: list[tuple[str, DefaultAssignmentType, Any]] = []
        for name, field in fields_map.items():
            if name in meta.fetch_fields:
                continue
            set_value = (
                object.__setattr__ if name in fields_db_projection and name != generated_pk_field_name else setattr
            )
            default = field.default
            is_plain_default = not field._default_is_coroutine and not callable(default)
            # None set on a generated primary key leaves the instance's key to the database, as it is.
            if (
                is_plain_default
                and default is None
                and not field.has_db_default()
                and (set_value is object.__setattr__ or name == generated_pk_field_name)
            ):
                defaults.append((name, DefaultAssignmentType.CONSTANT, None))
            elif (
                is_plain_default
                and set_value is object.__setattr__
                and isinstance(default, (int, float, str, bool, bytes))
                and type(default) in field.get_assign_normalized_types()
            ):
                defaults.append((name, DefaultAssignmentType.CONSTANT, default))
            elif callable(default) and not field._default_is_coroutine and set_value is object.__setattr__:
                # Called for every instance - its result of a type to_python() returns unchanged is
                # set as it is.
                defaults.append(
                    (
                        name,
                        DefaultAssignmentType.CALL,
                        (default, list(field.get_assign_normalized_types()), field.to_python),
                    )
                )
            else:
                defaults.append((name, DefaultAssignmentType.FIELD, set_value))
        return constructor_class(columns, relations, defaults, Expression, InstanceInitialization.assign_default)
