import sys
import enum
import types
import typing
# noinspection PyUnresolvedReferences
import builtins
import datetime
import collections
import collections.abc
# noinspection PyUnresolvedReferences
from base64 import encodebytes, decodebytes
from contextlib import contextmanager
from dataclasses import is_dataclass, MISSING

# noinspection PyUnresolvedReferences
from mashumaro.exceptions import MissingField, UnserializableField,\
    UnserializableDataError


PY_36 = sys.version_info < (3, 7)
PY_37 = sys.version_info >= (3, 7)

NoneType = type(None)


def get_imported_module_names():
    # noinspection PyUnresolvedReferences
    return {value.__name__ for value in globals().values()
            if isinstance(value, types.ModuleType)}


INITIAL_MODULES = get_imported_module_names()


def get_type_origin(t):
    try:
        if PY_36:
            return t.__extra__
        elif PY_37:
            return t.__origin__
    except AttributeError:
        return t


def type_name(t):
    try:
        return f"{t.__module__}.{t.__name__}"
    except AttributeError:
        return str(t)


def is_special_typing_primitive(t):
    try:
        issubclass(t, object)
        return False
    except TypeError:
        return True


def is_generic(t):
    try:
        # noinspection PyProtectedMember
        # noinspection PyUnresolvedReferences
        return t.__class__ is typing._GenericAlias
    except AttributeError:
        if PY_36:
            try:
                # noinspection PyUnresolvedReferences
                return t.__class__ == typing.GenericMeta
            except AttributeError:
                return False
        else:
            raise NotImplementedError


def is_union(t):
    try:
        return t.__origin__ is typing.Union
    except AttributeError:
        return False


