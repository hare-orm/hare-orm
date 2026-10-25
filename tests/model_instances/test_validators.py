from decimal import Decimal

import pytest

from hare.exceptions import ValidationError
from hare.fields.data.text import CharField
from hare.fields.validators import (
    CommaSeparatedIntegerListValidator,
    DomainNameValidator,
    EmailValidator,
    InvalidDomainName,
    InvalidEmailAddress,
    InvalidScheme,
    InvalidURL,
    IPv4Validator,
    IPv6Validator,
    IPv46Validator,
    MaxDigitsValidator,
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
    URLValidator,
    validate_domain_name,
    validate_email,
    validate_url,
)
from tests.shared_models.testmodels_postgres import ValidatorModel


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "max_digits, decimal_places, value",
    [
        (3, 0, Decimal("123")),
        (5, 2, Decimal("123.45")),
        (5, 2, Decimal("1.5")),  # fewer decimal digits than decimal_places is fine
        (3, 0, Decimal("0")),
        (1, 0, Decimal("-9")),  # sign doesn't count as a digit
    ],
)
def test_max_digits_validator_allows_value_within_bounds(max_digits, decimal_places, value):
    MaxDigitsValidator(max_digits, decimal_places)(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "max_digits, decimal_places, value",
    [
        (3, 0, Decimal("1234")),  # too many total digits
        (5, 2, Decimal("123.456")),  # too many decimal digits
        (5, 2, Decimal("1234.5")),  # whole part alone exceeds max_digits - decimal_places
        (2, 0, Decimal("100")),
    ],
)
def test_max_digits_validator_rejects_value_out_of_bounds(max_digits, decimal_places, value):
    with pytest.raises(ValidationError):
        MaxDigitsValidator(max_digits, decimal_places)(value)


@pytest.mark.database_independent
def test_max_digits_validator_rejects_none():
    with pytest.raises(ValidationError, match="Value must not be None"):
        MaxDigitsValidator(3, 0)(None)


@pytest.mark.database_independent
def test_field_validate_wraps_non_validation_error_exception_from_custom_validator():
    """Field.validate() used to catch only ValidationError, letting any other exception a
    plain callable validator raises (docs explicitly allow "a plain callable works too") escape
    unwrapped - breaking the single catchable-ValidationError contract the rest of the framework
    relies on (see Field.to_db_value()'s own identical wrapping of ValueError/InvalidOperation)."""

    def raise_type_error(value):
        raise TypeError("boom")

    field = CharField(max_length=10, validators=[raise_type_error])
    with pytest.raises(ValidationError, match="boom") as exc_info:
        field.validate("hello")
    assert isinstance(exc_info.value.__cause__, TypeError)


