"""A Meta option a model can no longer declare is refused when the class is defined - left
unnoticed, the uniqueness or optimistic lock it asks for would silently not exist."""

import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.models import Model


@pytest.mark.parametrize(
    ("option", "value", "replacement"),
    [
        ("unique_together", (("name", "code"),), "UniqueConstraint"),
        ("version_field", "revision", "optimistic_lock_field"),
    ],
)
def test_unsupported_meta_option_is_refused(option, value, replacement):
    meta = type("Meta", (), {"abstract": True, option: value})
    with pytest.raises(ConfigurationError, match=rf"Meta\.{option} isn't supported - .*{replacement}"):
        type(
            "Refused",
            (Model,),
            {
                "__module__": __name__,
                "name": fields.CharField(max_length=10),
                "code": fields.CharField(max_length=10),
                "revision": fields.IntField(default=0),
                "Meta": meta,
            },
        )
