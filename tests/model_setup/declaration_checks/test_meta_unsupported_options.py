"""A Meta option a model can no longer declare is refused when the class is defined - left
unnoticed, the uniqueness or optimistic lock it asks for would silently not exist."""

import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.models import Model


@pytest.mark.database_independent
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


def define_model(meta_attributes, meta_bases=()):
    return type(
        "Defined",
        (Model,),
        {
            "__module__": __name__,
            "name": fields.CharField(max_length=10),
            "Meta": type("Meta", meta_bases, {"abstract": True, **meta_attributes}),
        },
    )


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("option", "replacement"),
    [("db_table", "Meta.table"), ("app_label", "Meta.app"), ("db_table_comment", "Meta.table_description")],
)
def test_an_option_of_another_name_names_the_option_meant(option, replacement):
    with pytest.raises(ConfigurationError, match=rf"Meta\.{option} isn't supported - .*{replacement}"):
        define_model({option: "value"})


@pytest.mark.database_independent
def test_an_unknown_meta_attribute_is_refused():
    with pytest.raises(ConfigurationError, match=r"Meta\.ordring isn't a model option - did you mean Meta\.ordering"):
        define_model({"ordring": ("name",)})
    with pytest.raises(ConfigurationError, match=r"Meta\.verbose_name isn't a model option$"):
        define_model({"verbose_name": "thing"})


@pytest.mark.database_independent
def test_an_inherited_meta_attribute_is_checked_too():
    class SharedMeta:
        colour = "red"

    with pytest.raises(ConfigurationError, match=r"Meta\.colour isn't a model option"):
        define_model({}, (SharedMeta,))


@pytest.mark.database_independent
def test_an_option_an_extension_allows_is_taken():
    from hare.contrib.versioning import VersionedModel  # noqa: F401 - allows new_version_excluded_fields

    assert define_model({"new_version_excluded_fields": ("name",)})._meta.abstract


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("table", 5, r"Meta\.table takes a text, got 5"),
        ("table", "", r"Meta\.table is empty - it names nothing"),
        ("schema", [], r"Meta\.schema takes a text, got \[\]"),
        ("extensions", [5], r"Meta\.extensions entries are each the name of an extension, got 5"),
    ],
)
def test_an_option_given_a_value_of_another_type_is_refused(option, value, message):
    """A text given for a list was read letter by letter, a number for a name failed far from the
    model with an AttributeError, and "yes" for a flag was taken as set."""
    with pytest.raises(ConfigurationError, match=message):
        define_model({option: value})


@pytest.mark.database_independent
def test_options_of_their_own_types_are_taken():
    model = define_model({"table": "defined", "ordering": ["name"], "extensions": ("citext",), "managed": False})
    assert (model._meta.db_table, model._meta.extensions, model._meta.managed) == ("defined", ("citext",), False)
    model = define_model({"table": None, "schema": None, "indexes": ["name"]})
    assert (model._meta.schema, model._meta.indexes) == (None, (["name"],))
