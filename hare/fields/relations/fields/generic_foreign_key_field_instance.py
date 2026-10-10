from __future__ import annotations

import keyword
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar, overload

from hare.classes.class_path import ClassPath
from hare.exceptions import ConfigurationError, QueryError, ValidationError
from hare.fields.constants import CASCADE, GENERIC_FOREIGN_KEY_TYPE_FIELD
from hare.fields.enums import OnDelete, RelationLoadStrategy
from hare.fields.field import Field
from hare.fields.relations.constants import GENERIC_FOREIGN_KEY_CHECK_SUFFIX
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.constraints.check_constraint import CheckConstraint
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")

#: A model a branch points at - a class, ``"app.Model"`` or a swappable setting.
type BranchTarget = type[Model] | str | SwappableModelReference


class GenericForeignKeyFieldInstance(Generic[TModel]):
    """A relation to a row of one of several models - an exclusive arc of real foreign keys. Each
    target model gets a branch: a nullable ``ForeignKeyField`` named by its key in ``to``, with a
    column, a database foreign key, an index and a backward relation of its own, used as any other
    relation. A ``CHECK`` keeps exactly one branch set - at most one with ``null=True``.

    The field itself has no column: reading it gives the related object of the branch set,
    assigning it sets that branch and clears the others, and a query names it to mean its branches
    (``target=obj``, ``target__in=[...]``, ``target__type="post"``, ``target__isnull``,
    ``select_related("target")``, ``prefetch_related("target")``, ``values("target__type")``,
    ``order_by("target__type")``).

    Args:
        to: The targets - a dict of branch name to model (a class, ``"app.Model"`` or
            ``swappable("SETTING")``), or ``swappable("SETTING")`` of a setting holding such a dict.
            A branch name is the public name of its target: its column (``post_id``), attribute,
            filter path and the ``type`` the field reports.
        related_name: The backward relation on every target - ``post.comments``.
        on_delete: What happens to a row when the row of its branch is deleted. With ``SET_NULL``
            the field must be ``null=True``. With ``SET_DEFAULT`` hare sets the branch of
            ``default`` and clears the others in one ``UPDATE``; the branches then have no
            database foreign key, as any relation hare runs ``on_delete`` for itself.
        db_constraint: Whether the branches get database foreign keys.
        null: Whether a row may have no branch set.
        default: A saved instance of one of the targets - or a callable returning one - a new
            instance is given when no branch is.
        lazy: The loading strategy of every branch, as ``ForeignKeyField(lazy=)``.
        kwargs: Passed to every branch's ``ForeignKeyField`` - ``db_index``, ``sensitive``,
            ``description``.

    Raises:
        ConfigurationError: ``to`` isn't a non-empty dict of identifiers to models, two branches
            name the same model, ``to_field`` is given, or ``on_delete`` doesn't fit ``null`` or
            ``default``.
    """

    #: The names of the generic foreign keys of every concrete model - a filter key without one of
    #: them is no business of theirs.
    declared_names: ClassVar[set[str]] = set()

    def __init__(
        self,
        to: dict[str, BranchTarget] | SwappableModelReference,
        related_name: str | None = None,
        on_delete: OnDelete = CASCADE,
        db_constraint: bool = True,
        *,
        null: bool = False,
        default: Model | Callable[[], Model] | None = None,
        lazy: RelationLoadStrategy | None = None,
        **kwargs: Any,
    ) -> None:
        if "to_field" in kwargs:
            raise ConfigurationError(
                "GenericForeignKeyField takes no to_field - every branch references its target's primary key"
            )
        if not isinstance(to, (dict, SwappableModelReference)):
            raise ConfigurationError(
                f"GenericForeignKeyField takes a dict of branch name to model, or swappable(...), got {to!r}"
            )
        if isinstance(to, dict):
            self.check_targets(to)
        ForeignKeyFieldInstance.validate_on_delete(on_delete)
        if on_delete == OnDelete.SET_NULL and not null:
            raise ConfigurationError("GenericForeignKeyField with on_delete=SET_NULL must be null=True")
        if on_delete == OnDelete.SET_DEFAULT and default is None:
            raise ConfigurationError("GenericForeignKeyField with on_delete=SET_DEFAULT needs a default=")
        if not isinstance(null, bool) or not isinstance(db_constraint, bool):
            raise ConfigurationError("GenericForeignKeyField: null and db_constraint must be bools")
        if lazy is not None and lazy not in set(RelationLoadStrategy):
            raise ConfigurationError("lazy can only be 'joined', 'select', or None")
        self.to = to
        self.related_name = related_name
        self.on_delete = on_delete
        self.db_constraint = db_constraint
        self.null = null
        self.default = default
        self.lazy = lazy
        self.branch_kwargs = kwargs
        #: The name of the field on its model - set when the model class is built.
        self.model_field_name = ""
        #: The model the field belongs to.
        self.model: type[Model] = None  # type: ignore[assignment]
        #: The branch names, in declaration order - known once the targets are.
        self.branch_names: tuple[str, ...] = tuple(to) if isinstance(to, dict) else ()
        #: The branch of each target model - set once the relations are initialized.
        self.branch_by_model: dict[type[Model], str] = {}

    @staticmethod
    def check_targets(targets: dict[Any, Any]) -> None:
        """Checks the branches of a ``to`` dict.

        Args:
            targets: Branch name to model.

        Raises:
            ConfigurationError: The dict is empty, a name isn't a Python identifier, a model is
                neither a class nor ``"app.Model"`` nor ``swappable(...)``, or two names give the
                same model.
        """
        if not targets:
            raise ConfigurationError("GenericForeignKeyField needs at least one target in to=")
        seen_targets: dict[Any, str] = {}
        for branch_name, target in targets.items():
            if not isinstance(branch_name, str) or not branch_name.isidentifier() or keyword.iskeyword(branch_name):
                raise ConfigurationError(
                    f"GenericForeignKeyField: the branch name {branch_name!r} isn't a Python identifier - it names "
                    "the branch's column, attribute and filter path"
                )
            ForeignKeyFieldInstance.validate_model_name(target)
            target_key = target if isinstance(target, (str, SwappableModelReference)) else id(target)
            if target_key in seen_targets:
                raise ConfigurationError(
                    f"GenericForeignKeyField: {seen_targets[target_key]!r} and {branch_name!r} name the same model - "
                    "an object of it couldn't tell which branch it is"
                )
            seen_targets[target_key] = branch_name

    def get_branch_fields(self, targets: dict[str, BranchTarget]) -> dict[str, ForeignKeyFieldInstance[Any]]:
        """Builds a branch for each target.

        Args:
            targets: Branch name to model.

        Returns:
            The branches, by name.
        """
        branches: dict[str, ForeignKeyFieldInstance[Any]] = {}
        branch: ForeignKeyFieldInstance[Any]
        for branch_name, target in targets.items():
            if self.on_delete == OnDelete.SET_DEFAULT:
                # hare sets the default branch itself - one branch alone has no default, so the
                # construction rule of a lone SET_DEFAULT relation doesn't apply.
                with Field.replaying_migration_scope():
                    branch = ForeignKeyFieldInstance(
                        target,
                        self.related_name,
                        OnDelete.SET_DEFAULT,
                        db_constraint=False,
                        null=True,
                        lazy=self.lazy,
                        db_index=self.branch_kwargs.get("db_index", True),
                        **{key: value for key, value in self.branch_kwargs.items() if key != "db_index"},
                    )
            else:
                branch = ForeignKeyFieldInstance(
                    target,
                    self.related_name,
                    self.on_delete,
                    db_constraint=self.db_constraint,
                    null=True,
                    lazy=self.lazy,
                    **self.branch_kwargs,
                )
            #: The generic relation the branch belongs to.
            branch.generic_relation = self
            branches[branch_name] = branch
        return branches

    def get_check_constraint(self) -> CheckConstraint:
        """The ``CHECK`` keeping exactly one branch set - at most one with ``null=True``."""
        # Local imports: the ddl package imports the fields package.
        from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
        from hare.ddl.constraints.check_constraint import CheckConstraint

        return CheckConstraint(
            check=ExclusiveArcCondition(self.branch_names, allow_none=self.null),
            name=self.get_check_constraint_name(),
        )

    def get_check_constraint_name(self) -> str:
        """The name of the field's ``CHECK`` - unique within its table."""
        return f"{self.model_field_name}{GENERIC_FOREIGN_KEY_CHECK_SUFFIX}"

    def bind_branches(self) -> None:
        """Learns the model of each branch once the relations are initialized, and checks them.

        Raises:
            ConfigurationError: Two branches point at the same model, a target was registered by
                ``register_live_models()``, or ``default`` isn't an instance of a target.
        """
        fields_map = self.model._meta.fields_map
        self.branch_by_model = {}
        for branch_name in self.branch_names:
            related_model = fields_map[branch_name].related_model  # type: ignore[attr-defined]
            if related_model in self.branch_by_model:
                raise ConfigurationError(
                    f"{self.get_label()}: {self.branch_by_model[related_model]!r} and {branch_name!r} name the same "
                    f"model {related_model.__name__} - an object of it couldn't tell which branch it is"
                )
            if related_model._meta.registered_live:
                raise ConfigurationError(
                    f"{self.get_label()}: the target {related_model.__name__} was registered by "
                    "register_live_models() - a generic relation targets configured models only"
                )
            self.branch_by_model[related_model] = branch_name
        if self.default is not None and not callable(self.default):
            self.get_branch_of(self.default)

    def get_label(self) -> str:
        """``Model.field``, as an error names the field."""
        return f"{self.model.__name__}.{self.model_field_name}" if self.model is not None else "GenericForeignKeyField"

    def get_branch_of(self, value: Model) -> str:
        """The branch an object of a target belongs to.

        Args:
            value: The object.

        Returns:
            The branch name.

        Raises:
            ValidationError: The object isn't of a target model - the error of a foreign key given
                an object of another model.
        """
        branch_name = self.branch_by_model.get(type(value))
        if branch_name is None:
            expected = ", ".join(f"'{model.__name__}'" for model in self.branch_by_model)
            raise ValidationError(
                f"Invalid type for relationship field '{self.model_field_name}'. "
                f"Expected one of the model types {expected}, but got '{type(value).__name__}'. "
                "Make sure you're using the correct model class for this relationship."
            )
        return branch_name

    def get_branch_values(self, value: Model | dict[str, Any] | None) -> dict[str, Any]:
        """The value of every branch for a value of the field.

        Args:
            value: An object of a target, or None.

        Returns:
            Branch name to value - the object on its branch, None on the others.

        Raises:
            QueryError: None for a field that isn't ``null=True``.
            ValidationError: The object isn't of a target model.
        """
        if value is None:
            if not self.null:
                raise QueryError(f"{self.model_field_name} is non nullable field, but null was passed")
            return dict.fromkeys(self.branch_names)
        if isinstance(value, dict):
            return self.get_column_values(value)
        branch_name = self.get_branch_of(value)
        return {name: value if name == branch_name else None for name in self.branch_names}

    def get_key_values(self, value: Model) -> dict[str, Any]:
        """An object of a target as ``{"type": "<branch>", <the target's key fields>}``.

        Args:
            value: The object.

        Returns:
            The branch and the key values.
        """
        branch_name = self.get_branch_of(value)
        key_fields = self.model._meta.fields_map[branch_name].to_field_instances  # type: ignore[attr-defined]
        return {
            GENERIC_FOREIGN_KEY_TYPE_FIELD: branch_name,
            **{key_field.model_field_name: getattr(value, key_field.model_field_name) for key_field in key_fields},
        }

    def get_column_values(self, value: dict[str, Any]) -> dict[str, Any]:
        """The key columns ``{"type": "<branch>", <the target's key fields>}`` stands for: those of
        its branch set to the key, every other branch's cleared.

        Args:
            value: The branch and the key values.

        Returns:
            Key column to value.

        Raises:
            ValidationError: The type names no branch, or a key field is missing.
        """
        branch_name = value.get(GENERIC_FOREIGN_KEY_TYPE_FIELD)
        if branch_name not in self.branch_names:
            raise ValidationError(
                f"{self.model_field_name}: the type {branch_name!r} names no branch - one of "
                f"{', '.join(repr(name) for name in self.branch_names)}"
            )
        fields_map = self.model._meta.fields_map
        column_values: dict[str, Any] = {}
        for name in self.branch_names:
            branch = fields_map[name]
            source_fields = branch.source_fields  # type: ignore[attr-defined]
            if name != branch_name:
                column_values.update(dict.fromkeys(source_fields))
                continue
            key_fields = branch.to_field_instances  # type: ignore[attr-defined]
            for source_field, key_field in zip(source_fields, key_fields, strict=True):
                if key_field.model_field_name not in value:
                    raise ValidationError(
                        f"{self.model_field_name}: the key field {key_field.model_field_name!r} of {branch_name!r} "
                        "is missing"
                    )
                column_values[source_field] = value[key_field.model_field_name]
        return column_values

    def get_default_branch_values(self) -> dict[str, Model | None] | None:
        """The branch values of ``default``; None without one."""
        if self.default is None:
            return None
        default = self.default() if callable(self.default) else self.default
        return self.get_branch_values(default)

    def get_set_default_values(self) -> dict[str, Any]:
        """The key columns ``on_delete=SET_DEFAULT`` writes: those of the default's branch get its
        key, every other branch's are cleared.

        Returns:
            Key column to value.

        Raises:
            ConfigurationError: The default isn't saved.
        """
        default = self.default() if callable(self.default) else self.default
        branch_name = self.get_branch_of(default)  # type: ignore[arg-type]
        fields_map = self.model._meta.fields_map
        values: dict[str, Any] = {}
        for name in self.branch_names:
            branch = fields_map[name]
            source_fields = branch.source_fields  # type: ignore[attr-defined]
            if name == branch_name:
                key_names = tuple(field.model_field_name for field in branch.to_field_instances)  # type: ignore[attr-defined]
                key_values = [getattr(default, key_name) for key_name in key_names]
                if any(key_value is None for key_value in key_values):
                    raise ConfigurationError(f"{self.get_label()}: the default {default!r} isn't saved")
                values.update(zip(source_fields, key_values, strict=True))
            else:
                values.update(dict.fromkeys(source_fields))
        return values

    def get_branch_column(self, branch_name: str) -> str:
        """The key column field a branch is read as set by - its first: the ``CHECK`` keeps a
        composite key's columns set or unset together. A target hidden by its default scope (soft
        deleted, another tenant's) still counts as set.

        Args:
            branch_name: The branch.

        Returns:
            The field name - ``post_id``.
        """
        return str(self.model._meta.fields_map[branch_name].source_fields[0])  # type: ignore[attr-defined]

    def get_filled_branch(self, obj: Model) -> str | None:
        """The branch an obj has set.

        Args:
            obj: An obj of the field's model.

        Returns:
            The branch name; None when none is set.
        """
        fields_map = self.model._meta.fields_map
        obj_values = obj.__dict__
        for name in self.branch_names:
            related = obj_values.get(f"_{name}")
            if related is not None:
                return name
            source_fields = fields_map[name].source_fields  # type: ignore[attr-defined]
            if any(obj_values.get(source_field) is not None for source_field in source_fields):
                return name
        return None

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The call building the field - its path, arguments and keyword arguments."""
        kwargs: dict[str, Any] = {}
        if self.related_name is not None:
            kwargs["related_name"] = self.related_name
        if self.on_delete != CASCADE:
            kwargs["on_delete"] = self.on_delete
        if not self.db_constraint:
            kwargs["db_constraint"] = False
        if self.null:
            kwargs["null"] = True
        if self.default is not None:
            kwargs["default"] = self.default
        if self.lazy is not None:
            kwargs["lazy"] = self.lazy
        kwargs.update(self.branch_kwargs)
        return ClassPath.get(self.__class__), [self.to], kwargs

    @overload
    def __get__(self, obj: None, owner: Any) -> Self: ...

    @overload
    def __get__(self, obj: object, owner: Any) -> Any: ...

    def __get__(self, obj: Any, owner: Any) -> Any:
        """The field on the class; on an obj, the related object of the branch it has set -
        awaited as a foreign key's is (``await comment.target``). With none set: None once the
        branches are loaded or assigned, else an awaitable giving None - as a foreign key."""
        if obj is None:
            return self
        branch_name = self.get_filled_branch(obj)
        if branch_name is None:
            obj_values = obj.__dict__
            if all(f"_{name}" in obj_values for name in self.branch_names):
                # Every branch loaded or assigned: None, as a foreign key read from its cache.
                return None
            # Local import: the query package imports the fields package.
            from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitable

            return NoneAwaitable
        return getattr(obj, branch_name)

    def __set__(self, obj: Any, value: Model | dict[str, Any] | None) -> None:
        for branch_name, branch_value in self.get_branch_values(value).items():
            setattr(obj, branch_name, branch_value)

    def __repr__(self) -> str:
        return f"<GenericForeignKeyField {self.get_label()}>"
