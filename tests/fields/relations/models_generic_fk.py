from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.fields import CASCADE, PROTECT, RESTRICT, SET_DEFAULT, SET_NULL
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "generic_author"


class Post(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        table = "generic_post"


class Photo(Model):
    id = fields.IntField(primary_key=True)
    url = fields.CharField(max_length=100)

    class Meta:
        table = "generic_photo"


class ArticleVersion(Model):
    pk = CompositePrimaryKey("article", "version")
    article = fields.IntField()
    version = fields.IntField()
    body = fields.CharField(max_length=50, default="")

    class Meta:
        table = "generic_article_version"


class Comment(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50, default="")
    target = fields.GenericForeignKeyField(
        {"post": Post, "photo": "models.Photo", "article_version": ArticleVersion},
        related_name="comments",
        on_delete=CASCADE,
    )

    class Meta:
        table = "generic_comment"


class Reaction(Model):
    id = fields.IntField(primary_key=True)
    subject = fields.GenericForeignKeyField(
        {"reacted_post": Post, "reacted_photo": Photo}, related_name="reactions", on_delete=SET_NULL, null=True
    )

    class Meta:
        table = "generic_reaction"


class Pin(Model):
    id = fields.IntField(primary_key=True)
    pinned = fields.GenericForeignKeyField(
        {"pinned_post": Post, "pinned_photo": Photo}, related_name="pins", on_delete=PROTECT
    )

    class Meta:
        table = "generic_pin"


class Flag(Model):
    id = fields.IntField(primary_key=True)
    flagged = fields.GenericForeignKeyField(
        {"flagged_post": Post, "flagged_photo": Photo}, related_name="flags", on_delete=RESTRICT
    )

    class Meta:
        table = "generic_flag"


def get_default_post() -> Post:
    """The post a bookmark falls back to - saved by the tests as id 1."""
    post = Post(id=1, title="default")
    post._saved_in_db = True
    return post


class Bookmark(Model):
    id = fields.IntField(primary_key=True)
    item = fields.GenericForeignKeyField(
        {"bookmarked_post": Post, "bookmarked_photo": Photo},
        related_name="bookmarks",
        on_delete=SET_DEFAULT,
        default=get_default_post,
    )

    class Meta:
        table = "generic_bookmark"


class Activity(Model):
    """Two generic relations sharing a target."""

    id = fields.IntField(primary_key=True)
    subject = fields.GenericForeignKeyField(
        {"subject_author": Author, "subject_post": Post}, related_name="subject_activities"
    )
    actor = fields.GenericForeignKeyField({"actor_author": Author}, related_name="actor_activities", null=True)

    class Meta:
        table = "generic_activity"


class Draft(Model):
    """A soft-deleted target."""

    id = fields.IntField(primary_key=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "generic_draft"
        soft_delete_field = "deleted_at"


class Shop(Model):
    """A tenant-scoped target."""

    id = fields.IntField(primary_key=True)
    region = fields.CharField(max_length=10)

    class Meta:
        table = "generic_shop"
        tenant_field = "region"


class Mention(Model):
    id = fields.IntField(primary_key=True)
    mentioned = fields.GenericForeignKeyField(
        {"mentioned_draft": Draft, "mentioned_shop": Shop, "mentioned_post": Post}, related_name="mentions"
    )

    class Meta:
        table = "generic_mention"


class Like(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField(Author, related_name="likes")
    target = fields.GenericForeignKeyField({"liked_post": Post, "liked_photo": Photo}, related_name="likes")

    class Meta:
        table = "generic_like"
        constraints = [UniqueConstraint(fields=("author", "target"), name="one_like")]
        indexes = [("author", "target"), Index(fields=("target", "-id"), name="recent_likes")]
