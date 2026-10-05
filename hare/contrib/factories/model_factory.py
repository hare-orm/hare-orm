from __future__ import annotations

import typing
from types import FunctionType
from typing import Any, ClassVar, Generic, TypeVar

from hare.contrib.factories.constants import FACTORY_OWN_ATTRIBUTES, PARAMETERS_CLASS_NAME
from hare.contrib.factories.field_declarations.declaration import Declaration
from hare.contrib.factories.field_declarations.lazy_attribute import LazyAttribute
from hare.contrib.factories.field_declarations.post_declaration import PostDeclaration
from hare.contrib.factories.field_declarations.sub_factory import SubFactory
from hare.contrib.factories.trait import Trait
from hare.exceptions import ConfigurationError
from hare.models.tenancy.tenancy import Tenancy

if typing.TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class ModelFactory(Generic[TModel]):
    """Makes objects of a model for tests - each field from a declaration, a value given to the call
    winning over it::

        class UserFactory(ModelFactory[User]):
            name = Sequence(lambda number: f"user{number}")
            email = LazyAttribute(lambda user: f"{user.name}@example.com")
            team = SubFactory(TeamFactory)

            class Params:
                admin = Trait(is_staff=True)


        user = await UserFactory.create(name="ann")
        admin = await UserFactory.create(admin=True)
        draft = UserFactory.build(team=team)
        users = await UserFactory.create_batch(10)

    A plain value is used as it is; a ``Declaration`` makes one per object; a ``SubFactory`` makes the
    related object; a ``RelatedFactory`` or ``ManyToMany`` acts once the object is created. ``Params``
    holds ``Trait``s and plain parameters - a ``LazyAttribute`` reads the parameters, the model never
    gets them. A model's ``Meta.tenant_field`` comes from the active ``Tenancy.scope()`` when not
    given.
    """

    #: The model - the factory's type argument, ``ModelFactory[User]``.
    factory_model: ClassVar[type[Model]]
    #: The declarations of the fields, by name, the base factories' first.
    factory_declarations: ClassVar[dict[str, Any]] = {}
    #: The parameters and traits of ``Params``, by name.
    factory_parameters: ClassVar[dict[str, Any]] = {}
    #: The number the factory's next object gets in its sequence.
    factory_sequence_number: ClassVar[int] = 0

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.get_generic_model()
        if model is not None:
            cls.factory_model = model
        declarations: dict[str, Any] = {}
        parameters: dict[str, Any] = {}
        for factory_class in reversed(cls.__mro__):
            if not issubclass(factory_class, ModelFactory) or factory_class is ModelFactory:
                continue
            for name, value in vars(factory_class).items():
                if name == PARAMETERS_CLASS_NAME and isinstance(value, type):
                    parameters.update(
                        (parameter_name, parameter)
                        for parameter_name, parameter in vars(value).items()
                        if not parameter_name.startswith("_")
                    )
                elif ModelFactory.is_declaration(name, value):
                    declarations[name] = value
        cls.factory_declarations = declarations
        cls.factory_parameters = parameters
        cls.factory_sequence_number = 0

    @staticmethod
    def is_declaration(name: str, value: Any) -> bool:
        """Whether a factory's class attribute declares a field - not a method, a private name or
        what the factory keeps for itself."""
        return not (
            name.startswith("_")
            or name in FACTORY_OWN_ATTRIBUTES
            or isinstance(value, FunctionType | classmethod | staticmethod | property)
        )

    @classmethod
    def get_generic_model(cls) -> type[Model] | None:
        """The model a class names as its type argument - None for a subclass of a factory naming one."""
        for base in getattr(cls, "__orig_bases__", ()):
            origin = typing.get_origin(base)
            if isinstance(origin, type) and issubclass(origin, ModelFactory):
                (model,) = typing.get_args(base)
                if isinstance(model, type):
                    return model
        return None

    @classmethod
    def get_model(cls) -> type[TModel]:
        """The model the factory makes.

        Raises:
            ConfigurationError: The factory names none.
        """
        model = getattr(cls, "factory_model", None)
        if model is None:
            raise ConfigurationError(f"{cls.__name__} names no model - declare it as ModelFactory[TheModel]")
        return typing.cast("type[TModel]", model)

    @classmethod
    def reset_sequence(cls, number: int = 0) -> None:
        """Starts the factory's sequence again.

        Args:
            number: The number of the next object.
        """
        cls.factory_sequence_number = number

    @classmethod
    def get_next_sequence_number(cls) -> int:
        """The number of the factory's next object in its sequence."""
        number = cls.factory_sequence_number
        cls.factory_sequence_number = number + 1
        return number

    @classmethod
    def get_declarations(cls, given: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
        """The declarations of one object - the traits its parameters turn on applied, the values given
        winning.

        Args:
            given: The values given to the call, by name.

        Returns:
            The declarations of the fields and parameters, the values given for the post
            declarations, and the names of the plain parameters - never the model's.
        """
        values = dict(given)
        declarations = dict(cls.factory_declarations)
        parameter_names: set[str] = set()
        for name, parameter in cls.factory_parameters.items():
            if isinstance(parameter, Trait):
                if values.pop(name, False):
                    declarations.update(parameter.values)
            else:
                parameter_names.add(name)
                declarations[name] = parameter
        post_values = {
            name: values.pop(name) for name in list(values) if isinstance(declarations.get(name), PostDeclaration)
        }
        declarations.update(values)
        return declarations, post_values, parameter_names

    @classmethod
    def get_values(cls, declarations: dict[str, Any], related_objects: dict[str, Any]) -> dict[str, Any]:
        """The values of one object - each declaration evaluated, the lazy attributes last.

        Args:
            declarations: The object's declarations.
            related_objects: The objects its ``SubFactory`` declarations made, by name - none for a
                built object.

        Returns:
            The values by name, the parameters among them.
        """
        sequence_number = cls.get_next_sequence_number()
        values: dict[str, Any] = {}
        for name, declaration in declarations.items():
            if name in related_objects:
                values[name] = related_objects[name]
            elif isinstance(declaration, Declaration) and not isinstance(declaration, LazyAttribute):
                values[name] = declaration.evaluate(sequence_number)
            elif not isinstance(declaration, LazyAttribute | PostDeclaration | SubFactory):
                values[name] = declaration
        for name, declaration in declarations.items():
            if isinstance(declaration, LazyAttribute):
                values[name] = declaration.evaluate_from(values)
        return values

    @classmethod
    def build(cls, **given: Any) -> TModel:
        """An object of the model, not saved - without the related objects of its ``SubFactory``
        declarations (a model takes only a saved one; give it) and its post declarations.

        Args:
            given: Values winning over the declarations, by field name - or a parameter of ``Params``.

        Returns:
            The object.
        """
        declarations, _post_values, parameter_names = cls.get_declarations(given)
        return cls.get_object(cls.get_values(declarations, {}), parameter_names)

    @classmethod
    def get_object(cls, values: dict[str, Any], parameter_names: set[str]) -> TModel:
        """An object of the model from its values - the parameters left out, the tenant field filled from
        the active scope."""
        model = cls.get_model()
        model_values = {name: value for name, value in values.items() if name not in parameter_names}
        if model._meta.tenant_field:
            Tenancy.fill_create_values(model, model_values)
        return model(**model_values)

    @classmethod
    async def create(cls, **given: Any) -> TModel:
        """An object of the model, saved - its related objects created first, its post declarations
        applied after.

        Args:
            given: Values winning over the declarations, by field name - or a parameter of ``Params``,
                or what a ``RelatedFactory``/``ManyToMany`` takes.

        Returns:
            The object.
        """
        declarations, post_values, parameter_names = cls.get_declarations(given)
        related_objects = {
            name: await declaration.create()
            for name, declaration in declarations.items()
            if isinstance(declaration, SubFactory)
        }
        values = cls.get_values(declarations, related_objects)
        model_values = {name: value for name, value in values.items() if name not in parameter_names}
        obj = await cls.get_model().objects.create(**model_values)
        for name, declaration in declarations.items():
            if isinstance(declaration, PostDeclaration):
                await declaration.apply(obj, name, post_values.get(name))
        return obj

    @classmethod
    def build_batch(cls, size: int, **given: Any) -> list[TModel]:
        """``size`` objects of the model, not saved.

        Args:
            size: How many.
            given: As ``build()`` takes.

        Returns:
            The objects.
        """
        return [cls.build(**given) for _ in range(size)]

    @classmethod
    async def create_batch(cls, size: int, **given: Any) -> list[TModel]:
        """``size`` objects of the model, saved - in one ``bulk_create()`` when none of them makes a
        related object or acts once created, else one by one.

        Args:
            size: How many.
            given: As ``create()`` takes.

        Returns:
            The objects.
        """
        declarations, _post_values, _parameter_names = cls.get_declarations(given)
        if any(isinstance(declaration, SubFactory | PostDeclaration) for declaration in declarations.values()):
            return [await cls.create(**given) for _ in range(size)]
        objs = cls.build_batch(size, **given)
        await cls.get_model().objects.bulk_create(objs)
        return objs
