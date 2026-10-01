from __future__ import annotations

import ast
import inspect
import linecache
import re
import sys
from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.cache import Cache
from hare.exceptions import ConfigurationError, DoesNotExist, ValidationError
from hare.fields.base.field import Field
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.data.numeric.int_field import IntField
from hare.fields.generated import GeneratedField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.constants import ADDITIVE_ABSTRACT_META_KEYS, FIELD_COMMENT_RE, STATEMENT_BLOCK_FIELDS
from hare.models.enums import ModelOption
from hare.models.meta_info import MetaInfo
from hare.query.manager import Manager
from hare.query.queryset import QuerySetSingle

if TYPE_CHECKING:
    from hare.models.model import Model

TModel = TypeVar("TModel", bound="Model")


class ModelMeta(type):
    __slots__ = ()

    #: (source file,) -> the file's lines and the last line of each class in it by the class's first
    #: line (``_get_class_spans()``).
    source_class_spans: ClassVar[Cache[tuple[list[str], dict[int, int]]]] = Cache(Cache.max_size_from_env())
    #: () -> the names a model field may not use (``get_reserved_field_names()``).
    reserved_field_names: ClassVar[Cache[frozenset[str]]] = Cache(Cache.max_size_from_env())

    def __new__(cls, name: str, bases: tuple[type, ...], attrs: dict[str, Any]) -> ModelMeta:
        fields_db_projection: dict[str, str] = {}
        meta_class: type[Model.Meta] = attrs.get("Meta", type("Meta", (), {}))
        pk_attr: str | tuple[str, ...] = "id"

        # The Meta attributes of abstract ancestors are merged into this class's own Meta - except
        # `abstract`, which would make every subclass abstract.
        #
        # canonical_mro is the C3 order of the ancestors, nearest first. In a diamond, a base
        # overriding a value wins over a sibling that only passes the common ancestor's value
        # through, whatever their order in `bases`.
        canonical_mro = cls._linearize_mro(bases)
        inherited_meta_attrs: dict[str, Any] = {}
        for ancestor in canonical_mro:
            ancestor_meta = ancestor.__dict__.get("Meta")
            if ancestor_meta is None or not getattr(ancestor_meta, ModelOption.ABSTRACT, False):
                # Only an abstract ancestor's Meta is inherited - a concrete one's table would map
                # two models onto one table.
                continue
            for key, value in vars(ancestor_meta).items():
                if key.startswith("__") or key in ("abstract", *ADDITIVE_ABSTRACT_META_KEYS):
                    # ADDITIVE_ABSTRACT_META_KEYS are unioned across sibling bases, not resolved
                    # by nearest-wins - handled separately below.
                    continue
                # canonical_mro is nearest-ancestor-first, so the first (nearest) ancestor whose
                # OWN Meta genuinely declares `key` wins - `key not in inherited_meta_attrs` here
                # means exactly "no nearer ancestor has already claimed this name".
                if key not in inherited_meta_attrs:
                    inherited_meta_attrs[key] = value

        # Additive keys are collected per base and concatenated across bases. Within one base's own
        # ancestor chain a redeclared key still replaces the ancestor's value.
        additive_values_by_key: dict[str, list[Any]] = {}
        for base in bases:
            base_meta_attrs: dict[str, Any] = {}
            for ancestor in reversed(base.__mro__):
                ancestor_meta = ancestor.__dict__.get("Meta")
                if ancestor_meta is None or not getattr(ancestor_meta, ModelOption.ABSTRACT, False):
                    continue
                for key, value in vars(ancestor_meta).items():
                    if key not in ADDITIVE_ABSTRACT_META_KEYS:
                        continue
                    base_meta_attrs[key] = value
            for key, value in base_meta_attrs.items():
                if not value:
                    continue
                entries = additive_values_by_key.setdefault(key, [])
                entries.extend(item for item in value if item not in entries)
        for key, entries in additive_values_by_key.items():
            # Deep-copied per concrete class - an Index resolves its expressions against the first
            # model asking. Copied after the merge, so an entry reached through two bases of a
            # diamond is kept once.
            copied_entries = [deepcopy(entry) for entry in entries]
            inherited_meta_attrs[key] = copied_entries if key == ModelOption.INDEXES else tuple(copied_entries)
        if inherited_meta_attrs:
            own_meta_attrs = {k: v for k, v in vars(meta_class).items() if not k.startswith("__")}
            meta_class = type("Meta", (), {**inherited_meta_attrs, **own_meta_attrs})

        # The fields of the bases, base by base in the order given - this sets the column order:
        # ancestor fields first. A diamond's value is corrected right after, in place.
        inherited_attrs: dict[str, Any] = {}
        for base in bases:
            cls._search_for_field_attributes(base, inherited_attrs, name)
        cls._apply_diamond_field_precedence(canonical_mro, inherited_attrs)
        # The names this class's own body declares, taken before `attrs` is rebound - an own
        # override, as opposed to a name only inherited.
        own_attrs_keys = frozenset(attrs)
        if inherited_attrs:
            # Ensure that the inherited fields are before the defined ones.
            attrs = {**inherited_attrs, **attrs}
        is_abstract = getattr(meta_class, "abstract", False)
        if name != "Model":
            attrs, pk_attr = cls._parse_custom_pk(
                attrs, pk_attr, name, is_abstract, cls._declares_no_primary_key(meta_class, name)
            )
        fields_map, fk_fields, m2m_fields, o2o_fields = cls._dispatch_fields(attrs, fields_db_projection, is_abstract)
        if name != "Model":
            cls._check_field_name_conflicts(fields_map, name)

        # The fields this class's own body declares or overrides, plus a synthesized `id` - a
        # subclass tells them from fields only passed through.
        own_field_names = frozenset(key for key in fields_map if key in own_attrs_keys or key not in inherited_attrs)

        # Clean the class attributes
        for slot in fields_map:
            attrs.pop(slot, None)
        attrs["_meta"] = meta = cls.build_meta(
            meta_class,
            fields_map,
            fields_db_projection,
            fk_fields,
            o2o_fields,
            m2m_fields,
            pk_attr,
            own_field_names,
        )
        attrs["objects"] = meta.manager = cls._get_default_manager(
            name, attrs, "objects" in own_attrs_keys, meta_class, meta.manager
        )

        new_class = super().__new__(cls, name, bases, attrs)
        for field in meta.fields_map.values():
            field.model = new_class  # type: ignore[assignment]
            if isinstance(field, GeneratedField):
                # A GeneratedField's output_field is a field of its own, not in fields_map - bound
                # here.
                field.output_field.model = new_class  # type: ignore[assignment]

        # A class without fields - the base Model, a mixin - has no comment to read.
        if fields_map and not attrs.get("_no_comments"):
            for fname, comment in cls._get_comments(new_class).items():  # type: ignore[arg-type]
                if fname in fields_map:
                    fields_map[fname].docstring = comment
                    if fields_map[fname].description is None:
                        fields_map[fname].description = comment.split("\n")[0]

        if new_class.__doc__ and not meta.table_description:
            meta.table_description = inspect.cleandoc(new_class.__doc__).split("\n")[0]
        for value in attrs.values():
            if isinstance(value, Manager):
                value._model = new_class  # type: ignore[assignment]
        meta._model = new_class  # type: ignore[assignment]
        meta.manager._model = new_class  # type: ignore[assignment]
        meta.finalise_fields()
        return new_class

    @staticmethod
    def _linearize_mro(bases: tuple[type, ...]) -> tuple[type, ...]:
        """C3-linearizes ``bases`` as a ``class`` statement would. Computed by hand: a throwaway probe
        class would register itself as a subclass of every base.

        Args:
            bases: The base classes, in the order given to the ``class`` statement.

        Returns:
            Every class in ``bases`` and their ancestors, each once, nearest first.

        Raises:
            TypeError: ``bases`` has no consistent linearization.
        """
        sequences: list[list[type]] = [list(base.__mro__) for base in bases] + [list(bases)]
        result: list[type] = []
        while True:
            sequences = [sequence for sequence in sequences if sequence]
            if not sequences:
                return tuple(result)
            candidate: type | None = None
            for sequence in sequences:
                head = sequence[0]
                if not any(head in other[1:] for other in sequences):
                    candidate = head
                    break
            if candidate is None:
                raise TypeError(f"Cannot create a consistent method resolution order for bases {bases!r}")
            result.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] is candidate:
                    del sequence[0]

    @staticmethod
    def _get_default_manager(
        name: str,
        attrs: dict[str, Any],
        declares_objects: bool,
        meta_class: type,
        meta_manager: Manager[Any],
    ) -> Manager[Any]:
        """The manager behind ``Model.objects`` - the model's default manager, which a JOIN to
        the model is scoped by: the ``objects`` the class body declares, else ``Meta.manager``,
        else the ``objects`` of a base model, else a plain ``Manager``.

        Args:
            name: The model's name.
            attrs: The attributes collected for the class - a base model's managers copied in.
            declares_objects: Whether the class body itself assigns ``objects``.
            meta_class: The model's ``Meta``, with its abstract ancestors' options merged in.
            meta_manager: The manager made from ``Meta.manager``, or a plain one.

        Returns:
            The manager.

        Raises:
            ConfigurationError: ``objects`` is assigned something that isn't a ``Manager``.
        """
        objects = attrs.get("objects")
        if declares_objects:
            if not isinstance(objects, Manager):
                raise ConfigurationError(f"{name}.objects must be a Manager, got {type(objects).__name__}")
            return objects
        # Read without running the manager's descriptor - off a class it gives a queryset.
        if inspect.getattr_static(meta_class, ModelOption.MANAGER, None) is None and isinstance(objects, Manager):
            return objects
        return meta_manager

    @staticmethod
    def _copy_manager_attributes(base: type, attrs: dict[str, Any]) -> None:
        """Adds a fresh copy of every ``Manager`` attribute ``base`` itself declares to ``attrs``,
        unless ``attrs`` already holds that name.

        Args:
            base: The class whose own namespace is scanned.
            attrs: The attributes being collected for the class under construction.
        """
        for key, value in base.__dict__.items():
            if isinstance(value, Manager) and key not in attrs:
                attrs[key] = value.copy_unbound()

    @classmethod
    def _search_for_field_attributes(cls, base: type, attrs: dict[str, Any], name: str) -> None:
        """Collects the field attributes of ``base`` and its ancestors into ``attrs`` - a name already
        there is kept, so a derived class wins; several bases are read left to right.

        A base that is a model already has its fields resolved (``_meta.fields_map``) - they are
        taken before its ancestors are walked. The walk sets the field order; a diamond's value is
        corrected by ``_apply_diamond_field_precedence()``.

        ``name`` is the class being built, for an error message.
        """
        if meta := getattr(base, "_meta", None):
            # Deep-copied: concrete subclasses of one abstract base must not share Field instances -
            # each is bound to its model.
            for key, value in meta.fields_map.items():
                if key not in attrs:
                    attrs[key] = deepcopy(value)
            # A composite primary key's marker is consumed when the abstract base is built - made
            # anew for each concrete subclass.
            if meta.has_composite_primary_key and meta.pk_attr:
                existing = next((value for value in attrs.values() if isinstance(value, CompositePrimaryKey)), None)
                if existing is not None and existing.field_names != meta.pk_attr:
                    # Another base contributed a different composite primary key. The same one
                    # reached through two branches of a diamond is no conflict.
                    raise ConfigurationError(
                        f"Can't create model {name} with two CompositePrimaryKey declarations "
                        "inherited from different abstract base classes"
                    )
                if existing is None:
                    attrs["__inherited_composite_pk__"] = CompositePrimaryKey(*meta.pk_attr)
            # Every manager attribute of a Model base, copied with its constructor state so each
            # subclass gets its own instance to bind.
            cls._copy_manager_attributes(base, attrs)
            for parent in base.__mro__[1:]:
                # Searching for Field attributes in the class hierarchy - see this method's own
                # docstring for why this runs AFTER base's own contribution above, not before.
                cls._search_for_field_attributes(parent, attrs, name)
        else:
            for parent in base.__mro__[1:]:
                # Searching for Field attributes in the class hierarchy
                cls._search_for_field_attributes(parent, attrs, name)
            # For mixin classes. deepcopy for the same reason as the abstract-base branch above -
            # two concrete models sharing the same mixin would otherwise share the literal same
            # Field instances too.
            for key, value in base.__dict__.items():
                if isinstance(value, Field) and key not in attrs:
                    attrs[key] = deepcopy(value)
            # A manager declared on a plain mixin is bound to each concrete model the same way a
            # manager inherited from an abstract Model base is - otherwise it stays unbound
            # (model=None) and every query through it crashes.
            cls._copy_manager_attributes(base, attrs)

    @staticmethod
    def _apply_diamond_field_precedence(canonical_mro: tuple[type, ...], attrs: dict[str, Any]) -> None:
        """Corrects, in place, the value of a field name the per-base walk took from the wrong ancestor
        of a diamond - a base overriding it wins over one passing the common ancestor's value
        through. The field order stays.

        Args:
            canonical_mro: Every ancestor of the class being built, nearest first.
            attrs: The field attributes collected, corrected in place.
        """
        winning_value_by_key: dict[str, Field[Any]] = {}
        for ancestor in canonical_mro:
            if meta := getattr(ancestor, "_meta", None):
                # Only the ancestor's own declared fields count - what it inherited belongs to its
                # own ancestors, which get their turn.
                for key in meta.own_field_names:
                    winning_value_by_key.setdefault(key, meta.fields_map[key])
            else:
                # A plain mixin (no _meta at all) has no "own vs inherited" split to make - a
                # Field instance in its own __dict__ is unambiguously its own declaration.
                for key, value in ancestor.__dict__.items():
                    if isinstance(value, Field):
                        winning_value_by_key.setdefault(key, value)
        for key, value in winning_value_by_key.items():
            if key in attrs:
                attrs[key] = deepcopy(value)

    @staticmethod
    def _parse_composite_pk(attrs: dict[str, Any], name: str) -> tuple[dict[str, Any], tuple[str, ...]] | None:
        """Finds a ``pk = CompositePrimaryKey(...)`` declaration, validates it and takes the marker out
        of ``attrs`` - it isn't a field. None when there is none.
        """
        marker_key = None
        for key, value in attrs.items():
            if isinstance(value, CompositePrimaryKey):
                if marker_key is not None:
                    raise ConfigurationError(f"Can't create model {name} with two CompositePrimaryKey declarations")
                marker_key = key
        if marker_key is None:
            return None

        composite_pk = attrs[marker_key]
        for field_name in composite_pk.field_names:
            field = attrs.get(field_name)
            if not isinstance(field, Field):
                raise ConfigurationError(f"CompositePrimaryKey field '{field_name}' is not a field on model {name}")
            if field.pk:
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} must not itself be "
                    "primary_key=True - the composite declaration is what makes it part of the PK"
                )
            if field.generated:
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} can't be DB-generated "
                    "- every component of a composite key must be assigned explicitly"
                )
            if not field.has_db_field:
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} is a relation field "
                    "(ForeignKeyField/OneToOneField/ManyToManyField) - it has no single DB column of "
                    "its own (only its generated shadow column, e.g. '<field>_id'), so it can't be a "
                    "composite primary key component"
                )

        attrs = dict(attrs)
        del attrs[marker_key]
        return attrs, composite_pk.field_names

    @staticmethod
    def _check_generated_non_pk_fields(attrs: dict[str, Any], name: str) -> None:
        """Rejects a DB-generated non-primary-key field whose class has no generation support.

        Args:
            attrs: The model class attributes.
            name: The model class name.

        Raises:
            ConfigurationError: If such a field is found.
        """
        for key, value in attrs.items():
            if not isinstance(value, Field) or value.pk or not value.generated:
                continue
            if not value.allows_generated:
                raise ConfigurationError(
                    f"Field '{key}' ({value.__class__.__name__}) on model {name} can't be DB-generated"
                )

    @staticmethod
    def _declares_no_primary_key(meta_class: type, name: str) -> bool:
        """Whether a model's ``Meta`` declares ``primary_key = None`` - a table with no primary key,
        such as an append-only log or a columnar store's table.

        Args:
            meta_class: The model's ``Meta``, with what abstract ancestors declared merged in.
            name: The model class name.

        Returns:
            True for ``primary_key = None``.

        Raises:
            ConfigurationError: ``Meta.primary_key`` is anything but None - a primary key is
                declared on its field (``primary_key=True``) or with ``CompositePrimaryKey``.
        """
        if not hasattr(meta_class, ModelOption.PRIMARY_KEY):
            return False
        if getattr(meta_class, ModelOption.PRIMARY_KEY) is not None:
            raise ConfigurationError(
                f"Model {name}: Meta.primary_key only takes None, for a table without a primary key - "
                "declare a primary key with primary_key=True on its field or with CompositePrimaryKey"
            )
        return True

    @staticmethod
    def _parse_custom_pk(
        attrs: dict[str, Any],
        pk_attr: str | tuple[str, ...],
        name: str,
        is_abstract: bool,
        declares_no_primary_key: bool = False,
    ) -> tuple[dict[str, Any], str | tuple[str, ...]]:
        """Finds the model's primary key: a ``CompositePrimaryKey``, the field declared with
        ``primary_key=True``, an ``id`` added when there is neither - or none at all, with
        ``Meta.primary_key = None`` (the key is then ``()``).

        Args:
            attrs: The model class attributes.
            pk_attr: The primary key found so far.
            name: The model class name.
            is_abstract: Whether the model is abstract - an abstract model gets no ``id``.
            declares_no_primary_key: Whether ``Meta.primary_key = None``.

        Returns:
            The attributes, with an added ``id`` or without the composite key marker, and the
            primary key's field name(s).

        Raises:
            ConfigurationError: Two primary keys, a primary key alongside
                ``Meta.primary_key = None``, a generated field that can't be, or an ``id`` field
                that isn't the primary key of a model declaring none.
        """
        if composite := ModelMeta._parse_composite_pk(attrs, name):
            if declares_no_primary_key:
                raise ConfigurationError(
                    f"Model {name} declares both Meta.primary_key = None and a CompositePrimaryKey"
                )
            ModelMeta._check_generated_non_pk_fields(composite[0], name)
            return composite
        ModelMeta._check_generated_non_pk_fields(attrs, name)

        custom_pk_present = False
        for key, value in attrs.items():
            if isinstance(value, Field):
                if value.pk:
                    if custom_pk_present:
                        raise ConfigurationError(
                            f"Can't create model {name} with two primary keys, only single primary key is supported"
                        )
                    if value.generated and not value.allows_generated:
                        raise ConfigurationError(f"Field '{key}' ({value.__class__.__name__}) can't be DB-generated")
                    custom_pk_present = True
                    pk_attr = key

        if declares_no_primary_key:
            if custom_pk_present:
                raise ConfigurationError(
                    f"Model {name} declares both Meta.primary_key = None and the primary key field {pk_attr!r}"
                )
            return attrs, ()
        if not custom_pk_present and not is_abstract:
            if "id" not in attrs:
                attrs = {"id": IntField(primary_key=True), **attrs}

            if not isinstance(attrs["id"], Field) or not attrs["id"].pk:
                raise ConfigurationError(
                    f"Can't create model {name} without explicit primary key if field 'id' already present"
                )
        return attrs, pk_attr

    @staticmethod
    def _dispatch_fields(
        attrs: dict[str, Any], fields_db_projection: dict[str, str], is_abstract: bool
    ) -> tuple[
        dict[str, Field[Any]],
        set[str],
        set[str],
        set[str],
    ]:
        fields_map: dict[str, Field[Any]] = {}
        fk_fields: set[str] = set()
        m2m_fields: set[str] = set()
        o2o_fields: set[str] = set()
        for key, value in attrs.items():
            if isinstance(value, Field):
                if is_abstract:
                    value = deepcopy(value)

                fields_map[key] = value
                value.model_field_name = key

                if isinstance(value, OneToOneFieldInstance):
                    o2o_fields.add(key)
                elif isinstance(value, ForeignKeyFieldInstance):
                    fk_fields.add(key)
                elif isinstance(value, ManyToManyFieldInstance):
                    m2m_fields.add(key)
                else:
                    fields_db_projection[key] = value.source_field or key
        return (fields_map, fk_fields, m2m_fields, o2o_fields)

    @staticmethod
    def get_reserved_field_names() -> frozenset[str]:
        """Names a model field may not use because they would shadow a Model or ModelMeta attribute -
        worked out once, not for every model class.

        Returns:
            Every non-dunder attribute name on Model's full MRO and on ModelMeta.
        """
        reserved_names = ModelMeta.reserved_field_names.get(())
        if reserved_names is not None:
            return reserved_names
        # hare.models.model imports ModelMeta from this module at module level, so a top-level
        # import here would be circular - deferred to first use. Safe because this only ever
        # runs well after both hare.models.model and hare.models.meta_class have fully loaded.
        from hare.models.model import Model

        # The whole MRO: Model's methods live on the classes it is composed from, not in
        # Model.__dict__.
        reserved_names = frozenset(
            {key for base in Model.__mro__ for key in base.__dict__ if not key.startswith("__")}
            | {key for key in ModelMeta.__dict__ if not key.startswith("__")}
        )
        ModelMeta.reserved_field_names[()] = reserved_names
        return reserved_names

    @staticmethod
    def _check_field_name_conflicts(fields_map: dict[str, Field[Any]], name: str) -> None:
        reserved_names = ModelMeta.get_reserved_field_names()
        conflicts = sorted(set(fields_map).intersection(reserved_names))
        if conflicts:
            conflict_list = ", ".join(conflicts)
            raise ConfigurationError(
                f"Model {name} has field name(s) that conflict with default Model attributes: {conflict_list}"
            )

    @staticmethod
    def _get_comments(model_cls: type[Model]) -> dict[str, str]:
        """The ``#:`` comments standing right before the model's attributes, by field name. ``{model}``
        in a comment is replaced with the model class's name.

        Args:
            model_cls: The class whose source is read.

        Returns:
            The comments by field name.
        """
        first_line = getattr(model_cls, "__firstlineno__", None)
        try:
            filename = inspect.getsourcefile(model_cls)
        except TypeError, OSError:
            return {}
        if first_line is None or filename is None:
            return {}
        # A source file changed since it was read is read again.
        linecache.checkcache(filename)
        module = sys.modules.get(model_cls.__module__)
        lines = linecache.getlines(filename, module.__dict__ if module is not None else None)
        last_line = ModelMeta._get_class_spans(filename, lines).get(first_line)
        if last_line is None:
            return {}
        # Only model_cls's own body, never an ancestor's - so every match belongs to model_cls
        # itself, and the placeholder always resolves to model_cls.__name__.
        source = "".join(lines[first_line - 1 : last_line])
        comments = {}
        for comment_block, field_name in FIELD_COMMENT_RE.findall(source):
            comment = re.sub(r"(^\s*#:\s*|\s*$)", "", comment_block, flags=re.MULTILINE)
            comments[field_name] = comment.replace("{model}", model_cls.__name__)

        return comments

    @staticmethod
    def _get_class_spans(filename: str, lines: list[str]) -> dict[int, int]:
        """The classes of a source file - parsed once per file, not per class.

        Args:
            filename: The file.
            lines: Its lines.

        Returns:
            Each class's last line by its first line (a decorator's, when it has one).
        """
        key = (filename,)
        cached = ModelMeta.source_class_spans.get(key)
        if cached is not None and cached[0] is lines:
            return cached[1]
        spans: dict[int, int] = {}
        try:
            tree = ast.parse("".join(lines))
        except SyntaxError, ValueError:
            tree = None
        # Statements only - a class is never defined inside an expression.
        statements: list[ast.AST] = list(tree.body) if tree is not None else []
        while statements:
            node = statements.pop()
            if isinstance(node, ast.ClassDef) and node.end_lineno is not None:
                first_line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                spans[first_line] = node.end_lineno
            for block_field in STATEMENT_BLOCK_FIELDS:
                block = getattr(node, block_field, None)
                if block:
                    statements.extend(block)
        ModelMeta.source_class_spans[key] = (lines, spans)
        return spans

    @staticmethod
    def build_meta(
        meta_class: type[Model.Meta],
        fields_map: dict[str, Field[Any]],
        fields_db_projection: dict[str, str],
        fk_fields: set[str],
        o2o_fields: set[str],
        m2m_fields: set[str],
        pk_attr: str | tuple[str, ...],
        own_field_names: frozenset[str],
    ) -> MetaInfo:
        meta = MetaInfo(meta_class)
        meta.fields_map = fields_map
        meta.fields_db_projection = fields_db_projection
        meta.fk_fields = fk_fields
        meta.o2o_fields = o2o_fields
        meta.m2m_fields = m2m_fields
        meta.pk_attr = pk_attr
        meta.own_field_names = own_field_names
        if isinstance(pk_attr, tuple):
            # A composite PK has no single Field/column representing it - meta.pk/db_pk_column
            # stay unset (their MetaInfo.__init__ defaults). Every code path that needs the
            # composite key works off meta.pk_attr directly instead (the query executor, DDL generation).
            meta.pk_fields = tuple(fields_map[name] for name in pk_attr)
        elif pk_field := fields_map.get(pk_attr):
            meta.pk = pk_field
            if pk_field.source_field:
                meta.db_pk_column = pk_field.source_field
            elif isinstance(pk_field, OneToOneFieldInstance):
                meta.db_pk_column = f"{pk_attr}_id"
            else:
                meta.db_pk_column = pk_attr
        meta._inited = False
        if not fields_map:
            meta.abstract = True
        return meta

    def __getitem__(cls: type[TModel], key: Any) -> QuerySetSingle[TModel]:  # type: ignore[misc]
        return ModelMeta._get_by_primary_key(cls, key)  # type: ignore[return-value]

    @staticmethod
    async def _get_by_primary_key(model: type[TModel], key: Any) -> TModel:
        """The object ``Model[key]`` reads.

        Raises:
            DoesNotExist: No object has that primary key.
        """
        message = f"{model.__name__} has no object with {model._meta.pk_attr}={key}"
        try:
            return await model.objects.get(pk=key)
        except DoesNotExist:
            raise DoesNotExist(model, message) from None
        except (ValueError, ValidationError) as exc:
            # The key can't be coerced to the pk field's type: still "no such object" for a
            # subscript, with the real cause chained.
            raise DoesNotExist(model, message) from exc
