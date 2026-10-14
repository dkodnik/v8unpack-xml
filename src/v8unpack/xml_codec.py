# -*- coding: utf-8 -*-
"""
XML-кодек для хранения исходников метаданных 1С (вместо JSON).

Формат приближен к формату 1С:
- корневой элемент - имя типа объекта (Form, Template, ExternalDataProcessor...)
  с uuid в атрибуте;
- словари кодируются семантическими XML-тегами:
  * ключ, являющийся валидным XML-именем и не равным служебным (item, value),
    -> дочерний элемент <Ключ>...</Ключ>;
  * прочие ключи (с пробелами, '/', и т.п.) -> <item name="ключ">...</item>;
- списки (сырые brace-массивы внутреннего формата 1С) кодируются компактным
  текстом внутреннего формата 1С ({1,{...},"строка"}) в тексте элемента
  <value>...</value> - это родной формат 1С, обеспечивающий точный
  round-trip через JsonContainerDecoder;
- скаляры (str/int/float/bool/None) - текст дочернего элемента:
  строки без атрибутов, числа - t="i"/t="f", bool - t="b", None - t="n".

Текстовые значения записываются в одну строку (без добавления переносов
внутрь текста), что позволяет восстанавливать строки точно.
"""
import io
import os
import re
from xml.etree import ElementTree as Et

from .ext_exception import ExtException
from .json_container_decoder import JsonContainerDecoder

ITEM_TAG = 'item'
VALUE_TAG = 'value'
NAME_ATTR = 'name'
TYPE_ATTR = 't'
TYPE_INT = 'i'
TYPE_FLOAT = 'f'
TYPE_BOOL = 'b'
TYPE_NULL = 'n'
TYPE_LIST = 'l'
TYPE_DICT = 'd'

# Служебные имена тегов - не могут использоваться как имена ключей словаря
RESERVED_TAGS = {ITEM_TAG, VALUE_TAG}

# Валидное имя тега: буква/подчёркивание в начале, далее буквы/цифры/._-
# (кириллица разрешена)
re_valid_tag = re.compile(r'^[^\d\W][\w.\-]*$', re.UNICODE)


def _is_valid_tag(name) -> bool:
    if not isinstance(name, str) or not name or name in RESERVED_TAGS or ':' in name:
        return False
    return re_valid_tag.match(name) is not None


