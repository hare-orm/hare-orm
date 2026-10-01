from decimal import Decimal

import pytest

from hare.contrib.test import requires_features
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
from hare.query.expressions import F
from hare.query.functions import Concat
from tests.testmodels import ValidatorModel


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


def test_max_digits_validator_rejects_none():
    with pytest.raises(ValidationError, match="Value must not be None"):
        MaxDigitsValidator(3, 0)(None)


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


@pytest.mark.asyncio
async def test_validator_regex(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(regex="ccc")
    await ValidatorModel.objects.create(regex="abcd")


@pytest.mark.asyncio
async def test_validator_max_length(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(max_length="aaaaaa")
    await ValidatorModel.objects.create(max_length="aaaaa")


@pytest.mark.asyncio
async def test_validator_min_length(db):
    with pytest.raises(ValidationError, match="Length of 'aa' 2 < 3"):
        await ValidatorModel.objects.create(min_length="aa")
    await ValidatorModel.objects.create(min_length="aaaa")


@pytest.mark.asyncio
async def test_validator_min_value(db):
    # min value is 10
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(min_value=9)
    await ValidatorModel.objects.create(min_value=10)

    # min value is Decimal("1.0")
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(min_value_decimal=Decimal("0.9"))
    await ValidatorModel.objects.create(min_value_decimal=Decimal("1.0"))


@pytest.mark.asyncio
async def test_validator_max_value(db):
    # max value is 20
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(max_value=21)
    await ValidatorModel.objects.create(max_value=20)

    # max value is Decimal("2.0")
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(max_value_decimal=Decimal("3.0"))
    await ValidatorModel.objects.create(max_value_decimal=Decimal("2.0"))


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


@pytest.mark.asyncio
async def test_validator_ipv4(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(ipv4="aaaaaa")
    await ValidatorModel.objects.create(ipv4="8.8.8.8")


@pytest.mark.asyncio
async def test_validator_ipv6(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(ipv6="aaaaaa")
    await ValidatorModel.objects.create(ipv6="::")


@pytest.mark.asyncio
async def test_validator_ipv46(db):
    with pytest.raises(ValidationError, match="'aaaaaa' is not a valid IPv4 or IPv6 address."):
        await ValidatorModel.objects.create(ipv46="aaaaaa")
    await ValidatorModel.objects.create(ipv46="::")
    await ValidatorModel.objects.create(ipv46="8.8.8.8")


@pytest.mark.asyncio
async def test_validator_comma_separated_integer_list(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(comma_separated_integer_list="aaaaaa")
    await ValidatorModel.objects.create(comma_separated_integer_list="1,2,3")


@pytest.mark.asyncio
async def test_prevent_saving(db):
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.create(min_value_decimal=Decimal("0.9"))

    assert await ValidatorModel.objects.all().count() == 0


@pytest.mark.asyncio
async def test_save(db):
    with pytest.raises(ValidationError):
        record = ValidatorModel(min_value_decimal=Decimal("0.9"))
        await record.save()

    record.min_value_decimal = Decimal("1.5")
    await record.save()


@pytest.mark.asyncio
async def test_save_with_update_fields(db):
    record = await ValidatorModel.objects.create(min_value_decimal=Decimal("2"))

    record.min_value_decimal = Decimal("0.9")
    with pytest.raises(ValidationError):
        await record.save(update_fields=["min_value_decimal"])


@pytest.mark.asyncio
async def test_update(db):
    record = await ValidatorModel.objects.create(min_value_decimal=Decimal("2"))

    record.min_value_decimal = Decimal("0.9")
    with pytest.raises(ValidationError):
        await record.save()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_save_with_f_expression_bypassing_validators_raises_validation_error(db):
    """An F()-expression's SQL-side arithmetic (Concat here) used to bypass Field.validate()
    entirely, on every dialect - only IntField/DecimalField on SQLite got a narrow validation
    pass (round BF numeric-range fix). A CharField's IPv4Validator (a validator no DB column
    type backs - unlike CharField's own automatic MaxLengthValidator, which Postgres's VARCHAR(n)
    already happens to enforce natively) must reject an F()-expression's result too on
    Model.save(), not just a plain assigned value, on every backend."""
    record = await ValidatorModel.objects.create(ipv4="8.8.8.8")
    record.ipv4 = Concat("ipv4", "x")
    with pytest.raises(ValidationError):
        await record.save()
    fresh = await ValidatorModel.objects.get(pk=record.pk)
    assert fresh.ipv4 == "8.8.8.8"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_queryset_update_with_f_expression_bypassing_validators_raises_validation_error(db):
    """Same gap as the Model.save() case above, for QuerySet.update()."""
    record = await ValidatorModel.objects.create(ipv4="8.8.8.8")
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.filter(pk=record.pk).update(ipv4=Concat("ipv4", "x"))
    fresh = await ValidatorModel.objects.get(pk=record.pk)
    assert fresh.ipv4 == "8.8.8.8"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_queryset_update_with_f_expression_bypassing_validators_rolls_back_whole_update(db):
    """A single `.update()` can match several rows in one statement - the whole statement must
    roll back the moment ANY row's computed value fails validation, not just the one row that
    actually fails, mirroring the existing numeric round BF behavior for IntField/DecimalField.
    min_value's custom MinValueValidator(10) bound is never enforced at the column-type level on
    ANY backend (unlike IntField's own int32 bounds, which Postgres's INT4 column type already
    rejects on its own) - so unlike round BF's numeric-range fix (SQLite only, since only SQLite
    lacked a native column type backing IntField's own bounds), this one must hold on Postgres
    too."""
    in_range = await ValidatorModel.objects.create(min_value=20)
    overflowing = await ValidatorModel.objects.create(min_value=12)
    with pytest.raises(ValidationError):
        await ValidatorModel.objects.filter(pk__in=[in_range.pk, overflowing.pk]).update(min_value=F("min_value") - 5)
    refreshed_in_range = await ValidatorModel.objects.get(pk=in_range.pk)
    refreshed_overflowing = await ValidatorModel.objects.get(pk=overflowing.pk)
    assert refreshed_in_range.min_value == 20
    assert refreshed_overflowing.min_value == 12


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


def test_domain_name_validator_invalid_idn_disabled():
    validator = DomainNameValidator(accept_idna=False)
    with pytest.raises(InvalidDomainName):
        validator("münchen.de")


@pytest.mark.parametrize("value", ["example.com\n", "example.com\n\n", "exa\nmple.com"])
def test_domain_name_validator_rejects_embedded_newline(value):
    with pytest.raises(InvalidDomainName):
        validate_domain_name(value)


def test_regex_validator_rejects_none():
    validator = RegexValidator(r"^\d+$", 0)
    with pytest.raises(ValidationError, match="Value must not be None"):
        validator(None)


def test_domain_name_validator_rejects_none():
    with pytest.raises(InvalidDomainName):
        validate_domain_name(None)


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


def test_url_validator_max_length():
    long_url = "http://example.com/" + "a" * 2100
    with pytest.raises(InvalidURL):
        validate_url(long_url)


def test_url_validator_rejects_none():
    with pytest.raises(InvalidURL):
        validate_url(None)


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


def test_email_validator_valid_allowed_domains():
    validator = EmailValidator(allowed_domains=["example.com", "test.com"])
    validator("user@example.com")
    validator("user@test.com")


def test_email_validator_invalid_allowed_domains():
    validator = EmailValidator(allowed_domains=["example.com"])
    validator("user@example.com")
    with pytest.raises(InvalidEmailAddress):
        validator("user@")
    with pytest.raises(InvalidEmailAddress):
        validator("user@invalid..com")


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


def test_email_validator_rejects_none():
    with pytest.raises(InvalidEmailAddress):
        validate_email(None)


def test_regex_validator_custom_message():
    validator = RegexValidator(r"^\d+$", 0, message="custom regex message")
    with pytest.raises(ValidationError, match="^custom regex message$"):
        validator("abc")


def test_regex_validator_default_message_unchanged():
    validator = RegexValidator(r"^\d+$", 0)
    with pytest.raises(ValidationError, match=r"^Value 'abc' does not match regex '\^\\d\+\$'$"):
        validator("abc")


def test_max_length_validator_custom_message():
    validator = MaxLengthValidator(3, message="too long")
    with pytest.raises(ValidationError, match="^too long$"):
        validator("abcdef")


def test_max_length_validator_default_message_unchanged():
    validator = MaxLengthValidator(3)
    with pytest.raises(ValidationError, match=r"^Length of 'abcdef' 6 > 3$"):
        validator("abcdef")


def test_min_length_validator_custom_message():
    validator = MinLengthValidator(3, message="too short")
    with pytest.raises(ValidationError, match="^too short$"):
        validator("a")


def test_min_length_validator_default_message_unchanged():
    validator = MinLengthValidator(3)
    with pytest.raises(ValidationError, match=r"^Length of 'a' 1 < 3$"):
        validator("a")


def test_min_value_validator_custom_message():
    validator = MinValueValidator(10, message="value too small")
    with pytest.raises(ValidationError, match="^value too small$"):
        validator(5)


def test_min_value_validator_default_message_unchanged():
    validator = MinValueValidator(10)
    with pytest.raises(ValidationError, match=r"^Value should be greater or equal to 10$"):
        validator(5)


def test_max_value_validator_custom_message():
    validator = MaxValueValidator(10, message="value too large")
    with pytest.raises(ValidationError, match="^value too large$"):
        validator(20)


def test_max_value_validator_default_message_unchanged():
    validator = MaxValueValidator(10)
    with pytest.raises(ValidationError, match=r"^Value should be less or equal to 10$"):
        validator(20)


def test_max_digits_validator_custom_message():
    validator = MaxDigitsValidator(3, 0, message="too many digits")
    with pytest.raises(ValidationError, match="^too many digits$"):
        validator(Decimal("1234"))


def test_max_digits_validator_default_message_unchanged():
    validator = MaxDigitsValidator(3, 0)
    with pytest.raises(ValidationError, match=r"^Value '1234' has 4 digits, more than max_digits=3$"):
        validator(Decimal("1234"))


def test_comma_separated_integer_list_validator_custom_message():
    validator = CommaSeparatedIntegerListValidator(message="not a valid list")
    with pytest.raises(ValidationError, match="^not a valid list$"):
        validator("aaaaaa")


def test_comma_separated_integer_list_validator_default_message_unchanged():
    validator = CommaSeparatedIntegerListValidator()
    with pytest.raises(ValidationError, match=r"^Value 'aaaaaa' does not match regex"):
        validator("aaaaaa")


def test_domain_name_validator_custom_message():
    validator = DomainNameValidator(message="not a valid domain")
    with pytest.raises(InvalidDomainName, match="^not a valid domain$"):
        validator("---.com")


def test_domain_name_validator_default_message_unchanged():
    with pytest.raises(InvalidDomainName, match="^Invalid domain name$"):
        validate_domain_name("---.com")


def test_url_validator_custom_message():
    validator = URLValidator(message="not a valid url")
    with pytest.raises(InvalidURL, match="^not a valid url$"):
        validator("not-a-url")


def test_url_validator_default_message_unchanged():
    with pytest.raises(InvalidURL, match="^Invalid URL$"):
        validate_url("http://256.1.1.1")


def test_url_validator_invalid_scheme_custom_message():
    validator = URLValidator(allowed_schemes=["http", "https"], message="scheme not allowed")
    with pytest.raises(InvalidScheme, match="^scheme not allowed$"):
        validator("ftp://example.com")


def test_url_validator_invalid_scheme_default_message_unchanged():
    validator = URLValidator(allowed_schemes=["http", "https"])
    with pytest.raises(InvalidScheme, match="^Invalid scheme: ftp is not allowed$"):
        validator("ftp://example.com")


def test_email_validator_custom_message():
    validator = EmailValidator(message="not a valid email")
    with pytest.raises(InvalidEmailAddress, match="^not a valid email$"):
        validator("not-an-email")


def test_email_validator_default_message_unchanged():
    with pytest.raises(InvalidEmailAddress, match="^Invalid email address$"):
        validate_email("not-an-email")