@pytest.mark.postgresql_models
@pytest.mark.parametrize(
    ("field_name", "bad_value", "good_values", "message"),
    [
        pytest.param("regex", "ccc", ("abcd",), None, id="validator_regex"),
        pytest.param("max_length", "aaaaaa", ("aaaaa",), None, id="validator_max_length"),
        pytest.param("min_length", "aa", ("aaaa",), "Length of 'aa' 2 < 3", id="validator_min_length"),
        # min value is 10, Decimal("1.0"); max value is 20, Decimal("2.0")
        pytest.param("min_value", 9, (10,), None, id="validator_min_value"),
        pytest.param("min_value_decimal", Decimal("0.9"), (Decimal("1.0"),), None, id="validator_min_value_decimal"),
        pytest.param("max_value", 21, (20,), None, id="validator_max_value"),
        pytest.param("max_value_decimal", Decimal("3.0"), (Decimal("2.0"),), None, id="validator_max_value_decimal"),
        pytest.param("ipv4", "aaaaaa", ("8.8.8.8",), None, id="validator_ipv4"),
        pytest.param("ipv6", "aaaaaa", ("::",), None, id="validator_ipv6"),
        pytest.param(
            "ipv46",
            "aaaaaa",
            ("::", "8.8.8.8"),
            "'aaaaaa' is not a valid IPv4 or IPv6 address.",
            id="validator_ipv46",
        ),
        pytest.param(
            "comma_separated_integer_list", "aaaaaa", ("1,2,3",), None, id="validator_comma_separated_integer_list"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_field_validator_refuses_a_bad_value_on_create(db, field_name, bad_value, good_values, message):
    with pytest.raises(ValidationError, match=message):
        await ValidatorModel.objects.create(**{field_name: bad_value})
    for good_value in good_values:
        await ValidatorModel.objects.create(**{field_name: good_value})


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_validator_min_max_value_enforced_by_bulk_create_and_bulk_update(db):
    """IntField.__init__ always appends its own int32-range MinValueValidator/MaxValueValidator
    to field.validators, after any user-supplied ones - min_value/max_value above end up with
    TWO MinValueValidator/MaxValueValidator instances each. The Rust-serialize fast path
    (RustSerializeExecutorMixin._build_rust_serialize_plan_cached, bucket 0) used to collapse
    these to a single (min_value, max_value) pair by letting the last validator iterated win,
    which silently discarded the user's tighter bound in favor of IntField's own wide int32
    range - bulk_create()/bulk_update() accepted values create()/save() correctly reject."""
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.bulk_create([ValidatorModel(min_value=9)])
    assert await ValidatorModel.objects.all().count() == 0
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.bulk_create([ValidatorModel(max_value=21)])
    assert await ValidatorModel.objects.all().count() == 0

    record = await ValidatorModel.objects.create(min_value=10, max_value=20)
    record.min_value = 9
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.bulk_update([record], fields=["min_value"])
    record.min_value = 10
    record.max_value = 21
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.bulk_update([record], fields=["max_value"])


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_prevent_saving(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(min_value_decimal=Decimal("0.9"))

    assert await ValidatorModel.objects.all().count() == 0


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_save(db):
    with pytest.raises(ValidationError):
        record = ValidatorModel(min_value_decimal=Decimal("0.9"))
        await record.save()

    record.min_value_decimal = Decimal("1.5")
    await record.save()


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_save_with_update_fields(db):
    record = await ValidatorModel.objects.create(min_value_decimal=Decimal("2"))

    record.min_value_decimal = Decimal("0.9")
    with pytest.raises(ValidationError):
        await record.save(update_fields=["min_value_decimal"])


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_update(db):
    record = await ValidatorModel.objects.create(min_value_decimal=Decimal("2"))

    record.min_value_decimal = Decimal("0.9")
    with pytest.raises(ValidationError):
        await record.save()


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "example.com",
        "sub.example.com",
        "example.co.uk",
        "münchen.de",
        "sub1.sub2.example.org",
        "UPPER-CASE.is.ok.net",
        "hare.github.io",
        "example.space",
        "❤️.website",
    ],
)
def test_domain_name_validator_valid(value):
    validate_domain_name(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "",
        "---.com",
        "example-.com",
        "under_line.com",
        "💻.tech",
    ],
)
def test_domain_name_validator_invalid(value):
    with pytest.raises(InvalidDomainName):
        validate_domain_name(value)


@pytest.mark.database_independent
def test_domain_name_validator_invalid_idn_disabled():
    validator = DomainNameValidator(accept_idna=False)
    with pytest.raises(InvalidDomainName):
        validator("münchen.de")


@pytest.mark.database_independent
@pytest.mark.parametrize("value", ["example.com\n", "example.com\n\n", "exa\nmple.com"])
def test_domain_name_validator_rejects_embedded_newline(value):
    with pytest.raises(InvalidDomainName):
        validate_domain_name(value)


@pytest.mark.database_independent
def test_regex_validator_rejects_none():
    validator = RegexValidator(r"^\d+$", 0)
    with pytest.raises(ValidationError, match="Value must not be None"):
        validator(None)


@pytest.mark.database_independent
def test_domain_name_validator_rejects_none():
    with pytest.raises(InvalidDomainName):
        validate_domain_name(None)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://www.example.com/path?query=1",
        "ftp://ftp.example.com/file.txt",
        "http://localhost:8080",
        "http://192.168.1.1",
        "http://8.8.8.8:8080",
        "https://[::1]",
        "https://[2001:db8::1]:443",
        "http://user:pass@example.com",
        "http://example.com#fragment",
    ],
)
def test_url_validator_valid(value):
    validate_url(value)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://example.com",
    ],
)
def test_url_validator_valid_custom_schemes(value):
    validator = URLValidator(allowed_schemes=["http", "https"])
    validator(value)


