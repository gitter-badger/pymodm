# Copyright 2016 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from bson.dbref import DBRef
from bson.son import SON

from pymodm import errors
from pymodm.base.options import MongoOptions
from pymodm.common import (
    register_document, get_document, validate_mapping,
    validate_list_tuple_or_none, validate_boolean_or_none,
    validate_boolean)
from pymodm.compat import with_metaclass
from pymodm.errors import ValidationError, InvalidModel, OperationError
from pymodm.fields import ObjectIdField
from pymodm.manager import Manager


class MongoModelMetaclass(type):
    """Base metaclass for all Models."""

    def __new__(mcls, name, bases, attrs):
        model_parents = [
            base for base in bases if isinstance(base, MongoModelMetaclass)]
        # Only perform Model initialization steps if the class has inherited
        # from a Model base class (i.e. MongoModel/EmbeddedMongoModel).
        if not model_parents:
            return type.__new__(mcls, name, bases, attrs)

        new_class = type.__new__(
            mcls, name, bases, {'__module__': attrs['__module__']})

        # User-defined or inherited metadata
        meta = attrs.get('Meta', getattr(new_class, 'Meta', None))
        options = MongoOptions(meta)

        # Let the Options object take care of merging relevant options.
        new_class.add_to_class('_mongometa', options)

        # Add all attributes to class.
        for attr in attrs:
            new_class.add_to_class(attr, attrs[attr])

        def should_inherit_field(parent_class, field):
            # Never shadow fields defined on the new class.
            if field.attname in new_class._mongometa.fields_dict:
                return False
            # Never inherit an implicit primary key.
            if field.primary_key and parent_class._mongometa.implicit_id:
                return False
            return True

        # Also add fields from parents into the metadata.
        for base in model_parents:
            if hasattr(base, '_mongometa'):
                parent_fields = base._mongometa.get_fields()
                for field in parent_fields:
                    if should_inherit_field(base, field):
                        new_class.add_to_class(field.attname, field)

        # Add class-local copies of Exceptions.
        new_class.add_to_class('DoesNotExist', errors.DoesNotExist)
        new_class.add_to_class(
            'MultipleObjectsReturned', errors.MultipleObjectsReturned)

        # Discover and store class hierarchy for later.
        class_name = new_class._mongometa.object_name
        new_class._subclasses = set([class_name])
        for base in new_class._get_bases(bases):
            if base._mongometa.final:
                raise InvalidModel(
                    'Cannot extend class %s, '
                    'because it has been declared final.'
                    % base._mongometa.object_name)
            base._subclasses.add(class_name)

        # Add class to the registry.
        register_document(new_class)

        return new_class

    @staticmethod
    def _get_bases(bases):
        found_bases = set()
        for base in bases:
            # Is it an ODM class?
            if hasattr(base, '_mongometa'):
                found_bases.add(base)
                found_bases.update(base._get_bases(base.__bases__))
        return found_bases

    def add_to_class(cls, name, value):
        """Add an attribute to this class.

        If the value defines a `contribute_to_class` method, it will be run
        with this class and the given name as its arguments.
        """
        # Check if value is an object and defines contribute_to_class.
        if hasattr(value, 'contribute_to_class'):
            value.contribute_to_class(cls, name)
        else:
            setattr(cls, name, value)


class TopLevelMongoModelMetaclass(MongoModelMetaclass):
    """Metaclass for all top-level (i.e. not embedded) Models."""
    def __new__(mcls, name, bases, attrs):
        new_class = super(TopLevelMongoModelMetaclass, mcls).__new__(
            mcls, name, bases, attrs)
        # Conceptually the same as 'if new_class is MongoModelBase'.
        if not hasattr(new_class, '_mongometa'):
            return new_class

        # Check for a primary key field. If there isn't one, put one there.
        if new_class._mongometa.pk is None:
            id_field = ObjectIdField(primary_key=True)
            new_class.add_to_class('_id', id_field)
            new_class._mongometa.implicit_id = True

        # Add QuerySet Manager.
        manager = new_class._find_manager()
        if manager is None:
            manager = Manager()
            new_class.add_to_class('objects', manager)
        new_class._default_manager = manager

        return new_class

    def _find_manager(cls):
        first_manager = None
        for name in cls.__dict__:
            attr = getattr(cls, name)
            if isinstance(attr, Manager):
                if first_manager is None:
                    first_manager = attr
                elif first_manager.creation_order > attr.creation_order:
                    first_manager = attr
        return first_manager


