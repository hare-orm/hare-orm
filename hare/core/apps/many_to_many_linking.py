from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.apps.relation_linking import RelationLinking
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.models import Model
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.core.apps.apps import Apps
    from hare.core.apps.declarations import TableIdentity


class ManyToManyLinking:
    """Binds both sides and the through table of a many-to-many declaration in an app registry."""

    @staticmethod
    def _link_many_to_many(
        apps: Apps,
        app_name: str,
        model_name: str,
        model: type[Model],
        field: str,
        table_holders: dict[TableIdentity, tuple[type[Model], str | None]],
    ) -> None:
        """Links a many-to-many field declared on a model: its target, its through table and keys,
        its backward name, and the generated field of the other side.

        Args:
            app_name: The model's app.
            model_name: The model's name in it.
            model: The model.
            field: The field's name.
            table_holders: Who holds each table name - ``RelationLinking.get_table_holders()``.

        Raises:
            ConfigurationError: A model of the relation has no primary key, a self-referential
                relation goes through a model, or the backward name is taken or reserved.
        """
        many_to_many_object: ManyToManyFieldInstance[Any] = model._meta.fields_map[field]  # type: ignore[assignment]
        if many_to_many_object._generated:
            return

        related_model, related_model_name = RelationLinking.get_related_model(apps, many_to_many_object.model_name)
        many_to_many_object.related_model = related_model
        # A link row names both rows by their keys.
        model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}"')
        related_model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}" to it')

        through_model, forward_keys, backward_keys, generated_on_delete = (
            ManyToManyLinking._get_many_to_many_through_keys(apps, model, field, many_to_many_object, related_model)
        )
        many_to_many_object.forward_key, many_to_many_object.forward_keys = forward_keys[0], forward_keys
        many_to_many_object.backward_key, many_to_many_object.backward_keys = backward_keys[0], backward_keys

        backward_relation_name = many_to_many_object.related_name = RelationLinking.get_backward_relation_name(  # type: ignore[assignment]
            many_to_many_object.related_name, model._meta.db_table, model._meta.app, model.__name__
        )
        is_swapped = model._meta.swapped is not None
        existing_relation = related_model._meta.fields_map.get(backward_relation_name)  # type: ignore[arg-type]
        previous_relation = (
            existing_relation.related_model._meta.fields_map.get(field)
            if isinstance(existing_relation, ManyToManyFieldInstance) and existing_relation._generated
            else None
        )
        shares_backward_relation = (
            isinstance(existing_relation, ManyToManyFieldInstance)
            and isinstance(previous_relation, ManyToManyFieldInstance)
            and existing_relation.related_name == field
            and existing_relation.forward_keys == backward_keys
            and existing_relation.backward_keys == forward_keys
            and apps.get_table_identity(
                existing_relation.related_model,
                existing_relation.related_model._meta.schema,
                existing_relation.related_model._meta.db_table,
            )
            == apps.get_table_identity(model, model._meta.schema, model._meta.db_table)
            and (
                previous_relation.declared_through == many_to_many_object.declared_through
                or (
                    through_model is not None
                    and existing_relation.through_model_class is not None
                    and apps.get_table_identity(
                        existing_relation.through_model_class,
                        existing_relation.through_model_class._meta.schema,
                        existing_relation.through_model_class._meta.db_table,
                    )
                    == apps.get_table_identity(through_model, through_model._meta.schema, through_model._meta.db_table)
                )
            )
        )
        if not is_swapped and existing_relation is not None and not shares_backward_relation:
            raise ConfigurationError(
                f"backward relation {backward_relation_name!r} duplicates in model {related_model_name}"
                ' - use a related_name template such as "%(app_label)s_%(class)s_..." to give each'
                " concrete model a distinct backward-accessor name."
            )
        if not is_swapped:
            RelationLinking.check_related_name_not_reserved(related_model, backward_relation_name, model, field)  # type: ignore[arg-type]

        ManyToManyLinking._set_many_to_many_through_table(
            apps, model, field, many_to_many_object, related_model, through_model, is_swapped, table_holders
        )

        # The through model is passed as the class, with its reconciled on_delete.
        many_to_many_relation = ManyToManyFieldInstance(
            f"{app_name}.{model_name}",
            through_model if through_model is not None else many_to_many_object.through,
            forward_key=many_to_many_object.backward_key,
            backward_key=many_to_many_object.forward_key,
            related_name=field,
            on_delete=generated_on_delete,
            field_type=model,
            description=many_to_many_object.description,
        )
        many_to_many_relation._generated = True
        many_to_many_relation.through = many_to_many_object.through
        many_to_many_relation.through_schema = many_to_many_object.through_schema
        many_to_many_relation.through_model = through_model
        many_to_many_relation.through_model_class = through_model
        # Swapped from the owning side, which already resolved both sides.
        many_to_many_relation.forward_keys = many_to_many_object.backward_keys
        many_to_many_relation.backward_keys = many_to_many_object.forward_keys
        LookupInfoBuilder.forget_descriptions()
        if not is_swapped and not shares_backward_relation:
            related_model._meta.add_field(backward_relation_name, many_to_many_relation)  # type: ignore[arg-type]

    @staticmethod
    def _get_many_to_many_through_keys(
        apps: Apps,
        model: type[Model],
        field: str,
        many_to_many_object: ManyToManyFieldInstance[Any],
        related_model: type[Model],
    ) -> tuple[type[Model] | None, tuple[str, ...], tuple[str, ...], Any]:
        """The through model of a many-to-many field, the columns of its keys to each side, and the
        ``on_delete`` of the generated other side.

        Args:
            model: The model declaring the field.
            field: The field's name.
            many_to_many_object: The field.
            related_model: The target.

        Returns:
            The through model (None for a through table of its own), the forward keys, the backward
            keys and the generated side's ``on_delete``.

        Raises:
            ConfigurationError: A self-referential relation goes through a model.
        """
        through_model: type[Model] | None = None
        if many_to_many_object.through_model is not None:
            # The through model's own foreign keys must be resolved first. The declared
            # reference isn't overwritten - migration state copies the field as
            # declared.
            through_model, _ = RelationLinking.get_related_model(apps, many_to_many_object.through_model)
            many_to_many_object.through_model_class = through_model
            RelationLinking.ensure_foreign_key_or_one_to_one_inited(apps, through_model)
            if apps.model_identity(model) == apps.model_identity(related_model):
                raise ConfigurationError(
                    f'ManyToManyField "{model.__name__}.{field}" is self-referential '
                    f"through {through_model.__name__!r} - hare cannot auto-detect "
                    "which of the through model's two FK fields belongs to which "
                    "side. Use a plain opaque through= table name instead for a "
                    "self-referential relation."
                )
            backward_foreign_key = RelationLinking.find_many_to_many_through_foreign_key(
                apps, through_model, model, "owning"
            )
            forward_foreign_key = RelationLinking.find_many_to_many_through_foreign_key(
                apps, through_model, related_model, "related"
            )
            forward_keys = forward_foreign_key.db_column_names
            backward_keys = backward_foreign_key.db_column_names
            # The declared on_delete, taken before the reconciliations below overwrite
            # it.
            declared_on_delete = many_to_many_object.on_delete
            many_to_many_object.on_delete = RelationLinking.reconcile_many_to_many_through_on_delete(
                declared_on_delete, backward_foreign_key, field, model.__name__
            )
            generated_on_delete = RelationLinking.reconcile_many_to_many_through_on_delete(
                declared_on_delete, forward_foreign_key, field, related_model.__name__
            )
        else:
            # The class name, not db_table - the related model may not have been visited
            # yet.
            # Expanded from the keys as declared - never from columns an earlier linking filled in.
            forward_keys = RelationLinking.expand_many_to_many_key(
                many_to_many_object.declared_forward_key,
                related_model.__name__.lower(),
                related_model._meta.primary_key_attribute_names,
            )
            backward_keys = RelationLinking.expand_many_to_many_key(
                many_to_many_object.declared_backward_key,
                model._meta.db_table,
                model._meta.primary_key_attribute_names,
            )
            if not many_to_many_object.declared_backward_key and backward_keys == forward_keys:
                # Self-referential: the default backward side would collide with the
                # forward side's columns.
                backward_keys = RelationLinking.expand_many_to_many_key(
                    "", f"{model._meta.db_table}_rel", model._meta.primary_key_attribute_names
                )
            # Both sides are columns of one table hare creates - a name twice, as its database tells names
            # apart, failed its CREATE TABLE.
            clashing_keys = {apps.get_identifier_key(model, key) for key in forward_keys} & {
                apps.get_identifier_key(model, key) for key in backward_keys
            }
            if clashing_keys:
                raise ConfigurationError(
                    f'ManyToManyField "{model.__name__}.{field}": forward_key and backward_key name the same '
                    f"column {sorted(clashing_keys)} of the through table - give the two sides different names"
                )
            # The automatic through table has no foreign key field of its own - both
            # directions share the declared on_delete.
            generated_on_delete = many_to_many_object.on_delete
        return through_model, forward_keys, backward_keys, generated_on_delete

    @staticmethod
    def _set_many_to_many_through_table(
        apps: Apps,
        model: type[Model],
        field: str,
        many_to_many_object: ManyToManyFieldInstance[Any],
        related_model: type[Model],
        through_model: type[Model] | None,
        is_swapped: bool,
        table_holders: dict[TableIdentity, tuple[type[Model], str | None]],
    ) -> None:
        """Sets a many-to-many field's through table and schema - the through model's, else the
        declared table, else one named after both models.

        Args:
            model: The model declaring the field.
            field: The field's name.
            many_to_many_object: The field.
            related_model: The target.
            through_model: The through model, None for a through table of its own.
            is_swapped: Whether the model is swapped out.
            table_holders: Who holds each table name - the field's automatic through table added.

        Raises:
            ConfigurationError: The generated table name is taken.
        """
        if through_model is not None:
            # The through model's own table, in its own schema.
            many_to_many_object.through = through_model._meta.db_table
            many_to_many_object.through_schema = through_model._meta.schema
        else:
            if not many_to_many_object.through:
                # A model referenced by class but not registered here gets the name its
                # registry's generator would give.
                related_model_table_name = related_model._meta.db_table or apps.get_generated_table_name(related_model)
                # A swappable target is named by the field, not by the model the
                # setting happens to point at - the table must not depend on it.
                through = (
                    f"{model._meta.db_table}_{field}"
                    if isinstance(many_to_many_object.model_name, SwappableModelReference)
                    else f"{model._meta.db_table}_{related_model_table_name}"
                )
                # Shortened with a digest to fit the identifier limit.
                through = Identifiers.get_within_limit(through)
                if not is_swapped:
                    RelationLinking.check_auto_through_table_name_free(apps, table_holders, through, model, field)
                many_to_many_object.through = through
            many_to_many_object.through_schema = model._meta.schema
