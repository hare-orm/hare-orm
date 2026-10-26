from hare import fields
from hare.models import Model


class RegionalContact(Model):
    id = fields.IntField(primary_key=True)
    phone = fields.PhoneField(null=True)
    us_phone = fields.PhoneField(region="US", null=True)

    class Meta:
        table = "text_format_regional_contact"
