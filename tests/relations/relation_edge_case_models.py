from hare import fields
from hare.models import Model


class EdgeStudent(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    courses = fields.ManyToManyField("models.EdgeCourse", related_name="students", through="models.EdgeEnrollment")
    soft_courses = fields.ManyToManyField(
        "models.EdgeCourse", related_name="soft_students", through="models.EdgeSoftEnrollment"
    )

    class Meta:
        table = "edge_student"


class EdgeCourse(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        table = "edge_course"


class EdgeEnrollment(Model):
    id = fields.IntField(primary_key=True, generated=True)
    student = fields.ForeignKeyField("models.EdgeStudent", related_name="enrollments", source_field="stud_ref")
    course = fields.ForeignKeyField("models.EdgeCourse", related_name="enrollments", source_field="course_ref")
    grade = fields.IntField(null=True)

    class Meta:
        table = "edge_enrollment"


class EdgeSoftEnrollment(Model):
    id = fields.IntField(primary_key=True, generated=True)
    student = fields.ForeignKeyField("models.EdgeStudent", related_name="soft_enrollments")
    course = fields.ForeignKeyField("models.EdgeCourse", related_name="soft_enrollments")
    grade = fields.IntField(null=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "edge_soft_enrollment"
        soft_delete_field = "deleted_at"


class EdgeTarget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "edge_target"


class EdgeHiddenCascade(Model):
    id = fields.IntField(primary_key=True)
    target = fields.ForeignKeyField(
        "models.EdgeTarget", related_name=False, on_delete=fields.CASCADE, db_constraint=False
    )

    class Meta:
        table = "edge_hidden_cascade"


class EdgeHiddenSetNull(Model):
    id = fields.IntField(primary_key=True)
    target = fields.ForeignKeyField(
        "models.EdgeTarget", related_name=False, on_delete=fields.SET_NULL, null=True, db_constraint=False
    )

    class Meta:
        table = "edge_hidden_set_null"


class EdgeHiddenProtect(Model):
    id = fields.IntField(primary_key=True)
    target = fields.ForeignKeyField("models.EdgeTarget", related_name=False, on_delete=fields.PROTECT, null=True)

    class Meta:
        table = "edge_hidden_protect"


class EdgeHiddenOneToOne(Model):
    id = fields.IntField(primary_key=True)
    target = fields.OneToOneField(
        "models.EdgeTarget", related_name=False, on_delete=fields.CASCADE, null=True, db_constraint=False
    )

    class Meta:
        table = "edge_hidden_one_to_one"


class EdgeCycleFirst(Model):
    id = fields.IntField(primary_key=True)
    other = fields.ForeignKeyField(
        "models.EdgeCycleSecond", related_name="firsts", on_delete=fields.CASCADE, null=True
    )

    class Meta:
        table = "edge_cycle_first"


class EdgeCycleSecond(Model):
    id = fields.IntField(primary_key=True)
    other = fields.ForeignKeyField(
        "models.EdgeCycleFirst", related_name="seconds", on_delete=fields.CASCADE, null=True, db_constraint=False
    )

    class Meta:
        table = "edge_cycle_second"


class EdgeChainTop(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "edge_chain_top"


class EdgeChainMiddle(Model):
    id = fields.IntField(primary_key=True)
    top = fields.ForeignKeyField(
        "models.EdgeChainTop", related_name="middles", on_delete=fields.CASCADE, db_constraint=False
    )

    class Meta:
        table = "edge_chain_middle"


class EdgeChainLower(Model):
    id = fields.IntField(primary_key=True)
    middle = fields.ForeignKeyField("models.EdgeChainMiddle", related_name="lowers", on_delete=fields.CASCADE)

    class Meta:
        table = "edge_chain_lower"


class EdgeChainBottom(Model):
    id = fields.IntField(primary_key=True)
    lower = fields.ForeignKeyField(
        "models.EdgeChainLower", related_name="bottoms", on_delete=fields.CASCADE, db_constraint=False
    )

    class Meta:
        table = "edge_chain_bottom"


class EdgeTreeNode(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.EdgeTreeNode", related_name="children", on_delete=fields.CASCADE, null=True, db_constraint=False
    )

    class Meta:
        table = "edge_tree_node"


class EdgeSoftTreeNode(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField(
        "models.EdgeSoftTreeNode", related_name="children", on_delete=fields.CASCADE, null=True
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "edge_soft_tree_node"
        soft_delete_field = "deleted_at"


class EdgePublisher(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    code = fields.CharField(max_length=20, unique=True)

    class Meta:
        table = "edge_publisher"


class EdgeAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    publisher = fields.ForeignKeyField(
        "models.EdgePublisher", related_name="authors", null=True, on_delete=fields.SET_NULL
    )

    class Meta:
        table = "edge_author"


class EdgeBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author = fields.ForeignKeyField("models.EdgeAuthor", related_name="books", null=True, on_delete=fields.SET_NULL)
    publisher = fields.ForeignKeyField(
        "models.EdgePublisher", related_name="books_by_code", to_field="code", null=True, on_delete=fields.CASCADE
    )

    class Meta:
        table = "edge_book"


class EdgeProfile(Model):
    id = fields.IntField(primary_key=True)
    bio = fields.CharField(max_length=50)
    author = fields.OneToOneField("models.EdgeAuthor", related_name="profile", null=True, on_delete=fields.CASCADE)

    class Meta:
        table = "edge_profile"


class EdgeTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "edge_tag"


class EdgePost(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    tags = fields.ManyToManyField("models.EdgeTag", related_name="posts")
    rich_tags = fields.ManyToManyField("models.EdgeTag", related_name="rich_posts", through="models.EdgePostTag")

    class Meta:
        table = "edge_post"


class EdgePostTag(Model):
    id = fields.IntField(primary_key=True, generated=True)
    post = fields.ForeignKeyField("models.EdgePost", related_name="post_tag_rows")
    tag = fields.ForeignKeyField("models.EdgeTag", related_name="tag_post_rows")
    weight = fields.IntField(default=1)
    note = fields.CharField(max_length=50, null=True)

    class Meta:
        table = "edge_post_tag"