class MongoModelBase(object):
    """Base class for MongoModel and EmbeddedMongoModel."""

    def __init__(self, *args, **kwargs):
        # Initialize dict for saving field values.
        self._data = {}

        # Turn ordered arguments into keyword arguments.
        if args:
            len_args = len(args)
            # Get field names in the order they are defined on the Model.
            all_field_names = (
                field.attname for field in self._mongometa.get_fields()
                if not (field.primary_key and self._mongometa.implicit_id))
            len_all_fields = (len(self._mongometa.fields_dict) -
                              int(self._mongometa.implicit_id))
            if len_args > len_all_fields:
                raise ValueError(
                    'Got %d arguments for only %d fields.'
                    % (len_args, len_all_fields))
            for i in range(len_args):
                next_field_name = next(all_field_names)
                if next_field_name in kwargs:
                    raise ValueError(
                        'Field %s specified more than once '
                        'in constructor for %s.'
                        % (next_field_name, self.__class__.__name__))
                kwargs[next_field_name] = args[i]

        # Set values for specified fields
        field_names = set(
            field.attname for field in self._mongometa.get_fields())
        for field in kwargs:
            if 'pk' == field:
                setattr(self, self._mongometa.pk.attname, kwargs[field])
            elif field not in field_names:
                raise ValueError(
                    'Unrecognized field name %r' % field)
            else:
                setattr(self, field, kwargs[field])

    def _find_referenced_objects(self, value):
        """Find all referenced objects in the given object."""
        references = []
        if isinstance(value, MongoModel):
            references.append(value)
        elif isinstance(value, list):
            for item in value:
                references.extend(self._find_referenced_objects(item))
        elif isinstance(value, EmbeddedMongoModel):
            for field_name in value:
                field_value = getattr(value, field_name)
                references.extend(value._find_referenced_objects(field_value))
        return references

    def _set_attributes(self, dict):
        """Set this object's attributes from a dict."""
        self._data.clear()
        field_names = {
            field.mongo_name: field.attname
            for field in self._mongometa.get_fields()
        }
        for field in dict:
            if '_cls' == field:
                continue
            elif '_id' == field and not self._mongometa.implicit_id:
                setattr(self, self._mongometa.pk.attname, dict[field])
            elif field not in field_names:
                raise ValueError(
                    'Unrecognized field name %r' % field)
            else:
                setattr(self, field_names[field], dict[field])

    @classmethod
    def from_document(cls, document):
        """Construct an instance of this class from the given document.

        :parameters:
          - `document`: A Python dictionary describing a MongoDB document.
             Fields within the document should be named according to each model
             field's `mongo_name` attribute, rather than the field's Python
             name.

        """
        dct = validate_mapping('document', document)
        cls_name = dct.get('_cls')
        if cls_name is not None:
            cls = get_document(cls_name)

        inst = cls()
        inst._set_attributes(dct)
        return inst

    def to_son(self):
        """Get this Model back as a SON object.

        :returns: a SON object representing thi object as a MongoDB document.

        """
        son = SON()
        for field in self._mongometa.get_fields():
            if field.is_undefined(self):
                continue
            raw_value = field.value_from_object(self)
            if field.is_blank(raw_value):
                son[field.mongo_name] = raw_value
            else:
                son[field.mongo_name] = field.to_mongo(raw_value)
        # Add metadata about our type, so that we instantiate the right class
        # when retrieving from MongoDB.
        if not self._mongometa.final:
            son['_cls'] = self._mongometa.object_name
        return son

    def clean(self):
        """Hook for custom validation rules run when
        :meth:`~pymodm.MongoModel.full_clean` is called.
        """
        pass

    def clean_fields(self, exclude=None):
        """
        Cleans all fields and raises a ValidationError containing a dict
        of all validation errors if any occur.

        :parameters:
          - `exclude`: A list of fields to exclude from validation.
        """
        exclude = validate_list_tuple_or_none('exclude', exclude)
        exclude = set(exclude) if exclude else set()
        error_dict = {}
        for field in self._mongometa.get_fields():
            if field.attname in exclude:
                continue
            try:
                field_value = field.value_from_object(self)
                field_empty = field.is_undefined(self)
                if field_empty and field.required:
                    error_dict[field.attname] = ValidationError(
                        'field is required.')
                elif not field_empty:
                    field.validate(field_value)
            except Exception as exc:
                error_dict[field.attname] = ValidationError(exc)
        if error_dict:
            raise ValidationError(error_dict)

    def full_clean(self, exclude=None):
        """Validate this Model.

        Calling this method will first validate all this Model's Fields and then
        call :meth:`~pymodm.MongoModel.clean`, which can
        be overridden to provide custom validation on the entire model.

        :parameters:
          - `exclude`: A list of fields to exclude from validation.
        """
        self.clean_fields(exclude=exclude)
        self.clean()

    def __iter__(self):
        return iter(self._data)

    def __str__(self):
        return '<%s object>' % self.__class__.__name__

    def __repr__(self):
        attrs = ('%s=%r' % (fname, getattr(self, fname)) for fname in self)
        return '%s(%s)' % (self.__class__.__name__, ', '.join(attrs))

    def __eq__(self, other):
        if isinstance(other, MongoModelBase):
            return self._data == other._data
        return NotImplemented


