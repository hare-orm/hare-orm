"""The text format codec writes what the field's own ``to_db_value`` writes - the same normalized
string, or the same error with the same message - for EmailField, URLField, SlugField and a
PhoneField without ``phonenumbers``, on a grid of valid, invalid and borderline values, on SQLite and
both PostgreSQL drivers."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.fields.data.text.phone_field import PhoneField
from hare.fields.validators import MinLengthValidator
from hare.query.rows.enums import ReadCodecType, WriteCodecType
from hare.query.rows.native.field_codecs import FieldCodecs
from tests.test_field_codecs import CONNECTIONS, assert_writes_match, make_field, native_rows

EMAIL_VALUES: list[Any] = [
    None,
    5,
    "",
    "a@example.com",
    "Ann.Lee+tag@Example.COM",
    "ANN@EXAMPLE.COM",
    "a@b.co",
    "first.middle.last@sub.example.museum",
    "!#$%&'*+/=?^_`{}|~-@example.com",
    "a@xn--80ak6aa92e.com",
    "a@example.xn--p1ai",
    "a@EXAMPLE.XN--P1AI",
    "a@ex--ample.com",
    "a@123.com",
    "a@localhost",
    "a@[192.168.0.1]",
    "a@[IPv6:::1]",
    '"quoted"@example.com',
    '"q@x"@Example.com',
    "a..b@example.com",
    ".a@example.com",
    "a.@example.com",
    "@example.com",
    "a@",
    "a",
    "a@b@example.com",
    "a@-example.com",
    "a@example-.com",
    "a@example.c",
    "a@example.c0m",
    "a@example.123",
    "a@example.-com",
    "a@exa_mple.com",
    "a@example.com.",
    "a@example..com",
    "а@пример.рф",
    "a@пример.рф",
    "a@EXAMPLE.İT",
    "İ@example.com",
    "a@ex\x00ample.com",
    "a\x00@example.com",
    "a@example.com\n",
    " a@example.com",
    "a@example.com ",
    "\ud800@example.com",
    "x" * 250 + "@example.com",
    "x" * 242 + "@example.com",
    "a@" + "b" * 63 + ".com",
    "a@" + "b" * 64 + ".com",
    "a@b." + "c" * 63,
    "a@b." + "c" * 64,
    "a@b.xn--" + "c" * 59,
    "a@b.xn--" + "c" * 60,
]

URL_VALUES: list[Any] = [
    None,
    7,
    "",
    "http://example.com",
    "https://Example.COM/path?q=1#frag",
    "HTTP://example.com",
    "HtTpS://example.com/",
    "ftp://example.com",
    "http://localhost:8000/",
    "http://LOCALHOST",
    "http://localhost.",
    "http://127.0.0.1",
    "http://255.255.255.255:1/",
    "http://256.0.0.1",
    "http://01.2.3.4",
    "http://1.2.3",
    "http://1.2.3.4.5",
    "http://[::1]:80/",
    "http://[::1",
    "http://user:pass@example.com",
    "http://user@example.com/",
    "http://example.com:",
    "http://example.com:123456",
    "http://example.com:99999",
    "http://example.com:8a",
    "http://exa mple.com",
    "http://example.com/ a",
    "http://example",
    "http://example.com.",
    "http://example.com..",
    "http:/example.com",
    "http:example.com",
    "example.com",
    "://example.com",
    "1http://example.com",
    "h_t://example.com",
    "http://-a.com",
    "http://a-.com",
    "http://a.-b.com",
    "http://пример.рф",
    "http://example.com/путь",
    "http://example.com\t",
    "\thttp://example.com",
    "http://",
    "http://?",
    "http:///path",
    "http://example.com/" + "a" * 2100,
    "http://example.com/" + "a" * 2000,
    "http://xn--80ak6aa92e.com",
    "http://a.b-c.de",
    "http://exa_mple.com",
    "http://example.com#",
    "http://example.com?",
    "http://example.com/a@b",
    "http://example.com\\@evil.com",
    "http://a." + ".".join(["b" * 60] * 5) + ".com",
    "http://a" + ".b" * 120 + ".com",
    "mailto:a@b.com",
    "http://example.com\x00",
    "http://example.com/\x7f",
]

SLUG_VALUES: list[Any] = [
    None,
    5,
    "",
    "abc",
    "a-b_c",
    "ABC123",
    "-",
    "_",
    "ab",
    "a",
    "a b",
    "a.b",
    "ab!",
    "привет-мир",
    "a\x00",
    "a\n",
    "x" * 10,
    "x" * 11,
    "x" * 50,
    "x" * 51,
]

PHONE_VALUES: list[Any] = [
    None,
    5,
    "",
    "+16502530000",
    "+12",
    "+1",
    "+",
    "+0123",
    "+123456789012345",
    "+1234567890123456",
    "16502530000",
    "+1 650 253 0000",
    "+1-650-253-0000",
    "+١٢٣٤٥",
    "+1650253000a",
    "+16502530000\n",
]


def assert_text_format_codec(field: Any) -> None:
    """The field is written by the text format codec on every connection."""
    for _connection_name, types, _native_types in CONNECTIONS:
        codec_type, _options = FieldCodecs.get_write_specification(field, types, None)
        assert codec_type is WriteCodecType.TEXT_FORMAT


@pytest.mark.parametrize(
    "field",
    [
        make_field(fields.EmailField(), "email"),
        make_field(fields.EmailField(null=True), "email"),
        make_field(fields.EmailField(lowercase=True), "email"),
        make_field(fields.EmailField(max_length=20), "email"),
    ],
    ids=["plain", "null", "lowercase", "short"],
)
def test_email_writes_match(field):
    assert_text_format_codec(field)
    assert_writes_match(field, EMAIL_VALUES)


@pytest.mark.parametrize(
    "field",
    [
        make_field(fields.URLField(), "website"),
        make_field(fields.URLField(null=True), "website"),
        make_field(fields.URLField(schemes=["ftp", "https"]), "website"),
        make_field(fields.URLField(max_length=30), "website"),
    ],
    ids=["plain", "null", "schemes", "short"],
)
def test_url_writes_match(field):
    assert_text_format_codec(field)
    assert_writes_match(field, URL_VALUES)


@pytest.mark.parametrize(
    "field",
    [
        make_field(fields.SlugField(), "slug"),
        make_field(fields.SlugField(null=True, allow_unicode=True), "slug"),
        make_field(fields.SlugField(max_length=10, validators=[MinLengthValidator(3)]), "slug"),
    ],
    ids=["plain", "unicode", "validators"],
)
def test_slug_writes_match(field):
    assert_text_format_codec(field)
    assert_writes_match(field, SLUG_VALUES)


def test_a_validator_with_a_message_is_checked_by_the_field():
    field = make_field(fields.SlugField(validators=[MinLengthValidator(3, message="too short")]), "slug")
    _codec_type, options = FieldCodecs.get_write_specification(field, CONNECTIONS[0][1], None)
    assert options["validate"] == field.validate
    assert_writes_match(field, SLUG_VALUES)


@pytest.mark.parametrize("null", [False, True])
def test_phone_without_phonenumbers_writes_match(monkeypatch, null):
    monkeypatch.setattr(PhoneField, "phonenumbers", None)
    monkeypatch.setattr(PhoneField, "phonenumbers_looked_up", True)
    field = make_field(fields.PhoneField(null=null), "phone")
    assert_text_format_codec(field)
    assert_writes_match(field, PHONE_VALUES)


def test_phone_with_phonenumbers_is_written_by_the_field():
    field = make_field(fields.PhoneField(region="US"), "phone")
    for _connection_name, types, _native_types in CONNECTIONS:
        codec_type, _options = FieldCodecs.get_write_specification(field, types, None)
        assert codec_type is WriteCodecType.CALL
    assert_writes_match(field, [*PHONE_VALUES, "(650) 253-0000", "+44 20 7946 0958"])


def test_a_subclass_converting_otherwise_is_written_by_the_field():
    class TrimmedEmailField(fields.EmailField):
        def get_normalized_address(self, address: str) -> str:
            return super().get_normalized_address(address.strip())

    field = make_field(TrimmedEmailField(), "email")
    codec_type, _options = FieldCodecs.get_write_specification(field, CONNECTIONS[0][1], None)
    assert codec_type is WriteCodecType.CALL
    assert_writes_match(field, [" a@Example.com "])


def refuse_fallback(value: Any, instance: Any) -> Any:
    raise AssertionError(f"{value!r} was left to the field")


@pytest.mark.parametrize(
    ("field", "values"),
    [
        (fields.EmailField(), {"a@example.com": "a@example.com", "Ann+x@Example.COM": "Ann+x@example.com"}),
        (fields.EmailField(lowercase=True), {"Ann@Example.COM": "ann@example.com"}),
        (
            fields.URLField(),
            {"https://Example.com:8080/a?b#c": "https://Example.com:8080/a?b#c", "http://10.0.0.1": "http://10.0.0.1"},
        ),
        (fields.SlugField(allow_unicode=True), {"my-first_post-2": "my-first_post-2"}),
    ],
    ids=["email", "lowercase", "url", "slug"],
)
def test_a_valid_value_is_written_by_the_codec_itself(field, values):
    field = make_field(field, "value")
    codec_type, options = FieldCodecs.get_write_specification(field, CONNECTIONS[0][1], None)
    codec = native_rows.FieldCodec(
        "value", ReadCodecType.AS_IS, {}, codec_type, {**options, "fallback": refuse_fallback}
    )
    for value, written in values.items():
        assert codec.write(value, None) == written
        if value == written:
            assert codec.write(value, None) is value


def test_an_e164_number_is_written_by_the_codec_itself(monkeypatch):
    monkeypatch.setattr(PhoneField, "phonenumbers", None)
    monkeypatch.setattr(PhoneField, "phonenumbers_looked_up", True)
    field = make_field(fields.PhoneField(), "phone")
    codec_type, options = FieldCodecs.get_write_specification(field, CONNECTIONS[0][1], None)
    codec = native_rows.FieldCodec(
        "phone", ReadCodecType.AS_IS, {}, codec_type, {**options, "fallback": refuse_fallback}
    )
    number = "+16502530000"
    assert codec.write(number, None) is number
