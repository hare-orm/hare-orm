from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from itertools import chain
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.apps.many_to_many_linking import ManyToManyLinking
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.core.apps.model_discovery import ModelDiscovery
from hare.core.apps.model_name_checks import ModelNameChecks
from hare.core.apps.relation_linking import RelationLinking
from hare.core.apps.swappable_models import SwappableModels
from hare.core.apps.table_names import TableNames
from hare.core.caching.caches import Caches
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.models import Model
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.sql import Table
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference, TableIdentity
    from hare.dialects.base.client.database_client import DatabaseClient


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
            with ModelBindingGraph(self):
                ModelDiscovery.load_from_config(self)

    def init_app(
        self,
        label: str,
        module_list: Iterable[ModuleType | str],
        init_relations: bool = True,
    ) -> dict[str, type[Model]]:
        """Registers the models of an app's modules under its label.

        Args:
            label: The app label.
            module_list: The modules of its models - module objects or dotted paths.
            init_relations: Whether to wire the relations of every model now.

        Returns:
            The app's models, by name.

        Raises:
            ConfigurationError: A module can't be imported, or a model is wrong.
        """
        with ModelBindingGraph(self):
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
        with ModelBindingGraph(self):
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
        ModelConnectionChecks.raise_if_names_differ_only_in_case(
            [model for app in self.apps.values() for model in app.values() if model._meta.swapped is None]
        )
        self.bind_base_queries()

    def bind_base_queries(self) -> None:
        """Binds every registered model's ``basetable``/``basequery``/``basequery_all_fields`` to its
        connection's dialect, without checking what the connection can write - enough to write a
        queryset as SQL where no connection is opened."""
        for app in self.apps.values():
            for model in app.values():
                self.bind_model_base_queries(model, model._meta.connection)

    @staticmethod
    def bind_model_base_queries(model: type[Model], connection: DatabaseClient) -> None:
        """Build one model's table and base queries using the explicitly selected client.

        The caller owns connection selection and model finalisation; historical registries
        can use their captured connection handler without consulting the current context.

        Args:
            model: The model whose table metadata and database fields are ready.
            connection: The client providing the query class for this binding.
        """
        previous_query_class = model._meta.query_class
        model._meta.basetable = Table(
            name=model._meta.db_table,
            schema=model._meta.schema,
            table_options=tuple(model._meta.table_options),
        )
        basequery = connection.query_class.from_(model._meta.basetable)
        model._meta.basequery = basequery
        model._meta.basequery_all_fields = basequery.select(*model._meta.db_fields)
        if connection.query_class is not previous_query_class:
            # The SQL cached for the model was rendered by the previous builder's dialect.
            Caches.forget_model_sql(model)

    def get_swappable_label(self, setting: str) -> str:
        """The ``app_label.ModelName`` label a ``swappable`` setting points at - the configured
        model, else the one model declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by exactly one model.
        """
        if setting in self.swappable_settings:
            return SwappableModels.get_single_model_label(setting, self.swappable_settings[setting])
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
                f"Unknown swappable setting {setting!r} - no model declares Meta.swappable = {setting!r}; "
                'set it in the "swappable" config section'
            )
        raise ConfigurationError(
            f"Swappable setting {setting!r} is declared by several models ({', '.join(default_labels)}) - "
            'choose one in the "swappable" config section'
        )

    def get_generated_table_name(self, model: type[Model]) -> str:
        """The table name of a model without ``Meta.table`` - from this registry's
        ``table_name_generator``, else the lower-cased class name.

        Args:
            model: The model.

        Returns:
            The name.

        Raises:
            ConfigurationError: The generator returned no text, or an empty one.
        """
        if self._table_name_generator is None:
            return model.__name__.lower()
        table_name = self._table_name_generator(model)
        if not isinstance(table_name, str) or not table_name.strip() or "\x00" in table_name:
            raise ConfigurationError(
                f"table_name_generator returned {table_name!r} for {model.__name__} - expected a table name"
            )
        return table_name

    def get_models_referencing(self, model: type[Model]) -> list[str]:
        """``"<Model>.<field>"`` for every OTHER registered model whose own forward FK/O2O/M2M
        field points at ``model``."""
        references: list[str] = []
        for registered_model in self.get_models_iterable():
            if registered_model is model:
                continue
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
        with ModelBindingGraph(self):
            models_to_remove = list(dict.fromkeys(models))
            for model in models_to_remove:
                if self.get_registered_app_label(model) is None:
                    raise ConfigurationError(f"Model {model.__name__!r} is not registered.")
            removed_model_names = {model._meta.full_name for model in models_to_remove}
            for model in models_to_remove:
                references = [
                    reference
                    for reference in self.get_models_referencing(model)
                    if reference.rsplit(".", 1)[0] not in removed_model_names
                ]
                if references:
                    raise ConfigurationError(
                        f"Cannot unregister {model._meta.full_name!r} - still referenced by "
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
                relation_field: ForeignKeyFieldInstance[Any] = meta.fields_map[field_name]  # type: ignore[assignment]
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

    def get_model(self, reference: ModelReference) -> type[Model]:
        """The model a reference names - an ``"app.Model"`` label, a ``swappable()`` setting (the model the
        setting points at), or a class (the registry's own class of the model, the class itself when the
        registry holds none).

        Args:
            reference: The reference.

        Returns:
            The model.

        Raises:
            ConfigurationError: The label isn't ``"app.Model"``, or names no registered app or model.
        """
        if isinstance(reference, SwappableModelReference):
            reference = self.get_swappable_label(reference.setting)
        if not isinstance(reference, str):
            return self.apps.get(reference._meta.app or "", {}).get(reference.__name__) or reference
        app_label, model_name = self.split_validated_reference(reference)
        try:
            return self.apps[app_label][model_name]
        except KeyError:
            if app_label not in self.apps:
                raise ConfigurationError(f"No app with name {app_label!r} registered.")
            raise ConfigurationError(f"No model with name {model_name!r} registered in app {app_label!r}.")

    def get_models_iterable(self) -> Iterable[type[Model]]:
        return chain.from_iterable(app.values() for app in self.apps.values())

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
        """Splits a model reference into its app label and model name.

        Args:
            reference: The reference, ``"<app>.<Model>"``.

        Returns:
            The app label and the model name.

        Raises:
            ConfigurationError: The reference isn't of two dot-separated parts.
        """
        if len(items := reference.split(".")) != 2:
            raise ConfigurationError(f'{reference!r} is not a model reference - expected "<app>.<Model>"')
        return items[0], items[1]

    @staticmethod
    def model_identity(target_model: type[Model]) -> tuple[str | None, str]:
        return target_model._meta.app, target_model.__name__

    def init_relations(self) -> None:
        """Wires the relations of every model and finalises each one - its lookups described; the querysets
        built before the models were set up become ordinary ones.
        """
        with ModelBindingGraph(self):
            self.link_relations()
            # The models a Meta.change_capture names by its label, before the capture is worked out.
            for app in self.apps.values():
                for model in app.values():
                    if model._meta.change_capture is not None:
                        model._meta.change_capture.link_models(self)
            # Every model's lookups, once all relations are wired - its filters and orderings are
            # described without a connection, right after early binding (Hare.bind_models()) too.
            for app in self.apps.values():
                for model in app.values():
                    model._meta.finalise_model()

    def _check_table_name_collisions(self) -> None:
        """Check a live registry's tables; a migration registry may retain aliases of one table."""
        ModelNameChecks._check_table_name_collisions(self)

    def link_relations(self) -> None:
        """Wires the relations of every model not inited yet - both sides of each - and marks it
        inited; no model is finalised."""
        # Every model gets its table name before the main pass: the through table names below use
        # the related model's.
        TableNames.assign_generated_table_names(self)
        SwappableModels.init_swapped_models(self)
        self._check_table_name_collisions()
        swappable_targets = SwappableModels.get_swappable_targets(self)
        for app in self.apps.values():
            for model in app.values():
                if model._meta.generic_foreign_key_fields and not model._meta._inited:
                    GenericForeignKeys.expand_swappable_targets(model, swappable_targets)

        ModelNameChecks._check_object_names(self)

        table_holders = RelationLinking.get_table_holders(self)
        for app_name, app in self.apps.items():
            for model_name, model in app.items():
                if model._meta._inited:
                    continue
                RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, model)
                for field in sorted(model._meta.many_to_many_fields):
                    ManyToManyLinking._link_many_to_many(self, app_name, model_name, model, field, table_holders)
                for generic_field in model._meta.generic_foreign_key_fields.values():
                    if generic_field.branch_names:
                        generic_field.bind_branches()
                # Only once every relation of the model resolved - a model whose initialisation
                # raised part-way must not look finished to a later init_relations() pass.
                model._meta._inited = True

    def get_table_identity(self, model: type[Model], schema: str | None, table: str) -> TableIdentity:
        """Identify a physical table using the connection's identifier comparison rules.

        Schema-less declarations retain None: their effective namespace is selected by
        the connection. Historical registries without connections keep declared spellings.
        """
        return (
            model._meta.default_connection,
            self.get_identifier_key(model, schema) if schema is not None else None,
            self.get_identifier_key(model, table),
        )

    def get_identifier_key(self, model: type[Model], name: str) -> str:
        """What a name of a model's table is told apart by on the database of its connection.

        Args:
            model: The model.
            name: The name.

        Returns:
            Its key.
        """
        connection_alias: str = model._meta.default_connection  # type: ignore[assignment]
        return self._connections.get_dialect(connection_alias).literals.get_identifier_key(name)