class MongoModel(with_metaclass(TopLevelMongoModelMetaclass, MongoModelBase)):
    """Base class for all top-level models.

    A MongoModel definition typically includes a number of field instances
    and possibly a ``Meta`` class attribute that provides metadata or settings
    specific to the model.

    MongoModels can be instantiated either with positional or keyword arguments.
    Positional arguments are bound to the fields in the order the fields are
    defined on the model. Keyword argument names are the same as the names of
    the fields:

      >>> class User(MongoModel):
      ...     email = fields.EmailField(primary_key=True)
      ...     name = fields.CharField()
      >>> # Using positional arguments:
      >>> jane = User('jane@examplesarehard.net', 'Jane')
      >>> # Keyword arguments:
      >>> roy = User(name='Roy', email='roy@examplesarehard.net')

    Note that creating an instance of MongoModel does not create a document
    in the database.

    """

    @classmethod
    def register_delete_rule(cls, related_model, related_field, rule):
        """Specify what to do when an instance of this class is deleted.

        :parameters:
          - `related_model`: The class that references this class.
          - `related_field`: The name of the field in ``related_model`` that
            references this class.
          - `rule`: The delete rule. See
            :attr:`~pymodm.fields.ReferenceField` for details.

        """
        cls._mongometa.delete_rules[(related_model, related_field)] = rule

    @property
    def pk(self):
        """An alias for the primary key (called `_id` in MongoDB)."""
        if self._mongometa.pk is not None:
            return getattr(self, self._mongometa.pk.attname)

    @pk.setter
    def pk(self, value):
        if self._mongometa.pk is None:
            raise ValueError('No primary key set for %s'
                             % self._mongometa.object_name)
        setattr(self, self._mongometa.pk.attname, value)

    @property
    def _qs(self):
        if not hasattr(self, '__queryset'):
            self.__queryset = None
        if (self.__queryset is None and
                not self._mongometa.pk.is_undefined(self)):
            self.__queryset = self.__class__._default_manager.raw(
                {'_id': self._mongometa.pk.to_mongo(self.pk)})
        return self.__queryset

    def save(self, cascade=None, full_clean=True, force_insert=False):
        """Save this document into MongoDB.

        If there is no value for the primary key on this Model instance, the
        instance will be inserted into MongoDB. Otherwise, the entire document
        will be replaced with this version (upserting if necessary).

        :parameters:
          - `cascade`: If ``True``, all dereferenced MongoModels contained in
            this Model instance will also be saved.
          - `full_clean`: If ``True``, the
            :meth:`~pymodm.MongoModel.full_clean` method
            will be called before persisting this object.
          - `force_insert`: If ``True``, always do an insert instead of a
            replace.

        :returns: This object, with the `pk` property filled in if it wasn't
        already.

        """
        cascade = validate_boolean_or_none('cascade', cascade)
        full_clean = validate_boolean('full_clean', full_clean)
        force_insert = validate_boolean('force_insert', force_insert)
        if full_clean:
            self.full_clean()
        if cascade or (self._mongometa.cascade and cascade is not False):
            for field_name in self:
                for referenced_object in self._find_referenced_objects(
                        getattr(self, field_name)):
                    referenced_object.save()
        if force_insert or self._mongometa.pk.is_undefined(self):
            result = self._mongometa.collection.insert_one(self.to_son())
            self.pk = result.inserted_id
        else:
            result = self._mongometa.collection.replace_one(
                {'_id': self._mongometa.pk.to_mongo(self.pk)},
                self.to_son(), upsert=True)
        return self

    def delete(self):
        """Delete this object from MongoDB."""
        self._qs.delete()

    def is_valid(self):
        """Return ``True`` if the data in this Model is valid.

        This method runs the
        :meth:`~pymodm.MongoModel.full_clean`
        method and returns ``True`` if no ValidationError was raised.

        """
        try:
            self.full_clean()
        except ValidationError:
            return False
        return True

    def refresh_from_db(self, fields=None):
        """Reload this object from the database.

        .. warning:: This method will reload the object from the database,
           with possibly only a few fields set. Calling
           :meth:`~pymodm.MongoModel.save` after this may revert
           or unset fields.

        :parameters:
          - `fields`: An iterable of fields to reload. Defaults to all fields.
        """
        fields = validate_list_tuple_or_none('fields', fields)
        if self._qs is None:
            raise OperationError('Cannot refresh from db before saving.')
        qs = self._qs.values()
        if fields:
            qs = qs.only(*fields)
        db_inst = qs.first()

        self._set_attributes(db_inst)
        return self

    def __eq__(self, other):
        if self.pk is not None:
            if isinstance(other, self.__class__) and other.pk is not None:
                return self.pk == other.pk
            elif isinstance(other, DBRef):
                return self.pk == other.id
        return self is other


class EmbeddedMongoModel(with_metaclass(MongoModelMetaclass, MongoModelBase)):
    """Base class for models that represent embedded documents."""
    pass