@pytest.mark.database_independent
def test_url_validator_invalid_scheme():
    validator = URLValidator(allowed_schemes=["http", "https"])
    with pytest.raises(InvalidScheme):
        validator("ftp://example.com")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-url",
        "http://",
        "http:// space.com",
        "http://[::gggg]",
        "http://256.1.1.1",
        "http://" + "a" * 254 + ".com",
    ],
)
def test_url_validator_invalid(value):
    with pytest.raises(InvalidURL):
        validate_url(value)


@pytest.mark.database_independent
def test_url_validator_max_length():
    long_url = "http://example.com/" + "a" * 2100
    with pytest.raises(InvalidURL):
        validate_url(long_url)


@pytest.mark.database_independent
def test_url_validator_rejects_none():
    with pytest.raises(InvalidURL):
        validate_url(None)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "user@example.com",
        "user.name@example.com",
        "user+tag@example.co.uk",
        "user@sub.domain.com",
        "user@[192.168.1.1]",
        "user@[::1]",
        "a+b@example.com",
        "a-b@example.com",
        "a_b@example.com",
        "test@test.co.uk",
    ],
)
def test_email_validator_valid(value):
    validate_email(value)


@pytest.mark.database_independent
def test_email_validator_valid_allowed_domains():
    validator = EmailValidator(allowed_domains=["example.com", "test.com"])
    validator("user@example.com")
    validator("user@test.com")


@pytest.mark.database_independent
def test_email_validator_invalid_allowed_domains():
    validator = EmailValidator(allowed_domains=["example.com"])
    validator("user@example.com")
    with pytest.raises(InvalidEmailAddress):
        validator("user@")
    with pytest.raises(InvalidEmailAddress):
        validator("user@invalid..com")


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-email",
        "user@",
        "@example.com",
        "user@.com",
        "user@com.",
        "user@com..com",
        "a" * 330 + "@example.com",
    ],
)
def test_email_validator_invalid(value):
    with pytest.raises(InvalidEmailAddress):
        validate_email(value)


@pytest.mark.database_independent
def test_email_validator_rejects_none():
    with pytest.raises(InvalidEmailAddress):
        validate_email(None)


