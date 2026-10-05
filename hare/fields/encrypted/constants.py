from __future__ import annotations

from hare.query.enums import Lookup

#: Lookups still usable on an EncryptedTextField - Fernet tokens are non-deterministic (a fresh
#: random IV per write), so no value-comparing lookup could ever match a stored row.
ENCRYPTED_TEXT_FIELD_SUPPORTED_LOOKUPS = frozenset({Lookup.ISNULL, Lookup.NOT_ISNULL})

#: Lookups of an EncryptedTextField with a blind index - equality and membership compare the HMAC of
#: the value with the stored one.
BLIND_INDEX_SUPPORTED_LOOKUPS = frozenset(
    {Lookup.EXACT, Lookup.NOT, Lookup.IN, Lookup.NOT_IN, Lookup.ISNULL, Lookup.NOT_ISNULL}
)

#: The equality and membership lookups a blind index answers instead of its encrypted field.
BLIND_INDEX_COMPARED_LOOKUPS = ("", "not", "in", "not_in")

#: The characters of a blind index - the hex HMAC-SHA256 of the value.
BLIND_INDEX_LENGTH = 64

#: Separates the blind index key derived from an encryption secret from any other use of the secret.
BLIND_INDEX_KEY_DERIVATION_PREFIX = b"hare-orm blind index\x00"

#: Lookups still usable on an EncryptedJSONField with plaintext keys - key-presence lookups work;
#: every value-comparing lookup can't match for the same reason as above.
ENCRYPTED_JSON_FIELD_SUPPORTED_LOOKUPS = frozenset(
    {Lookup.ISNULL, Lookup.NOT_ISNULL, Lookup.HAS_KEY, Lookup.HAS_KEYS, Lookup.HAS_ANY_KEYS}
)

#: Lookups still usable on an EncryptedJSONField with encrypted keys - a key is a Fernet token too,
#: so no key-presence lookup can match either.
ENCRYPTED_JSON_FIELD_ENCRYPTED_KEYS_SUPPORTED_LOOKUPS = frozenset({Lookup.ISNULL, Lookup.NOT_ISNULL})

#: The rows FieldEncryption.reencrypt() reads and writes at once, a transaction each.
REENCRYPT_BATCH_SIZE = 500
