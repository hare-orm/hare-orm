from __future__ import annotations

from typing import Any

from hare import fields
from hare.contrib.outbox import OutboxEvent
from hare.contrib.versioning.versioned_model import VersionedModel
from hare.fields.enums import OnDelete
from hare.gis import PointField
from hare.models import Model
from hare.vectors import VectorField
from tests.contracts.constants import CONTRACT_APP


class ContribModels:
    """Builders of the models of the contrib cases - each makes its classes anew, in the contract app."""

    @staticmethod
    def make_model(
        class_name: str,
        table: str,
        *,
        bases: tuple[type, ...] = (Model,),
        meta_options: dict[str, Any] | None = None,
        meta_bases: tuple[type, ...] = (),
        **attributes: Any,
    ) -> type[Model]:
        """One model.

        Args:
            class_name: The class name.
            table: The table.
            bases: The base classes.
            meta_options: The ``Meta`` options besides the app and table.
            meta_bases: The base classes of ``Meta``.
            attributes: The fields.

        Returns:
            The model.
        """
        meta = type("Meta", meta_bases, {"app": CONTRACT_APP, "table": table, **(meta_options or {})})
        return type(class_name, bases, {"__module__": __name__, **attributes, "Meta": meta})

    @staticmethod
    def make_outbox_subclass() -> list[type[Model]]:
        """A subclass of the outbox event with a column added."""
        return [
            ContribModels.make_model(
                "ShopOutboxEvent",
                "shop_outbox_event",
                bases=(OutboxEvent,),
                meta_bases=(OutboxEvent.Meta,),
                shop_id=fields.IntField(null=True),
            )
        ]

    @staticmethod
    def make_soft_delete_versioned() -> list[type[Model]]:
        """A versioned model with a soft delete."""
        return [
            ContribModels.make_model(
                "VersionedPage",
                "versioned_page",
                bases=(VersionedModel,),
                meta_bases=(VersionedModel.Meta,),
                meta_options={"abstract": False, "soft_delete_field": "deleted_at"},
                title=fields.TextField(),
                deleted_at=fields.DatetimeField(null=True),
            )
        ]

    @staticmethod
    def make_tenant_model() -> list[type[Model]]:
        """A model with a tenant."""
        return [
            ContribModels.make_model(
                "TenantInvoice",
                "tenant_invoice",
                meta_options={"tenant_field": "company_id"},
                id=fields.IntField(primary_key=True),
                company_id=fields.IntField(),
                number=fields.CharField(max_length=20),
            )
        ]

    @staticmethod
    def make_encrypted_with_blind_index() -> list[type[Model]]:
        """A model with an encrypted field and its blind index, unique."""
        return [
            ContribModels.make_model(
                "SecretContact",
                "secret_contact",
                id=fields.IntField(primary_key=True),
                email=fields.EncryptedTextField(blind_index=True, unique=True),
            )
        ]

    @staticmethod
    def make_gis_model() -> list[type[Model]]:
        """A model with a point."""
        return [
            ContribModels.make_model(
                "Landmark",
                "landmark",
                id=fields.IntField(primary_key=True),
                location=PointField(null=True),
            )
        ]

    @staticmethod
    def make_vector_model() -> list[type[Model]]:
        """A model with a vector."""
        return [
            ContribModels.make_model(
                "Embedding",
                "embedding",
                id=fields.IntField(primary_key=True),
                values=VectorField(3, null=True),
            )
        ]

    @staticmethod
    def make_generic_foreign_key_models() -> list[type[Model]]:
        """A model pointing at one of two others."""
        post = ContribModels.make_model("CommentedPost", "commented_post", id=fields.IntField(primary_key=True))
        photo = ContribModels.make_model("CommentedPhoto", "commented_photo", id=fields.IntField(primary_key=True))
        comment = ContribModels.make_model(
            "GenericComment",
            "generic_comment",
            id=fields.IntField(primary_key=True),
            target=fields.GenericForeignKeyField(
                {"post": f"{CONTRACT_APP}.CommentedPost", "photo": f"{CONTRACT_APP}.CommentedPhoto"},
                related_name="comments",
            ),
        )
        return [post, photo, comment]

    @staticmethod
    def make_composite_key_model() -> list[type[Model]]:
        """A model with a composite primary key."""
        return [
            ContribModels.make_model(
                "ThingRevision",
                "thing_revision",
                thing_id=fields.IntField(),
                revision=fields.IntField(),
                name=fields.TextField(),
                pk=fields.CompositePrimaryKey("thing_id", "revision"),
            )
        ]

    @staticmethod
    def make_many_to_many_through_models() -> list[type[Model]]:
        """Two models linked by a many-to-many relation through a model between them."""
        tag = ContribModels.make_model(
            "ArticleTag", "article_tag", id=fields.IntField(primary_key=True), name=fields.CharField(max_length=30)
        )
        article = ContribModels.make_model(
            "Article",
            "article",
            id=fields.IntField(primary_key=True),
            tags=fields.ManyToManyField(
                f"{CONTRACT_APP}.ArticleTag", through=f"{CONTRACT_APP}.ArticleTagLink", related_name="articles"
            ),
        )
        link = ContribModels.make_model(
            "ArticleTagLink",
            "article_tag_link",
            id=fields.IntField(primary_key=True),
            article=fields.ForeignKeyField(f"{CONTRACT_APP}.Article", on_delete=OnDelete.CASCADE),
            tag=fields.ForeignKeyField(f"{CONTRACT_APP}.ArticleTag", on_delete=OnDelete.CASCADE),
        )
        return [tag, article, link]
