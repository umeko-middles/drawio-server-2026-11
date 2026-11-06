import base64
import re
import zlib
from urllib.parse import unquote_to_bytes
from xml.etree import ElementTree as ET


MAX_XML_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
BLANK_XML = (
    '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
    '</root></mxGraphModel>'
)


class InvalidDiagram(ValueError):
    pass


class DiagramTooLarge(InvalidDiagram):
    pass


class DiagramTree(ET.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        raise InvalidDiagram("DOCTYPE is not allowed")


def parse_xml(xml: str) -> ET.Element:
    declaration = re.match(r"\s*<\?xml\s+[^?]*?encoding\s*=\s*['\"]([^'\"]+)", xml)
    if declaration and declaration[1].lower() != "utf-8":
        raise InvalidDiagram("XML encoding must be UTF-8")
    try:
        return ET.fromstring(xml, parser=ET.XMLParser(target=DiagramTree()))
    except ET.ParseError as exc:
        raise InvalidDiagram(f"Malformed XML at line {exc.position[0]}, column {exc.position[1]}") from exc


def check_model(model: ET.Element) -> None:
    if model.tag != "mxGraphModel" or len(model) != 1 or model[0].tag != "root":
        raise InvalidDiagram("Expected mxGraphModel with a root element")


def expand_page(data: str, remaining: int) -> bytes:
    try:
        compressed = base64.b64decode("".join(data.split()), validate=True)
        inflater = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
        expanded = inflater.decompress(compressed, remaining + 1)
    except (ValueError, zlib.error) as exc:
        raise InvalidDiagram("Invalid compressed Draw.io page") from exc
    if len(expanded) > remaining:
        raise DiagramTooLarge("Expanded diagram exceeds 16 MiB")
    if not inflater.eof or inflater.unused_data:
        raise InvalidDiagram("Invalid compressed Draw.io page")
    return expanded


def validate_xml(raw: bytes | bytearray) -> tuple[str, int]:
    if len(raw) > MAX_XML_BYTES:
        raise DiagramTooLarge("Draw.io XML exceeds 4 MiB")
    try:
        xml = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidDiagram("XML encoding must be UTF-8") from exc
    document = parse_xml(xml)
    if document.tag == "mxGraphModel":
        check_model(document)
        return xml, 1
    if document.tag != "mxfile" or not len(document):
        raise InvalidDiagram("Expected mxfile with at least one diagram, or mxGraphModel")

    remaining = MAX_EXPANDED_BYTES
    for page in document:
        if page.tag != "diagram" or len(page) > 1:
            raise InvalidDiagram("Expected a Draw.io diagram page")
        if len(page):
            check_model(page[0])
        elif page.text and page.text.strip():
            expanded = expand_page(page.text, remaining)
            remaining -= len(expanded)
            try:
                check_model(parse_xml(unquote_to_bytes(expanded).decode("utf-8")))
            except UnicodeDecodeError as exc:
                raise InvalidDiagram("Invalid UTF-8 in compressed Draw.io page") from exc
        else:
            raise InvalidDiagram("Empty Draw.io diagram page")
    return xml, len(document)
