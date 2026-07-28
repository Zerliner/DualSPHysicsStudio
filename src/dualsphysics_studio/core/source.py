"""Safe XML parsing and lossless source-location support.

``xml.etree`` is used only as a semantic tree.  The original Unicode string is
the authoritative representation and edits are applied to precise spans in that
string.  Comments, indentation, quote style and unknown nodes therefore survive
round trips unless the user edits that exact token.
"""

from __future__ import annotations

from dataclasses import dataclass
import codecs
import re
from typing import Iterator
import xml.etree.ElementTree as ET

from .errors import SourceLocationError, UnsafeXmlError, XmlSyntaxError


MAX_XML_CHARS = 20 * 1024 * 1024
MAX_XML_BYTES = 4 * MAX_XML_CHARS
MAX_XML_ELEMENTS = 250_000
MAX_XML_DEPTH = 256

_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_DECL_ENCODING = re.compile(
    br"^\s*<\?xml\s+[^>]*encoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_NAME = re.compile(r"[A-Za-z_:\u0080-\uffff][\w.\-:\u0080-\uffff]*")


def local_name(tag: str) -> str:
    """Return the namespace-independent local name used by GenCase XML."""

    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def is_element(node: ET.Element) -> bool:
    return isinstance(node.tag, str)


def secure_parse(text: str) -> ET.Element:
    """Parse XML while prohibiting DTDs and all entity declarations.

    The standard library parser never receives a document containing a DTD or
    declared entity.  This blocks external-entity access as well as expansion
    bombs.  Conservative size, element-count and nesting limits add predictable
    memory behaviour for a desktop editor.
    """

    if not isinstance(text, str):
        raise TypeError("XML source must be a Unicode string")
    if len(text) > MAX_XML_CHARS:
        raise UnsafeXmlError(f"XML 文件超过安全上限 {MAX_XML_CHARS // (1024 * 1024)} MiB")
    declaration = _FORBIDDEN_DECLARATION.search(text)
    if declaration:
        raise UnsafeXmlError("为防止外部实体和实体展开，DualSPHysics Studio 不允许 DTD/ENTITY")
    try:
        parser = ET.XMLParser()
        root = ET.fromstring(text, parser=parser)
    except ET.ParseError as exc:
        line = column = None
        if getattr(exc, "position", None):
            line, column = exc.position
        raise XmlSyntaxError(str(exc), line=line, column=column) from exc

    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_XML_ELEMENTS:
            raise UnsafeXmlError(f"XML 元素数量超过安全上限 {MAX_XML_ELEMENTS}")
        if depth > MAX_XML_DEPTH:
            raise UnsafeXmlError(f"XML 嵌套深度超过安全上限 {MAX_XML_DEPTH}")
        stack.extend((child, depth + 1) for child in list(node) if is_element(child))
    return root


@dataclass(slots=True)
class SourceToken:
    path: str
    name: str
    start: int
    start_tag_end: int
    close_start: int | None
    end: int
    self_closing: bool
    has_element_children: bool = False

    @property
    def start_tag_span(self) -> tuple[int, int]:
        return (self.start, self.start_tag_end)

    @property
    def full_span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def content_span(self) -> tuple[int, int] | None:
        if self.self_closing or self.close_start is None:
            return None
        return (self.start_tag_end, self.close_start)


@dataclass(slots=True)
class _StackEntry:
    token: SourceToken
    child_counts: dict[str, int]


class SourceIndex:
    """Index XML element start tags and content spans without reformatting XML."""

    def __init__(self, text: str):
        self.text = text
        self.tokens: dict[str, SourceToken] = {}
        self._scan()

    def require(self, path: str) -> SourceToken:
        try:
            return self.tokens[path]
        except KeyError as exc:
            raise SourceLocationError(f"找不到 XML 节点：{path}") from exc

    def _scan(self) -> None:
        text = self.text
        position = 0
        root_counts: dict[str, int] = {}
        stack: list[_StackEntry] = []
        element_count = 0

        while True:
            start = text.find("<", position)
            if start < 0:
                break

            if text.startswith("<!--", start):
                end = text.find("-->", start + 4)
                if end < 0:
                    raise XmlSyntaxError("未结束的 XML 注释")
                position = end + 3
                continue
            if text.startswith("<![CDATA[", start):
                end = text.find("]]>", start + 9)
                if end < 0:
                    raise XmlSyntaxError("未结束的 CDATA")
                position = end + 3
                continue
            if text.startswith("<?", start):
                end = text.find("?>", start + 2)
                if end < 0:
                    raise XmlSyntaxError("未结束的处理指令")
                position = end + 2
                continue
            if text.startswith("<!", start):
                # secure_parse has already rejected DTD and ENTITY.  Any other
                # declaration is not part of the supported DualSPHysics format.
                end = _find_tag_end(text, start)
                position = end
                continue

            tag_end = _find_tag_end(text, start)
            raw_tag = text[start:tag_end]

            if raw_tag.startswith("</"):
                match = _NAME.match(raw_tag, 2)
                if match and stack:
                    closing_name = local_name(match.group(0))
                    entry = stack.pop()
                    if entry.token.name != closing_name:
                        # The semantic parser supplies the friendlier syntax
                        # error.  This protects callers using SourceIndex alone.
                        raise XmlSyntaxError(
                            f"结束标签 {closing_name} 与 {entry.token.name} 不匹配"
                        )
                    entry.token.close_start = start
                    entry.token.end = tag_end
                position = tag_end
                continue

            match = _NAME.match(raw_tag, 1)
            if not match:
                position = tag_end
                continue
            name = local_name(match.group(0))
            counters = stack[-1].child_counts if stack else root_counts
            occurrence = counters.get(name, 0) + 1
            counters[name] = occurrence
            parent_path = stack[-1].token.path if stack else ""
            path = f"{parent_path}/{name}[{occurrence}]"
            self_closing = raw_tag.rstrip().endswith("/>")
            token = SourceToken(
                path=path,
                name=name,
                start=start,
                start_tag_end=tag_end,
                close_start=None,
                end=tag_end,
                self_closing=self_closing,
            )
            self.tokens[path] = token
            element_count += 1
            if element_count > MAX_XML_ELEMENTS:
                raise UnsafeXmlError(f"XML 元素数量超过安全上限 {MAX_XML_ELEMENTS}")
            if stack:
                stack[-1].token.has_element_children = True
            if not self_closing:
                stack.append(_StackEntry(token, {}))
                if len(stack) > MAX_XML_DEPTH:
                    raise UnsafeXmlError(f"XML 嵌套深度超过安全上限 {MAX_XML_DEPTH}")
            position = tag_end

        if stack:
            raise XmlSyntaxError(f"元素 {stack[-1].token.name} 未结束")


def _find_tag_end(text: str, start: int) -> int:
    quote: str | None = None
    position = start + 1
    while position < len(text):
        char = text[position]
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == ">":
            return position + 1
        position += 1
    raise XmlSyntaxError("未结束的 XML 标签")


def build_element_paths(root: ET.Element) -> tuple[dict[int, str], dict[str, ET.Element]]:
    """Build paths matching :class:`SourceIndex` for an ElementTree."""

    by_identity: dict[int, str] = {}
    by_path: dict[str, ET.Element] = {}

    def visit(node: ET.Element, path: str) -> None:
        by_identity[id(node)] = path
        by_path[path] = node
        counts: dict[str, int] = {}
        for child in list(node):
            if not is_element(child):
                continue
            name = local_name(child.tag)
            counts[name] = counts.get(name, 0) + 1
            visit(child, f"{path}/{name}[{counts[name]}]")

    root_name = local_name(root.tag)
    visit(root, f"/{root_name}[1]")
    return by_identity, by_path


def iter_elements(root: ET.Element, name: str | None = None) -> Iterator[ET.Element]:
    for node in root.iter():
        if not is_element(node):
            continue
        if name is None or local_name(node.tag) == name:
            yield node


def decode_xml_bytes(data: bytes) -> tuple[str, str, bytes]:
    """Decode XML and return ``(text, codec, original_bom)``."""

    bom = b""
    codec = "utf-8"
    payload = data
    if data.startswith(codecs.BOM_UTF8):
        bom = codecs.BOM_UTF8
        payload = data[len(bom) :]
        codec = "utf-8"
    elif data.startswith(codecs.BOM_UTF16_LE):
        bom = codecs.BOM_UTF16_LE
        payload = data[len(bom) :]
        codec = "utf-16-le"
    elif data.startswith(codecs.BOM_UTF16_BE):
        bom = codecs.BOM_UTF16_BE
        payload = data[len(bom) :]
        codec = "utf-16-be"
    else:
        declaration = _XML_DECL_ENCODING.search(data[:512])
        if declaration:
            try:
                codec = declaration.group(1).decode("ascii")
                codecs.lookup(codec)
            except (UnicodeDecodeError, LookupError) as exc:
                raise XmlSyntaxError("XML 声明了不受支持的字符编码") from exc
    try:
        return payload.decode(codec), codec, bom
    except UnicodeDecodeError as exc:
        raise XmlSyntaxError(f"无法按 {codec} 解码 XML：{exc}") from exc


def encode_xml_text(text: str, codec: str, bom: bytes = b"") -> bytes:
    try:
        return bom + text.encode(codec)
    except UnicodeEncodeError as exc:
        raise XmlSyntaxError(f"修改后的内容无法按原编码 {codec} 保存：{exc}") from exc
