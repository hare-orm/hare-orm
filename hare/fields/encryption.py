"""At-rest encryption of ``EncryptedTextField``/``EncryptedJSONField`` with Fernet (the ``encryption``
extra). To change the key, configure the new secret with the old ones as ``previous_keys``, run
``reencrypt_fields()``, then drop the old secrets.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ConfigurationError, DecryptionError, QueryError
from hare.fields.constants import REENCRYPT_BATCH_SIZE
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from cryptography.fernet import MultiFernet

    from hare.models import Model


class FieldEncryption:
    """Process-wide Fernet key holder shared by every encrypted field."""

    #: ``cryptography.fernet``, imported by the first ``get_fernet_module()`` rather than with hare.
    fernet_module: ClassVar[ModuleType | None] = None
    fernet: ClassVar[MultiFernet | None] = None

    @classmethod
    def configure(cls, secret_key: str, previous_keys: Sequence[str] = ()) -> None:
        """Derives the field-encryption keys: values are encrypted with ``secret_key`` and
        decrypted with it or any of ``previous_keys``.

        Args:
            secret_key: Any non-empty application secret; the Fernet key is
                ``urlsafe_b64encode(sha256(secret_key))``.
            previous_keys: The secrets used before - values written with them stay readable
                until ``reencrypt_fields()`` writes them with ``secret_key``.

        Raises:
            ConfigurationError: A secret isn't a non-empty string, or ``cryptography`` isn't
                installed.
        """
        if isinstance(previous_keys, str):
            raise ConfigurationError("configure_field_encryption() takes previous_keys as a sequence of secrets")
        secrets = [secret_key, *previous_keys]
        if not all(isinstance(secret, str) and secret for secret in secrets):
            raise ConfigurationError("configure_field_encryption() needs non-empty string secrets")
        fernet_module = cls.get_fernet_module()
        cls.fernet = fernet_module.MultiFernet(
            [
                fernet_module.Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
                for secret in secrets
            ]
        )

    @classmethod
    def reset(cls) -> None:
        """Forgets the configured key."""
        cls.fernet = None

    @classmethod
    def get_fernet_module(cls) -> ModuleType:
        """Returns ``cryptography.fernet``.

        Raises:
            ConfigurationError: ``cryptography`` isn't installed.
        """
        if cls.fernet_module is None:
            try:
                from cryptography import fernet as fernet_module
            except ImportError:
                raise ConfigurationError(
                    "Encrypted fields need the 'cryptography' package - install it with "
                    "`pip install hare-orm[encryption]` (or `poetry add hare-orm -E encryption`)."
                ) from None
            cls.fernet_module = fernet_module
        return cls.fernet_module

    @classmethod
    def get_fernet(cls) -> MultiFernet:
        """Returns the configured keys.

        Raises:
            ConfigurationError: ``cryptography`` isn't installed, or no key was configured.
        """
        cls.get_fernet_module()
        if cls.fernet is None:
            raise ConfigurationError(
                "An encrypted field was used before an encryption key was configured - call "
                "hare.fields.encryption.configure_field_encryption(secret_key) at startup, before reading or "
                "writing any EncryptedTextField/EncryptedJSONField."
            )
        return cls.fernet

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """Encrypts ``plaintext`` into a Fernet token.

        Args:
            plaintext: The value to encrypt.

        Returns:
            The token as text.
        """
        return cls.get_fernet().encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, token: str, field_label: str) -> str:
        """Decrypts a Fernet token.

        Args:
            token: The stored token.
            field_label: ``Model.field`` name used in the error message.

        Returns:
            The plaintext.

        Raises:
            DecryptionError: The token wasn't produced with the configured key, or isn't a token.
        """
        fernet = cls.get_fernet()
        try:
            return fernet.decrypt(token.encode()).decode()
        except cls.get_fernet_module().InvalidToken as exc:
            raise DecryptionError(
                f"{field_label}: stored value can't be decrypted - it was encrypted with a different key than "
                "configure_field_encryption()'s secret_key and every one of its previous_keys, or it isn't an "
                "encrypted token."
            ) from exc

    @classmethod
    async def reencrypt(cls, models: Sequence[type[Model]], *, batch_size: int = REENCRYPT_BATCH_SIZE) -> int:
        """Writes every encrypted value of ``models`` with the current key - soft-deleted rows and
        every tenant's included, a batch per transaction, ordered by the primary key.

        Args:
            models: The models.
            batch_size: The rows read and written at once.

        Returns:
            How many rows were written.

        Raises:
            ConfigurationError: A model has no primary key to write its rows by, or ``batch_size``
                isn't a positive int.
        """
        # Deferred: the query layer imports the fields, which import this module.
        from hare.query.scopes.row_scopes import RowScopes
        from hare.transactions.transactions import Transactions

        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise QueryError(f"reencrypt_fields() needs a positive int batch_size, got {batch_size!r}")
        written = 0
        for model in models:
            encrypted_field_names = [
                name
                for name, field in model._meta.fields_map.items()
                if getattr(field, "encrypted", False) and name in model._meta.fields_db_projection
            ]
            if not encrypted_field_names:
                continue
            model._meta.raise_if_no_primary_key(f"reencrypt_fields() of {model.__name__}")
            rows = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True))
            batch: list[Any] = []
            async for row in rows.order_by(*model._meta.pk_attr_names).iterator(chunk_size=batch_size):
                batch.append(row)
                if len(batch) == batch_size:
                    written += await cls.write_batch(model, batch, encrypted_field_names, Transactions)
                    batch = []
            if batch:
                written += await cls.write_batch(model, batch, encrypted_field_names, Transactions)
        return written

    @staticmethod
    async def write_batch(model: type[Model], rows: list[Any], field_names: list[str], transactions: Any) -> int:
        """Writes the encrypted fields of a batch of rows in one transaction - as they are on a
        database without transactions.

        Args:
            model: The rows' model.
            rows: The rows, read (and so decrypted) with any configured key.
            field_names: The encrypted fields.
            transactions: ``Transactions``.

        Returns:
            How many rows were written.
        """
        # Deferred: see reencrypt().
        from hare.query.scopes.row_scopes import RowScopes

        queryset = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True))
        # A database without transactions writes the batch as it is.
        if not model._meta.db.features.supports_transactions:
            await queryset.bulk_update(rows, fields=field_names)
            return len(rows)
        async with transactions.atomic(model._meta.default_connection):
            await queryset.bulk_update(rows, fields=field_names)
        return len(rows)


def configure_field_encryption(secret_key: str, previous_keys: Sequence[str] = ()) -> None:
    """Configures the keys every ``EncryptedTextField``/``EncryptedJSONField`` uses: values are
    written with ``secret_key`` and read with it or any of ``previous_keys``.

    Args:
        secret_key: Any non-empty application secret.
        previous_keys: The secrets used before, while their values aren't written again.

    Raises:
        ConfigurationError: A secret is empty/not a string, or ``cryptography`` isn't installed.
    """
    FieldEncryption.configure(secret_key, previous_keys)


async def reencrypt_fields(*models: type[Model], batch_size: int = REENCRYPT_BATCH_SIZE) -> int:
    """Writes every encrypted value of ``models`` with the current key - after
    ``configure_field_encryption(new_secret, previous_keys=[old_secret])``; once it has run, the old
    secret can be dropped.

    Args:
        models: The models with encrypted fields.
        batch_size: The rows read and written at once, a transaction each.

    Returns:
        How many rows were written.

    Raises:
        ConfigurationError: A model has no primary key, or ``batch_size`` isn't a positive int.
    """
    return await FieldEncryption.reencrypt(models, batch_size=batch_size)
