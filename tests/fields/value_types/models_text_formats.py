from hare import fields
from hare.fields.validators import MinLengthValidator
from hare.models import Model


class Contact(Model):
    id = fields.IntField(primary_key=True)
    email = fields.EmailField()
    login_email = fields.EmailField(lowercase=True, null=True)
    website = fields.URLField(null=True)
    mirror = fields.URLField(schemes=["ftp", "https"], null=True)
    slug = fields.SlugField(null=True)
    unicode_slug = fields.SlugField(allow_unicode=True, null=True, db_index=False)
    short_slug = fields.SlugField(max_length=10, null=True, validators=[MinLengthValidator(3)])
    phone = fields.PhoneField(null=True)
    us_phone = fields.PhoneField(region="US", null=True)

    class Meta:
        table = "text_format_contact"