class CodeBuilder:
    def __init__(self, cls):
        self.cls = cls
        self.lines = None            # type: typing.Optional[typing.List[str]]
        self.modules = None          # type: typing.Optional[typing.Set[str]]
        self._current_indent = None  # type: typing.Optional[str]

    def reset(self):
        self.lines = []
        self.modules = INITIAL_MODULES.copy()
        self._current_indent = ''

    @property
    def namespace(self):
        return self.cls.__dict__

    @property
    def fields(self):
        return typing.get_type_hints(self.cls)

    @property
    def defaults(self):
        return {name: self.namespace.get(name, MISSING) for name in self.fields}

    def _add_type_modules(self, *types_):
        for t in types_:
            module = t.__module__
            if module not in self.modules:
                self.modules.add(module)
                self.add_line(f"import {module}")
                self.add_line(f"globals()['{module}'] = {module}")
            args = getattr(t, '__args__', ())
            if args:
                self._add_type_modules(*args)
            constraints = getattr(t, '__constraints__', ())
            if constraints:
                self._add_type_modules(*constraints)

    def add_line(self, line):
        self.lines.append(f"{self._current_indent}{line}")

    @contextmanager
    def indent(self):
        self._current_indent += ' ' * 4
        try:
            yield
        finally:
            self._current_indent = self._current_indent[:-4]

    def compile(self):
        exec("\n".join(self.lines), globals(), self.__dict__)

    def add_from_dict(self):

        self.reset()
        if not self.fields:
            return

        self.add_line('@classmethod')
        self.add_line("def from_dict(cls, d, use_bytes=False, use_enum=False):")
        with self.indent():
            self.add_line('try:')
            with self.indent():
                self.add_line("kwargs = {}")
                for fname, ftype in self.fields.items():
                    self._add_type_modules(ftype)
                    self.add_line(f"value = d.get('{fname}', MISSING)")
                    self.add_line("if value is None:")
                    with self.indent():
                        self.add_line(f"kwargs['{fname}'] = None")
                    self.add_line("else:")
                    with self.indent():
                        if self.defaults[fname] is MISSING:
                            self.add_line(f"if value is MISSING:")
                            with self.indent():
                                self._add_type_modules(ftype)
                                self.add_line(f"raise MissingField('{fname}',"
                                              f"{type_name(ftype)},cls)")
                            self.add_line("else:")
                            with self.indent():
                                unpacked_value = self._unpack_field_value(
                                    fname, ftype, self.cls)
                                self.add_line(
                                    f"kwargs['{fname}'] = {unpacked_value}")
                        else:
                            self.add_line("if value is not MISSING:")
                            with self.indent():
                                unpacked_value = self._unpack_field_value(
                                    fname, ftype, self.cls)
                                self.add_line(
                                    f"kwargs['{fname}'] = {unpacked_value}")
            self.add_line('except AttributeError:')
            with self.indent():
                self.add_line('if not isinstance(d, dict):')
                with self.indent():
                    self.add_line(f"raise ValueError('Argument for "
                                  f"{type_name(self.cls)}.from_dict method "
                                  f"should be a dict instance') from None")
                self.add_line('else:')
                with self.indent():
                    self.add_line('raise')
            self.add_line("return cls(**kwargs)")
        self.add_line(f"setattr(cls, 'from_dict', from_dict)")
        self.compile()

    def add_to_dict(self):

        self.reset()
        if not self.fields:
            return

        self.add_line("def to_dict(self, use_bytes=False, use_enum=False):")
        with self.indent():
            self.add_line("kwargs = {}")
            for fname, ftype in self.fields.items():
                self.add_line(f"value = getattr(self, '{fname}')")
                packed_value = self._pack_value(fname, ftype, self.cls)
                self.add_line(f"kwargs['{fname}'] = {packed_value}")
            self.add_line("return kwargs")
        self.add_line(f"setattr(cls, 'to_dict', to_dict)")
        self.compile()

    def _pack_value(self, fname, ftype, parent, value_name='value'):

        if is_dataclass(ftype):
            return f"{value_name}.to_dict(use_bytes, use_enum)"

        origin_type = get_type_origin(ftype)
        if is_special_typing_primitive(origin_type):
            # TODO: упаковывать dataclass и вложенные типы
            return value_name
        elif issubclass(origin_type, typing.Collection):
            args = getattr(ftype, '__args__', ())

            def inner_expr(arg_num=0, v_name='value'):
                return self._pack_value(fname, args[arg_num], parent, v_name)

            if issubclass(origin_type, (typing.List,
                                        typing.Deque,
                                        typing.Tuple,
                                        typing.AbstractSet)):
                if is_generic(ftype):
                    return f'[{inner_expr()} for value in {value_name}]'
                elif ftype is list:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.List[T] instead')
                elif ftype is collections.deque:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Deque[T] instead')
                elif ftype is tuple:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Tuple[T] instead')
                elif ftype is set:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Set[T] instead')
                elif ftype is frozenset:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.FrozenSet[T] instead')
            elif issubclass(origin_type, typing.ChainMap):
                if ftype is collections.ChainMap:
                    raise UnserializableField(
                        fname, ftype, parent,
                        'Use typing.ChainMap[KT,VT] instead'
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            'ChainMaps with dataclasses as keys '
                            'are not supported by mashumaro')
                    else:
                        return f'[{{{inner_expr(0,"key")}:{inner_expr(1)} ' \
                               f'for key,value in m.items()}} ' \
                               f'for m in value.maps]'
            elif issubclass(origin_type, typing.Mapping):
                if ftype is dict:
                    raise UnserializableField(
                        fname, ftype, parent,
                        'Use typing.Dict[KT,VT] or Mapping[KT,VT] instead'
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            'Mappings with dataclasses as keys '
                            'are not supported by mashumaro')
                    else:
                        return f'{{{inner_expr(0,"key")}: {inner_expr(1)} ' \
                               f'for key, value in {value_name}.items()}}'
            elif issubclass(origin_type, typing.ByteString):
                return f'{value_name} if use_bytes else ' \
                       f'encodebytes({value_name}).decode()'
            elif issubclass(origin_type, str):
                return value_name
            elif issubclass(origin_type, typing.Sequence):
                if is_generic(ftype):
                    return f'[{inner_expr()} for value in {value_name}]'
        elif issubclass(origin_type, enum.Enum):
            return f'{value_name} if use_enum else {value_name}.value'
        elif origin_type in (bool, int, float, NoneType):
            return value_name
        elif origin_type in (datetime.datetime, datetime.date, datetime.time):
            return f'{value_name}.isoformat()'
        elif origin_type is datetime.timedelta:
            return f'{value_name}.total_seconds()'

        raise UnserializableField(fname, ftype, parent)

    def _unpack_field_value(self, fname, ftype, parent, value_name='value'):

        if is_dataclass(ftype):
            return f"{type_name(ftype)}.from_dict({value_name}, " \
                   f"use_bytes, use_enum)"

        origin_type = get_type_origin(ftype)
        if is_special_typing_primitive(origin_type):
            # TODO: распаковывать dataclass и вложенные типы
            if origin_type in (typing.Any, typing.AnyStr):
                return value_name
            elif is_union(ftype):
                # TODO: выбирать в рантайме подходящий тип
                args = getattr(ftype, '__args__', ())
                if len(args) == 2 and args[1] == NoneType:  # it is Optional
                    if is_dataclass(args[0]):
                        return self._unpack_field_value(fname, args[0], parent)
                    else:
                        return value_name
                else:
                    return value_name
            elif hasattr(origin_type, '__constraints__'):
                if origin_type in origin_type.__constraints__:
                    # TODO: выбирать в рантайме подходящий тип
                    return value_name
        elif issubclass(origin_type, typing.Collection):
            args = getattr(ftype, '__args__', ())

            def inner_expr(arg_num=0, v_name='value'):
                return self._unpack_field_value(
                    fname, args[arg_num], parent, v_name)

            if issubclass(origin_type, typing.List):
                if is_generic(ftype):
                    return f'[{inner_expr()} for value in {value_name}]'
                elif ftype is list:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.List[T] instead')
            elif issubclass(origin_type, typing.Deque):
                if is_generic(ftype):
                    return f'collections.deque([{inner_expr()} ' \
                           f'for value in {value_name}])'
                elif ftype is collections.deque:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Deque[T] instead')
            elif issubclass(origin_type, typing.Tuple):
                if is_generic(ftype):
                    return f'tuple([{inner_expr()} for value in {value_name}])'
                elif ftype is tuple:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Tuple[T] instead')
            elif issubclass(origin_type, typing.FrozenSet):
                if is_generic(ftype):
                    return f'frozenset([{inner_expr()} ' \
                           f'for value in {value_name}])'
                elif ftype is frozenset:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.FrozenSet[T] instead')
            elif issubclass(origin_type, typing.AbstractSet):
                if is_generic(ftype):
                    return f'set([{inner_expr()} for value in {value_name}])'
                elif ftype is set:
                    raise UnserializableField(
                        fname, ftype, parent, 'Use typing.Set[T] instead')
            elif issubclass(origin_type, typing.ChainMap):
                if ftype is collections.ChainMap:
                    raise UnserializableField(
                        fname, ftype, parent,
                        'Use typing.ChainMap[KT,VT] instead'
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            'ChainMaps with dataclasses as keys '
                            'are not supported by mashumaro')
                    else:
                        return f'collections.ChainMap(' \
                               f'*[{{{inner_expr(0,"key")}:{inner_expr(1)} ' \
                               f'for key, value in m.items()}} ' \
                               f'for m in {value_name}])'
            elif issubclass(origin_type, typing.Mapping):
                if ftype is dict:
                    raise UnserializableField(
                        fname, ftype, parent,
                        'Use typing.Dict[KT,VT] or Mapping[KT,VT] instead'
                    )
                elif is_generic(ftype):
                    if is_dataclass(args[0]):
                        raise UnserializableDataError(
                            'Mappings with dataclasses as keys '
                            'are not supported by mashumaro')
                    else:
                        return f'{{{inner_expr(0,"key")}: {inner_expr(1)} ' \
                               f'for key, value in {value_name}.items()}}'
            elif issubclass(origin_type, typing.ByteString):
                if origin_type is bytes:
                    return f'{value_name} if use_bytes else ' \
                           f'decodebytes({value_name}.encode())'
                elif origin_type is bytearray:
                    return f'bytearray({value_name} if use_bytes else ' \
                           f'decodebytes({value_name}.encode()))'
            elif issubclass(origin_type, str):
                return value_name
            elif issubclass(origin_type, typing.Sequence):
                if is_generic(ftype):
                    return f'[{inner_expr()} for value in {value_name}]'
        elif issubclass(origin_type, enum.Enum):
            return f'{value_name} if use_enum ' \
                   f'else {type_name(origin_type)}({value_name})'
        elif origin_type in (bool, int, float, NoneType):
            return value_name
        elif origin_type is datetime.datetime:
            return f'datetime.datetime.fromisoformat({value_name})'
        elif origin_type is datetime.date:
            return f'datetime.date.fromisoformat({value_name})'
        elif origin_type is datetime.time:
            return f'datetime.time.fromisoformat({value_name})'
        elif origin_type is datetime.timedelta:
            return f'datetime.timedelta(seconds={value_name})'

        raise UnserializableField(fname, ftype, parent)
