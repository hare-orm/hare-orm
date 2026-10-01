# `VersionedModel` (`hare.contrib.versioning`)

An append-only versioning base: every change creates a new row sharing the same `id` but a higher
`version`, instead of mutating a row in place.

```python
class VersionedModel(Model):
    id = fields.UUIDField(default=uuid4, db_index=True)
    version = fields.PositiveSmallIntField(default=1)
    pk = CompositePrimaryKey("id", "version")
    NEW_VERSION_EXCLUDED_FIELDS: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    @classmethod
    async def get_last_version_or_exception(
        cls, *args: Q, exception: type[Exception] | None = None, **kwargs: Any
    ) -> Self: ...

    def get_new_version(self, **kwargs: Any) -> Self: ...      # raises QueryError if kwargs has "id"/"version"
    async def create_new_version(self, **kwargs: Any) -> Self: ...
```

`id` and `version` together are the table's real primary key, not a surrogate one with a bolted-on
uniqueness constraint — which means a foreign key can target a specific version with a real,
enforced `FOREIGN KEY` (see
[Relations — targeting a composite primary key](../models/relations.md#targeting-a-composite-primary-key)).

```python
class Article(VersionedModel):
    title = fields.CharField(max_length=200)
    config = fields.JSONField(default=dict)

    NEW_VERSION_EXCLUDED_FIELDS = ("published_at",)


current = await Article.get_last_version_or_exception(id=article_id)
draft = await current.create_new_version(title="Updated title")
```

`get_new_version`/`create_new_version` copy every direct field via `deepcopy`, except `id`,
`version`, and anything listed in `NEW_VERSION_EXCLUDED_FIELDS`, bumping `version` by 1. A
database-computed column (`GeneratedField`, any `generated=True` field) and an `auto_now=True`
column are never copied - they get a fresh value when the new version is written. A
forward FK/O2O can be listed or overridden by its relation name (`owner`) or by its shadow column
(`owner_id`) - either way the shadow column is not copied from the old version. An instance loaded
with `.only()`/`.defer()` raises `IncompleteInstanceError` naming the unfetched fields, unless they
are passed as overrides; database-computed and `auto_now` columns never need to be loaded.

Combine `VersionedModel` with your own project's base model through multiple inheritance:

```python
class AppVersionedModel(YourBaseModel, VersionedModel):
    class Meta(YourBaseModel.Meta, VersionedModel.Meta):
        pass
```

!!! note "Out of scope"
    Querying "state as of version X", diffing between versions, and automatic cleanup of old
    versions aren't built in — layer those on top if you need them.

!!! warning "A `unique=True` field conflicts with append-only versioning"
    Every version is a real, permanent row — `create_new_version()` never deletes or overwrites
    the previous one. A `unique=True` field (or a `UniqueConstraint` in `Meta.constraints`) that
    isn't reset via `NEW_VERSION_EXCLUDED_FIELDS` keeps its old value on the new row too, and the
    second version collides with the first at the database level:

    ```python
    class Article(VersionedModel):
        slug = fields.CharField(max_length=200, unique=True)
        title = fields.CharField(max_length=200)


    article = await Article.objects.create(slug="my-article", title="Draft")
    await article.create_new_version(title="Revised")  # IntegrityError: slug already exists
    ```

    A field meant to identify the same logical document across all its versions (a slug, an
    external key) needs a different scoping strategy than a bare `unique=True` - e.g. a
    `condition`-based partial `UniqueConstraint` scoped to the latest version, or dropping
    uniqueness in the database and enforcing "one active slug" at the application level instead.