def _escape_text(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\r', '&#13;')


def _escape_attr(text: str) -> str:
    return (
        text
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace('\r', '&#13;')
        .replace('\n', '&#10;')
        .replace('\t', '&#9;')
    )


def _unescape_attr(text: str) -> str:
    # ElementTree сам разэкранирует значения атрибутов
    return text


def _scalar_type_attr(value) -> str:
    if isinstance(value, bool):
        return f' {TYPE_ATTR}="{TYPE_BOOL}"'
    if isinstance(value, int):
        return f' {TYPE_ATTR}="{TYPE_INT}"'
    if isinstance(value, float):
        return f' {TYPE_ATTR}="{TYPE_FLOAT}"'
    if value is None:
        return f' {TYPE_ATTR}="{TYPE_NULL}"'
    return ''


def _scalar_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


_brace_decoder = None


def _get_brace_decoder() -> JsonContainerDecoder:
    global _brace_decoder
    if _brace_decoder is None:
        _brace_decoder = JsonContainerDecoder()
    return _brace_decoder


def _is_pure_list(value) -> bool:
    """Список из str/списков (сырой brace-массив, возможно вложенный)."""
    if not isinstance(value, list):
        return False
    return _is_braceable(value)


def _is_braceable(value) -> bool:
    for elem in value:
        if isinstance(elem, str):
            continue
        if isinstance(elem, list) and _is_braceable(elem):
            continue
        return False
    return True


def encode_to_string(data, root_tag: str, uuid=None, attributes: dict = None) -> str:
    """Сериализует данные (dict/список/скаляр) в XML-строку.

    uuid и прочие id-данные (manager_uuid1..4 и т.п.) пишутся атрибутами корня.
    """
    dec = _get_brace_decoder()
    attrs = ''
    if uuid is not None:
        attrs += f' uuid="{_escape_attr(str(uuid))}"'
    if attributes:
        for key, val in attributes.items():
            attrs += f' {_escape_attr(str(key))}="{_escape_attr(str(val))}"'
    parts = [f'<{root_tag}{attrs}>\n']
    _encode_value(parts, data, dec, 0)
    parts.append(f'</{root_tag}>')
    body = ''.join(parts)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + '\n'


def _indent(level: int) -> str:
    return '\t' * level


def _encode_value(parts, value, dec: JsonContainerDecoder, level: int):
    """Кодирует значение как содержимое родительского элемента.

    Вызывается после '>'
    """
    if isinstance(value, dict):
        _encode_dict(parts, value, dec, level)
    elif isinstance(value, list):
        # весь список - компактный brace-текст
        text = dec.encode_root_object([value]) if value else ''
        parts.append(f'{_indent(level + 1)}<{VALUE_TAG} {TYPE_ATTR}="{TYPE_LIST}">'
                     f'{_escape_text(text)}</{VALUE_TAG}>\n{_indent(level)}')
    else:
        parts.append(_escape_text(_scalar_text(value)))


def _encode_dict(parts, value: dict, dec: JsonContainerDecoder, level: int):
    if not value:
        return
    first = True
    for key, val in value.items():
        if not first:
            parts.append('\n')
        first = False
        parts.append(_indent(level + 1))
        _encode_field(parts, key, val, dec, level + 1)
    parts.append('\n')
    parts.append(_indent(level))


def _encode_brace_text(dec: JsonContainerDecoder, val: list):
    """Кодирует список в brace-текст с гарантией точного восстановления.

    JsonContainerDecoder искажает некоторые строки (##base64: и т.п.),
    поэтому проверяем round-trip; при несовпадении возвращаем None.
    """
    text = dec.encode_object(val, False) if val else ''
    if not val:
        return text
    try:
        data = dec.decode_file(io.StringIO(text))
        back = data[0] if len(data) == 1 else data
        if back == val:
            return text.lstrip('\n')
    except Exception:
        pass
    return None


def _encode_field(parts, key, val, dec: JsonContainerDecoder, level: int):
    """Кодирует пару ключ/значение словаря как дочерний элемент."""
    if _is_valid_tag(key):
        tag, attrs = key, ''
    else:
        tag, attrs = ITEM_TAG, f' {NAME_ATTR}="{_escape_attr(str(key))}"'
    if isinstance(val, dict):
        if not val:
            parts.append(f'<{tag}{attrs} {TYPE_ATTR}="{TYPE_DICT}"></{tag}>')
        else:
            parts.append(f'<{tag}{attrs}>\n')
            _encode_dict(parts, val, dec, level)
            parts.append(f'</{tag}>')
    elif _is_pure_list(val):
        # компактный brace-текст родного формата 1С (с проверкой round-trip)
        text = _encode_brace_text(dec, val)
        if text is not None:
            parts.append(f'<{tag}{attrs} {TYPE_ATTR}="{TYPE_LIST}">{_escape_text(text)}</{tag}>')
            return
        # fallback: структурное кодирование ниже
        parts.append(f'<{tag}{attrs}>\n')
        if val:
            first = True
            for elem in val:
                if not first:
                    parts.append('\n')
                first = False
                parts.append(_indent(level + 1))
                _encode_item(parts, elem, dec, level + 1)
            parts.append('\n')
            parts.append(_indent(level))
        parts.append(f'</{tag}>')
    elif isinstance(val, list):
        # список словарей/смешанный - повторяющиеся элементы <item>
        parts.append(f'<{tag}{attrs}>\n')
        if val:
            first = True
            for elem in val:
                if not first:
                    parts.append('\n')
                first = False
                parts.append(_indent(level + 1))
                _encode_item(parts, elem, dec, level + 1)
            parts.append('\n')
            parts.append(_indent(level))
        parts.append(f'</{tag}>')
    else:
        type_attr = _scalar_type_attr(val)
        parts.append(f'<{tag}{attrs}{type_attr}>{_escape_text(_scalar_text(val))}</{tag}>')


def decode_from_string(text: str):
    """Разбирает XML-строку, возвращает структуру (dict/list/str/...)."""
    try:
        root = Et.fromstring(text)
    except Et.ParseError as err:
        raise ExtException(message='Ошибка разбора XML', detail=str(err))
    return _decode_element_value(root)


def _encode_item(parts, val, dec: JsonContainerDecoder, level: int):
    """Кодирует один элемент списка <item>...</item>."""
    if isinstance(val, dict):
        if not val:
            parts.append(f'<{ITEM_TAG} {TYPE_ATTR}="{TYPE_DICT}"></{ITEM_TAG}>')
        else:
            parts.append(f'<{ITEM_TAG}>\n')
            _encode_dict(parts, val, dec, level)
            parts.append(f'</{ITEM_TAG}>')
    elif _is_pure_list(val):
        text = _encode_brace_text(dec, val)
        if text is not None:
            parts.append(f'<{ITEM_TAG} {TYPE_ATTR}="{TYPE_LIST}">{_escape_text(text)}</{ITEM_TAG}>')
            return
        parts.append(f'<{ITEM_TAG}>\n')
        if val:
            first = True
            for elem in val:
                if not first:
                    parts.append('\n')
                first = False
                parts.append(_indent(level + 1))
                _encode_item(parts, elem, dec, level + 1)
            parts.append('\n')
            parts.append(_indent(level))
        parts.append(f'</{ITEM_TAG}>')
    elif isinstance(val, list):
        parts.append(f'<{ITEM_TAG}>\n')
        if val:
            first = True
            for elem in val:
                if not first:
                    parts.append('\n')
                first = False
                parts.append(_indent(level + 1))
                _encode_item(parts, elem, dec, level + 1)
            parts.append('\n')
            parts.append(_indent(level))
        parts.append(f'</{ITEM_TAG}>')
    else:
        type_attr = _scalar_type_attr(val)
        parts.append(f'<{ITEM_TAG}{type_attr}>{_escape_text(_scalar_text(val))}</{ITEM_TAG}>')


def _decode_element_value(elem):
    """Значение, содержащееся в элементе (учитывая имя тега и атрибуты)."""
    if len(elem) == 0:
        return _decode_scalar(elem)
    result = {}
    result_list = []
    has_dict_entry = False
    has_list_entry = False
    for child in elem:
        if child.tag == VALUE_TAG:
            # единственный <value> - весь список целиком
            text = child.text if child.text is not None else ''
            if not text:
                return []
            dec = _get_brace_decoder()
            try:
                data = dec.decode_file(io.StringIO(text))
                return data[0] if len(data) == 1 else data
            except ExtException:
                raise
            except Exception as err:
                raise ExtException(message='Ошибка разбора brace-данных в XML',
                                   detail=f'{elem.tag}: {err}')
        if child.tag == ITEM_TAG and NAME_ATTR not in child.attrib:
            has_list_entry = True
            result_list.append(_decode_element_value(child))
        else:
            has_dict_entry = True
            key = child.attrib.get(NAME_ATTR) if child.tag == ITEM_TAG else child.tag
            if key in result:
                raise ExtException(message='Дублирующийся ключ в XML', detail=f'{elem.tag}/{key}')
            result[key] = _decode_element_value(child)
    if has_dict_entry and has_list_entry:
        raise ExtException(message='Смешанный список/словарь в XML', detail=elem.tag)
    if has_list_entry:
        return result_list
    return result


def _decode_scalar(elem):
    t = elem.attrib.get(TYPE_ATTR)
    text = elem.text if elem.text is not None else ''
    if t == TYPE_LIST:
        if not text:
            return []
        dec = _get_brace_decoder()
        try:
            data = dec.decode_file(io.StringIO(text))
            if len(data) == 1:
                return data[0]
            return data
        except ExtException:
            raise
        except Exception as err:
            raise ExtException(message='Ошибка разбора brace-данных в XML',
                               detail=f'{elem.tag}: {err}')
    if t == TYPE_DICT:
        return {}
    if t == TYPE_INT:
        return int(text)
    if t == TYPE_FLOAT:
        return float(text)
    if t == TYPE_BOOL:
        return text.strip() == 'true'
    if t == TYPE_NULL:
        return None
    return text


def get_root_tag(file_name: str) -> str:
    """Вычисляет имя корневого тега по имени файла.

    'Form.xml' -> Form, 'Form.elem.xml' -> Form, 'Form.id.xml' -> Form
    """
    base = os.path.basename(str(file_name))
    for suffix in ('.elem.xml', '.id.xml', '.xml'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    else:
        base = base.rsplit('.', 1)[0] if '.' in base else base
    if not base or not _is_valid_tag(base):
        base = 'MetaDataObject'
    return base


def write_xml(data, path, file_name: str, uuid=None, attributes: dict = None):
    """Записывает данные в XML-файл (аналог helper.json_write)."""
    root_tag = get_root_tag(file_name)
    _path = os.path.normpath(os.path.join(path, file_name))
    try:
        os.makedirs(path, exist_ok=True)
        text = encode_to_string(data, root_tag, uuid=uuid, attributes=attributes)
        with open(_path, 'w', encoding='utf-8', newline='') as file:
            file.write(text)
    except Exception as err:
        raise ExtException(message='Ошибка записи XML', detail=f'{err} в файле ({_path})')


def read_xml(path, file_name: str):
    """Читает XML-файл, возвращает структуру данных (аналог helper.json_read)."""
    _path = os.path.normpath(os.path.join(path, file_name))
    try:
        with open(_path, 'r', encoding='utf-8') as file:
            text = file.read()
        return decode_from_string(text)
    except FileNotFoundError as err:
        raise err
    except Et.ParseError as err:
        raise ExtException(message='Ошибка разбора XML', detail=f'{err} в файле ({_path})')
    except ExtException:
        raise
    except Exception as err:
        raise ExtException(message='Ошибка чтения XML', detail=f'{err} в файле ({_path})')


def read_uuid(path, file_name: str):
    """Читает uuid из атрибута корневого элемента XML-файла (или None)."""
    _path = os.path.normpath(os.path.join(path, file_name))
    try:
        with open(_path, 'r', encoding='utf-8') as file:
            text = file.read()
        root = Et.fromstring(text)
        return root.attrib.get('uuid')
    except FileNotFoundError as err:
        raise err
    except Exception as err:
        raise ExtException(message='Ошибка чтения uuid из XML', detail=f'{err} в файле ({_path})')


def read_attributes(path, file_name: str) -> dict:
    """Читает все атрибуты корневого элемента XML-файла (uuid, manager_uuid1..4 и т.п.)."""
    _path = os.path.normpath(os.path.join(path, file_name))
    try:
        with open(_path, 'r', encoding='utf-8') as file:
            text = file.read()
        root = Et.fromstring(text)
        return dict(root.attrib)
    except FileNotFoundError as err:
        raise err
    except Exception as err:
        raise ExtException(message='Ошибка чтения атрибутов из XML', detail=f'{err} в файле ({_path})')
