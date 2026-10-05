from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.fields.constants import CASCADE, FOREIGN_KEY_COLUMN_SUFFIX
from hare.fields.enums import OnDelete
from hare.fields.relations.declarations import RelationProperty
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.models import Model
from hare.models.class_building.model_field_collection import ModelFieldCollection
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.apps.apps import Apps
    from hare.fields.field import Field


class RelationLinking:
    """The relations of the registered models linked to each other: each relation's related model found
    by name, its related_name worked out and checked, a many-to-many relation's through model and
    its foreign keys resolved, and the models a relation targets."""

    @staticmethod
    def get_related_model_by_name(apps: Apps, related_app_name: str, related_model_name: str) -> type[Model]:
        """The model a relation names by its app and model name.

        Args:
            apps: The registry.
            related_app_name: The app's name.
            related_model_name: The model's name.

        Returns:
            The model.

        Raises:
            ConfigurationError: No such app, or no such model in it.
        """
        if related_app_name not in apps.apps:
            raise ConfigurationError(
                f"No app with name '{related_app_name}' registered. Please check your model names in "
                "ForeignKeyFields and configurations."
            )
        return apps.get_model(related_app_name, related_model_name)

    @staticmethod
    def get_related_model(
        apps: Apps, reference: str | type[Model] | SwappableModelReference
    ) -> tuple[type[Model], str]:
        """A relation's own `model_name`/`reference` is either the target model class
        directly, a `"<appname>.<modelname>"` string that still needs resolving through
        the app registry, or a swappable reference naming the model a setting points at.

        Args:
            apps: The registry.
            reference: The related model - a class, its ``"app.Model"`` name or a swappable model reference.

        Raises:
            ConfigurationError: The target doesn't exist or is abstract, or is a swapped model
                named directly instead of through ``swappable()``.
        """
        if isinstance(reference, SwappableModelReference):
            related_app_name, related_model_name = apps.split_validated_reference(
                apps.get_swappable_label(reference.setting)
            )
            return RelationLinking.get_related_model_by_name(
                apps, related_app_name, related_model_name
            ), related_model_name
        related_model, related_model_name = RelationLinking.get_named_related_model(apps, reference)
        if apps.REJECTS_SWAPPED_RELATION_TARGETS and related_model._meta.swapped is not None:
            raise ConfigurationError(
                f'A relation points at "{related_model._meta.full_name}", which has been swapped for '
                f'"{related_model._meta.swapped}" by the {related_model._meta.swappable} setting - declare it '
                f'with swappable("{related_model._meta.swappable}") instead'
            )
        return related_model, related_model_name

    @staticmethod
    def get_named_related_model(apps: Apps, reference: str | type[Model]) -> tuple[type[Model], str]:
        """The model a class or ``"<appname>.<modelname>"`` reference names.

        Args:
            apps: The registry.
            reference: The related model - a class or its ``"app.Model"`` name.

        Raises:
            ConfigurationError: The target doesn't exist or is abstract.
        """
        if not isinstance(reference, str):
            if reference._meta.abstract:
                # An abstract model has no primary key to relate to.
                raise ConfigurationError(
                    f"{reference.__name__} is an abstract model (Meta.abstract = True) - it can never be "
                    "the target of a relation. Point the relation at a concrete model instead."
                )
            return reference, reference.__name__
        related_app_name, related_model_name = apps.split_validated_reference(reference)
        return RelationLinking.get_related_model_by_name(
            apps, related_app_name, related_model_name
        ), related_model_name

    @staticmethod
    def apply_related_name_template(related_name: str, model: type[Model]) -> str:
        """Substitutes ``%(app_label)s``/``%(class)s`` in a declared ``related_name`` - each concrete
        subclass of an abstract base gets its own backward name.
        """
        if "%(app_label)s" not in related_name and "%(class)s" not in related_name:
            return related_name
        return related_name % {"app_label": model._meta.app, "class": model.__name__.lower()}

    @staticmethod
    def init_foreign_key_or_one_to_one_field(
        apps: Apps, model: type[Model], field: str, is_one_to_one: bool = False
    ) -> None:
        foreign_key_object = cast(
            "OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any]", model._meta.fields_map[field]
        )
        related_model, related_model_name = RelationLinking.get_related_model(apps, foreign_key_object.model_name)
        to_field_names, related_fields = RelationLinking.get_key_targets(
            apps, model, field, foreign_key_object, related_model, related_model_name, is_one_to_one
        )
        foreign_key_object.to_field_instance = related_fields[0]  # unchanged semantics for existing readers
        foreign_key_object.to_field_names = to_field_names
        foreign_key_object.to_field_instances = tuple(related_fields)
        foreign_key_object.field_type = foreign_key_object.to_field_instance.field_type

        source_fields, key_source_fields = RelationLinking.add_key_fields(
            model, field, foreign_key_object, related_fields, to_field_names, is_one_to_one
        )
        foreign_key_object.related_model = related_model
        foreign_key_object.source_fields = tuple(source_fields)
        foreign_key_object.db_column_names = tuple(key_source_fields)
        foreign_key_object.source_field = source_fields[0]  # unchanged for len==1; "primary" shadow column otherwise
        if is_one_to_one and foreign_key_object.pk:
            model._meta.primary_key_attribute = source_fields[0]
            LookupInfoBuilder.forget_descriptions()
        foreign_key_relation = RelationLinking.get_backward_relation(
            model, foreign_key_object, source_fields, key_source_fields, is_one_to_one
        )
        RelationLinking.add_backward_relation(
            model, field, foreign_key_object, related_model, related_model_name, foreign_key_relation
        )

    @staticmethod
    def get_key_targets(
        apps: Apps,
        model: type[Model],
        field: str,
        foreign_key_object: OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any],
        related_model: type[Model],
        related_model_name: str,
        is_one_to_one: bool,
    ) -> tuple[tuple[str, ...], list[Field[Any]]]:
        """The fields of the target a relation's key references - its ``to_field``, else the target's
        primary key - each a column of its own, and the ``to_field`` set to them.

        Args:
            apps: The registry.
            model: The model of the relation.
            field: The relation's name.
            foreign_key_object: The relation.
            related_model: The target.
            related_model_name: The target's name, for the errors.
            is_one_to_one: Whether the relation is one-to-one.

        Returns:
            The referenced fields' names and the fields.

        Raises:
            ConfigurationError: The ``to_field`` names no field, or no unique one, or not the whole
                composite primary key in its order; the target's one-to-one primary key forms a
                cycle; a one-to-one primary key targets a composite key; the target's primary key
                compares WITHOUT OVERLAPS.
        """
        if to_field := foreign_key_object.to_field:
            to_field_names: tuple[str, ...] = to_field if isinstance(to_field, tuple) else (to_field,)
            related_fields = []
            for name in to_field_names:
                if name not in related_model._meta.fields_map:
                    # May be the shadow column of the target's own not yet initialized relation.
                    RelationLinking.ensure_foreign_key_or_one_to_one_inited(apps, related_model)
                related_field = related_model._meta.fields_map.get(name)
                if not related_field:
                    raise ConfigurationError(f'there is no field named "{name}" in model "{related_model_name}"')
                related_fields.append(related_field)
            if len(to_field_names) == 1:
                if not related_fields[0].unique:
                    raise ConfigurationError(
                        f'field "{to_field_names[0]}" in model "{related_model_name}" is not unique'
                    )
            elif to_field_names != related_model._meta.primary_key_attribute_names:
                # A composite to_field must be the target's whole primary key, in its order.
                raise ConfigurationError(
                    f'{"OneToOneField" if is_one_to_one else "ForeignKeyField"} "{model.__name__}.{field}" '
                    f'to_field={to_field_names} must exactly match "{related_model_name}"\'s composite '
                    f"primary key {related_model._meta.primary_key_attribute_names} (in the same order) - an "
                    "arbitrary composite UniqueConstraint target isn't supported."
                )
        elif related_model._meta.has_composite_primary_key:
            to_field_names = cast("tuple[str, ...]", related_model._meta.primary_key_attribute)
            related_fields = list(related_model._meta.pk_fields)
            foreign_key_object.to_field = to_field_names
        else:
            relation_label = "OneToOneField" if is_one_to_one else "ForeignKeyField"
            related_model._meta.raise_if_no_primary_key(
                f'{relation_label} "{model.__name__}.{field}" to it without to_field='
            )
            to_field_names = (cast("str", related_model._meta.primary_key_attribute),)
            related_fields = [related_model._meta.pk]
            foreign_key_object.to_field = related_model._meta.primary_key_attribute

        if any(isinstance(related_field, OneToOneFieldInstance) for related_field in related_fields):
            # The target (typically its OneToOneField(primary_key=True)) is itself a relation with
            # no column of its own - reference the shadow column it stores its value in instead.
            RelationLinking.ensure_foreign_key_or_one_to_one_inited(apps, related_model)
            related_fields = [
                related_model._meta.fields_map[related_field.source_field]
                if isinstance(related_field, OneToOneFieldInstance) and related_field.source_field
                else related_field
                for related_field in related_fields
            ]
            if any(isinstance(related_field, OneToOneFieldInstance) for related_field in related_fields):
                raise ConfigurationError(
                    f'{"OneToOneField" if is_one_to_one else "ForeignKeyField"} "{model.__name__}.{field}" targets '
                    f'"{related_model_name}", whose own OneToOneField primary key forms a cycle with it.'
                )
            to_field_names = tuple(related_field.model_field_name for related_field in related_fields)
            foreign_key_object.to_field = to_field_names if len(to_field_names) > 1 else to_field_names[0]

        if is_one_to_one and foreign_key_object.pk and len(to_field_names) > 1:
            raise ConfigurationError(
                f'OneToOneField "{model.__name__}.{field}" can\'t both target a composite primary key '
                f'and be used as "{model.__name__}"\'s own primary key - a composite own-PK must be '
                "declared directly via CompositePrimaryKey, not derived from a composite O2O target"
            )

        if related_model._meta.pk_without_overlaps and len(to_field_names) > 1:
            raise ConfigurationError(
                f'{"OneToOneField" if is_one_to_one else "ForeignKeyField"} "{model.__name__}.{field}" targets the '
                f'primary key of "{related_model_name}", which compares its range WITHOUT OVERLAPS - a foreign '
                "key can't reference it; target a unique field of it instead"
            )

        return to_field_names, related_fields

    @staticmethod
    def add_key_fields(
        model: type[Model],
        field: str,
        foreign_key_object: OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any],
        related_fields: list[Field[Any]],
        to_field_names: tuple[str, ...],
        is_one_to_one: bool,
    ) -> tuple[list[str], list[str]]:
        """Adds the key fields of a relation - a copy of each referenced field under the relation's
        shadow name, ``<field>_id`` or ``<field>_<name>`` per component - and a composite one-to-one
        key's unique constraint.

        Args:
            model: The model of the relation.
            field: The relation's name.
            foreign_key_object: The relation.
            related_fields: The referenced fields.
            to_field_names: Their names.
            is_one_to_one: Whether the relation is one-to-one.

        Returns:
            The key fields' names and their columns.
        """
        shadow_names: tuple[str, ...]
        if len(to_field_names) == 1:
            shadow_names = (f"{field}{FOREIGN_KEY_COLUMN_SUFFIX}",)
        else:
            # "<field>_<to_field_name>" per component, e.g. script -> script_id,
            # script_version - deterministic, never collides with the single-column
            # convention (which never appends a second "_<name>" after "_id").
            shadow_names = tuple(f"{field}_{name}" for name in to_field_names)

        # foreign_key_object.source_field (singular) only makes sense as a DB-column-name override for the
        # single-column case - a composite FK's per-column DB names are never customized via the
        # one source_field= kwarg on the logical field.
        key_source_field_names = (
            ((foreign_key_object.source_field or shadow_names[0]),) if len(shadow_names) == 1 else shadow_names
        )
        if foreign_key_object.index and (
            not all(related_field.indexable for related_field in related_fields)
            or model._meta.is_indexed_by_leading_columns(
                field, list(zip(shadow_names, key_source_field_names, strict=True))
            )
        ):
            # An index that already leads with the key column(s) serves the relation's lookups.
            foreign_key_object.index = False

        source_fields: list[str] = []
        key_source_fields: list[str] = []
        for shadow_name, related_field, key_source_field in zip(
            shadow_names, related_fields, key_source_field_names, strict=True
        ):
            key_foreign_key_object = copy(related_field)
            key_foreign_key_object.reference = foreign_key_object
            key_foreign_key_object.source_field = key_source_field
            # _default_is_coroutine travels with default - left as the target pk's own flag, an
            # async pk default made every FK to that model await its own (None) default on save.
            for attribute_name in (
                "index",
                "default",
                "_default_is_coroutine",
                "null",
                "generated",
                "description",
                "db_default",
                "sensitive",
            ):
                setattr(key_foreign_key_object, attribute_name, getattr(foreign_key_object, attribute_name))
            if len(shadow_names) > 1:
                # A composite key gets one index over all of its columns, not one per column.
                key_foreign_key_object.index = False
            if is_one_to_one:
                key_foreign_key_object.pk = foreign_key_object.pk
                # A composite one-to-one's uniqueness is one constraint over all key columns, added
                # below.
                key_foreign_key_object.unique = foreign_key_object.unique if len(shadow_names) == 1 else False
            else:
                key_foreign_key_object.pk = False
                key_foreign_key_object.unique = False
            model._meta.add_field(shadow_name, key_foreign_key_object)
            source_fields.append(shadow_name)
            key_source_fields.append(key_source_field)

        if is_one_to_one and len(shadow_names) > 1:
            # Added once: a model rendered from migration state already carries it.
            composite_unique_constraint = UniqueConstraint(fields=shadow_names)
            if composite_unique_constraint not in model._meta.constraints:
                model._meta.constraints = (*model._meta.constraints, composite_unique_constraint)

        return source_fields, key_source_fields

    @staticmethod
    def get_backward_relation(
        model: type[Model],
        foreign_key_object: OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any],
        source_fields: list[str],
        key_source_fields: list[str],
        is_one_to_one: bool,
    ) -> BackwardForeignKeyRelation[Any]:
        """The backward relation of a relation - what the target reads the rows pointing at it by.

        Args:
            model: The model of the relation.
            foreign_key_object: The relation.
            source_fields: Its key fields' names.
            key_source_fields: Their columns.
            is_one_to_one: Whether the relation is one-to-one.

        Returns:
            The backward relation.
        """
        foreign_key_relation = (
            BackwardOneToOneRelation(
                model,
                source_fields[0],
                key_source_fields[0],
                null=True,
                description=foreign_key_object.description,
                relation_fields=tuple(source_fields),
                relation_source_fields=tuple(key_source_fields),
            )
            if is_one_to_one
            else BackwardForeignKeyRelation(
                model,
                source_fields[0],
                key_source_fields[0],
                null=foreign_key_object.null,
                description=foreign_key_object.description,
                relation_fields=tuple(source_fields),
                relation_source_fields=tuple(key_source_fields),
            )
        )
        foreign_key_relation.to_field_instance = foreign_key_object.to_field_instance
        foreign_key_relation.to_field_names = foreign_key_object.to_field_names
        foreign_key_relation.to_field_instances = foreign_key_object.to_field_instances
        return foreign_key_relation

    @staticmethod
    def add_backward_relation(
        model: type[Model],
        field: str,
        foreign_key_object: OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any],
        related_model: type[Model],
        related_model_name: str,
        foreign_key_relation: BackwardForeignKeyRelation[Any],
    ) -> None:
        """Adds a relation's backward relation to its target - under its ``related_name``, hidden for
        ``related_name=False``, none for a swapped model.

        Args:
            model: The model of the relation.
            field: The relation's name.
            foreign_key_object: The relation.
            related_model: The target.
            related_model_name: The target's name, for the errors.
            foreign_key_relation: The backward relation.

        Raises:
            ConfigurationError: The backward relation's name is taken on the target, or reserved.
        """
        if model._meta.swapped is not None:
            # A swapped model has no table and no rows - nothing on the target points back at it,
            # and its backward accessor would clash with the one of the model swapped in.
            return
        if (backward_relation_name := foreign_key_object.related_name) is False:
            # No public accessor, but on_delete still has to reach the rows pointing back.
            related_model._meta.add_hidden_backward_relation(f"{model._meta.full_name}.{field}", foreign_key_relation)
            return
        if not backward_relation_name:
            backward_relation_name = f"{model._meta.db_table}s"
        else:
            backward_relation_name = foreign_key_object.related_name = RelationLinking.apply_related_name_template(
                backward_relation_name, model
            )
        if backward_relation_name in related_model._meta.fields:
            raise ConfigurationError(
                f'backward relation "{backward_relation_name}" duplicates in model {related_model_name}'
                ' - use a related_name template such as "%(app_label)s_%(class)s_..." to give each'
                " concrete model a distinct backward-accessor name."
            )
        RelationLinking.check_related_name_not_reserved(related_model, backward_relation_name, model, field)
        related_model._meta.add_field(backward_relation_name, foreign_key_relation)

    @staticmethod
    def check_related_name_not_reserved(
        related_model: type[Model], backward_relation_name: str, model: type[Model], field: str
    ) -> None:
        """Rejects a ``related_name`` that would shadow a ``Model`` attribute (``save``,
        ``filter``, ``pk``, ...) or one of ``related_model``'s own methods/attributes.

        Raises:
            ConfigurationError: ``backward_relation_name`` is taken.
        """
        taken = backward_relation_name in ModelFieldCollection.get_reserved_field_names()
        if not taken:
            for base in related_model.__mro__:
                if base in Model.__mro__ or backward_relation_name not in base.__dict__:
                    continue
                attribute = base.__dict__[backward_relation_name]
                # A relation accessor generated by an earlier initialization of the same class.
                taken = not isinstance(attribute, RelationProperty)
                break
        if taken:
            raise ConfigurationError(
                f'related_name "{backward_relation_name}" of "{model.__name__}.{field}" would shadow the '
                f'"{backward_relation_name}" attribute of model {related_model.__name__} - choose another '
                "related_name."
            )

    @staticmethod
    def check_auto_through_table_name_free(apps: Apps, through: str, model: type[Model], field: str) -> None:
        """Rejects an auto-generated M2M through table name already used by another M2M field's
        through table or by a model's own table.

        Args:
            apps: The registry.
            through: The automatic through table's name.
            model: The model declaring the field.
            field: The field's name.

        Raises:
            ConfigurationError: ``through`` is taken.
        """
        for registered_model in apps.get_models_iterable():
            if registered_model._meta.swapped is not None:
                # A swapped model has no table, nor through tables of its own.
                continue
            if registered_model._meta.db_table == through and registered_model._meta.schema == model._meta.schema:
                raise ConfigurationError(
                    f'ManyToManyField "{model.__name__}.{field}" gets the auto-generated through table '
                    f'"{through}", which is the table of model {registered_model.__name__} - set through= '
                    "explicitly."
                )
            for many_to_many_field_name in registered_model._meta.many_to_many_fields:
                other_many_to_many_field = cast(
                    "ManyToManyFieldInstance[Any]", registered_model._meta.fields_map[many_to_many_field_name]
                )
                if other_many_to_many_field._generated or (
                    registered_model is model and many_to_many_field_name == field
                ):
                    continue
                if (
                    other_many_to_many_field.through == through
                    and other_many_to_many_field.through_schema == model._meta.schema
                ):
                    raise ConfigurationError(
                        f'ManyToManyField "{model.__name__}.{field}" gets the auto-generated through table '
                        f'"{through}", already used by "{registered_model.__name__}.{many_to_many_field_name}" - set '
                        "through= explicitly on one of them."
                    )

    @staticmethod
    def ensure_foreign_key_or_one_to_one_inited(apps: Apps, target_model: type[Model]) -> None:
        """Sets up every forward relation of ``target_model`` - a ``ManyToManyField(through=...)``
        needs its through model's foreign keys resolved first.

        Args:
            apps: The registry.
            target_model: The model whose forward relations are set up.
        """
        if target_model._meta._foreign_key_or_one_to_one_inited:
            return
        target_model._meta._foreign_key_or_one_to_one_inited = True
        # A OneToOneField primary key goes first: a relation pointing back at this model while
        # its fields are still being initialized needs its primary key column already in place.
        one_to_one_fields = list(target_model._meta.one_to_one_fields)
        primary_key_one_to_one_fields = [
            field for field in one_to_one_fields if target_model._meta.fields_map[field].pk
        ]
        for field in primary_key_one_to_one_fields:
            RelationLinking.init_foreign_key_or_one_to_one_field(apps, target_model, field, is_one_to_one=True)
        for field in sorted(target_model._meta.foreign_key_fields):
            RelationLinking.init_foreign_key_or_one_to_one_field(apps, target_model, field)
        for field in one_to_one_fields:
            if field not in primary_key_one_to_one_fields:
                RelationLinking.init_foreign_key_or_one_to_one_field(apps, target_model, field, is_one_to_one=True)

    @staticmethod
    def find_many_to_many_through_foreign_key(
        through_model: type[Model], target_model: type[Model], side_label: str
    ) -> ForeignKeyFieldInstance[Any]:
        """The one ``ForeignKeyField`` of ``through_model`` pointing at ``target_model``, matched by
        app and name - migration state renders its own copies of the model classes.

        Raises:
            ConfigurationError: There is no such field, or there are several.
        """
        # Local import: the registry module imports this module.
        from hare.core.apps.apps import Apps

        target_key = Apps.model_identity(target_model)
        candidates = [
            name
            for name in through_model._meta.foreign_key_fields
            if (related := cast("ForeignKeyFieldInstance[Any]", through_model._meta.fields_map[name]).related_model)
            is not None
            and Apps.model_identity(related) == target_key
        ]
        if len(candidates) != 1:
            raise ConfigurationError(
                f'ManyToManyField through model "{through_model.__name__}" must have exactly one '
                f'ForeignKeyField pointing to "{target_model.__name__}" for its {side_label} side, '
                f"found {len(candidates)}."
            )
        through_foreign_key = cast("ForeignKeyFieldInstance[Any]", through_model._meta.fields_map[candidates[0]])
        # Every M2M read/write keys a through row by the target's primary key - a to_field= on any
        # other column would store values no M2M operation can match.
        if tuple(through_foreign_key.to_field_names) != target_model._meta.primary_key_attribute_names:
            raise ConfigurationError(
                f'ManyToManyField through model "{through_model.__name__}" field "{candidates[0]}" must '
                f'reference "{target_model.__name__}"\'s primary key '
                f"{target_model._meta.primary_key_attribute_names}, not to_field={through_foreign_key.to_field!r}."
            )
        return through_foreign_key

    @staticmethod
    def reconcile_many_to_many_through_on_delete(
        intended_on_delete: OnDelete,
        foreign_key_field: ForeignKeyFieldInstance[Any],
        many_to_many_field_name: str,
        side_label: str,
    ) -> OnDelete:
        """Reconciles a ``ManyToManyField(through=Model)``'s ``on_delete`` with the through model's
        foreign key to one side - the key is what the delete cascade and the DDL act on. A side left
        at the default ``CASCADE`` takes the other's; two different declared values raise.

        Raises:
            ConfigurationError: Both sides declare different values, or the value doesn't fit the
                foreign key (``SET_NULL`` needs ``null=True``, ``SET_DEFAULT`` a ``db_default`` - or
                a ``default`` with ``db_constraint=False``).
        """
        foreign_key_on_delete = foreign_key_field.on_delete
        if intended_on_delete in (foreign_key_on_delete, CASCADE):
            return foreign_key_on_delete
        if foreign_key_on_delete != CASCADE:
            raise ConfigurationError(
                f'ManyToManyField "{many_to_many_field_name}" declares on_delete={intended_on_delete.name}, but '
                f"its through model's own FK field pointing at the {side_label} side already declares "
                f"on_delete={foreign_key_on_delete.name} - set on_delete only once, on the through model's FK field."
            )
        if intended_on_delete == OnDelete.SET_NULL and not foreign_key_field.null:
            raise ConfigurationError(
                f'ManyToManyField "{many_to_many_field_name}" declares on_delete=SET_NULL, but its through '
                f"model's own FK field pointing at the {side_label} side isn't null=True - add "
                "null=True to that field."
            )
        if intended_on_delete == OnDelete.SET_DEFAULT:
            # The FK field's own constructor never saw SET_DEFAULT (it was left at CASCADE), so
            # the requirement it would have enforced there is checked here instead.
            requirement_error = foreign_key_field.get_set_default_requirement_error()
            if requirement_error is not None:
                raise ConfigurationError(
                    f'ManyToManyField "{many_to_many_field_name}" declares on_delete=SET_DEFAULT, but its through '
                    f"model's own FK field pointing at the {side_label} side doesn't qualify: "
                    f"{requirement_error}."
                )
        foreign_key_field.on_delete = intended_on_delete
        return intended_on_delete

    @staticmethod
    def expand_many_to_many_key(explicit_key: str, base_name: str, pk_names: tuple[str, ...]) -> tuple[str, ...]:
        """The through-table column names of one side of a ``ManyToManyField``: one column for a
        single-column key, one ``<prefix>_<pk_name>`` column per part of a composite key.
        """
        if len(pk_names) == 1:
            return (explicit_key or Identifiers.get_within_limit(f"{base_name}{FOREIGN_KEY_COLUMN_SUFFIX}"),)
        prefix = explicit_key or base_name
        return tuple(Identifiers.get_within_limit(f"{prefix}_{name}") for name in pk_names)

    @staticmethod
    def get_relation_targets(model: type[Model]) -> set[type[Model]]:
        """Every model ``model``'s own forward FK/O2O/M2M fields point at (itself included for a
        self-referential relation) - the models it adds backward relations to."""
        targets: set[type[Model]] = set()
        meta = model._meta
        for field_name in meta.foreign_key_fields | meta.one_to_one_fields | meta.many_to_many_fields:
            field = meta.fields_map[field_name]
            if isinstance(field, ManyToManyFieldInstance) and field._generated:
                continue
            related_model = getattr(field, "related_model", None)
            if isinstance(related_model, type):
                targets.add(related_model)
        return targets
