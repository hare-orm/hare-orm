from __future__ import annotations

from typing import Any

from hare import fields
from hare.fields.enums import OnDelete
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.writes.enums import WriteRole
from tests.contracts.writes.write_model_set import WriteModelSet
from tests.contracts.writes.write_observer import WriteObserver
from tests.utils.database_under_test import DatabaseUnderTest


class WriteModels:
    """The models of the write matrix, made anew for each test: a soft-deletable subject with a unique name,
    a note pointing at it, a tag and the through model linking them - each carrying what an observer adds."""

    @staticmethod
    def make_model(
        class_name: str, table: str, observer: WriteObserver | None, role: WriteRole | None, **attributes: Any
    ) -> type[Model]:
        """One model of the matrix.

        Args:
            class_name: The class name.
            table: The table.
            observer: The observer whose fields and options the model carries, None for none.
            role: The model's role, None for a model of no observer.
            attributes: The fields of the model.

        Returns:
            The model.
        """
        meta_options: dict[str, Any] = {"app": CONTRACT_APP, "table": table}
        if role in {WriteRole.SUBJECT, WriteRole.NOTE}:
            meta_options["soft_delete_field"] = "deleted_at"
        if observer is not None and role is not None:
            attributes.update(observer.get_field_attributes(role))
            meta_options.update(observer.get_meta_options(role))
        namespace: dict[str, Any] = {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True, generated=False),
            **attributes,
            "Meta": type("Meta", (), meta_options),
        }
        return type(class_name, (Model,), namespace)

    @staticmethod
    def make(observer: WriteObserver | None = None) -> WriteModelSet:
        """The models of the matrix for an observer.

        Args:
            observer: The observer, None for plain models.

        Returns:
            The models.
        """
        features = DatabaseUnderTest.get_dialect().features
        keeps_unique = features.supports_unique_constraints
        keeps_keys = features.supports_foreign_keys
        tag = WriteModels.make_model("WriteTag", "write_tag", None, None, name=fields.CharField(max_length=50))
        row_fields = {
            "name": fields.CharField(max_length=50, unique=keeps_unique),
            "amount": fields.IntField(default=0),
            "deleted_at": fields.DatetimeField(null=True),
        }
        subject = WriteModels.make_model(
            "WriteSubject",
            "write_subject",
            observer,
            WriteRole.SUBJECT,
            tags=fields.ManyToManyField(f"{CONTRACT_APP}.WriteTag", through=f"{CONTRACT_APP}.WriteLink"),
            **row_fields,
        )
        note = WriteModels.make_model(
            "WriteNote",
            "write_note",
            observer,
            WriteRole.NOTE,
            subject=fields.ForeignKeyField(
                f"{CONTRACT_APP}.WriteSubject",
                related_name="notes",
                on_delete=OnDelete.CASCADE,
                null=True,
                db_constraint=keeps_keys,
            ),
            name=fields.CharField(max_length=50, unique=keeps_unique),
            amount=fields.IntField(default=0),
            deleted_at=fields.DatetimeField(null=True),
        )
        link = WriteModels.make_model(
            "WriteLink",
            "write_link",
            observer,
            WriteRole.LINK,
            id=fields.IntField(primary_key=True),
            subject=fields.ForeignKeyField(
                f"{CONTRACT_APP}.WriteSubject", on_delete=OnDelete.CASCADE, db_constraint=keeps_keys
            ),
            tag=fields.ForeignKeyField(
                f"{CONTRACT_APP}.WriteTag", on_delete=OnDelete.CASCADE, db_constraint=keeps_keys
            ),
        )
        return WriteModelSet(
            subject=subject,
            note=note,
            tag=tag,
            link=link,
            extras=observer.get_extra_models() if observer is not None else (),
        )
