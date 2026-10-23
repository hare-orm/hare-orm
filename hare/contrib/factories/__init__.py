"""Factories of model objects for tests - an asynchronous factory_boy::

class UserFactory(ModelFactory[User]):
    name = Sequence(lambda number: f"user{number}")
    email = LazyAttribute(lambda user: f"{user.name}@example.com")
    team = SubFactory(TeamFactory)

user = await UserFactory.create(name="ann")
"""

from __future__ import annotations

from hare.contrib.factories.field_declarations.declaration import Declaration
from hare.contrib.factories.field_declarations.faker import Faker
from hare.contrib.factories.field_declarations.iterator import Iterator
from hare.contrib.factories.field_declarations.lazy_attribute import LazyAttribute
from hare.contrib.factories.field_declarations.lazy_function import LazyFunction
from hare.contrib.factories.field_declarations.many_to_many import ManyToMany
from hare.contrib.factories.field_declarations.post_declaration import PostDeclaration
from hare.contrib.factories.field_declarations.related_factory import RelatedFactory
from hare.contrib.factories.field_declarations.sequence import Sequence
from hare.contrib.factories.field_declarations.sub_factory import SubFactory
from hare.contrib.factories.model_factory import ModelFactory
from hare.contrib.factories.trait import Trait

__all__ = [
    "Declaration",
    "Faker",
    "Iterator",
    "LazyAttribute",
    "LazyFunction",
    "ManyToMany",
    "ModelFactory",
    "PostDeclaration",
    "RelatedFactory",
    "Sequence",
    "SubFactory",
    "Trait",
]
