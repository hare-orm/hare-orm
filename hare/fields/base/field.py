from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable, Iterable
from copy import deepcopy
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast, overload

from hare.core.cache import Cache
from hare.core.registries import Registries
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from hare.fields.constants import DB_DEFAULT_NOT_SET, SENSITIVE_VALUE_PLACEHOLDER, DbDefaultNotSet
from hare.fields.enums import NativeWriteCheck, RelationType
from hare.fields.registered_lookup import RegisteredLookup
from hare.fields.registered_transform import RegisteredTransform
from hare.fields.swappable import SwappableModelReference
from hare.fields.validators.validator import Validator
from hare.query.enums import LookupValueShape
from hare.sql.terms.base.term import Term
from hare.utils.class_path import ClassPath
from hare.warnings import RedundantDbDefaultWarning

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model
    from hare.query.filters.field_lookup import FieldLookup

    LookupFunc = Callable[["Field[Any] | None"], FieldLookup]
from hare.fields.base.database_default import DatabaseDefault
from hare.fields.base.field_meta import FieldMeta

TValue = TypeVar("TValue")


class Field(Generic[TValue], metaclass=FieldMeta):
    """
    Base Field type.

    Args:
        source_field: Provide a source_field name if the DB column name needs to be
            something specific instead of generated off the field name.
        generated: Is this field DB-generated?
        primary_key: Is this field a Primary Key? Can only have a single such field on the
            Model, and if none is specified it will autogenerate a default primary key
            called ``id``.
        null: Is this field nullable?
        default: A default value for the field if not specified on Model creation.
            This can also be a callable for dynamic defaults in which case we will call it.
            The default value will not be part of the schema.
        db_default: A database-level default value. This can be a static value or an
            instance of ``SqlDefault``.
        unique: Is this field unique?
        db_index: Should this field be indexed by itself?
        description: Field description - the column's DB comment in the generated DDL.
        validators: Validators for this field.
        sensitive: Does this field hold secret data (credentials, tokens, personal data)? Listed
            in ``Model._meta.sensitive_fields``, and dropped by
            ``pydantic_model_creator(..., exclude_sensitive=True)``. Doesn't affect the DB schema.

    **Class Attributes:**
    These attributes needs to be defined when defining an actual field type.

    .. attribute:: field_type
        :annotation: type[Any]

        The Python type the field is.
        If adding a type as a mixin, FieldMeta will automatically set this to that.

    .. attribute:: indexable
        :annotation: bool = True

        Is the field indexable? Set to False if this field can't be indexed reliably.

    .. attribute:: has_db_field
        :annotation: bool = True

        Does this field have a direct corresponding DB column? Or is the field virtualized?

    .. attribute:: keeps_native_db_values
        :annotation: bool = True

        Whether a value the driver already returns as ``field_type`` is read as it is, without
        ``from_db_value()``. A class defining ``to_python()`` or ``from_db_value()`` reads every
        value through them unless it declares this itself.

    .. attribute:: native_write_check
        :annotation: NativeWriteCheck | None = None

        Declared by a field class whose own ``to_db_value`` adds checks to
        ``Field.to_db_value``: what those checks do for a value that is already exactly
        ``field_type``. The Rust write accelerator runs that check itself and keeps the field on
        its fast path; any other value goes through the class's real ``to_db_value``. Read only
        from the class that defines the ``to_db_value`` in use - a subclass overriding
        ``to_db_value`` again leaves the fast path until it declares its own.

    .. attribute:: allows_generated
        :annotation: bool = False

        Is this field able to be DB-generated?

    .. attribute:: generated_requires_primary_key
        :annotation: bool = False

        Whether hare can only create this field's column as a DB-generated primary key - its
        ``GENERATED_SQL`` is an auto-increment primary key definition. A generated non-primary-key
        field of such a class still maps an existing column (e.g. a Postgres IDENTITY column).

    .. attribute:: requires_extension
        :annotation: Optional[str] = None

        Name of a database extension this field's SQL type needs (e.g. ``"citext"``). The
        migration autodetector adds a ``CreateExtension`` for it automatically wherever the
        field is used, without requiring a matching ``Meta.extensions`` declaration.

    .. attribute:: supported_lookups
        :annotation: Optional[frozenset[str]] = None

        Lookup suffixes (``"isnull"``, ``"has_key"``, ...) still usable on this field - a
        value-comparing lookup can't work on e.g. a non-deterministically encrypted column.
        ``None`` means every lookup is usable.

    .. attribute:: relation_type
        :annotation: Optional[RelationType] = None

        Which relation the field is (``RelationType.FOREIGN_KEY``, ``ONE_TO_ONE``,
        ``MANY_TO_MANY`` and the two backward sides); ``None`` for a field holding a plain value.

    .. attribute:: encrypted
        :annotation: bool = False

        Whether the field stores its value encrypted (``EncryptedTextField``,
        ``EncryptedJSONField``) - the database sees only ciphertext, so the field can't be
        compared by value, ordered, grouped or aggregated.

    .. attribute:: enum_type
        :annotation: Optional[type[Enum]] = None

        The enum class whose members the field holds (``IntEnumField``, ``CharEnumField``);
        ``None`` for any other field.

    .. attribute:: SQL_TYPE
        :annotation: str

        The SQL type as a string that the DB will use.

    .. attribute:: GENERATED_SQL
        :annotation: str

        The SQL that instructs the DB to auto-generate this field.
        Required if ``allows_generated`` is ``True``.

    .. attribute:: SUPPORTED_DIALECTS
        :annotation: Optional[frozenset[str]] = None

        The dialects the field has a column type on; ``None`` means every dialect.

    **Per-dialect storage:**

    A dialect stores a field class its own way through its type registry - a column type, the
    DDL of a generated primary key and a cast wrapped around the column wherever it is compared
    (``hare.dialects.base.types.TypeMapping``). A field class uses the mapping of its nearest
    registered base class:

    .. code-block:: py3

        DialectRegistry.get_dialect("sqlite").types.register(MoneyField, TypeMapping(column_type="TEXT"))
    """

    # Field_type is a readonly property for the instance, it is set by FieldMeta
    field_type: type[Any] = None  # type: ignore[assignment]
    indexable: bool = True
    has_db_field: bool = True
    keeps_native_db_values: ClassVar[bool] = True
    native_write_check: ClassVar[NativeWriteCheck | None] = None
    allows_generated: bool = False
    generated_requires_primary_key: bool = False
    requires_extension: str | None = None
    supported_lookups: ClassVar[frozenset[str] | None] = None
    relation_type: ClassVar[RelationType | None] = None
    encrypted: ClassVar[bool] = False
    enum_type: type[Enum] | None = None
    SQL_TYPE: str = None  # type: ignore[assignment]
    GENERATED_SQL: str = None  # type: ignore[assignment]
    SUPPORTED_DIALECTS: ClassVar[frozenset[str] | None] = None
    # A custom Field subclass can set a stable "dotted path" here (e.g. "myapp.fields.MyField") -
    # generated migrations then always import from this path instead of `__module__`, which
    # changes whenever the class is moved between modules.
    migration_import_path: str | None = None

    #: Whether the field's value is an array, a range or a JSON value - an annotation of such a
    #: field takes the value's own lookups (``__contains``, ``__overlap``, ``__len``, ...).
    holds_container_value: ClassVar[bool] = False

    #: Registry of custom `__suffix` lookups, populated via register_lookup().
    registered_lookups: ClassVar[dict[str, RegisteredLookup]] = {}
    #: Path segments added to this class with register_transform(), by segment.
    registered_transforms: ClassVar[dict[str, RegisteredTransform]] = {}
    #: (field class,) -> its constructor's parameters other than ``self``, with their defaults
    #: (``_get_constructor_parameters()``).
    constructor_parameters: ClassVar[Cache[tuple[tuple[str, Any], ...]]] = Cache(Cache.max_size_from_env())
    #: The field's lookups by suffix, with the generation of ``FieldLookups.built_lookups`` they
    #: were built in - never copied with the field.
    built_lookups: tuple[int, dict[str, FieldLookup]] | None = None

    @classmethod
    def register_lookup(
        cls,
        lookup_name: str,
        lookup: LookupFunc | None = None,
        *,
        value_shape: LookupValueShape = LookupValueShape.VALUE,
        value_type: Any = None,
        dialects: Iterable[str] | None = None,
        required_extension: str | None = None,
    ) -> Callable[[LookupFunc], LookupFunc] | None:
        """Registers a ``__{lookup_name}`` lookup for this field class and its subclasses. Registered
        on ``Field`` itself, it is a lookup of every value, a value with no field included
        (``lookup`` then gets None). Can be called any time - later, the registries' caches are
        dropped and the bound models' filters rebuilt. The filter value's shape is part of the
        lookup: ``get_lookup_info()`` reports it, and a query on a dialect outside ``dialects``
        raises ``UnSupportedError`` before any SQL is built.

        Args:
            lookup_name: The suffix without the double underscore (``"within_km"``).
            lookup: ``(field) -> FieldLookup`` - omitted to use this as a decorator.
            value_shape: Whether the value is one value, a list or a two-item range.
            value_type: The type of the value (of each item); None means the field's own type.
            dialects: The dialects the lookup runs on; None means every dialect.
            required_extension: The database extension the lookup needs.

        Returns:
            None when called directly; a decorator when ``lookup`` is omitted.
        """
        if lookup is None:

            def decorator(fn: LookupFunc) -> LookupFunc:
                cls.register_lookup(
                    lookup_name,
                    fn,
                    value_shape=value_shape,
                    value_type=value_type,
                    dialects=dialects,
                    required_extension=required_extension,
                )
                return fn

            return decorator
        if "registered_lookups" not in cls.__dict__:
            cls.registered_lookups = {}
        cls.registered_lookups[lookup_name] = RegisteredLookup(
            builder=lookup,
            value_shape=value_shape,
            value_type=value_type,
            dialects=None if dialects is None else frozenset(dialects),
            required_extension=required_extension,
        )
        Registries.changed()
        return None

    # These methods are just to make IDE/Linters happy:
    if TYPE_CHECKING:

        def __new__(cls, *args: Any, **kwargs: Any) -> Self:
            return super().__new__(cls)

        @overload
        def __get__(self, instance: None, owner: type[Model]) -> Field[TValue]: ...

        @overload
        def __get__(self, instance: Model, owner: type[Model]) -> TValue: ...

        def __get__(self, instance: Model | None, owner: type[Model]) -> Field[TValue] | TValue: ...

        def __set__(self, instance: Model, value: TValue) -> None: ...

    def __init__(
        self,
        source_field: str | None = None,
        generated: bool = False,
        primary_key: bool | None = None,
        null: bool = False,
        default: Any = None,
        db_default: Any = DB_DEFAULT_NOT_SET,
        unique: bool = False,
        db_index: bool | None = None,
        description: str | None = None,
        model: Model | None = None,
        validators: list[Validator | Callable[[Any], None]] | None = None,
        sensitive: bool = False,
        **kwargs: Any,
    ) -> None:
        if not isinstance(sensitive, bool):
            raise ConfigurationError(f"{self.__class__.__name__}: sensitive must be a bool, got {sensitive!r}")
        if kwargs:
            raise TypeError(
                f"{self.__class__.__name__}() got unexpected keyword arguments: {', '.join(sorted(kwargs))}"
            )
        if null and primary_key:
            raise ConfigurationError(f"{self.__class__.__name__} can't be both null=True and primary_key=True")
        if primary_key and self.indexable:
            # Not for a type that can't be indexed - the PRIMARY KEY constraint already covers it.
            db_index = True
            unique = True
        if not self.indexable and (unique or db_index):
            raise ConfigurationError(f"{self.__class__.__name__} can't be indexed")
        self.source_field = source_field
        self.generated = generated
        self.pk = bool(primary_key)
        self.default = default
        # Cached: the check is slow, and Model.__init__ would run it per instance.
        self._default_is_coroutine = self._is_async_default(default)
        #: Whether the static ``default`` already has the form ``to_python`` gives
        #: it - None until the first instance construction checks it.
        self.static_default_is_normalized: bool | None = None
        #: ``get_assign_normalized_types()``, cached by the first callable default normalized.
        self.assign_normalized_types: frozenset[type] | None = None
        #: ``(validators, their count, the native checks of them)`` - the checks ``validate()`` makes
        #: of a value of exactly ``field_type``, worked out by its first such value.
        self.value_checks: tuple[list[Any], int, Any] | None = None
        self.db_default = db_default
        if self.has_db_default() and callable(self.db_default):
            raise ConfigurationError(
                f"{self.__class__.__name__}: db_default must be a static value or SqlDefault(...), not a callable"
            )
        if default is not None and self.has_db_default() and self.is_db_default_redundant_with_default():
            warnings.warn(
                f"{self.__class__.__name__}: both `default` and `db_default` are set - `db_default` will never"
                " be applied since `default` always provides a value before an INSERT is sent to the database.",
                RedundantDbDefaultWarning,
                stacklevel=2,
            )
        if default is not None and self.generated:
            # A generated column is left out of every INSERT - its default is never used.
            warnings.warn(
                f"{self.__class__.__name__}: `default` is set on a generated field - it will never be applied "
                "since a generated column's value always comes from the database, never from an INSERT.",
                RedundantDbDefaultWarning,
                stacklevel=2,
            )
        self.null = null
        self.unique = unique
        self.index = bool(db_index)
        self.model_field_name = ""
        self.description = description
        self.docstring: str | None = None
        self.validators: list[Validator | Callable[[Any], None]] = validators or []
        #: The validators the column type itself enforces where numeric columns reject out-of-range
        #: values - an integer's bounds, a decimal's digit count.
        self.validators_enforced_by_column_type: list[Validator | Callable[[Any], None]] = []
        self.sensitive = sensitive
        # TODO: consider making this not be set from constructor
        self.model: type[Model] = model  # type: ignore[assignment]
        self.reference: Field[Any] | None = None

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("built_lookups", None)
        return state

    def __copy__(self) -> Field[TValue]:
        cls = self.__class__
        result = cls.__new__(cls)
        result.__dict__.update(self.__getstate__())
        result.validators = list(self.validators)
        result.validators_enforced_by_column_type = list(self.validators_enforced_by_column_type)
        return result

    def get_validators_not_enforced_by_column(self, dialect: Dialect) -> list[Validator | Callable[[Any], None]]:
        """The validators a value computed in SQL - an ``F()`` expression in ``update()`` - still
        has to pass in Python on ``dialect``: every validator, less the ones the field's column type
        enforces where the dialect's integer and decimal columns reject out-of-range values.

        Args:
            dialect: The dialect the value is written on.

        Returns:
            The validators.
        """
        if not dialect.enforces_numeric_ranges or not self.validators_enforced_by_column_type:
            return self.validators
        return [validator for validator in self.validators if validator not in self.validators_enforced_by_column_type]

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        """
        Converts from the Python type to the DB type.

        Args:
            value: Current python value in model.
            instance: Model class or Model instance provided to look up. Due to metacoding, to
                determine if this is an instance reliably, check
                ``hasattr(instance, "_saved_in_db")``.
        """
        if value is not None and not isinstance(value, self.field_type):
            value = Field.to_python(self, value)
        self.validate(value)
        return value

    def get_validation_error(self, error: BaseException, value: Any, message: str | None = None) -> ValidationError:
        """Wraps an exception raised while converting or validating ``value``. For a sensitive field
        the message hides the value and ``error`` isn't chained.

        Args:
            error: The exception raised.
            value: The value.
            message: The full message, instead of the field name followed by ``error``'s text.

        Returns:
            The error to raise.
        """
        if message is None:
            message = f"{self.model_field_name}: {self.hide_value_in_message(str(error), value)}"
        validation_error = ValidationError(message)
        if not self.sensitive:
            validation_error.__cause__ = error
        return validation_error

    def get_value_for_message(self, value: Any) -> str:
        """Returns how ``value`` is shown in an error message.

        Args:
            value: The value.

        Returns:
            ``repr(value)``, or a placeholder for a sensitive field.
        """
        return SENSITIVE_VALUE_PLACEHOLDER if self.sensitive else repr(value)

    def hide_value_in_message(self, message: str, value: Any) -> str:
        """Replaces every occurrence of ``value`` in a sensitive field's error message with a
        placeholder.

        Args:
            message: The error message.
            value: The value the message may contain.

        Returns:
            The message, unchanged for a non-sensitive field.
        """
        if not self.sensitive or value is None:
            return message
        shown_values = [repr(value), str(value)]
        if isinstance(value, Enum):
            shown_values += [repr(value.value), str(value.value)]
        for shown_value in sorted(shown_values, key=len, reverse=True):
            if shown_value:
                message = message.replace(f"'{shown_value}'", SENSITIVE_VALUE_PLACEHOLDER)
                message = message.replace(shown_value, SENSITIVE_VALUE_PLACEHOLDER)
        return message

    def to_lookup_value(self, value: Any, instance: type[Model] | Model) -> Any:
        return self.to_db_value(value, instance)

    def get_unsupported_lookup_message(self) -> str:
        """Returns the error message for a lookup outside ``supported_lookups``."""
        supported = ", ".join(f"__{lookup}" for lookup in sorted(self.supported_lookups or ()))
        return (
            f"{self.model_field_name}: this lookup isn't supported on {type(self).__name__}. "
            f"Supported lookups: {supported}."
        )

    def to_python(self, value: Any) -> Any:
        """The field's Python value of ``value`` - a value assigned to an instance, and a value
        read from the database unless ``from_db_value()`` reads it its own way.

        Args:
            value: The value.

        Returns:
            The value as ``field_type``; None stays None.

        Raises:
            ValidationError: The value doesn't convert. The exception a malformed value raises
                varies by type - ValueError for int()/float(), TypeError for bytes(str),
                decimal.InvalidOperation for Decimal() - so any is caught and raised as the
                catchable ValidationError every other data-shape failure is.
        """
        if value is None or isinstance(value, self.field_type):
            return value
        validation_error = None
        try:
            value = self.field_type(value)  # pylint: disable=E1102
        except Exception as exc:
            validation_error = self.get_validation_error(exc, value)
        if validation_error is not None:
            raise validation_error
        return value

    def get_read_codec_spec(self, types: TypeRegistry, zone_name: str | None) -> tuple[str, dict[str, Any]] | None:
        """The ``rust.native.rows`` codec reading a column of this field, for a field that reads its
        values in a way of its own the codec repeats exactly - a container of other fields' values.

        Args:
            types: The dialect's type registry.
            zone_name: The configured zone under ``use_tz``, else None.

        Returns:
            The read codec kind and its options; None leaves the column to the codec of the field's
            type, or to ``from_db_value()``.
        """
        return None

    def from_db_value(self, value: Any) -> Any:
        """The field's Python value of a value read from the database - ``to_python()``, unless
        the field stores its value differently from how it holds it (an encrypted token, JSON
        text, microseconds).

        Args:
            value: The value the driver returned.

        Returns:
            The Python value.
        """
        return self.to_python(value)

    def get_assign_normalized_types(self) -> frozenset[type]:
        """Returns the exact types whose values ``to_python`` always returns
        unchanged - a default value of one of them skips that call on instance construction.

        Returns:
            ``{field_type}``, or nothing when ``field_type`` isn't a single class.
        """
        return frozenset({self.field_type}) if isinstance(self.field_type, type) else frozenset()

    def get_default_value_on_assign(self, value: Any) -> Any:
        """Normalizes a callable default's result the way an assigned value is, so memory holds
        the same type a later read returns.

        Args:
            value: The default value.

        Returns:
            The normalized value.
        """
        if self.assign_normalized_types is None:
            self.assign_normalized_types = self.get_assign_normalized_types()
        if value is None or type(value) in self.assign_normalized_types:
            return value
        return self.to_python(value)

    def get_static_default_value(self) -> Any:
        """Returns this instance's own copy of the static (non-callable) ``default``, normalized
        the way an assigned value is.

        Returns:
            The default value.
        """
        default = self.default
        value = default if isinstance(default, (int, float, str, bool, bytes)) else deepcopy(default)
        if self.static_default_is_normalized is None:
            self.static_default_is_normalized = type(default) in self.get_assign_normalized_types()
        if self.static_default_is_normalized:
            return value
        return self.to_python(value)

    @classmethod
    def get_native_write_check(cls) -> NativeWriteCheck | None:
        """The check the ``to_db_value`` in use adds for a value that is exactly ``field_type`` -
        ``native_write_check`` of the class that defines that ``to_db_value``.

        Returns:
            The check; None when ``to_db_value`` is ``Field``'s own, or its class declares no
            ``native_write_check`` (its conversion is unknown to the write accelerator).
        """
        for klass in cls.__mro__:
            if "to_db_value" in klass.__dict__:
                if klass is Field:
                    return None
                return cast("NativeWriteCheck | None", klass.__dict__.get("native_write_check"))
        return None

    def validate(self, value: Any) -> None:
        """
        Validate whether given value is valid

        Args:
            value: Value to be validation

        Raises:
            ValidationError: If validator check is not passed
        """
        if self.null and value is None:
            return
        if type(value) is self.field_type:
            validators = self.validators
            value_checks = self.value_checks
            if value_checks is None or value_checks[0] is not validators or value_checks[1] != len(validators):
                value_checks = self.value_checks = (validators, len(validators), self._get_native_value_checks())
            if value_checks[2] is not None:
                value_checks[2].check(value, self.model_field_name)
                return
        for v in self.validators:
            validation_error = None
            try:
                if isinstance(value, Enum):
                    v(value.value)
                else:
                    v(value)
            except Exception as exc:
                # A validator may be a plain callable raising anything - wrapped into
                # ValidationError.
                validation_error = self.get_validation_error(exc, value)
            if validation_error is not None:
                raise validation_error

    def _get_native_value_checks(self) -> Any:
        """The ``rust.native.rows.ValueChecks`` making the validators' checks of a value of exactly
        ``field_type`` in one call.

        Returns:
            The checks; None when a validator checks another way, or its message hides a sensitive
            value.
        """
        if self.sensitive or not self.validators:
            return None
        # Imported here: hare.query imports the fields package.
        from hare.query.rows.enums import InlineCheckKind
        from hare.query.rows.field_codecs import FieldCodecs
        from hare.query.rows.hydrate_accelerator import HydrateAccelerator

        value_checks_class = getattr(HydrateAccelerator.module, "ValueChecks", None)
        checks = FieldCodecs.get_inline_checks(self)
        # A codec checks the digits of a value already quantized - validate() gets any value.
        if (
            value_checks_class is None
            or checks is None
            or any(kind is InlineCheckKind.MAX_DIGITS for kind, _ in checks)
        ):
            return None
        return value_checks_class(self.model_field_name, checks)

    def has_db_default(self) -> bool:
        return not isinstance(self.db_default, DbDefaultNotSet)

    def is_db_default_redundant_with_default(self) -> bool:
        """Whether a ``db_default`` set alongside ``default`` can never take effect.

        Returns:
            True for a plain column (``default`` always provides a value before an INSERT); False
            for a field whose ``db_default`` is also read by the database itself outside of an
            INSERT, e.g. a foreign key's ``ON DELETE SET DEFAULT``.
        """
        return True

    @staticmethod
    def _is_async_default(default: Any) -> bool:
        """Whether calling ``default()`` gives an awaitable - an ``async def`` function, possibly in
        ``functools.partial``, or an object whose ``__call__`` is one.
        """
        return inspect.iscoroutinefunction(default) or inspect.iscoroutinefunction(getattr(default, "__call__", None))

    @property
    def required(self) -> bool:
        """
        Returns ``True`` if the field is required to be provided.

        It needs to be non-nullable and not have a default or be DB-generated to be required.
        """
        return self.default is None and not self.null and not self.generated and not self.has_db_default()

    def get_db_default_value(self) -> DatabaseDefault | None:
        """Return a DatabaseDefault instance if this field has a db_default, else None."""
        if self.has_db_default():
            return DatabaseDefault(self)
        return None

    @property
    def constraints(self) -> dict[str, Any]:
        """
        Returns a dict with constraints defined in the Pydantic/JSONSchema format.
        """
        return {}

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups of the field's type of value, by suffix (``""`` is equality) - the generic set
        by default; a field class with a set of its own overrides it. ``register_lookup()`` lookups
        are added to it.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the fields package.
        from hare.query.filters.field_lookups import FieldLookups

        return FieldLookups.get_generic(self)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """How one path segment after the field's name reads inside its value - an array's item
        (``tags__0``) or length (``tags__len``), a range's bound (``during__startswith``) - so a
        lookup (``tags__0__startswith``), ``F()``, ``values()`` and ``order_by()`` can read it.

        Args:
            segment: The path segment.

        Returns:
            The function building the term the segment reads from the field's own term, and the
            field of the value it reads; None when the segment reads nothing inside the value.
        """
        for klass in type(self).__mro__:
            registered_transform = klass.__dict__.get("registered_transforms", {}).get(segment)
            if registered_transform is not None:
                return registered_transform.get_transform(self)
        return None

    @classmethod
    def register_transform(
        cls,
        segment: str,
        get_transform: Callable[[Field[Any]], tuple[Callable[[Term], Term], Field[Any]]],
        *,
        required_extension: str | None = None,
    ) -> None:
        """Adds a path segment reading inside the value of this field class and its subclasses -
        how a dialect gives field classes it doesn't define one (PostgreSQL's ``unaccent``).

        Args:
            segment: The path segment.
            get_transform: Builds the transform for one field: the function building the term the
                segment reads, and the field of the value it reads.
            required_extension: The database extension the transform needs.
        """
        if "registered_transforms" not in cls.__dict__:
            cls.registered_transforms = {}
        cls.registered_transforms[segment] = RegisteredTransform(get_transform, required_extension)
        Registries.changed()

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """What the filter value of a lookup on this field is, where the field's own lookups take
        something other than a value of the field's type - an array's ``__contains`` takes a list
        of elements, a range's ``__overlap`` a range. ``get_lookup_info()`` reports it.

        Args:
            lookup: The lookup's name, ``""`` for plain equality.

        Returns:
            Whether the value is one value, a list or a range, and the type of the value (of each
            item of a list or range); None where the value is described as for any field.
        """
        return None

    def get_generated_from_field_names(self) -> tuple[str, ...]:
        """The model's other fields a generated column is computed from, where the field names them (a
        ``TSVectorField``'s ``source_fields``) - a migration refuses to remove one while this field
        reads it, and a rename renames it here.

        Returns:
            The field names, none by default.
        """
        return ()

    def with_renamed_generated_from_field(self, old_name: str, new_name: str) -> Field[Any]:
        """A copy of the field computed from ``new_name`` wherever it was computed from
        ``old_name`` - see ``get_generated_from_field_names()``.

        Args:
            old_name: The field's old name.
            new_name: The field's new name.

        Returns:
            The renamed copy, or the field itself when it isn't computed from ``old_name``.
        """
        return self

    def get_like_text_function(self) -> Callable[[Term], Term] | None:
        """How the field's value is turned into text for the LIKE-family lookups (``__contains``,
        ``__istartswith``, ...) and the pattern lookups (``__posix_regex``, a dialect's own).

        Returns:
            The function building the text term from the field's term; None casts the value to
            ``VARCHAR``.
        """
        return None

    def get_python_type(self) -> Any:
        """The Python type the field's values have - a relation's related model, a container's element
        type - where ``field_type`` doesn't tell it.
        """
        return getattr(self, "related_model", self.field_type)

    def get_value_annotation(self) -> Any:
        """The Python type of one value of this field: its enum for an enum field, else
        ``get_python_type()``."""
        return self.enum_type or self.get_python_type()

    def get_annotation(self) -> Any:
        """The annotation of this field's value in a schema - a pydantic model, a framework DTO,
        a request parameter: ``get_value_annotation()``, ``| None`` when the field is nullable."""
        value_annotation = self.get_value_annotation()
        return value_annotation | None if self.null else value_annotation

    def get_db_field_types(self) -> dict[str, str] | None:
        """The column type of this field on each dialect.

        Returns:
            The field's own type under ``""``, plus each registered dialect that stores it
            differently, or None for a field without a column.
        """
        if not self.has_db_field:  # pragma: nocoverage
            return None
        default = self.SQL_TYPE
        column_types = {"": default}
        for dialect in DialectRegistry.get_dialects():
            if self.SUPPORTED_DIALECTS is not None and dialect.name not in self.SUPPORTED_DIALECTS:
                continue
            column_type = self.get_column_type(dialect)
            if column_type != default:
                column_types[dialect.name] = column_type
        return column_types

    def check_dialect_supported(self, dialect: Dialect) -> None:
        """Raises unless the field has a column type on ``dialect``.

        Args:
            dialect: The dialect.

        Raises:
            UnSupportedError: The field only exists on other dialects.
        """
        if self.SUPPORTED_DIALECTS is not None and dialect.name not in self.SUPPORTED_DIALECTS:
            supported = ", ".join(sorted(self.SUPPORTED_DIALECTS))
            raise UnSupportedError(
                f"{type(self).__name__} field '{self.model_field_name}' only exists on {supported} and "
                f"can't generate DDL for the {dialect} dialect."
            )

    def get_column_type(self, dialect: Dialect) -> str:
        """The column type of this field on ``dialect``.

        Args:
            dialect: The dialect.

        Returns:
            The type.

        Raises:
            UnSupportedError: The field only exists on other dialects.
        """
        self.check_dialect_supported(dialect)
        column_type = dialect.types.get_column_type(self)
        return column_type if column_type is not None else self.SQL_TYPE

    def get_generated_sql(self, dialect: Dialect) -> str | None:
        """The DDL that makes the database generate this field's column on ``dialect``.

        Args:
            dialect: The dialect.

        Returns:
            The DDL, or None when the field has none.
        """
        generated_sql = dialect.types.get_generated_sql(self)
        return generated_sql if generated_sql is not None else self.GENERATED_SQL

    def get_generated_column_sql(self, dialect: Dialect) -> str | None:
        """The generation clause for this field's non-primary-key generated column.

        Args:
            dialect: The dialect.

        Returns:
            The clause, or None when the field has none.

        Raises:
            ConfigurationError: If the class's generated SQL only defines a primary key.
        """
        if self.generated_requires_primary_key:
            raise ConfigurationError(
                f"Field '{self.model_field_name}' ({self.__class__.__name__}) is DB-generated outside the "
                "primary key - hare can only create such a column as an auto-increment primary key. Map an "
                "existing generated column (e.g. a Postgres IDENTITY column) on a Meta.managed = False model"
            )
        return self.get_generated_sql(dialect)

    def get_function_cast(self, dialect: Dialect) -> Callable[[Field[Any], Term], Term] | None:
        """The cast ``dialect`` wraps this field's column in wherever it is compared or ordered.

        Args:
            dialect: The dialect.

        Returns:
            A ``(field, term) -> term`` cast, or None.
        """
        return dialect.types.get_function_cast(self)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        """What the stored values must fit when an ``AlterField`` turns ``old_field`` into this field -
        a value beyond it would be truncated or rounded. Overridden by field types with a
        size-bounded column.

        Args:
            old_field: The previous definition.

        Returns:
            The limit, None if the change never narrows.
        """
        return None

    @classmethod
    def _get_constructor_parameters(cls) -> tuple[tuple[str, Any], ...]:
        """The parameters of the class's constructor and their defaults - read off its signature
        once per class.

        Returns:
            Each parameter's name and default, ``inspect.Parameter.empty`` for none.
        """
        key = (cls,)
        parameters = Field.constructor_parameters.get(key)
        if parameters is None:
            parameters = Field.constructor_parameters[key] = tuple(
                (name, parameter.default) for name, parameter in inspect.signature(cls.__init__).parameters.items()
            )
        return parameters

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        # migration_import_path lets a custom Field class pin a "stable" import path - without
        # this, moving the class to another module silently breaks ALREADY-generated migrations
        # (which otherwise embed `__module__`, accurate only at generation time).
        path = getattr(self.__class__, "migration_import_path", None) or (ClassPath.get(self.__class__))
        kwargs: dict[str, Any] = {}
        if self.source_field:
            kwargs["source_field"] = self.source_field
        if self.generated:
            kwargs["generated"] = self.generated
        if self.pk:
            kwargs["primary_key"] = self.pk
        if self.null:
            kwargs["null"] = self.null
        if self.default is not None:
            kwargs["default"] = self.default
        # A primary key is unique and indexed by itself.
        implied_by_primary_key = self.pk and self.indexable
        if self.unique and not implied_by_primary_key:
            kwargs["unique"] = self.unique
        if self.index and not implied_by_primary_key:
            kwargs["db_index"] = self.index
        if self.description is not None:
            kwargs["description"] = self.description
        if getattr(self, "db_constraint", True) is False:
            kwargs["db_constraint"] = False
        if hasattr(self, "to_field") and getattr(self, "to_field") is not None:
            kwargs["to_field"] = getattr(self, "to_field")
        if self.has_db_default():
            kwargs["db_default"] = self.db_default

        # The class's own arguments (max_length, the related model) come first, like the
        # constructor call they describe.
        own_kwargs: dict[str, Any] = {}
        for name, default in self._get_constructor_parameters():
            # sensitive never affects the DB schema - kept out of migrations entirely.
            if name in ("self", "args", "kwargs", "model", "validators", "db_default", "sensitive"):
                continue
            if name == "field_type" and self.__class__.__name__ == "ManyToManyFieldInstance":
                continue
            if name in kwargs:
                continue
            if not hasattr(self, name):
                continue
            value = getattr(self, name)
            if name == "model_name" and value is not None:
                if not isinstance(value, str) and hasattr(value, "_meta"):
                    value = f"{value._meta.app}.{value.__name__}"
            if name == "through" and getattr(self, "through_model", None) is not None:
                # The declared through model, not the resolved table name, is written - a replay
                # must see a through model again.
                through_model = self.through_model  # type: ignore[attr-defined]
                value = (
                    through_model
                    if isinstance(through_model, (str, SwappableModelReference))
                    else f"{through_model._meta.app}.{through_model.__name__}"
                )
            # A value the constructor takes by default needn't be written.
            if default is not inspect.Parameter.empty and value == default:
                continue
            own_kwargs[name] = value
        return path, [], {**own_kwargs, **kwargs}
