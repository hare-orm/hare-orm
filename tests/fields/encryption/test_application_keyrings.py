from __future__ import annotations

import asyncio
import json

import pytest

from hare.core.hare_context import HareContext
from hare.exceptions import DecryptionError
from hare.fields import EncryptedJSONField
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.query.execution.query_execution import QueryExecution

pytestmark = [pytest.mark.database_independent, pytest.mark.requires("encryption")]


def test_replacing_one_applications_keys_does_not_change_another():
    first, second = HareContext(), HareContext()
    FieldEncryption.configure("first application", context=first)
    FieldEncryption.configure("second application", context=second)
    with first:
        token = FieldEncryption.encrypt("content")
        index = FieldEncryption.get_blind_index("content")
    with second:
        with pytest.raises(DecryptionError):
            FieldEncryption.decrypt(token, "secret")
        assert FieldEncryption.get_blind_index("content") != index
        FieldEncryption.configure("replacement application")
    with first:
        assert FieldEncryption.decrypt(token, "secret") == "content"
        assert FieldEncryption.get_blind_index("content") == index


@pytest.mark.asyncio
async def test_execution_retains_keyring_while_configuration_changes():
    application = HareContext()
    FieldEncryption.configure("old application", context=application)
    with application:
        execution = QueryExecution()
        before = FieldEncryption.encrypt("content")
        FieldEncryption.configure("new application")

        async def read():
            await asyncio.sleep(0)
            return FieldEncryption.decrypt(before, "secret"), FieldEncryption.encrypt("content")

        value, token = await execution.call(read)
        assert value == "content"
        with pytest.raises(DecryptionError):
            FieldEncryption.decrypt(token, "secret")
        assert FieldEncryption.binding.get() is None


def test_json_conversion_uses_one_keyring_even_when_encoder_reconfigures_it():
    application = HareContext()
    FieldEncryption.configure("old application", context=application)
    previous = application.encryption_keyring

    def encode(value):
        FieldEncryption.configure("new application")
        return json.dumps(value)

    field = EncryptedJSONField(encoder=encode)
    with application:
        stored = field.to_db_value({"left": "a", "right": "b"}, None)
        assert application.encryption_keyring is not previous
        for token, expected in zip(json.loads(stored).values(), ('"a"', '"b"'), strict=True):
            assert previous.fernet.decrypt(token.encode()).decode() == expected
        assert FieldEncryption.binding.get() is None