@pytest.mark.parametrize(
    ("validate", "value", "exception_class", "message"),
    [
        pytest.param(
            RegexValidator(r"^\d+$", 0, message="custom regex message"),
            "abc",
            ValidationError,
            "^custom regex message$",
            id="regex_validator_custom_message",
        ),
        pytest.param(
            RegexValidator(r"^\d+$", 0),
            "abc",
            ValidationError,
            r"^Value 'abc' does not match regex '\^\\d\+\$'$",
            id="regex_validator_default_message_unchanged",
        ),
        pytest.param(
            MaxLengthValidator(3, message="too long"),
            "abcdef",
            ValidationError,
            "^too long$",
            id="max_length_validator_custom_message",
        ),
        pytest.param(
            MaxLengthValidator(3),
            "abcdef",
            ValidationError,
            r"^Length of 'abcdef' 6 > 3$",
            id="max_length_validator_default_message_unchanged",
        ),
        pytest.param(
            MinLengthValidator(3, message="too short"),
            "a",
            ValidationError,
            "^too short$",
            id="min_length_validator_custom_message",
        ),
        pytest.param(
            MinLengthValidator(3),
            "a",
            ValidationError,
            r"^Length of 'a' 1 < 3$",
            id="min_length_validator_default_message_unchanged",
        ),
        pytest.param(
            MinValueValidator(10, message="value too small"),
            5,
            ValidationError,
            "^value too small$",
            id="min_value_validator_custom_message",
        ),
        pytest.param(
            MinValueValidator(10),
            5,
            ValidationError,
            r"^Value should be greater or equal to 10$",
            id="min_value_validator_default_message_unchanged",
        ),
        pytest.param(
            MaxValueValidator(10, message="value too large"),
            20,
            ValidationError,
            "^value too large$",
            id="max_value_validator_custom_message",
        ),
        pytest.param(
            MaxValueValidator(10),
            20,
            ValidationError,
            r"^Value should be less or equal to 10$",
            id="max_value_validator_default_message_unchanged",
        ),
        pytest.param(
            MaxDigitsValidator(3, 0, message="too many digits"),
            Decimal("1234"),
            ValidationError,
            "^too many digits$",
            id="max_digits_validator_custom_message",
        ),
        pytest.param(
            MaxDigitsValidator(3, 0),
            Decimal("1234"),
            ValidationError,
            r"^Value '1234' has 4 digits, more than max_digits=3$",
            id="max_digits_validator_default_message_unchanged",
        ),
        pytest.param(
            CommaSeparatedIntegerListValidator(message="not a valid list"),
            "aaaaaa",
            ValidationError,
            "^not a valid list$",
            id="comma_separated_integer_list_validator_custom_message",
        ),
        pytest.param(
            CommaSeparatedIntegerListValidator(),
            "aaaaaa",
            ValidationError,
            r"^Value 'aaaaaa' does not match regex",
            id="comma_separated_integer_list_validator_default_message_unchanged",
        ),
        pytest.param(
            DomainNameValidator(message="not a valid domain"),
            "---.com",
            InvalidDomainName,
            "^not a valid domain$",
            id="domain_name_validator_custom_message",
        ),
        pytest.param(
            validate_domain_name,
            "---.com",
            InvalidDomainName,
            "^Invalid domain name$",
            id="domain_name_validator_default_message_unchanged",
        ),
        pytest.param(
            URLValidator(message="not a valid url"),
            "not-a-url",
            InvalidURL,
            "^not a valid url$",
            id="url_validator_custom_message",
        ),
        pytest.param(
            validate_url, "http://256.1.1.1", InvalidURL, "^Invalid URL$", id="url_validator_default_message_unchanged"
        ),
        pytest.param(
            URLValidator(allowed_schemes=["http", "https"], message="scheme not allowed"),
            "ftp://example.com",
            InvalidScheme,
            "^scheme not allowed$",
            id="url_validator_invalid_scheme_custom_message",
        ),
        pytest.param(
            URLValidator(allowed_schemes=["http", "https"]),
            "ftp://example.com",
            InvalidScheme,
            "^Invalid scheme: ftp is not allowed$",
            id="url_validator_invalid_scheme_default_message_unchanged",
        ),
        pytest.param(
            EmailValidator(message="not a valid email"),
            "not-an-email",
            InvalidEmailAddress,
            "^not a valid email$",
            id="email_validator_custom_message",
        ),
        pytest.param(
            validate_email,
            "not-an-email",
            InvalidEmailAddress,
            "^Invalid email address$",
            id="email_validator_default_message_unchanged",
        ),
    ],
)
@pytest.mark.database_independent
def test_the_message_of_a_validator(validate, value, exception_class, message):
    with pytest.raises(exception_class, match=message):
        validate(value)


@pytest.mark.parametrize(
    ("validator_class", "default_message"),
    [
        (IPv4Validator, "^'x' is not a valid IPv4 address.$"),
        (IPv6Validator, "^'x' is not a valid IPv6 address.$"),
        (IPv46Validator, "^'x' is not a valid IPv4 or IPv6 address.$"),
    ],
)
def test_ip_address_validators_take_a_custom_message(validator_class, default_message):
    with pytest.raises(ValidationError, match="^not an address$"):
        validator_class(message="not an address")("x")
    with pytest.raises(ValidationError, match=default_message):
        validator_class()("x")
