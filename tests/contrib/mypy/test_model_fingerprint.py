from types import ModuleType

import pytest

from hare import fields
from hare.contrib.mypy.model_registry import ModelRegistry
from hare.core.config import HareConfig
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from hare.typing_info.bound_models import BoundModels

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("change", ["array_element", "key_order", "accepted_input", "child_lookups", "write_policy"])
def test_fingerprint_tracks_bound_type_contracts(change):
    fingerprints = []
    for changed in (False, True, False):
        module = ModuleType("typing_contract_models")
        number = fields.IntField()
        child = fields.IntField()
        keys = ("number", "code")
        if changed:
            if change == "array_element":
                child = fields.CharField(max_length=20)
            elif change == "key_order":
                keys = tuple(reversed(keys))
            elif change == "child_lookups":
                child.supported_lookups = frozenset({""})
            elif change == "write_policy":
                pass
            else:
                number.accepted_value_types = (str,)
        module.__models__ = [
            type(
                "Book",
                (Model,),
                {
                    "__module__": module.__name__,
                    "number": number,
                    "code": fields.CharField(max_length=20),
                    "tags": fields.ArrayField(child),
                    "version": fields.IntField(default=0),
                    "Meta": type(
                        "Meta",
                        (),
                        {"optimistic_lock_field": "version"} if changed and change == "write_policy" else {},
                    ),
                    "pk": CompositePrimaryKey(*keys),
                },
            )
        ]
        bound = BoundModels(
            HareConfig.from_dict(
                {
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {"typing": {"models": [module], "default_connection": "default"}},
                }
            )
        )
        fingerprints.append(ModelRegistry._get_fingerprint("typing", bound))
    assert fingerprints[0] != fingerprints[1]
    assert fingerprints[0] == fingerprints[2]
