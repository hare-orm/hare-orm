import pytest

from hare.fields.encrypted.field_encryption import FieldEncryption
from tests.fields.encryption.constants import SECRET_KEY


@pytest.fixture
def field_encryption_key():
    FieldEncryption.configure(SECRET_KEY)
    yield
    FieldEncryption.reset()
