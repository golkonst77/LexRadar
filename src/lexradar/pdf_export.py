"""Small offline Unicode PDF export using pypdf and an installed TrueType font."""

import struct
import textwrap
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def _dict(**values):
    return DictionaryObject({NameObject("/" + k): v for k, v in values.items()})


def _glyph_map(font):
    """Read a Windows BMP cmap (format 4); reject unsupported fonts explicitly."""
    tables = {}
    for i in range(struct.unpack_from(">H", font, 4)[0]):
        tag, _, offset, size = struct.unpack_from(">4sIII", font, 12 + 16 * i)
        tables[tag] = (offset, size)
    cmap = tables[b"cmap"][0]
    for i in range(struct.unpack_from(">H", font, cmap + 2)[0]):
        platform, encoding, offset = struct.unpack_from(">HHI", font, cmap + 4 + i * 8)
        base = cmap + offset
        if platform == 3 and encoding == 1 and struct.unpack_from(">H", font, base)[0] == 4:
            break
    else:
        raise ValueError("Font requires Windows BMP cmap format 4")
    count = struct.unpack_from(">H", font, base + 6)[0] // 2
    ends = base + 14
    starts = ends + 2 * count + 2
    deltas = starts + 2 * count
    ranges = deltas + 2 * count
    mapping = bytearray(131072)
    for i in range(count):
        end = struct.unpack_from(">H", font, ends + 2 * i)[0]
        start = struct.unpack_from(">H", font, starts + 2 * i)[0]
        delta = struct.unpack_from(">h", font, deltas + 2 * i)[0]
        distance = struct.unpack_from(">H", font, ranges + 2 * i)[0]
        for code in range(start, end + 1):
            glyph = code
            if distance:
                glyph = struct.unpack_from(
                    ">H", font, ranges + 2 * i + distance + 2 * (code - start)
                )[0]
            if glyph or not distance:
                glyph = (glyph + delta) & 65535
            struct.pack_into(">H", mapping, 2 * code, glyph)
    return bytes(mapping)


def write_pdf(lines, output: Path, *, font_path: Path | None = None):
    """Export plain text with embedded Cyrillic font; no browser, URLs or JavaScript."""
    font_path = font_path or Path("/usr/share/fonts/truetype/freefont/FreeMono.ttf")
    font_bytes = font_path.read_bytes()
    glyphs = _glyph_map(font_bytes)
    text_lines = []
    for line in lines:
        text_lines.extend(textwrap.wrap(str(line), width=88) or [""])
    for char in "".join(text_lines):
        code = ord(char)
        if code > 65535 or (not char.isspace() and glyphs[code * 2 : code * 2 + 2] == b"\0\0"):
            raise ValueError(f"PDF font does not cover U+{code:04X}")
    writer = PdfWriter()

    def stream(data):
        obj = DecodedStreamObject()
        obj.set_data(data)
        return writer._add_object(obj)

    descriptor = writer._add_object(
        _dict(
            Type=NameObject("/FontDescriptor"),
            FontName=NameObject("/FreeMono"),
            Flags=NumberObject(33),
            FontBBox=ArrayObject([NumberObject(x) for x in [-1000, -500, 2000, 1500]]),
            ItalicAngle=NumberObject(0),
            Ascent=NumberObject(800),
            Descent=NumberObject(-200),
            CapHeight=NumberObject(700),
            StemV=NumberObject(80),
            FontFile2=stream(font_bytes),
        )
    )
    descendant = writer._add_object(
        _dict(
            Type=NameObject("/Font"),
            Subtype=NameObject("/CIDFontType2"),
            BaseFont=NameObject("/FreeMono"),
            FontDescriptor=descriptor,
            CIDSystemInfo=_dict(
                Registry=TextStringObject("Adobe"),
                Ordering=TextStringObject("Identity"),
                Supplement=NumberObject(0),
            ),
            DW=NumberObject(600),
            CIDToGIDMap=stream(glyphs),
        )
    )
    unicode_map = b"""/CIDInit /ProcSet findresource begin
12 dict begin begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def /CMapType 2 def
1 begincodespacerange <0000> <FFFF> endcodespacerange
1 beginbfrange <0000> <FFFF> <0000> endbfrange
endcmap CMapName currentdict /CMap defineresource pop end end"""
    font = writer._add_object(
        _dict(
            Type=NameObject("/Font"),
            Subtype=NameObject("/Type0"),
            BaseFont=NameObject("/FreeMono"),
            Encoding=NameObject("/Identity-H"),
            DescendantFonts=ArrayObject([descendant]),
            ToUnicode=stream(unicode_map),
        )
    )
    for offset in range(0, len(text_lines), 54):
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = _dict(Font=_dict(F1=font))
        content = ["BT /F1 10 Tf 13 TL 32 804 Td"]
        for line in text_lines[offset : offset + 54]:
            content.append("<" + line.encode("utf-16-be").hex() + "> Tj T*")
        content.append("ET")
        page[NameObject("/Contents")] = stream("\n".join(content).encode("ascii"))
    with output.open("xb") as target:
        writer.write(target)
