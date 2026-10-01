from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast
from uuid import uuid4

from hare import fields
from hare.exceptions import DoesNotExist, IncompleteInstanceError, QueryError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance


class VersionedModel(Model):
    """An append-only versioned model: every change creates a new row with the same ``id`` and a higher
    ``version`` instead of changing a row in place. ``(id, version)`` is the composite primary key.

    Example::

        class AppVersionedModel(YourBaseModel, VersionedModel):
            class Meta(YourBaseModel.Meta, VersionedModel.Meta):
                pass
    """

    id = fields.UUIDField(default=uuid4, db_index=True)
    version = fields.PositiveSmallIntField(default=1)

    pk = CompositePrimaryKey("id", "version")

    #: Extra field names, beyond "id"/"version" themselves, to leave out when cloning into the
    #: next version - override on a subclass for fields that must always be reset (e.g. a
    #: single-use token).
    NEW_VERSION_EXCLUDED_FIELDS: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    @classmethod
    async def get_last_version_or_exception(
        cls, *args: Q, exception: type[Exception] | None = None, **kwargs: Any
    ) -> Self:
        """
        Returns the highest-``version`` row matching the given filters.

        Args:
            exception: Exception class to raise if no match is found. Defaults to
                ``hare.exceptions.DoesNotExist``.
            args: ``Q`` filters, ANDed together.
            kwargs: Field filters.

        Raises:
            Exception: If no matching version exists.
        """
        queryset = cls.objects.filter(*args, **kwargs)
        if cls._meta.soft_delete_field is not None:
            # Deleted versions too - the highest version may be soft-deleted, and the next one must
            # not collide with it.
            queryset = queryset.include_deleted()
        obj = await queryset.order_by("-version").first()
        if not obj:
            if exception:
                raise exception()
            raise DoesNotExist(cls)
        return obj

    def get_new_version(self, **kwargs: Any) -> Self:
        """
        Builds the next version of this row, without saving it.

        Args:
            **kwargs: Field values to override on the new version.

        Raises:
            QueryError: If ``kwargs`` tries to override ``id`` or ``version`` - this method owns
                both, to guarantee the "same id, version+1" invariant.
            IncompleteInstanceError: If this instance was loaded with ``.only()``/``.defer()``
                and a field that would be copied was never fetched.
        """
        if "id" in kwargs or "version" in kwargs:
            raise QueryError("get_new_version() manages 'id' and 'version' itself - they can't be overridden.")
        excluded = {"id", "version"} | set(self.NEW_VERSION_EXCLUDED_FIELDS)
        relation_field_names = self._meta.fk_fields | self._meta.o2o_fields
        # A forward FK/O2O is stored in its shadow column(s) (e.g. "owner_id") - excluding or
        # overriding the relation by name has to skip copying those columns too, or the stale
        # copied shadow value survives the exclusion or conflicts with the override.
        for field_name in (set(self.NEW_VERSION_EXCLUDED_FIELDS) | kwargs.keys()) & relation_field_names:
            excluded.update(cast("ForeignKeyFieldInstance[Any]", self._meta.fields_map[field_name]).source_fields)
        if self._meta.soft_delete_field is not None:
            # A new version isn't born deleted.
            excluded.add(self._meta.soft_delete_field)
        # A database-computed column and an auto_now column get a fresh value on write anyway,
        # so they're neither copied nor required on a partial instance.
        excluded.update(
            field_name
            for field_name, field_object in self._meta.fields_map.items()
            if field_object.generated or getattr(field_object, "auto_now", False)
        )
        direct_fields = self._meta.fields_map.keys() - self._meta.fetch_fields
        # deepcopy - a mutable field value (e.g. JSONField/ArrayField) would otherwise be the
        # literal same object on both this instance and the new version, so mutating one's
        # field in place would silently corrupt the other too.
        copied_fields = [field for field in direct_fields if field not in excluded and field not in kwargs]
        missing_fields = sorted(field for field in copied_fields if not hasattr(self, field))
        if missing_fields:
            raise IncompleteInstanceError(
                f"{type(self).__name__} is a partial instance (loaded with .only()/.defer()) - fields "
                f"{missing_fields} weren't fetched, so the next version can't copy them; pass them "
                "to get_new_version()/create_new_version() explicitly or load the full row"
            )
        data = {field: deepcopy(getattr(self, field)) for field in copied_fields}
        data["id"] = self.id
        data["version"] = self.version + 1
        data.update(kwargs)
        return type(self)(**data)

    async def create_new_version(self, **kwargs: Any) -> Self:
        """
        Builds and saves the next version of this row.

        Args:
            **kwargs: Field values to override on the new version.
        """
        new_version = self.get_new_version(**kwargs)
        await new_version.save()
        return new_version
