WIDGET_INITIAL_MIGRATION = """from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("name", fields.CharField(max_length=50)),
            ],
            options={"table": "widget"},
        ),
    ]
"""
