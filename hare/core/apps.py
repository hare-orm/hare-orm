import importlib
import warnings
from collections.abc import Callable, Iterable, Iterator
from copy import copy
from functools import partial
from inspect import isclass
from itertools import chain
from types import ModuleType
from typing import Any, ClassVar, cast

from hare.core.caches import Caches
from hare.core.connection_handler import ConnectionHandler
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.identifiers import Identifiers
from hare.exceptions import ConfigurationError
from hare.fields.constants import CASCADE, FK_COLUMN_SUFFIX
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.swappable import SwappableModelReference
from hare.models import Model
from hare.models.meta_class import ModelMeta
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from hare.query.queryset.calls_before_setup import CallsBeforeSetup
from hare.sql import Query, Table


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
        swappable: dict[str, str] | None = None,
    ) -> None:
        self.apps: dict[str, dict[str, type[Model]]] = {}
        #: Swappable model settings - setting name to the ``"app_label.ModelName"`` it points at.
        self.swappable_settings: dict[str, str] = dict(swappable or {})
        self._config = config or {}
        self._connections = connections
        self._table_name_generator = table_name_generator
        self._validate_connections = validate_connections
        if self._config:
            self._load_from_config()

    @staticmethod
    def _is_declared_in_module(attr: Any, module_name: str) -> bool:
        """Returns whether a module attribute was declared in that module or one of its submodules.

        Args:
            attr: The attribute.
            module_name: The module's name.

        Returns:
            True for a class whose ``__module__`` is the module or a submodule of it.
        """
        declaring_module_name = getattr(attr, "__module__", None)
        return isinstance(declaring_module_name, str) and (
            declaring_module_name == module_name or declaring_module_name.startswith(f"{module_name}.")
        )

    @staticmethod
    def _discover_models(models_path: ModuleType | str, app_label: str) -> list[type[Model]]:
        if isinstance(models_path, ModuleType):
            module = models_path
        else:
            try:
                module = importlib.import_module(models_path)
            except ImportError:
                raise ConfigurationError(f'Module "{models_path}" not found')
            except (NameError, AttributeError, SyntaxError) as exc:
                # The module exists but raised while its body executed (typo, bad reference,
                # broken syntax) - a different failure mode than "not found" above, so the
                # message says what actually happened instead of implying a missing module.
                raise ConfigurationError(f'Module "{models_path}" failed to import: {exc}') from exc
        discovered_models: list[type[Model]] = []
        # The models skipped as another app's - for the error below when every candidate is one.
        models_excluded_for_other_app: list[type[Model]] = []
        if possible_models := getattr(module, "__models__", None):
            try:
                possible_models = [*possible_models]
            except TypeError:
                possible_models = None
        if not possible_models:
            possible_models = [getattr(module, attr_name) for attr_name in dir(module)]
            if module.__spec__ is not None:
                # A module's models are the ones it declares, itself or in a submodule; a model
                # imported from elsewhere belongs to its own module's app. A module assembled at
                # runtime has no declarations - its attributes count.
                possible_models = [
                    attr for attr in possible_models if Apps._is_declared_in_module(attr, module.__name__)
                ]
        for attr in possible_models:
            if isclass(attr) and issubclass(attr, Model) and not attr._meta.abstract:
                if attr._meta.app and attr._meta.app != app_label:
                    models_excluded_for_other_app.append(attr)
                    continue
                attr._meta.app = app_label
                discovered_models.append(attr)
        if not discovered_models:
            if models_excluded_for_other_app:
                # Every candidate belongs to another app - usually a module whose classes kept the
                # app label of an earlier context.
                raise ConfigurationError(
                    f'Module "{models_path}" has no models for app label "{app_label}" - every '
                    f"candidate model in it ({', '.join(m.__name__ for m in models_excluded_for_other_app)}) "
                    f"is already registered under a different app label. If this module is meant "
                    f'to be shared between apps, re-registering it under "{app_label}" only works '
                    "for models that were never registered under a different label before."
                )
            warnings.warn(f'Module "{models_path}" has no models', RuntimeWarning, stacklevel=4)
        return discovered_models

    def init_app(
        self,
        label: str,
        module_list: Iterable[ModuleType | str],
        _init_relations: bool = True,
    ) -> dict[str, type[Model]]:
        app_models: list[type[Model]] = []
        for module in module_list:
            app_models += self._discover_models(module, label)

        # Merged, not replaced: a second registration under the label keeps the earlier models, and
        # the table name collision check sees them.
        self.apps.setdefault(label, {}).update({model.__name__: model for model in app_models})

        if _init_relations:
            self._init_relations()

        return self.apps[label]

    def _load_from_config(self) -> None:
        if self._connections is None:
            raise ConfigurationError("ConnectionHandler is required to load from config")
        for name, info in self._config.items():
            default_connection = info.get("default_connection", DEFAULT_CONNECTION_NAME)
            if default_connection not in self._connections.db_config:
                raise ConfigurationError(f'Unknown connection "{default_connection}" for app "{name}"')
            if self._validate_connections:
                self._connections.get(default_connection)

            self.init_app(name, info["models"], _init_relations=False)
            self._assign_default_connection(name, default_connection)

        self._check_swappable_settings()
        self._init_swapped_models()
        self.check_cross_connection_constraints()
        self._init_relations()
        if self._validate_connections:
            self._build_initial_querysets()

    def _assign_default_connection(self, app_label: str, default_connection: str) -> None:
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
                self._assign_default_connection(name, info.get("default_connection", DEFAULT_CONNECTION_NAME))
        self._init_relations()
        if self._validate_connections:
            self._build_initial_querysets()

    def _build_initial_querysets(self) -> None:
        """Binds every registered model's ``basetable``/``basequery``/``basequery_all_fields`` to this
        registry's connection and dialect. They live on the model class, so two contexts registering
        one module concurrently overwrite each other's binding - the last one wins.
        """
        for app in self.apps.values():
            for model in app.values():
                model._meta.basetable = Table(name=model._meta.db_table, schema=model._meta.schema)
                basequery = model._meta.db.query_class.from_(model._meta.basetable)
                model._meta.basequery = cast("Query", basequery)
                model._meta.basequery_all_fields = cast("Query", basequery.select(*model._meta.db_fields))

    def get_swappable_label(self, setting: str) -> str:
        """The ``app_label.ModelName`` label a ``swappable`` setting points at - the configured
        model, else the one model declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by exactly one model.
        """
        if setting in self.swappable_settings:
            return self.swappable_settings[setting]
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

    def get_swappable_model(self, setting: str) -> type[Model]:
        """The model a ``swappable`` setting points at - see ``get_swappable_label()``.

        Raises:
            ConfigurationError: The setting is unknown, or its model isn't registered.
        """
        app_label, model_name = self.get_swappable_label(setting).split(".", 1)
        return self.get_model(app_label, model_name)

    def _check_swappable_settings(self) -> None:
        """Checks every configured ``swappable`` setting points at a registered concrete model
        that isn't itself swapped for another one.

        Raises:
            ConfigurationError: A setting's model isn't registered (or is abstract), or is swapped
                by its own ``Meta.swappable`` setting - a chain of swaps.
        """
        for setting, label in self.swappable_settings.items():
            app_label, model_name = label.split(".", 1)
            target_model = self.apps.get(app_label, {}).get(model_name)
            if target_model is None:
                reason = (
                    "it is an abstract model (Meta.abstract = True)"
                    if self._is_abstract_model_of_app(app_label, model_name)
                    else f'app "{app_label}" has no such model'
                )
                raise ConfigurationError(f'Swappable setting "{setting}" points at "{label}", but {reason}')
            target_setting = target_model._meta.swappable
            if target_setting is not None and target_setting != setting:
                target_label = self.swappable_settings.get(target_setting, label)
                if target_label != label:
                    raise ConfigurationError(
                        f'Swappable setting "{setting}" points at "{label}", which the {target_setting} setting '
                        f'swaps for "{target_label}" - point {setting} at the final model directly'
                    )

    def _is_abstract_model_of_app(self, app_label: str, model_name: str) -> bool:
        """Whether one of ``app_label``'s configured model modules declares ``model_name`` as an
        abstract model."""
        for models_path in self._config.get(app_label, {}).get("models", ()):
            module = models_path if isinstance(models_path, ModuleType) else importlib.import_module(models_path)
            candidate = getattr(module, model_name, None)
            if isclass(candidate) and issubclass(candidate, Model) and candidate._meta.abstract:
                return True
        return False

    def _init_swapped_models(self) -> None:
        """Marks every model declaring ``Meta.swappable`` with the label its setting points at
        instead of it (``_meta.swapped``), or None while it is the model in use."""
        for app_label, app in self.apps.items():
            for model_name, model in app.items():
                setting = model._meta.swappable
                label = self.get_swappable_label(setting) if setting else None
                model._meta.swapped = label if label is not None and label != f"{app_label}.{model_name}" else None

    def _assign_generated_table_names(self) -> None:
        """Gives every model without ``Meta.table`` its name from this registry's
        ``table_name_generator`` (else the lower-cased class name) - on every init. Names derived
        from the table when relations were first set up (an automatic through table, a default
        ``related_name``) keep their first value.
        """
        renamed_models: list[type[Model]] = []
        for app in self.apps.values():
            for model in app.values():
                meta = model._meta
                if meta.db_table and meta.db_table != meta.generated_db_table:
                    continue
                generated_table = Identifiers.get_within_limit(
                    self._table_name_generator(model) if self._table_name_generator else model.__name__.lower()
                )
                if meta.db_table and generated_table != meta.db_table:
                    renamed_models.append(model)
                meta.db_table = generated_table
                meta.generated_db_table = generated_table
        if renamed_models:
            Caches.forget_model_caches(renamed_models)

    def check_cross_connection_constraints(self) -> None:
        """Refuses a database-enforced relation between models on different connections - a
        foreign key constraint can't reference a table in another database.

        Raises:
            ConfigurationError: A FK/O2O/M2M with db_constraint=True points at a model whose
                default connection differs.
        """
        for app in self.apps.values():
            for model in app.values():
                meta = model._meta
                for field_name in (*meta.fk_fields, *meta.o2o_fields, *meta.m2m_fields):
                    relation_field = meta.fields_map[field_name]
                    is_generated = getattr(relation_field, "_generated", False)
                    if is_generated or not getattr(relation_field, "db_constraint", False):
                        continue
                    related_model, _ = self._get_related_model(relation_field.model_name)  # type: ignore[attr-defined]
                    related_connection = related_model._meta.default_connection
                    if (
                        meta.default_connection is None
                        or related_connection is None
                        or related_connection == meta.default_connection
                    ):
                        continue
                    raise ConfigurationError(
                        f'Relation "{meta.full_name}.{field_name}" points at "{related_model._meta.full_name}", which '
                        f'lives on connection "{related_connection}" while "{meta.full_name}" lives on '
                        f'"{meta.default_connection}" - a database foreign key constraint can\'t span two '
                        "databases. Declare the field with db_constraint=False."
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
            if model in self.get_relation_targets(registered_model):
                meta = registered_model._meta
                for field_name in sorted(meta.fk_fields | meta.o2o_fields | meta.m2m_fields):
                    field = meta.fields_map[field_name]
                    if getattr(field, "related_model", None) is model and not getattr(field, "_generated", False):
                        references.append(f"{meta.full_name}.{field_name}")
        return references

    def remove_model(self, model: type[Model]) -> None:
        """Removes a registered model and everything its registration added elsewhere.

        Args:
            model: The registered model class.

        Raises:
            ConfigurationError: ``model`` isn't registered here, or another registered model
                still has a relation pointing at it.
        """
        self.remove_models([model])

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
            targets = self.get_relation_targets(model)
            affected_models.update(targets)
            for target in targets:
                backward_field_names = [
                    field_name
                    for field_name, field in target._meta.fields_map.items()
                    if getattr(field, "related_model", None) is model
                    and (
                        isinstance(field, BackwardFKRelation)
                        or (isinstance(field, ManyToManyFieldInstance) and field._generated)
                    )
                ]
                for field_name in backward_field_names:
                    target._meta.remove_field(field_name)
        for model in models_to_discard:
            meta = model._meta
            for field_name in meta.fk_fields | meta.o2o_fields:
                relation_field = cast("ForeignKeyFieldInstance[Any]", meta.fields_map[field_name])
                for shadow_name in relation_field.source_fields:
                    if shadow_name != field_name and shadow_name in meta.fields_map:
                        meta.remove_field(shadow_name)
            self.drop_registry_entry(model)
            meta._inited = False
            meta._fk_o2o_inited = False
            meta.default_connection = None
            meta.basetable = Table("")
            meta.basequery = Query()
            meta.basequery_all_fields = Query()
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
    def _split_reference(reference: str) -> tuple[str, str]:
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

    def _get_related_model_by_name(self, related_app_name: str, related_model_name: str) -> type[Model]:
        """Tests that the app and model really exist. Throws a ConfigurationError with a
        hopefully helpful message. If successful, returns the requested model.

        Raises:
            ConfigurationError: If no such app exists.
        """
        try:
            return self.apps[related_app_name][related_model_name]
        except KeyError:
            if related_app_name not in self.apps:
                raise ConfigurationError(
                    f"No app with name '{related_app_name}' registered."
                    f" Please check your model names in ForeignKeyFields"
                    f" and configurations."
                )
            raise ConfigurationError(
                f"No model with name '{related_model_name}' registered in app '{related_app_name}'."
            )

    def _get_related_model(self, reference: str | type[Model] | SwappableModelReference) -> tuple[type[Model], str]:
        """A relation's own `model_name`/`reference` is either the target model class
        directly, a `"<appname>.<modelname>"` string that still needs resolving through
        the app registry, or a swappable reference naming the model a setting points at.

        Raises:
            ConfigurationError: The target doesn't exist or is abstract, or is a swapped model
                named directly instead of through ``swappable()``.
        """
        if isinstance(reference, SwappableModelReference):
            related_app_name, related_model_name = self._split_reference(self.get_swappable_label(reference.setting))
            return self._get_related_model_by_name(related_app_name, related_model_name), related_model_name
        related_model, related_model_name = self._get_named_related_model(reference)
        if self.REJECTS_SWAPPED_RELATION_TARGETS and related_model._meta.swapped is not None:
            raise ConfigurationError(
                f'A relation points at "{related_model._meta.full_name}", which has been swapped for '
                f'"{related_model._meta.swapped}" by the {related_model._meta.swappable} setting - declare it '
                f'with swappable("{related_model._meta.swappable}") instead'
            )
        return related_model, related_model_name

    def _get_named_related_model(self, reference: str | type[Model]) -> tuple[type[Model], str]:
        """The model a class or ``"<appname>.<modelname>"`` reference names.

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
        related_app_name, related_model_name = self._split_reference(reference)
        return self._get_related_model_by_name(related_app_name, related_model_name), related_model_name

    @staticmethod
    def _apply_related_name_template(related_name: str, model: type[Model]) -> str:
        """Substitutes ``%(app_label)s``/``%(class)s`` in a declared ``related_name`` - each concrete
        subclass of an abstract base gets its own backward name.
        """
        if "%(app_label)s" not in related_name and "%(class)s" not in related_name:
            return related_name
        return related_name % {"app_label": model._meta.app, "class": model.__name__.lower()}

    def _init_fk_o2o_field(self, model: type[Model], field: str, is_o2o: bool = False) -> None:
        fk_object = cast("OneToOneFieldInstance[Any] | ForeignKeyFieldInstance[Any]", model._meta.fields_map[field])
        related_model, related_model_name = self._get_related_model(fk_object.model_name)

        if to_field := fk_object.to_field:
            to_field_names: tuple[str, ...] = to_field if isinstance(to_field, tuple) else (to_field,)
            related_fields = []
            for name in to_field_names:
                if name not in related_model._meta.fields_map:
                    # May be the shadow column of the target's own not yet initialized relation.
                    self._ensure_fk_o2o_inited(related_model)
                related_field = related_model._meta.fields_map.get(name)
                if not related_field:
                    raise ConfigurationError(f'there is no field named "{name}" in model "{related_model_name}"')
                related_fields.append(related_field)
            if len(to_field_names) == 1:
                if not related_fields[0].unique:
                    raise ConfigurationError(
                        f'field "{to_field_names[0]}" in model "{related_model_name}" is not unique'
                    )
            elif to_field_names != related_model._meta.pk_attr_names:
                # A composite to_field must be the target's whole primary key, in its order.
                raise ConfigurationError(
                    f'{"OneToOneField" if is_o2o else "ForeignKeyField"} "{model.__name__}.{field}" '
                    f'to_field={to_field_names} must exactly match "{related_model_name}"\'s composite '
                    f"primary key {related_model._meta.pk_attr_names} (in the same order) - an arbitrary "
                    "composite UniqueConstraint target isn't supported."
                )
        elif related_model._meta.has_composite_primary_key:
            to_field_names = cast("tuple[str, ...]", related_model._meta.pk_attr)
            related_fields = list(related_model._meta.pk_fields)
            fk_object.to_field = to_field_names
        else:
            relation_label = "OneToOneField" if is_o2o else "ForeignKeyField"
            related_model._meta.raise_if_no_primary_key(
                f'{relation_label} "{model.__name__}.{field}" to it without to_field='
            )
            to_field_names = (cast("str", related_model._meta.pk_attr),)
            related_fields = [related_model._meta.pk]
            fk_object.to_field = related_model._meta.pk_attr

        if any(isinstance(related_field, OneToOneFieldInstance) for related_field in related_fields):
            # The target (typically its OneToOneField(primary_key=True)) is itself a relation with
            # no column of its own - reference the shadow column it stores its value in instead.
            self._ensure_fk_o2o_inited(related_model)
            related_fields = [
                related_model._meta.fields_map[related_field.source_field]
                if isinstance(related_field, OneToOneFieldInstance) and related_field.source_field
                else related_field
                for related_field in related_fields
            ]
            if any(isinstance(related_field, OneToOneFieldInstance) for related_field in related_fields):
                raise ConfigurationError(
                    f'{"OneToOneField" if is_o2o else "ForeignKeyField"} "{model.__name__}.{field}" targets '
                    f'"{related_model_name}", whose own OneToOneField primary key forms a cycle with it.'
                )
            to_field_names = tuple(related_field.model_field_name for related_field in related_fields)
            fk_object.to_field = to_field_names if len(to_field_names) > 1 else to_field_names[0]

        if is_o2o and fk_object.pk and len(to_field_names) > 1:
            raise ConfigurationError(
                f'OneToOneField "{model.__name__}.{field}" can\'t both target a composite primary key '
                f'and be used as "{model.__name__}"\'s own primary key - a composite own-PK must be '
                "declared directly via CompositePrimaryKey, not derived from a composite O2O target"
            )

        fk_object.to_field_instance = related_fields[0]  # unchanged semantics for existing readers
        fk_object.to_field_names = to_field_names
        fk_object.to_field_instances = tuple(related_fields)
        fk_object.field_type = fk_object.to_field_instance.field_type

        shadow_names: tuple[str, ...]
        if len(to_field_names) == 1:
            shadow_names = (f"{field}{FK_COLUMN_SUFFIX}",)
        else:
            # "<field>_<to_field_name>" per component, e.g. script -> script_id,
            # script_version - deterministic, never collides with the single-column
            # convention (which never appends a second "_<name>" after "_id").
            shadow_names = tuple(f"{field}_{name}" for name in to_field_names)

        # fk_object.source_field (singular) only makes sense as a DB-column-name override for the
        # single-column case - a composite FK's per-column DB names are never customized via the
        # one source_field= kwarg on the logical field.
        key_source_field_names = (
            ((fk_object.source_field or shadow_names[0]),) if len(shadow_names) == 1 else shadow_names
        )
        if fk_object.index and (
            not all(related_field.indexable for related_field in related_fields)
            or model._meta.is_indexed_by_leading_columns(
                field, list(zip(shadow_names, key_source_field_names, strict=True))
            )
        ):
            # An index that already leads with the key column(s) serves the relation's lookups.
            fk_object.index = False

        source_fields: list[str] = []
        key_source_fields: list[str] = []
        for shadow_name, related_field, key_source_field in zip(
            shadow_names, related_fields, key_source_field_names, strict=True
        ):
            key_fk_object = copy(related_field)
            key_fk_object.reference = fk_object
            key_fk_object.source_field = key_source_field
            # _default_is_coroutine travels with default - left as the target pk's own flag, an
            # async pk default made every FK to that model await its own (None) default on save.
            for attr in (
                "index",
                "default",
                "_default_is_coroutine",
                "null",
                "generated",
                "description",
                "db_default",
                "sensitive",
            ):
                setattr(key_fk_object, attr, getattr(fk_object, attr))
            if len(shadow_names) > 1:
                # A composite key gets one index over all of its columns, not one per column.
                key_fk_object.index = False
            if is_o2o:
                key_fk_object.pk = fk_object.pk
                # A composite one-to-one's uniqueness is one constraint over all key columns, added
                # below.
                key_fk_object.unique = fk_object.unique if len(shadow_names) == 1 else False
            else:
                key_fk_object.pk = False
                key_fk_object.unique = False
            model._meta.add_field(shadow_name, key_fk_object)
            source_fields.append(shadow_name)
            key_source_fields.append(key_source_field)

        if is_o2o and len(shadow_names) > 1:
            # Added once: a model rendered from migration state already carries it.
            composite_unique_constraint = UniqueConstraint(fields=shadow_names)
            if composite_unique_constraint not in model._meta.constraints:
                model._meta.constraints = (*model._meta.constraints, composite_unique_constraint)

        fk_object.related_model = related_model
        fk_object.source_fields = tuple(source_fields)
        fk_object.db_column_names = tuple(key_source_fields)
        fk_object.source_field = source_fields[0]  # unchanged for len==1; "primary" shadow column otherwise
        if is_o2o and fk_object.pk:
            model._meta.pk_attr = source_fields[0]
            LookupInfoBuilder.forget_descriptions()
        fk_relation = (
            BackwardOneToOneRelation(
                model,
                source_fields[0],
                key_source_fields[0],
                null=True,
                description=fk_object.description,
                relation_fields=tuple(source_fields),
                relation_source_fields=tuple(key_source_fields),
            )
            if is_o2o
            else BackwardFKRelation(
                model,
                source_fields[0],
                key_source_fields[0],
                null=fk_object.null,
                description=fk_object.description,
                relation_fields=tuple(source_fields),
                relation_source_fields=tuple(key_source_fields),
            )
        )
        fk_relation.to_field_instance = fk_object.to_field_instance
        fk_relation.to_field_names = fk_object.to_field_names
        fk_relation.to_field_instances = fk_object.to_field_instances
        if model._meta.swapped is not None:
            # A swapped model has no table and no rows - nothing on the target points back at it,
            # and its backward accessor would clash with the one of the model swapped in.
            return
        if (backward_relation_name := fk_object.related_name) is False:
            # No public accessor, but on_delete still has to reach the rows pointing back.
            related_model._meta.add_hidden_backward_relation(f"{model._meta.full_name}.{field}", fk_relation)
            return
        if not backward_relation_name:
            backward_relation_name = f"{model._meta.db_table}s"
        else:
            backward_relation_name = fk_object.related_name = self._apply_related_name_template(
                backward_relation_name, model
            )
        if backward_relation_name in related_model._meta.fields:
            raise ConfigurationError(
                f'backward relation "{backward_relation_name}" duplicates in model {related_model_name}'
                ' - use a related_name template such as "%(app_label)s_%(class)s_..." to give each'
                " concrete model a distinct backward-accessor name."
            )
        self._check_related_name_not_reserved(related_model, backward_relation_name, model, field)
        related_model._meta.add_field(backward_relation_name, fk_relation)

    @staticmethod
    def _check_related_name_not_reserved(
        related_model: type[Model], backward_relation_name: str, model: type[Model], field: str
    ) -> None:
        """Rejects a ``related_name`` that would shadow a ``Model`` attribute (``save``,
        ``filter``, ``pk``, ...) or one of ``related_model``'s own methods/attributes.

        Raises:
            ConfigurationError: ``backward_relation_name`` is taken.
        """
        taken = backward_relation_name in ModelMeta.get_reserved_field_names()
        if not taken:
            for base in related_model.__mro__:
                if base in Model.__mro__ or backward_relation_name not in base.__dict__:
                    continue
                attribute = base.__dict__[backward_relation_name]
                # A relation accessor generated by an earlier initialization of the same class.
                taken = not (isinstance(attribute, property) and isinstance(attribute.fget, partial))
                break
        if taken:
            raise ConfigurationError(
                f'related_name "{backward_relation_name}" of "{model.__name__}.{field}" would shadow the '
                f'"{backward_relation_name}" attribute of model {related_model.__name__} - choose another '
                "related_name."
            )

    def _check_auto_through_table_name_free(self, through: str, model: type[Model], field: str) -> None:
        """Rejects an auto-generated M2M through table name already used by another M2M field's
        through table or by a model's own table.

        Raises:
            ConfigurationError: ``through`` is taken.
        """
        for registered_model in self.get_models_iterable():
            if registered_model._meta.swapped is not None:
                # A swapped model has no table, nor through tables of its own.
                continue
            if registered_model._meta.db_table == through and registered_model._meta.schema == model._meta.schema:
                raise ConfigurationError(
                    f'ManyToManyField "{model.__name__}.{field}" gets the auto-generated through table '
                    f'"{through}", which is the table of model {registered_model.__name__} - set through= '
                    "explicitly."
                )
            for m2m_field_name in registered_model._meta.m2m_fields:
                other_m2m_field = cast(
                    "ManyToManyFieldInstance[Any]", registered_model._meta.fields_map[m2m_field_name]
                )
                if other_m2m_field._generated or (registered_model is model and m2m_field_name == field):
                    continue
                if other_m2m_field.through == through and other_m2m_field.through_schema == model._meta.schema:
                    raise ConfigurationError(
                        f'ManyToManyField "{model.__name__}.{field}" gets the auto-generated through table '
                        f'"{through}", already used by "{registered_model.__name__}.{m2m_field_name}" - set '
                        "through= explicitly on one of them."
                    )

    def _ensure_fk_o2o_inited(self, target_model: type[Model]) -> None:
        """Sets up every forward relation of ``target_model`` - a ``ManyToManyField(through=...)``
        needs its through model's foreign keys resolved first.
        """
        if target_model._meta._fk_o2o_inited:
            return
        target_model._meta._fk_o2o_inited = True
        # A OneToOneField primary key goes first: a relation pointing back at this model while
        # its fields are still being initialized needs its primary key column already in place.
        o2o_fields = list(target_model._meta.o2o_fields)
        primary_key_o2o_fields = [field for field in o2o_fields if target_model._meta.fields_map[field].pk]
        for field in primary_key_o2o_fields:
            self._init_fk_o2o_field(target_model, field, is_o2o=True)
        for field in sorted(target_model._meta.fk_fields):
            self._init_fk_o2o_field(target_model, field)
        for field in o2o_fields:
            if field not in primary_key_o2o_fields:
                self._init_fk_o2o_field(target_model, field, is_o2o=True)

    @staticmethod
    def _model_identity(target_model: type[Model]) -> tuple[str | None, str]:
        return target_model._meta.app, target_model.__name__

    @staticmethod
    def _find_m2m_through_fk(
        through_model: type[Model], target_model: type[Model], side_label: str
    ) -> ForeignKeyFieldInstance[Any]:
        """The one ``ForeignKeyField`` of ``through_model`` pointing at ``target_model``, matched by
        app and name - migration state renders its own copies of the model classes.

        Raises:
            ConfigurationError: There is no such field, or there are several.
        """
        target_key = Apps._model_identity(target_model)
        candidates = [
            name
            for name in through_model._meta.fk_fields
            if (related := cast("ForeignKeyFieldInstance[Any]", through_model._meta.fields_map[name]).related_model)
            is not None
            and Apps._model_identity(related) == target_key
        ]
        if len(candidates) != 1:
            raise ConfigurationError(
                f'ManyToManyField through model "{through_model.__name__}" must have exactly one '
                f'ForeignKeyField pointing to "{target_model.__name__}" for its {side_label} side, '
                f"found {len(candidates)}."
            )
        through_fk = cast("ForeignKeyFieldInstance[Any]", through_model._meta.fields_map[candidates[0]])
        # Every M2M read/write keys a through row by the target's primary key - a to_field= on any
        # other column would store values no M2M operation can match.
        if tuple(through_fk.to_field_names) != target_model._meta.pk_attr_names:
            raise ConfigurationError(
                f'ManyToManyField through model "{through_model.__name__}" field "{candidates[0]}" must '
                f'reference "{target_model.__name__}"\'s primary key {target_model._meta.pk_attr_names}, '
                f"not to_field={through_fk.to_field!r}."
            )
        return through_fk

    @staticmethod
    def _reconcile_m2m_through_on_delete(
        intended_on_delete: OnDelete, fk_field: ForeignKeyFieldInstance[Any], m2m_field_name: str, side_label: str
    ) -> OnDelete:
        """Reconciles a ``ManyToManyField(through=Model)``'s ``on_delete`` with the through model's
        foreign key to one side - the key is what the delete cascade and the DDL act on. A side left
        at the default ``CASCADE`` takes the other's; two different declared values raise.

        Raises:
            ConfigurationError: Both sides declare different values, or the value doesn't fit the
                foreign key (``SET_NULL`` needs ``null=True``, ``SET_DEFAULT`` a ``db_default`` - or
                a ``default`` with ``db_constraint=False``).
        """
        fk_on_delete = fk_field.on_delete
        if intended_on_delete in (fk_on_delete, CASCADE):
            return fk_on_delete
        if fk_on_delete != CASCADE:
            raise ConfigurationError(
                f'ManyToManyField "{m2m_field_name}" declares on_delete={intended_on_delete.name}, but '
                f"its through model's own FK field pointing at the {side_label} side already declares "
                f"on_delete={fk_on_delete.name} - set on_delete only once, on the through model's FK field."
            )
        if intended_on_delete == OnDelete.SET_NULL and not fk_field.null:
            raise ConfigurationError(
                f'ManyToManyField "{m2m_field_name}" declares on_delete=SET_NULL, but its through '
                f"model's own FK field pointing at the {side_label} side isn't null=True - add "
                "null=True to that field."
            )
        if intended_on_delete == OnDelete.SET_DEFAULT:
            # The FK field's own constructor never saw SET_DEFAULT (it was left at CASCADE), so
            # the requirement it would have enforced there is checked here instead.
            requirement_error = fk_field.get_set_default_requirement_error()
            if requirement_error is not None:
                raise ConfigurationError(
                    f'ManyToManyField "{m2m_field_name}" declares on_delete=SET_DEFAULT, but its through '
                    f"model's own FK field pointing at the {side_label} side doesn't qualify: "
                    f"{requirement_error}."
                )
        fk_field.on_delete = intended_on_delete
        return intended_on_delete

    @staticmethod
    def _expand_m2m_key(explicit_key: str, base_name: str, pk_names: tuple[str, ...]) -> tuple[str, ...]:
        """The through-table column names of one side of a ``ManyToManyField``: one column for a
        single-column key, one ``<prefix>_<pk_name>`` column per part of a composite key.
        """
        if len(pk_names) == 1:
            return (explicit_key or Identifiers.get_within_limit(f"{base_name}{FK_COLUMN_SUFFIX}"),)
        prefix = explicit_key or base_name
        return tuple(Identifiers.get_within_limit(f"{prefix}_{name}") for name in pk_names)

    def _init_relations(self) -> None:
        self._link_relations()
        # Every model's lookups, once all relations are wired - its filters and orderings are
        # described without a connection, right after early binding (Hare.bind_models()) too.
        for app in self.apps.values():
            for model in app.values():
                model._meta.finalise_model()
        # The querysets built before the models were set up become ordinary ones.
        CallsBeforeSetup.apply_pending()

    def _link_relations(self) -> None:
        """Wires the relations of every model not inited yet - both sides of each - and marks it
        inited; no model is finalised."""
        # Every model gets its table name before the main pass: the through table names below use
        # the related model's.
        self._assign_generated_table_names()
        self._init_swapped_models()
        self._check_table_name_collisions()

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
                    from hare.ddl.triggers import Trigger

                    names.extend(trigger.name for trigger in model._meta.triggers if isinstance(trigger, Trigger))
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

        for app_name, app in self.apps.items():
            for model_name, model in app.items():
                if model._meta._inited:
                    continue
                self._ensure_fk_o2o_inited(model)

                for field in list(model._meta.m2m_fields):
                    m2m_object = cast("ManyToManyFieldInstance[Any]", model._meta.fields_map[field])
                    if m2m_object._generated:
                        continue

                    related_model, related_model_name = self._get_related_model(m2m_object.model_name)
                    m2m_object.related_model = related_model
                    # A link row names both rows by their keys.
                    model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}"')
                    related_model._meta.raise_if_no_primary_key(f'ManyToManyField "{model.__name__}.{field}" to it')

                    through_model: type[Model] | None = None
                    if m2m_object.through_model is not None:
                        # The through model's own foreign keys must be resolved first. The declared
                        # reference isn't overwritten - migration state copies the field as
                        # declared.
                        through_model, _ = self._get_related_model(m2m_object.through_model)
                        m2m_object.through_model_class = through_model
                        self._ensure_fk_o2o_inited(through_model)
                        if self._model_identity(model) == self._model_identity(related_model):
                            raise ConfigurationError(
                                f'ManyToManyField "{model.__name__}.{field}" is self-referential '
                                f'through "{through_model.__name__}" - hare cannot auto-detect '
                                "which of the through model's two FK fields belongs to which "
                                "side. Use a plain opaque through= table name instead for a "
                                "self-referential relation."
                            )
                        backward_fk = self._find_m2m_through_fk(through_model, model, "owning")
                        forward_fk = self._find_m2m_through_fk(through_model, related_model, "related")
                        forward_keys = forward_fk.db_column_names
                        backward_keys = backward_fk.db_column_names
                        # The declared on_delete, taken before the reconciliations below overwrite
                        # it.
                        declared_on_delete = m2m_object.on_delete
                        m2m_object.on_delete = self._reconcile_m2m_through_on_delete(
                            declared_on_delete, backward_fk, field, model.__name__
                        )
                        generated_on_delete = self._reconcile_m2m_through_on_delete(
                            declared_on_delete, forward_fk, field, related_model.__name__
                        )
                    else:
                        # The class name, not db_table - the related model may not have been visited
                        # yet.
                        forward_keys = self._expand_m2m_key(
                            m2m_object.forward_key, related_model.__name__.lower(), related_model._meta.pk_attr_names
                        )
                        backward_keys = self._expand_m2m_key(
                            m2m_object.backward_key, model._meta.db_table, model._meta.pk_attr_names
                        )
                        if not m2m_object.backward_key and backward_keys == forward_keys:
                            # Self-referential: the default backward side would collide with the
                            # forward side's columns.
                            backward_keys = self._expand_m2m_key(
                                "", f"{model._meta.db_table}_rel", model._meta.pk_attr_names
                            )
                        # The automatic through table has no foreign key field of its own - both
                        # directions share the declared on_delete.
                        generated_on_delete = m2m_object.on_delete
                    m2m_object.forward_key, m2m_object.forward_keys = forward_keys[0], forward_keys
                    m2m_object.backward_key, m2m_object.backward_keys = backward_keys[0], backward_keys

                    if not (backward_relation_name := m2m_object.related_name):
                        backward_relation_name = m2m_object.related_name = f"{model._meta.db_table}s"
                    else:
                        backward_relation_name = m2m_object.related_name = self._apply_related_name_template(
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
                        self._check_related_name_not_reserved(related_model, backward_relation_name, model, field)

                    if through_model is not None:
                        # The through model's own table, in its own schema.
                        m2m_object.through = through_model._meta.db_table
                        m2m_object.through_schema = through_model._meta.schema
                    else:
                        if not m2m_object.through:
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
                                if isinstance(m2m_object.model_name, SwappableModelReference)
                                else f"{model._meta.db_table}_{related_model_table_name}"
                            )
                            # Shortened with a digest to fit the identifier limit.
                            through = Identifiers.get_within_limit(through)
                            if not is_swapped:
                                self._check_auto_through_table_name_free(through, model, field)
                            m2m_object.through = through
                        m2m_object.through_schema = model._meta.schema

                    # The through model is passed as the class, with its reconciled on_delete.
                    m2m_relation = ManyToManyFieldInstance(
                        f"{app_name}.{model_name}",
                        through_model if through_model is not None else m2m_object.through,
                        forward_key=m2m_object.backward_key,
                        backward_key=m2m_object.forward_key,
                        related_name=field,
                        on_delete=generated_on_delete,
                        field_type=model,
                        description=m2m_object.description,
                    )
                    m2m_relation._generated = True
                    m2m_relation.through = m2m_object.through
                    m2m_relation.through_schema = m2m_object.through_schema
                    m2m_relation.through_model = through_model
                    m2m_relation.through_model_class = through_model
                    # Swapped from the owning side, which already resolved both sides.
                    m2m_relation.forward_keys = m2m_object.backward_keys
                    m2m_relation.backward_keys = m2m_object.forward_keys
                    LookupInfoBuilder.forget_descriptions()
                    if not is_swapped:
                        related_model._meta.add_field(backward_relation_name, m2m_relation)
                # Only once every relation of the model resolved - a model whose initialisation
                # raised part-way must not look finished to a later _init_relations() pass.
                model._meta._inited = True

    @staticmethod
    def get_relation_targets(model: type[Model]) -> set[type[Model]]:
        """Every model ``model``'s own forward FK/O2O/M2M fields point at (itself included for a
        self-referential relation) - the models it adds backward relations to."""
        targets: set[type[Model]] = set()
        meta = model._meta
        for field_name in meta.fk_fields | meta.o2o_fields | meta.m2m_fields:
            field = meta.fields_map[field_name]
            if isinstance(field, ManyToManyFieldInstance) and field._generated:
                continue
            related_model = getattr(field, "related_model", None)
            if isinstance(related_model, type):
                targets.add(related_model)
        return targets
