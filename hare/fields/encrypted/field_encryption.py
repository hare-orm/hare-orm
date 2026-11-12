"""At-rest encryption of ``EncryptedTextField``/``EncryptedJSONField`` with Fernet (the ``encryption``
extra). To change the key, configure the new secret with the old ones as ``previous_keys``, run
``FieldEncryption.reencrypt()``, then drop the old secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, DecryptionError, QueryError, ValidationError
from hare.fields.encrypted.constants import (
    BLIND_INDEX_KEY_DERIVATION_PREFIX,
    REENCRYPT_BATCH_SIZE,
    UNENCRYPTABLE_SURROGATE_MESSAGE,
)
from hare.fields.encrypted.encryption_keyring import EncryptionBinding, EncryptionKeyring
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet

    from hare.core.hare_context import HareContext
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class FieldEncryption:
    """Application keyrings and execution-local snapshots for encrypted field conversions."""

    #: ``cryptography.fernet``, imported by the first ``get_fernet_module()`` rather than with hare.
    fernet_module: ClassVar[ModuleType | None] = None
    hare_context_class: ClassVar[type[HareContext] | None] = None
    default_keyring: ClassVar[EncryptionKeyring | None] = None
    default_binding: ClassVar[EncryptionBinding] = EncryptionBinding(None, None)
    binding: ClassVar[ContextVar[EncryptionBinding | None]] = ContextVar("hare_encryption_binding", default=None)

    @classmethod
    def get_current_context(cls) -> HareContext | None:
        """The active application context, resolving ``HareContext`` once after module import."""
        hare_context_class = cls.hare_context_class
        if hare_context_class is None:
            from hare.core.hare_context import HareContext

            cls.hare_context_class = hare_context_class = HareContext
        return hare_context_class.get_current()

    @classmethod
    def configure(
        cls,
        secret_key: str,
        previous_keys: Sequence[str] = (),
        blind_index_key: str | None = None,
        *,
        context: HareContext | None = None,
    ) -> None:
        """Derives the field-encryption keys: values are encrypted with ``secret_key`` and
        decrypted with it or any of ``previous_keys``; blind indexes are HMACs with
        ``blind_index_key``.

        Args:
            secret_key: Any non-empty application secret; the Fernet key is
                ``urlsafe_b64encode(sha256(secret_key))``.
            previous_keys: The secrets used before - values written with them stay readable
                until ``FieldEncryption.reencrypt()`` writes them with ``secret_key``.
            blind_index_key: The secret of the blind indexes; derived from ``secret_key`` when not
                given.
            context: The application to configure. Defaults to the active application; outside
                a context, configures the default captured by subsequently created applications.

        Raises:
            ConfigurationError: A secret isn't a non-empty string, or ``cryptography`` isn't
                installed.
        """
        if isinstance(previous_keys, str):
            raise ConfigurationError("FieldEncryption.configure() takes previous_keys as a sequence of secrets")
        secrets = [secret_key, *previous_keys]
        if not all(isinstance(secret, str) and secret for secret in secrets):
            raise ConfigurationError("FieldEncryption.configure() needs non-empty string secrets")
        if blind_index_key is not None and not (isinstance(blind_index_key, str) and blind_index_key):
            raise ConfigurationError("FieldEncryption.configure() needs a non-empty string blind_index_key")
        index_key = (
            hashlib.sha256(blind_index_key.encode()).digest()
            if blind_index_key is not None
            else hashlib.sha256(BLIND_INDEX_KEY_DERIVATION_PREFIX + secret_key.encode()).digest()
        )
        fernet_module = cls.get_fernet_module()
        fernet = fernet_module.MultiFernet(
            [
                fernet_module.Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
                for secret in secrets
            ]
        )
        cls.set_keyring(EncryptionKeyring(fernet, index_key), context)

    @classmethod
    def set_keyring(cls, keyring: EncryptionKeyring | None, context: HareContext | None = None) -> None:
        """Replace the selected application's keys without modifying active conversions."""
        context = context if context is not None else cls.get_current_context()
        if context is None:
            cls.default_keyring = keyring
            cls.default_binding = EncryptionBinding(None, keyring)
        else:
            context.encryption_keyring = keyring
            context.encryption_binding = EncryptionBinding(context.identity, keyring)

    @classmethod
    def get_binding(cls) -> EncryptionBinding:
        """Capture the active application's keyring, inheriting a containing conversion."""
        context = cls.get_current_context()
        application = None if context is None else context.identity
        binding = cls.binding.get()
        if binding is not None and binding.application is application:
            return binding
        return cls.default_binding if context is None else context.encryption_binding

    @classmethod
    @contextmanager
    def bind(cls) -> Generator[EncryptionBinding]:
        """Use one keyring for a complete value conversion, including user encoders."""
        binding = cls.get_binding()
        token = cls.binding.set(binding)
        try:
            yield binding
        finally:
            cls.binding.reset(token)

    @classmethod
    def reset(cls, *, context: HareContext | None = None) -> None:
        """Forget the selected application's keys; existing executions keep their snapshot."""
        cls.set_keyring(None, context)

    @classmethod
    def get_blind_index(cls, plaintext: str) -> str:
        """The blind index of a value - its hex HMAC-SHA256, the same for the same value.

        Args:
            plaintext: The value.

        Returns:
            The index.

        Raises:
            ConfigurationError: No key was configured.
        """
        keyring = cls.get_binding().keyring
        if keyring is None:
            raise ConfigurationError(
                "A blind index was computed before an encryption key was configured - call "
                "hare.fields.encrypted.field_encryption.FieldEncryption.configure(secret_key) at startup."
            )
        return hmac.new(keyring.blind_index_key, cls.get_plaintext_bytes(plaintext), hashlib.sha256).hexdigest()

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
        keyring = cls.get_binding().keyring
        if keyring is None:
            raise ConfigurationError(
                "An encrypted field was used before an encryption key was configured - call "
                "hare.fields.encrypted.field_encryption.FieldEncryption.configure(secret_key) at startup, "
                "before reading or writing any EncryptedTextField/EncryptedJSONField."
            )
        return keyring.fernet

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """Encrypts ``plaintext`` into a Fernet token.

        Args:
            plaintext: The value to encrypt.

        Returns:
            The token as text.

        Raises:
            ValidationError: The text holds a lone surrogate.
        """
        return cls.get_fernet().encrypt(cls.get_plaintext_bytes(plaintext)).decode()

    @staticmethod
    def get_plaintext_bytes(plaintext: str) -> bytes:
        """The UTF-8 bytes of a text encrypted or indexed.

        Args:
            plaintext: The text.

        Returns:
            The bytes.

        Raises:
            ValidationError: The text holds a lone surrogate, which UTF-8 has no encoding of.
        """
        try:
            return plaintext.encode()
        except UnicodeEncodeError:
            raise ValidationError(UNENCRYPTABLE_SURROGATE_MESSAGE) from None

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
        except cls.get_fernet_module().InvalidToken as error:
            raise DecryptionError(
                f"{field_label}: stored value can't be decrypted - it was encrypted with a different key than "
                "FieldEncryption.configure()'s secret_key and every one of its previous_keys, or it isn't an "
                "encrypted token."
            ) from error

    @classmethod
    async def reencrypt(
        cls,
        *models: type[Model],
        batch_size: int = REENCRYPT_BATCH_SIZE,
        using: str | DatabaseClient | None = None,
    ) -> int:
        """Writes every encrypted value of ``models`` with the current key - after
        ``configure(new_secret, previous_keys=[old_secret])``; once it has run, the old secret can be
        dropped. Soft-deleted rows and every tenant's are included, a batch per transaction, ordered by
        the primary key.

        Args:
            models: The models.
            batch_size: The rows read and written at once.
            using: The connection used for both reading and writing. Defaults to each model's
                write connection, so replica routing cannot split the rotation.

        Returns:
            How many rows were written.

        Raises:
            ConfigurationError: A model has no primary key to write its rows by.
            QueryError: ``batch_size`` isn't a positive int.
        """
        # Deferred: the query layer imports the fields, which import this module.
        from hare.query.execution.query_execution import QueryExecution

        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise QueryError(
                f"FieldEncryption.reencrypt() needs a positive int batch_size, got "
                f"{ValueDescriptions.get_shown_value(batch_size)}"
            )
        return await QueryExecution.run_operation((), lambda: cls.reencrypt_models(models, batch_size, using))

    @classmethod
    async def reencrypt_models(
        cls,
        models: Sequence[type[Model]],
        batch_size: int,
        using: str | DatabaseClient | None,
    ) -> int:
        """Rotate every batch under the execution's captured keyring and selected connection."""
        from hare.core.connections.connections import Connections
        from hare.query.scopes.row_scopes import RowScopes

        written = 0
        # A model given twice is written once.
        for model in dict.fromkeys(models):
            encrypted_field_names = [
                name
                for name, field in model._meta.fields_map.items()
                if getattr(field, "encrypted", False) and name in model._meta.fields_db_projection
            ]
            if not encrypted_field_names:
                continue
            model._meta.raise_if_no_primary_key(f"FieldEncryption.reencrypt() of {model.__name__}")
            connection = Connections.get_client(using) or model.get_connection(for_write=True)
            rows = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True)).using(
                connection
            )
            batch: list[Any] = []
            async for row in rows.order_by(*model._meta.primary_key_attribute_names).iterator(chunk_size=batch_size):
                batch.append(row)
                if len(batch) == batch_size:
                    written += await cls.write_batch(model, batch, encrypted_field_names, connection)
                    batch = []
            if batch:
                written += await cls.write_batch(model, batch, encrypted_field_names, connection)
        return written

    @staticmethod
    async def write_batch(
        model: type[Model], rows: list[Any], field_names: list[str], connection: DatabaseClient
    ) -> int:
        """Writes the encrypted fields of a batch of rows in one transaction - as they are on a
        database without transactions.

        Args:
            model: The rows' model.
            rows: The rows, read (and so decrypted) with any configured key.
            field_names: The encrypted fields.
            connection: The connection selected for this model's rotation.

        Returns:
            How many rows were written.
        """
        # Deferred: see reencrypt().
        from hare.query.scopes.row_scopes import RowScopes

        queryset = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True))
        async with connection._in_transaction() as transaction:
            await queryset.using(transaction).bulk_update(rows, fields=field_names)
        return len(rows)
