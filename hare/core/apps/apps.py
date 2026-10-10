from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from itertools import chain
from types import ModuleType
from typing import Any, ClassVar, cast

from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.core.apps.model_discovery import ModelDiscovery
from hare.core.apps.relation_linking import RelationLinking
from hare.core.apps.swappable_models import SwappableModels
from hare.core.apps.table_name_checks import TableNameChecks
from hare.core.caching.caches import Caches
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.models import Model
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
from hare.sql import Table
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.identifiers import Identifiers


class Apps:
    """The registry of a context's apps and their models: app label -> model name -> model class,
    filled from the ``apps`` config (the models declared in each app's modules) or by
    ``init_app()``. Binds the models' relations, names the tables of models
    that don't name their own, resolves swappable model settings, and removes models from the
    registry. The caches built for models are dropped by ``Caches``."""

    #: Whether a relation pointing straight at a swapped model (not through ``swappable()``) is
    #: refused - a migration state replays historical files, which may do it.
    REJECTS_SWAPPED_RELATION_TARGETS: ClassVar[bool] = True

    def __init__(
        self,
        config: dict[str, dict[str, Any]] | None,
        connections: ConnectionHandler,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        *,
        validate_connections: bool = True,
        swappable: dict[str, str | dict[str, str]] | None = None,
    ) -> None:
        self.apps: dict[str, dict[str, type[Model]]] = {}
        #: Swappable model settings - setting name to the ``"app_label.ModelName"`` it points at.
        self.swappable_settings: dict[str, str | dict[str, str]] = dict(swappable or {})
        self._config = config or {}
        self._connections = connections
        self._table_name_generator = table_name_generator
        self._validate_connections = validate_connections
        if self._config:
            ModelDiscovery.load_from_config(self)

    def init_app(
        self,
        label: str,
        module_list: Iterable[ModuleType | str],
        init_relations: bool = True,
    ) -> dict[str, type[Model]]:
        app_models: list[type[Model]] = []
        for module in module_list:
            app_models += ModelDiscovery.discover_models(module, label)

        # Merged, not replaced: a second registration under the label keeps the earlier models, and
        # the table name collision check sees them.
        self.apps.setdefault(label, {}).update({model.__name__: model for model in app_models})

        if init_relations:
            self.init_relations()

        return self.apps[label]

    def assign_default_connection(self, app_label: str, default_connection: str) -> None:
        """Points every model registered under ``app_label`` at ``default_connection``."""
        for model in self.apps[app_label].values():
            model._meta.default_connection = default_connection

    def bind_models(self) -> None:
        """Binds the registered models to this registry again: their default connections, table
        names, swapped state, relations and base queries.

        A model class is shared by every registry that loads its module, and the last one to bind
        it wins - a nested context loading the same module with another configuration, or a
        re-init that failed half-way, leaves the models bound to that configuration. Calling this
        on the registry that should own them puts them back.
        """
        for name, info in self._config.items():
            if name in self.apps:
                self.assign_default_connection(name, info.get("default_connection", DEFAULT_CONNECTION_NAME))
        self.init_relations()
        if self._validate_connections:
            self.build_initial_querysets()

    def build_initial_querysets(self) -> None:
        """Binds every registered model's ``basetable``/``basequery``/``basequery_all_fields`` to this
        registry's connection and dialect. They live on the model class, so two contexts registering
        one module concurrently overwrite each other's binding - the last one wins. A model whose
        writes its connection can't make is refused first (``ModelConnectionChecks``).

        Raises:
            ConfigurationError: A model's connection can't make one of its writes.
        """
        for app in self.apps.values():
            for model in app.values():
                ModelConnectionChecks.check_model_writes(model)
                model._meta.basetable = Table(
                    name=model._meta.db_table,
                    schema=model._meta.schema,
                    table_options=tuple(model._meta.table_options),
                )
                basequery = model._meta.connection.query_class.from_(model._meta.basetable)
                model._meta.basequery = basequery
                model._meta.basequery_all_fields = basequery.select(*model._meta.db_fields)

    def get_swappable_label(self, setting: str) -> str:
        """The ``app_label.ModelName`` label a ``swappable`` setting points at - the configured
        model, else the one model declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by exactly one model.
        """
        if setting in self.swappable_settings:
            label = self.swappable_settings[setting]
            if isinstance(label, dict):
                raise ConfigurationError(
                    f'Swappable setting "{setting}" names several models - only a GenericForeignKeyField takes it'
                )
            return label
        default_labels = [
            f"{app_label}.{model_name}"
            for app_label, app in self.apps.items()
            for model_name, model in app.items()
            if model._meta.swappable == setting
        ]
        if len(default_labels) == 1:
            return default_labels[0]
        if not default_labels:
            raise ConfigurationError(
                f'Unknown swappable setting "{setting}" - no model declares Meta.swappable = "{setting}"; '
                'set it in the "swappable" config section'
            )
        raise ConfigurationError(
            f'Swappable setting "{setting}" is declared by several models ({", ".join(default_labels)}) - '
            'choose one in the "swappable" config section'
        )

    def _check_table_name_collisions(self) -> None:
        """Refuses two registered models resolving to the same table on one connection.

        Raises:
            ConfigurationError: Two models share a table.
        """
        # Two models defaulting to one table name are rejected here, naming both - per (connection,
        # schema), as schema generation groups them.
        model_by_table: dict[tuple[str | None, str | None, str], type[Model]] = {}
        for app in self.apps.values():
            for model in app.values():
                if model._meta.swapped is not None:
                    continue
                table_key = (model._meta.default_connection, model._meta.schema, model._meta.db_table)
                colliding_model = model_by_table.get(table_key)
                if colliding_model is not None and colliding_model is not model:
                    raise ConfigurationError(
                        f'Models "{colliding_model._meta.full_name}" and "{model._meta.full_name}" both '
                        f'resolve to table "{model._meta.db_table}" on connection '
                        f'"{model._meta.default_connection}" - give one of them an explicit Meta.table name.'
                    )
                model_by_table[table_key] = model

    def get_models_referencing(self, model: type[Model]) -> list[str]:
        """``"<Model>.<field>"`` for every OTHER registered model whose own forward FK/O2O/M2M
        field points at ``model``."""
        references: list[str] = []
        for registered_model in self.get_models_iterable():
            if registered_model is model:
                continue
            if model in RelationLinking.get_relation_targets(registered_model):
                meta = registered_model._meta
                for field_name in sorted(meta.foreign_key_fields | meta.one_to_one_fields | meta.many_to_many_fields):
                    field = meta.fields_map[field_name]
                    if getattr(field, "related_model", None) is model and not getattr(field, "_generated", False):
                        references.append(f"{meta.full_name}.{field_name}")
        return references

    def remove_models(self, models: Iterable[type[Model]]) -> None:
        """Removes registered models together with everything their registration added: the backward
        relations and generated many-to-many fields on their targets, their relation setup state,
        and every cache entry built for them or their targets. Relations among the removed models
        don't block it.

        Args:
            models: The registered model classes.

        Raises:
            ConfigurationError: A model isn't registered here, or a model outside ``models`` still
                has a relation pointing at one of them.
        """
        models_to_remove = list(dict.fromkeys(models))
        for model in models_to_remove:
            if self.get_registered_app_label(model) is None:
                raise ConfigurationError(f'Model "{model.__name__}" is not registered.')
        removed_model_names = {model._meta.full_name for model in models_to_remove}
        for model in models_to_remove:
            references = [
                reference
                for reference in self.get_models_referencing(model)
                if reference.rsplit(".", 1)[0] not in removed_model_names
            ]
            if references:
                raise ConfigurationError(
                    f'Cannot unregister "{model._meta.full_name}" - still referenced by '
                    f"{', '.join(references)}. Unregister the referencing model(s) first."
                )
        self.discard_models(models_to_remove)

    def discard_models(self, models: Iterable[type[Model]]) -> None:
        """Undoes whatever registering ``models`` got done, with no precondition checks - also
        safe for a model whose relation initialisation stopped part-way, or that never reached
        the registry at all.

        Args:
            models: The model classes to undo.
        """
        models_to_discard = list(dict.fromkeys(models))
        affected_models: set[type[Model]] = set(models_to_discard)
        for model in models_to_discard:
            targets = RelationLinking.get_relation_targets(model)
            affected_models.update(targets)
            for target in targets:
                backward_field_names = [
                    field_name
                    for field_name, field in target._meta.fields_map.items()
                    if getattr(field, "related_model", None) is model
                    and (
                        isinstance(field, BackwardForeignKeyRelation)
                        or (isinstance(field, ManyToManyFieldInstance) and field._generated)
                    )
                ]
                for field_name in backward_field_names:
                    target._meta.remove_field(field_name)
        for model in models_to_discard:
            meta = model._meta
            for field_name in meta.foreign_key_fields | meta.one_to_one_fields:
                relation_field = cast("ForeignKeyFieldInstance[Any]", meta.fields_map[field_name])
                for shadow_name in relation_field.source_fields:
                    if shadow_name != field_name and shadow_name in meta.fields_map:
                        meta.remove_field(shadow_name)
            self.drop_registry_entry(model)
            meta._inited = False
            meta._foreign_key_or_one_to_one_inited = False
            meta.default_connection = None
            meta.basetable = Table("")
            meta.basequery = QueryBuilder()
            meta.basequery_all_fields = QueryBuilder()
        Caches.forget_model_caches(affected_models)

    def drop_registry_entry(self, model: type[Model]) -> None:
        """Removes ``model``'s own entry from this registry (and its app, once empty) - touches
        nothing else; a no-op when ``model`` isn't registered.

        Args:
            model: The model class to remove.
        """
        app_label = self.get_registered_app_label(model)
        if app_label is None:
            return
        del self.apps[app_label][model.__name__]
        if not self.apps[app_label]:
            del self.apps[app_label]

    def get_registered_app_label(self, model: type[Model]) -> str | None:
        """The app label ``model`` itself (not merely a same-named class) is registered under.

        Args:
            model: The model class to look up.

        Returns:
            The app label, or None when ``model`` isn't registered here.
        """
        return next(
            (label for label, app_models in self.apps.items() if app_models.get(model.__name__) is model), None
        )

    def get_model_reference(self, model: type[Model]) -> str:
        return model._meta.full_name

    def get_model(self, app_label: str, model_name: str) -> type[Model]:
        try:
            return self.apps[app_label][model_name]
        except KeyError:
            if app_label not in self.apps:
                raise ConfigurationError(f"No app with name '{app_label}' registered.")
            raise ConfigurationError(f"No model with name '{model_name}' registered in app '{app_label}'.")

    def get_models_iterable(self) -> Iterable[type[Model]]:
        model_list_generator = (model_list for model_list in (app.values() for app in self.apps.values()))
        return chain.from_iterable(model_list_generator)

    def clear(self) -> None:
        self.apps.clear()

    def __contains__(self, key: str) -> bool:
        return key in self.apps

    def __iter__(self) -> Iterator[str]:
        return self.apps.__iter__()

    def values(self) -> Iterable[dict[str, type[Model]]]:
        return self.apps.values()

    def items(self) -> Iterable[tuple[str, dict[str, type[Model]]]]:
        return self.apps.items()

    def keys(self) -> Iterable[str]:
        return self.apps.keys()

    def __getitem__(self, key: str) -> dict[str, type[Model]]:
        return self.apps[key]

    def __setitem__(self, key: str, value: dict[str, type[Model]]) -> None:
        self.apps[key] = value

    @staticmethod
    def split_validated_reference(reference: str) -> tuple[str, str]:
        """Validates that `reference` follows the official naming conventions. Throws a
        ConfigurationError with a hopefully helpful message. If successful, returns the app and
        the model name.

        Raises:
            ConfigurationError: If reference is invalid.
        """
        if len(items := reference.split(".")) != 2:  # pragma: nocoverage
            raise ConfigurationError(
                f"'{reference}' is not a valid model reference Bad Reference."
                " Should be something like '<appname>.<modelname>'."
            )
        return items[0], items[1]

    @staticmethod
    def model_identity(target_model: type[Model]) -> tuple[str | None, str]:
        return target_model._meta.app, target_model.__name__

    def init_relations(self) -> None:
        self.link_relations()
        # Every model's lookups, once all relations are wired - its filters and orderings are
        # described without a connection, right after early binding (Hare.bind_models()) too.
        for app in self.apps.values():
            for model in app.values():
                model._meta.finalise_model()
        # The querysets built before the models were set up become ordinary ones.
        CallsBeforeSetup.apply_pending()

    def link_relations(self) -> None:
        """Wires the relations of every model not inited yet - both sides of each - and marks it
        inited; no model is finalised."""
        # Every model gets its table name before the main pass: the through table names below use
        # the related model's.
        TableNameChecks.assign_generated_table_names(self)
        SwappableModels.init_swapped_models(self)
        self._check_table_name_collisions()
        swappable_targets = SwappableModels.get_swappable_targets(self)
        for app in self.apps.values():
            for model in app.values():
                if model._meta.generic_foreign_key_fields and not model._meta._inited:
                    GenericForeignKeys.expand_swappable_targets(model, swappable_targets)

        self._check_inherited_object_names()

        for app_name, app in self.apps.items():
            for model_name, model in app.items():
                if model._meta._inited:
                    continue
                RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, model)
                for field in list(model._meta.many_to_many_fields):
                    self._link_many_to_many(app_name, model_name, model, field)
                for generic_field in model._meta.generic_foreign_key_fields.values():
                    if generic_field.branch_names:
                        generic_field.bind_branches()
                # Only once every relation of the model resolved - a model whose initialisation
                # raised part-way must not look finished to a later init_relations() pass.
                model._meta._inited = True

    def _check_inherited_object_names(self) -> None:
        """Refuses two models declaring a database object of the same name on one connection and
        schema.

        Raises:
            ConfigurationError: Two models declare such an object.
        """
        # An explicitly named unique or exclusion constraint, index or trigger inherited from an
        # abstract base would give every concrete subclass a database object of the same name -
        # rejected here, naming both models. A check constraint's name is per table and is exempt.
        constraint_by_name: dict[tuple[str | None, str | None, str], type[Model]] = {}
        for app in self.apps.values():
            for model in app.values():
                if model._meta.swapped is not None:
                    continue
                names = [
                    constraint.name
                    for constraint in model._meta.constraints
                    if isinstance(constraint, (UniqueConstraint, ExclusionConstraint)) and constraint.name
                ]
                names.extend(index.name for index in model._meta.indexes if isinstance(index, Index) and index.name)
                if model._meta.triggers:
                    # Imported only for a model declaring triggers - importing it here for every
                    # init would load hare.migrations, which imports this module.
                    from hare.ddl.schema_objects.trigger import Trigger

                    names.extend(trigger.name for trigger in model._meta.triggers if isinstance(trigger, Trigger))
                names.extend(
                    schema_object.name
                    for schema_object in (
                        *model._meta.views,
                        *model._meta.materialized_views,
                        *model._meta.dictionaries,
                        *model._meta.sequences,
                        *model._meta.functions,
                    )
                )
                for name in names:
                    constraint_key = (model._meta.default_connection, model._meta.schema, name)
                    colliding_model = constraint_by_name.get(constraint_key)
                    if colliding_model is not None and colliding_model is not model:
                        raise ConfigurationError(
                            f'Models "{colliding_model._meta.full_name}" and "{model._meta.full_name}" both '
                            f'declare a constraint/index named "{name}" on connection '
                            f'"{model._meta.default_connection}" - likely inherited unchanged from a shared '
                            "abstract base. Give each subclass its own explicit name."
                        )
                    constraint_by_name[constraint_key] = model

    def _link_many_to_many(self, app_name: str, model_name: str, model: type[Model], field: str) -> None:
        """Links a many-to-many field declared on a model: its target, its through table and keys,
        its backward name, and the generated field of the other side.

        Args:
            app_name: The model's app.
            model_name: The model's name in it.
            model: The model.
            field: The field's name.

        Raises:
            ConfigurationError: A model of the relation has no primary key, a self-referential
                relation goes through a model, or the backward name is taken or reserved.
        """
        many_to_many_object = cast("ManyToManyFieldInstance[Any]", model._meta.fields_map[field])
        if many_to_many_object._generated:
            return

        related_model, related_model_name = RelationLinking.get_related_model(self, many_to_many_object.model_name)
        many_to_many_object.related_model = related_model
        # A link row names both rows by their keys.
        model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}"')
        related_model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}" to it')

        through_model, forward_keys, backward_keys, generated_on_delete = self._get_many_to_many_through_keys(
            model, field, many_to_many_object, related_model
        )
        many_to_many_object.forward_key, many_to_many_object.forward_keys = forward_keys[0], forward_keys
        many_to_many_object.backward_key, many_to_many_object.backward_keys = backward_keys[0], backward_keys

        if not (backward_relation_name := many_to_many_object.related_name):
            backward_relation_name = many_to_many_object.related_name = f"{model._meta.db_table}s"
        else:
            backward_relation_name = many_to_many_object.related_name = RelationLinking.apply_related_name_template(
                backward_relation_name, model
            )
        is_swapped = model._meta.swapped is not None
        if not is_swapped and backward_relation_name in related_model._meta.fields:
            raise ConfigurationError(
                f'backward relation "{backward_relation_name}" duplicates in model {related_model_name}'
                ' - use a related_name template such as "%(app_label)s_%(class)s_..." to give each'
                " concrete model a distinct backward-accessor name."
            )
        if not is_swapped:
            RelationLinking.check_related_name_not_reserved(related_model, backward_relation_name, model, field)

        self._set_many_to_many_through_table(
            model, field, many_to_many_object, related_model, through_model, is_swapped
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
        if not is_swapped:
            related_model._meta.add_field(backward_relation_name, many_to_many_relation)

    def _get_many_to_many_through_keys(
        self,
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
            through_model, _ = RelationLinking.get_related_model(self, many_to_many_object.through_model)
            many_to_many_object.through_model_class = through_model
            RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, through_model)
            if self.model_identity(model) == self.model_identity(related_model):
                raise ConfigurationError(
                    f'ManyToManyField "{model.__name__}.{field}" is self-referential '
                    f'through "{through_model.__name__}" - hare cannot auto-detect '
                    "which of the through model's two FK fields belongs to which "
                    "side. Use a plain opaque through= table name instead for a "
                    "self-referential relation."
                )
            backward_foreign_key = RelationLinking.find_many_to_many_through_foreign_key(
                through_model, model, "owning"
            )
            forward_foreign_key = RelationLinking.find_many_to_many_through_foreign_key(
                through_model, related_model, "related"
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
            forward_keys = RelationLinking.expand_many_to_many_key(
                many_to_many_object.forward_key,
                related_model.__name__.lower(),
                related_model._meta.primary_key_attribute_names,
            )
            backward_keys = RelationLinking.expand_many_to_many_key(
                many_to_many_object.backward_key, model._meta.db_table, model._meta.primary_key_attribute_names
            )
            if not many_to_many_object.backward_key and backward_keys == forward_keys:
                # Self-referential: the default backward side would collide with the
                # forward side's columns.
                backward_keys = RelationLinking.expand_many_to_many_key(
                    "", f"{model._meta.db_table}_rel", model._meta.primary_key_attribute_names
                )
            # The automatic through table has no foreign key field of its own - both
            # directions share the declared on_delete.
            generated_on_delete = many_to_many_object.on_delete
        return through_model, forward_keys, backward_keys, generated_on_delete

    def _set_many_to_many_through_table(
        self,
        model: type[Model],
        field: str,
        many_to_many_object: ManyToManyFieldInstance[Any],
        related_model: type[Model],
        through_model: type[Model] | None,
        is_swapped: bool,
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
                related_model_table_name = related_model._meta.db_table or (
                    self._table_name_generator(related_model)
                    if self._table_name_generator
                    else related_model.__name__.lower()
                )
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
                    RelationLinking.check_auto_through_table_name_free(self, through, model, field)
                many_to_many_object.through = through
            many_to_many_object.through_schema = model._meta.schema
