from hare import fields
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP


class QueryContractModels:
    """Declarations whose keys and physical column names exercise query construction."""

    @staticmethod
    def without_primary_key():
        return type(
            "KeylessReading",
            (Model,),
            {
                "__module__": __name__,
                "group": fields.IntField(),
                "value": fields.IntField(),
                "Meta": type("Meta", (), {"app": CONTRACT_APP, "primary_key": None}),
            },
        )

    @staticmethod
    def with_named_columns(letter_case: str, long_prefix: str):
        transform = {"lower": str.lower, "upper": str.upper, "mixed": str.title}[letter_case]
        return type(
            "NamedColumns",
            (Model,),
            {
                "__module__": __name__,
                "a": fields.IntField(source_field=transform("hare depth")),
                "b": fields.IntField(source_field=transform("hare depth 2")),
                "pk": fields.CompositePrimaryKey("a", "b"),
                "app": fields.TextField(source_field=transform("hare app")),
                "model_name": fields.TextField(source_field=transform("hare model")),
                "first": fields.IntField(source_field=long_prefix + "a"),
                "second": fields.IntField(source_field=long_prefix + "b"),
                "window": fields.IntField(default=0, source_field=transform("hare window filter 0")),
                "ordering": fields.IntField(default=0, source_field=transform("hare distinct ordering 0")),
                "parent": fields.ForeignKeyField(
                    f"{CONTRACT_APP}.NamedColumns", related_name="children", null=True, db_constraint=False
                ),
                "Meta": type("Meta", (), {"app": CONTRACT_APP}),
            },
        )
