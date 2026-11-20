from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from itertools import chain
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.apps.many_to_many_linking import ManyToManyLinking
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.apps.model_binding_preparation import ModelBindingPreparation
from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.core.apps.model_discovery import ModelDiscovery
from hare.core.apps.model_name_checks import ModelNameChecks
from hare.core.apps.relation_linking import RelationLinking
from hare.core.apps.swappable_models import SwappableModels
from hare.core.apps.table_names import TableNames
from hare.core.connections.connection_handler import ConnectionHandler
from hare.exceptions import ConfigurationError
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.models import Model
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.sql import Table

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference, TableIdentity
    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.literals.sql_literals import SqlLiterals
    from hare.models.model_binding import ModelBinding


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
        context: HareContext | None = None,
        dialects: dict[str, Dialect] | None = None,
    ) -> None:
        self.apps: dict[str, dict[str, type[Model]]] = {}
        self.context = context
        self.binding_graph = ModelBindingGraph(
            dialects
            if dialects is not None
            else {alias: connections.get_dialect(alias) for alias in connections._db_config or {}}
        )
        self.binding_graph.seal()
        self._preparation: ModelBindingPreparation | None = None
        #: Swappable model settings - setting name to the ``"app_label.ModelName"`` it points at.
        self.swappable_settings: dict[str, str | dict[str, str]] = dict(swappable or {})
        self._config = config or {}
        self._connections = connections
        self._table_name_generator = table_name_generator
        self._validate_connections = validate_connections
        if self._config:
            with ModelBindingPreparation(self):
                ModelDiscovery.load_from_config(self)

    def get_binding(self, reference: ModelReference) -> ModelBinding:
        """Get this registry version's metadata, independent of class-global state."""
        model = reference if isinstance(reference, type) else self.get_model(reference)
        if self._preparation is not None:
            return self._preparation.graph.get_binding(model)
        return self.binding_graph.get_binding(model)

    def get_prepared_binding(self, model: type[Model]) -> ModelBinding | None:
        """Read an existing completed node of the published graph, without allocating it.

        Implicit model managers remain templates until their exact class has a completed
        binding. Explicit factories use get_binding and require registration.
        """
        binding = self.binding_graph.bindings.get(model)
        return binding if binding is not None and binding._inited else None

    def new(self, reference: ModelReference, **values: Any) -> Model:
        """Create through the ordinary model constructor using this explicit registry."""
        binding = self.get_binding(reference)
        from hare.models.instances.instance_initialization import InstanceInitialization

        return InstanceInitialization.new(binding, self.context, **values)

    def construct(self, reference: ModelReference, **values: Any) -> Model:
        """Use the ordinary unchecked constructor with this registry's metadata."""
        binding = self.get_binding(reference)
        return binding._model._construct_with_binding(binding, self.context, **values)

    def get_queryset(self, reference: ModelReference, *, using: str | DatabaseClient | None = None) -> Any:
        """Start a normal or historical query with this registry's explicit binding."""
        binding = self.get_binding(reference)
        queryset = binding.manager.create_queryset(binding=binding, context=self.context)
        return queryset if using is None else queryset.using(using)

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
        with ModelBindingPreparation(self):
            app_models: list[type[Model]] = []
            for module in module_list:
                app_models += ModelDiscovery.discover_models(module, label, apps=self)
            self._preparation.include(app_models)  # type: ignore[union-attr]
            for model in app_models:
                self.get_binding(model).app = label

            # Merged, not replaced: a second registration under the label keeps the earlier models, and
            # the table name collision check sees them.
            self.apps.setdefault(label, {}).update({model.__name__: model for model in app_models})

            if init_relations:
                self.rebind_relations()

            return self.apps[label]

    def assign_default_connection(self, app_label: str, default_connection: str) -> None:
        """Set connection placement on the draft nodes of one registered app."""
        if self._preparation is None:
            raise ConfigurationError("Connection placement can only change while preparing a model graph")
        for model in self.apps[app_label].values():
            self.get_binding(model).default_connection = default_connection

    def finish_model_binding(self) -> None:
        """Validate and publish live bindings after declarations and connections are assigned.

        Initial discovery, live registration and restoration use the same finalization.
        Callers retain their ModelBindingGraph boundary and discovery-specific checks.
        """
        from hare.core.apps.cross_connection_checks import CrossConnectionChecks
        from hare.core.apps.enum_member_checks import EnumMemberChecks

        self.rebind_relations()
        CrossConnectionChecks.check_cross_connection_constraints(self)
        EnumMemberChecks.add_to_models(self.get_binding(model) for model in self.get_models_iterable())
        CrossConnectionChecks.check_change_capture_connections(self)
        if self._validate_connections:
            self.build_initial_querysets()

    def build_initial_querysets(self) -> None:
        """Validate model writes and prepare graph-local SQL inputs for configured dialects."""
        for app in self.apps.values():
            for model in app.values():
                binding = self.get_binding(model)
                ModelConnectionChecks.check_model_writes(
                    binding,
                    self._connections.get_main(binding.default_connection),  # type: ignore[arg-type]
                )
        ModelConnectionChecks.raise_if_names_differ_only_in_case(
            [
                binding
                for app in self.apps.values()
                for model in app.values()
                if (binding := self.get_binding(model)).swapped is None
            ],
            self._connections,
        )
        self.bind_base_queries()

    def rebind_relations(self) -> None:
        """Rebuild a complete live registry from declarations, including reused model classes.

        Initial loading and live registration share this boundary. Historical registries
        prepare their snapshots through the same relation linker.
        """
        with ModelBindingPreparation(self):
            self.init_relations()

    def bind_base_queries(self) -> None:
        """Binds every registered model's ``basetable``/``basequery``/``basequery_all_fields`` to its
        connection's dialect, without checking what the connection can write - enough to write a
        queryset as SQL where no connection is opened."""
        if self._preparation is None:
            with ModelBindingPreparation(self):
                self.init_relations()
                self.bind_base_queries()
            return
        for app in self.apps.values():
            for model in app.values():
                binding = self.get_binding(model)
                self.bind_model_base_queries(
                    binding,
                    self._connections.get_main(binding.default_connection),  # type: ignore[arg-type]
                )

    @staticmethod
    def bind_model_base_queries(binding: ModelBinding, connection: DatabaseClient) -> None:
        """Prepare structural SQL inputs with the caller's explicitly selected client."""
        binding.basetable = Table(
            name=binding.db_table, schema=binding.schema, table_options=tuple(binding.table_options)
        )
        binding.basequery = connection.query_class.from_(binding.basetable)
        binding.basequery_all_fields = binding.basequery.select(*binding.db_fields)

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
            if self.get_binding(model).swappable == setting
        ]
        return SwappableModels.get_declared_model_label(setting, default_labels)

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
            meta = self.get_binding(registered_model)
            for field_name in sorted(meta.forward_key_relation_names | meta.many_to_many_fields):
                field = meta.fields_map[field_name]
                if getattr(field, "related_model", None) is model and not getattr(field, "_generated", False):
                    references.append(f"{meta.full_name}.{field_name}")
        return references

    def remove_models(self, models: Iterable[type[Model]]) -> None:
        """Validate removal against this published graph, then publish the remaining graph."""
        models_to_remove = list(dict.fromkeys(models))
        for model in models_to_remove:
            if self.get_registered_app_label(model) is None:
                raise ConfigurationError(f"Model {model.__name__!r} is not registered.")
        removed_names = {self.get_binding(model).full_name for model in models_to_remove}
        for model in models_to_remove:
            references = [
                reference
                for reference in self.get_models_referencing(model)
                if reference.rsplit(".", 1)[0] not in removed_names
            ]
            if references:
                raise ConfigurationError(
                    f"Cannot unregister {self.get_binding(model).full_name!r} - still referenced by "
                    f"{', '.join(references)}. Unregister the referencing model(s) first."
                )
        with ModelBindingPreparation(self) as preparation:
            for model in models_to_remove:
                app_label = self.get_registered_app_label(model)
                if app_label is not None:
                    app_models = self.apps[app_label]
                    del app_models[model.__name__]
                    if not app_models:
                        del self.apps[app_label]
                # Preparation holds an unpublished graph with a mutable mapping.
                preparation.graph.bindings.pop(model, None)  # type: ignore[attr-defined]
            self.finish_model_binding()

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
        setting points at), or the exact class registered in this version of the registry.

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
            if self.get_registered_app_label(reference) is not None:
                return reference
            raise ConfigurationError(f"Model {reference.__name__!r} is not registered in this registry")
        app_label, model_name = self.split_validated_reference(reference)
        try:
            return self.apps[app_label][model_name]
        except KeyError:
            if app_label not in self.apps:
                raise ConfigurationError(f"No app with name {app_label!r} registered.")
            raise ConfigurationError(f"No model with name {model_name!r} registered in app {app_label!r}.")

    def get_models_iterable(self) -> Iterable[type[Model]]:
        return chain.from_iterable(app.values() for app in self.apps.values())

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

    def model_identity(self, target_model: type[Model]) -> tuple[str | None, str]:
        return self.get_binding(target_model).app, target_model.__name__

    def init_relations(self) -> None:
        """Wire and finalise each node in the unpublished graph, including lookup metadata.
        Unbound queryset templates are replayed separately when a caller selects this graph.
        """
        with ModelBindingPreparation(self):
            self.link_relations()
            # The models a Meta.change_capture names by its label, before the capture is worked out.
            for app in self.apps.values():
                for model in app.values():
                    binding = self.get_binding(model)
                    if binding._inited and binding.change_capture is not None:
                        binding.change_capture.link_models(self)
            # Every model's lookups, once all relations are wired - its filters and orderings are
            # described without a connection, right after early binding (Hare.bind_models()) too.
            for app in self.apps.values():
                for model in app.values():
                    binding = self.get_binding(model)
                    if binding._inited:
                        binding.finalise_model()
                    else:
                        # Historical graphs can represent a migration before all relation targets
                        # exist. Keep their scalar column metadata; queries require complete nodes.
                        binding.finalise_fields()
                    binding.basequery_all_fields = binding.basequery.select(*binding.db_fields)

    def _check_table_name_collisions(self) -> None:
        """Check a live registry's tables; a migration registry may retain aliases of one table."""
        ModelNameChecks._check_table_name_collisions(self)

    def link_relations(self, *, deferred_models: frozenset[type[Model]] = frozenset()) -> None:
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
                if model in deferred_models:
                    continue
                binding = self.get_binding(model)
                if binding.generic_foreign_key_fields and not binding._inited:
                    GenericForeignKeys.expand_swappable_targets(binding, swappable_targets)

        ModelNameChecks._check_object_names(self)

        table_holders = RelationLinking.get_table_holders(self)
        for app_name, app in self.apps.items():
            for model_name, model in app.items():
                if model in deferred_models:
                    continue
                binding = self.get_binding(model)
                if binding._inited:
                    continue
                RelationLinking.ensure_foreign_key_or_one_to_one_inited(self, model)
                for field in sorted(binding.many_to_many_fields):
                    ManyToManyLinking._link_many_to_many(self, app_name, model_name, model, field, table_holders)
                for generic_field in binding.generic_foreign_key_fields.values():
                    if generic_field.branch_names:
                        generic_field.bind_branches()
                # Only once every relation of the model resolved - a model whose initialisation
                # raised part-way must not look finished to a later init_relations() pass.
                binding._inited = True

    def get_table_identity(self, model: type[Model], schema: str | None, table: str) -> TableIdentity:
        """Identify a physical table using the connection's identifier comparison rules.

        Schema-less declarations retain their dialect's effective declaration namespace:
        Each dialect supplies its effective default schema or retains an unspecified schema.
        """
        literals = self.get_identifier_rules(model)
        return (
            self.get_binding(model).default_connection,
            literals.get_schema_key(schema),
            literals.get_identifier_key(table),
        )

    def get_identifier_rules(self, model: type[Model]) -> SqlLiterals:
        """The captured dialect's comparison rules; pure states supply generic SQL rules."""
        binding = self.get_binding(model)
        return binding.graph.get_dialect(binding.default_connection).literals

    def get_identifier_key(self, model: type[Model], name: str) -> str:
        """Compare a name by the captured dialect, retaining spelling for a pure state."""
        literals = self.get_identifier_rules(model)
        return literals.get_identifier_key(name)
