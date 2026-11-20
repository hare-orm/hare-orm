from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet


@dataclass(frozen=True, slots=True)
class EncryptionKeyring:
    """One application's encryption and blind-index keys, replaced as a complete unit."""

    fernet: MultiFernet = field(repr=False)
    blind_index_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class EncryptionBinding:
    application: object | None
    keyring: EncryptionKeyring | None = field(repr=False)
