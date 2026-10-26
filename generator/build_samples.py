#!/usr/bin/env python3
"""Build every sample in samples/ and its expected output in expected/.

Usage:
    python generator/build_samples.py            # rewrite samples/ and expected/
    python generator/build_samples.py --check    # verify committed files match, change nothing

Only the Python standard library is used. The output is deterministic: entry order and
timestamps are fixed, and --check compares entry names and contents rather than ZIP bytes,
so a different zlib version cannot cause false failures.

Every sample is a plain test document. Nothing here imitates an invoice, a receipt or any
other official record, and no real names, companies or numbers are used (see AGENTS.md).
Samples that exercise invoice-style custom tags show ordinary values on an empty page.

Expected positions are computed from the scene each builder describes, using the
coordinate rules of GB/T 33190: a text object's Boundary origin, then its CTM, then the
TextCode X/Y. They do not come from any OFD reader.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent.parent
OFD_NS = "http://www.ofdspec.org/2016"
OLD_OFD_NS = "http://www.ofdspec.org"
FP_NS = "http://www.edrm.org.cn/schema/e-invoice/2019"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
A4 = (210.0, 297.0)

Affine = tuple[float, float, float, float, float, float]
IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# --------------------------------------------------------------------------- scene model


@dataclass
class Code:
    """A TextCode. X or Y left as None is omitted and reuses the previous TextCode's value."""

    text: str
    x: float | None = 0.0
    y: float | None = None
    delta_x: str | None = None


@dataclass
class TextObj:
    id: int
    codes: list[Code]
    boundary: tuple[float, float, float, float]
    size: float = 4.0
    ctm: Affine | None = None

    @staticmethod
    def simple(object_id: int, text: str, x: float, y: float, size: float = 4.0) -> TextObj:
        """Text whose first glyph's baseline sits at (x, y) on the page."""
        return TextObj(object_id, [Code(text, 0.0, size)], (x, y - size, max(len(text), 1) * size, size * 1.25), size)


@dataclass
class Block:
    """A PageBlock holding other objects."""

    id: int
    children: list[TextObj | Block | Composite]


@dataclass
class Composite:
    """A CompositeObject drawing a composite graphic unit."""

    id: int
    unit_id: int
    boundary: tuple[float, float, float, float]
    ctm: Affine | None = None


@dataclass
class Unit:
    """A CompositeGraphicUnit (lives in DocumentRes.xml)."""

    id: int
    objects: list[TextObj]
    width: float = 100.0
    height: float = 100.0


@dataclass
class PageSpec:
    objects: list[TextObj | Block | Composite]
    id: int = 0
    box: tuple[float, float] | None = None  # None: the document's PageArea
    template_id: int | None = None
    location: str | None = None  # BaseLoc override
    missing: bool = False  # reference the page but do not write its content


@dataclass
class Sample:
    name: str
    description: str
    conforming: bool
    features: list[str]
    pages: list[PageSpec]
    texts: list[str]  # expected text of each page, one line per text line, no blank lines
    title: str = "测试样例"
    page_area: tuple[float, float] = A4
    namespace: str = OFD_NS
    templates: dict[int, list[TextObj]] = field(default_factory=dict)
    template_locations: dict[int, str] = field(default_factory=dict)
    units: list[Unit] = field(default_factory=list)
    custom_data: dict[str, str] = field(default_factory=dict)
    custom_tag: str | None = None
    custom_tag_values: list[dict[str, str]] = field(default_factory=list)
    attachment: str | None = None
    attachment_values: list[dict[str, str]] = field(default_factory=list)
    absolute_paths: bool = False
    gbk_entry_names: bool = False
    xml_encoding: str = "UTF-8"
    bom: bool = False
    rename: dict[str, str] = field(default_factory=dict)  # entry name -> name actually written
    replace: dict[str, bytes] = field(default_factory=dict)  # entry name -> raw content


# --------------------------------------------------------------------------- XML writing


class Writer:
    def __init__(self, sample: Sample) -> None:
        self.s = sample
        self.prefix = "ofd:" if sample.namespace else ""

    def el(self, name: str, body: str = "", attributes: dict[str, object] | None = None, root: bool = False) -> str:
        attrs = dict(attributes or {})
        if root and self.s.namespace:
            attrs = {"xmlns:ofd": self.s.namespace, **attrs}
        rendered = "".join(f" {key}={quoteattr(_fmt(value))}" for key, value in attrs.items() if value is not None)
        tag = self.prefix + name
        if body == "":
            return f"<{tag}{rendered}/>"
        return f"<{tag}{rendered}>{body}</{tag}>"

    def xml(self, body: str) -> bytes:
        encoding = self.s.xml_encoding
        text = f'<?xml version="1.0" encoding="{encoding}"?>\n{body}\n'
        data = text.encode("gb18030" if encoding.upper() in ("GB18030", "GBK", "GB2312") else "utf-8")
        return (b"\xef\xbb\xbf" + data) if self.s.bom else data

    def text_object(self, obj: TextObj) -> str:
        codes = "".join(
            self.el("TextCode", escape(code.text), {"X": code.x, "Y": code.y, "DeltaX": code.delta_x}) for code in obj.codes
        )
        attrs: dict[str, object] = {"ID": obj.id, "Boundary": _fmt(obj.boundary), "Font": 2, "Size": obj.size}
        if obj.ctm is not None:
            attrs["CTM"] = _fmt(obj.ctm)
        return self.el("TextObject", codes, attrs)

    def objects(self, items: list[TextObj | Block | Composite]) -> str:
        out = []
        for item in items:
            if isinstance(item, TextObj):
                out.append(self.text_object(item))
            elif isinstance(item, Block):
                out.append(self.el("PageBlock", self.objects(item.children), {"ID": item.id}))
            else:
                attrs: dict[str, object] = {"ID": item.id, "Boundary": _fmt(item.boundary), "ResourceID": item.unit_id}
                if item.ctm is not None:
                    attrs["CTM"] = _fmt(item.ctm)
                out.append(self.el("CompositeObject", "", attrs))
        return "".join(out)

    def content(self, items: list[TextObj | Block | Composite], layer_id: int, page: PageSpec | None = None) -> bytes:
        body = ""
        if page is not None and page.box is not None:
            body += self.el("Area", self.el("PhysicalBox", f"0 0 {_fmt(page.box[0])} {_fmt(page.box[1])}"))
        if page is not None and page.template_id is not None:
            body += self.el("Template", "", {"TemplateID": page.template_id, "ZOrder": "Background"})
        body += self.el("Content", self.el("Layer", self.objects(items), {"ID": layer_id}))
        return self.xml(self.el("Page", body, root=True))


def build_parts(s: Sample) -> dict[str, bytes]:
    w = Writer(s)
    e = w.el
    loc = (lambda path: "/Doc_0/" + path) if s.absolute_paths else (lambda path: path)
    parts: dict[str, bytes] = {}

    datas = "".join(e("CustomData", escape(v), {"Name": k}) for k, v in s.custom_data.items())
    info = e("DocID", "0" * 32) + e("Title", escape(s.title)) + e("Creator", "ofd-samples") + e("CreationDate", "2026-01-01")
    if datas:
        info += e("CustomDatas", datas)
    doc_root = "/Doc_0/Document.xml" if s.absolute_paths else "Doc_0/Document.xml"
    parts["OFD.xml"] = w.xml(e("OFD", e("DocBody", e("DocInfo", info) + e("DocRoot", doc_root)), {"Version": "1.0", "DocType": "OFD"}, root=True))

    common = e("MaxUnitID", 9999)
    common += e("PageArea", e("PhysicalBox", f"0 0 {_fmt(s.page_area[0])} {_fmt(s.page_area[1])}"))
    common += e("PublicRes", "PublicRes.xml")
    if s.units:
        common += e("DocumentRes", "DocumentRes.xml")
    for template_id, objects in s.templates.items():
        location = s.template_locations.get(template_id, f"Tpls/Tpl_{template_id}/Content.xml")
        common += e("TemplatePage", "", {"ID": template_id, "BaseLoc": loc(location)})
        parts[f"Doc_0/Tpls/Tpl_{template_id}/Content.xml"] = w.content(objects, 9000 + template_id)

    pages = ""
    for index, page in enumerate(s.pages):
        location = page.location or f"Pages/Page_{index}/Content.xml"
        pages += e("Page", "", {"ID": page.id, "BaseLoc": loc(location)})
        if not page.missing:
            parts[f"Doc_0/Pages/Page_{index}/Content.xml"] = w.content(page.objects, 8000 + index, page)

    extras = ""
    if s.custom_tag is not None:
        extras += e("CustomTags", "Tags/CustomTags.xml")
        parts["Doc_0/Tags/CustomTags.xml"] = w.xml(e("CustomTags", e("CustomTag", e("FileLoc", "CustomTag.xml"), {"TypeID": "test"}), root=True))
        parts["Doc_0/Tags/CustomTag.xml"] = w.xml(s.custom_tag)
    if s.attachment is not None:
        extras += e("Attachments", "Attachs/Attachments.xml")
        item = e("Attachment", e("FileLoc", "data.xml"), {"ID": 7000, "Name": "data", "Format": "xml", "Visible": "false"})
        parts["Doc_0/Attachs/Attachments.xml"] = w.xml(e("Attachments", item, root=True))
        parts["Doc_0/Attachs/data.xml"] = w.xml(s.attachment)

    parts["Doc_0/Document.xml"] = w.xml(e("Document", e("CommonData", common) + e("Pages", pages) + extras, root=True))
    font = e("Font", "", {"ID": 2, "FontName": "宋体", "FamilyName": "宋体"})
    parts["Doc_0/PublicRes.xml"] = w.xml(e("Res", e("Fonts", font), {"BaseLoc": "Res"}, root=True))
    if s.units:
        units = "".join(
            e("CompositeGraphicUnit", e("Content", w.objects(list(unit.objects))), {"ID": unit.id, "Width": unit.width, "Height": unit.height})
            for unit in s.units
        )
        parts["Doc_0/DocumentRes.xml"] = w.xml(e("Res", e("CompositeGraphicUnits", units), root=True))

    parts.update(s.replace)
    return {s.rename.get(name, name): data for name, data in sorted(parts.items(), key=lambda item: _entry_order(item[0]))}


def _entry_order(name: str) -> tuple[int, str]:
    return (0 if name == "OFD.xml" else 1, name)


# --------------------------------------------------------------------------- expected output


def compose(outer: Affine, inner: Affine) -> Affine:
    A, B, C, D, E, F = outer
    a, b, c, d, e, f = inner
    return (A * a + C * b, B * a + D * b, A * c + C * d, B * c + D * d, A * e + C * f + E, B * e + D * f + F)


def object_transform(boundary: tuple[float, float, float, float], ctm: Affine | None) -> Affine:
    a, b, c, d, e, f = ctm or IDENTITY
    return (a, b, c, d, e + boundary[0], f + boundary[1])


def expected_objects(items: list[TextObj | Block | Composite], units: dict[int, Unit], outer: Affine, from_template: bool) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if isinstance(item, TextObj):
            a, b, c, d, e, f = compose(outer, object_transform(item.boundary, item.ctm))
            x = y = 0.0
            runs = []
            for code in item.codes:
                x = code.x if code.x is not None else x
                y = code.y if code.y is not None else y
                if code.text:
                    runs.append({"text": code.text, "x": _round(a * x + c * y + e), "y": _round(b * x + d * y + f)})
            scale = abs(a * d - b * c) ** 0.5
            out.append(
                {
                    "id": str(item.id),
                    "text": "".join(code.text for code in item.codes),
                    "size": _round(item.size * scale),
                    "from_template": from_template,
                    "runs": runs,
                }
            )
        elif isinstance(item, Block):
            out.extend(expected_objects(item.children, units, outer, from_template))
        else:
            unit = units[item.unit_id]
            out.extend(expected_objects(list(unit.objects), units, compose(outer, object_transform(item.boundary, item.ctm)), from_template))
    return out


def build_expected(s: Sample) -> dict:
    units = {unit.id: unit for unit in s.units}
    assert len(s.texts) == len(s.pages), f"{s.name}: one expected text per page"
    pages: list[dict] = []
    for page in s.pages:
        width, height = page.box or s.page_area
        objects: list[dict] = []
        readable = not page.missing
        if readable and page.template_id is not None:
            objects += expected_objects(list(s.templates[page.template_id]), units, IDENTITY, True)
        if readable:
            objects += expected_objects(page.objects, units, IDENTITY, False)
        pages.append(
            {
                "id": str(page.id),
                "width": width,
                "height": height,
                "readable": readable,
                "text": s.texts[len(pages)],
                "text_objects": objects,
            }
        )
    expected: dict = {
        "name": s.name,
        "description": s.description,
        "conforming": s.conforming,
        "features": s.features,
        "doc_info": {"title": s.title, "custom_data": s.custom_data},
        "pages": pages,
    }
    if s.custom_tag_values:
        expected["custom_tag_values"] = s.custom_tag_values
    if s.attachment_values:
        expected["attachment_values"] = s.attachment_values
    return expected


# --------------------------------------------------------------------------- the samples


def refs(*object_ids: int, page: int = 1, prefix: str = "ofd") -> str:
    return "".join(f'<{prefix}:ObjectRef PageRef="{page}">{object_id}</{prefix}:ObjectRef>' for object_id in object_ids)


def value_page(values: list[tuple[int, str]]) -> list[TextObj]:
    """A plain column of values, 8 mm apart: no labels, no layout."""
    return [TextObj.simple(object_id, text, 20, 30 + 8 * index) for index, (object_id, text) in enumerate(values)]


def samples() -> list[Sample]:
    s: list[Sample] = []

    s.append(Sample(
        name="hello",
        description="One page with one text object. The smallest useful OFD file.",
        conforming=True,
        features=["basic"],
        pages=[PageSpec([TextObj.simple(101, "你好，OFD！", 20, 30)])],
        texts=["你好，OFD！"],
    ))

    s.append(Sample(
        name="multi-page-sizes",
        description="Three pages: the document's A4 PageArea, an A4 landscape override, and a small custom page box.",
        conforming=True,
        features=["multiple pages", "page area override"],
        pages=[
            PageSpec([TextObj.simple(101, "第一页：A4 纵向", 20, 30)]),
            PageSpec([TextObj.simple(201, "第二页：A4 横向", 20, 30)], box=(297.0, 210.0)),
            PageSpec([TextObj.simple(301, "第三页：自定义尺寸", 10, 20)], box=(100.0, 60.0)),
        ],
        texts=["第一页：A4 纵向", "第二页：A4 横向", "第三页：自定义尺寸"],
    ))

    s.append(Sample(
        name="template-page",
        description="Each page draws a template with a header line behind its own content. "
        "Template text belongs to every page that uses the template.",
        conforming=True,
        features=["templates"],
        templates={1: [TextObj.simple(901, "模板页眉", 20, 20)]},
        pages=[
            PageSpec([TextObj.simple(101, "正文第一页", 20, 40)], template_id=1),
            PageSpec([TextObj.simple(201, "正文第二页", 20, 40)], template_id=1),
        ],
        texts=["模板页眉\n正文第一页", "模板页眉\n正文第二页"],
    ))

    s.append(Sample(
        name="text-transforms",
        description="Text placed in different ways: a scaling CTM, two TextCodes where the second omits Y "
        "and reuses the first one's, and DeltaX with the 'g' shorthand.",
        conforming=True,
        features=["CTM", "TextCode X/Y reuse", "DeltaX shorthand"],
        pages=[PageSpec([
            TextObj(101, [Code("缩放两倍", 1.0, 2.0)], (20.0, 20.0, 60.0, 10.0), size=2.0, ctm=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0)),
            TextObj(102, [Code("前半", 0.0, 4.0), Code("后半", 40.0, None)], (20.0, 40.0, 80.0, 6.0)),
            TextObj(103, [Code("间距相等的文字", 0.0, 4.0, delta_x="g 6 4.5")], (20.0, 60.0, 80.0, 6.0)),
        ])],
        texts=["缩放两倍\n前半 后半\n间距相等的文字"],
    ))

    s.append(Sample(
        name="page-blocks",
        description="Text nested three PageBlocks deep, plus text directly in the layer.",
        conforming=True,
        features=["PageBlock"],
        pages=[PageSpec([
            TextObj.simple(101, "图层中的文字", 20, 30),
            Block(110, [TextObj.simple(111, "第一层", 20, 40), Block(120, [Block(130, [TextObj.simple(131, "第三层", 20, 50)])])]),
        ])],
        texts=["图层中的文字\n第一层\n第三层"],
    ))

    s.append(Sample(
        name="composite-objects",
        description="A composite graphic unit holding text, drawn twice: once at its own scale and once "
        "scaled by half through the CompositeObject's CTM.",
        conforming=True,
        features=["CompositeObject", "CompositeGraphicUnit", "DocumentRes"],
        units=[Unit(500, [TextObj.simple(501, "复合对象", 5, 9)])],
        pages=[PageSpec([
            Composite(601, 500, (20.0, 20.0, 100.0, 100.0)),
            Composite(602, 500, (20.0, 60.0, 100.0, 100.0), ctm=(0.5, 0.0, 0.0, 0.5, 0.0, 0.0)),
        ])],
        texts=["复合对象\n复合对象"],
    ))

    s.append(Sample(
        name="absolute-paths",
        description="DocRoot, BaseLoc and template locations are absolute paths starting at the package root.",
        conforming=True,
        features=["absolute paths"],
        absolute_paths=True,
        templates={1: [TextObj.simple(901, "绝对路径模板", 20, 20)]},
        pages=[PageSpec([TextObj.simple(101, "绝对路径正文", 20, 30)], template_id=1)],
        texts=["绝对路径模板\n绝对路径正文"],
    ))

    s.append(Sample(
        name="old-namespace",
        description="Uses the namespace http://www.ofdspec.org (without /2016), which some producers write.",
        conforming=False,
        features=["namespace variant"],
        namespace=OLD_OFD_NS,
        pages=[PageSpec([TextObj.simple(101, "旧命名空间", 20, 30)])],
        texts=["旧命名空间"],
    ))

    s.append(Sample(
        name="path-case-mismatch",
        description="The template's BaseLoc says TPLS/tpl_1/content.xml but the entry is Tpls/Tpl_1/Content.xml. "
        "Readers on case-sensitive systems need a case-insensitive fallback.",
        conforming=False,
        features=["path case mismatch", "templates"],
        templates={1: [TextObj.simple(901, "大小写不一致的模板", 20, 20)]},
        template_locations={1: "TPLS/tpl_1/content.xml"},
        pages=[PageSpec([TextObj.simple(101, "正文", 20, 30)], template_id=1)],
        texts=["大小写不一致的模板\n正文"],
    ))

    s.append(Sample(
        name="gbk-entry-names",
        description="The document folder has a Chinese name stored as GBK bytes without the ZIP UTF-8 flag, "
        "as some older Chinese tools write it.",
        conforming=False,
        features=["ZIP entry name encoding"],
        gbk_entry_names=True,
        pages=[PageSpec([TextObj.simple(101, "中文目录名", 20, 30)])],
        texts=["中文目录名"],
    ))

    s.append(Sample(
        name="gb18030-xml",
        description="Every XML part is encoded as GB18030 and declares it.",
        conforming=False,
        features=["XML encoding"],
        xml_encoding="GB18030",
        pages=[PageSpec([TextObj.simple(101, "国标编码的文字", 20, 30)])],
        texts=["国标编码的文字"],
    ))

    s.append(Sample(
        name="utf8-bom",
        description="Every XML part starts with a UTF-8 byte order mark.",
        conforming=True,
        features=["XML encoding", "BOM"],
        bom=True,
        pages=[PageSpec([TextObj.simple(101, "带字节顺序标记", 20, 30)])],
        texts=["带字节顺序标记"],
    ))

    values = [(11, "00000001"), (12, "2026年01月02日"), (13, "测试单位甲"), (14, "TESTID0001"), (15, "测试单位乙"),
              (16, "100.00"), (17, "6.00"), (18, "106.00"), (19, "测试项目"), (20, "（续）")]
    s.append(Sample(
        name="custom-tags-root",
        description="Custom tags in the ofd:root layout: each field points at text objects on the page by ID. "
        "The page itself only lists the values. A field may join several objects (field Item joins 19 and 20).",
        conforming=True,
        features=["custom tags", "ObjectRef"],
        pages=[PageSpec(value_page(values))],
        custom_tag=(
            f'<ofd:root xmlns:ofd="{OFD_NS}" version="1.0">'
            f"<ofd:InvoiceNo>{refs(11)}</ofd:InvoiceNo><ofd:IssueDate>{refs(12)}</ofd:IssueDate>"
            f"<ofd:Buyer><ofd:BuyerName>{refs(13)}</ofd:BuyerName><ofd:BuyerTaxID>{refs(14)}</ofd:BuyerTaxID></ofd:Buyer>"
            f"<ofd:Seller><ofd:SellerName>{refs(15)}</ofd:SellerName></ofd:Seller>"
            f"<ofd:TaxExclusiveTotalAmount>{refs(16)}</ofd:TaxExclusiveTotalAmount><ofd:TaxTotalAmount>{refs(17)}</ofd:TaxTotalAmount>"
            f"<ofd:TaxInclusiveTotalAmount>{refs(18)}</ofd:TaxInclusiveTotalAmount><ofd:Item>{refs(19, 20)}</ofd:Item>"
            "</ofd:root>"
        ),
        custom_tag_values=[
            {"path": "InvoiceNo", "value": "00000001"},
            {"path": "IssueDate", "value": "2026年01月02日"},
            {"path": "Buyer/BuyerName", "value": "测试单位甲"},
            {"path": "Buyer/BuyerTaxID", "value": "TESTID0001"},
            {"path": "Seller/SellerName", "value": "测试单位乙"},
            {"path": "TaxExclusiveTotalAmount", "value": "100.00"},
            {"path": "TaxTotalAmount", "value": "6.00"},
            {"path": "TaxInclusiveTotalAmount", "value": "106.00"},
            {"path": "Item", "value": "测试项目（续）"},
        ],
        texts=["\n".join(text for _, text in values)],
    ))

    s.append(Sample(
        name="custom-tags-einvoice",
        description="Custom tags in the fp:eInvoice schema, with line items grouped under GoodsInfos/GoodsInfo. "
        "One value is written inline instead of by ObjectRef.",
        conforming=True,
        features=["custom tags", "ObjectRef", "GoodsInfo"],
        pages=[PageSpec(value_page([(31, "00000002"), (32, "测试物品一"), (33, "测试物品二")]))],
        custom_tag=(
            f'<fp:eInvoice xmlns:fp="{FP_NS}" version="1.0">'
            f"<fp:InvoiceNo>{refs(31, prefix='fp')}</fp:InvoiceNo>"
            "<fp:GoodsInfos>"
            f"<fp:GoodsInfo><fp:Item>{refs(32, prefix='fp')}</fp:Item><fp:Amount>1.00</fp:Amount></fp:GoodsInfo>"
            f"<fp:GoodsInfo><fp:Item>{refs(33, prefix='fp')}</fp:Item><fp:Amount>2.00</fp:Amount></fp:GoodsInfo>"
            "</fp:GoodsInfos></fp:eInvoice>"
        ),
        custom_tag_values=[
            {"path": "InvoiceNo", "value": "00000002"},
            {"path": "GoodsInfos/GoodsInfo/Item", "value": "测试物品一"},
            {"path": "GoodsInfos/GoodsInfo/Amount", "value": "1.00"},
            {"path": "GoodsInfos/GoodsInfo/Item", "value": "测试物品二"},
            {"path": "GoodsInfos/GoodsInfo/Amount", "value": "2.00"},
        ],
        texts=["00000002\n测试物品一\n测试物品二"],
    ))

    s.append(Sample(
        name="custom-data",
        description="Key fields stored as CustomData entries in OFD.xml, one with a separator in its name.",
        conforming=True,
        features=["CustomData"],
        custom_data={"发票号码": "00000003", "开票日期": "2026年03月04日", "价税合计": "11.00", "销售方地址、 电话": "测试地址 000"},
        pages=[PageSpec([TextObj.simple(101, "测试样例", 20, 30)])],
        texts=["测试样例"],
    ))

    s.append(Sample(
        name="broken-tag-prefix",
        description="The custom tag file uses an empty namespace prefix (<:eInvoice xmlns:=\"\">) and an "
        "undeclared ofd: prefix. Strict XML parsers reject it; tolerant readers can recover the values.",
        conforming=False,
        features=["custom tags", "malformed XML"],
        pages=[PageSpec(value_page([(41, "00000004")]))],
        custom_tag="<:eInvoice xmlns:=\"\"><InvoiceNo><ofd:ObjectRef PageRef=\"1\">41</ofd:ObjectRef></InvoiceNo></:eInvoice>",
        custom_tag_values=[{"path": "InvoiceNo", "value": "00000004"}],
        texts=["00000004"],
    ))

    s.append(Sample(
        name="xml-attachment",
        description="An XML attachment (listed in Attachs/Attachments.xml) carries field values directly.",
        conforming=True,
        features=["attachments"],
        attachment=f'<eInvoice xmlns:fp="{FP_NS}"><InvoiceNo>00000005</InvoiceNo><Buyer><BuyerName>测试单位丙</BuyerName></Buyer></eInvoice>',
        attachment_values=[{"path": "InvoiceNo", "value": "00000005"}, {"path": "Buyer/BuyerName", "value": "测试单位丙"}],
        pages=[PageSpec([TextObj.simple(101, "测试样例", 20, 30)])],
        texts=["测试样例"],
    ))

    s.append(Sample(
        name="missing-page-content",
        description="Page 2 is listed in Document.xml but its Content.xml is missing. "
        "Readers should still read pages 1 and 3.",
        conforming=False,
        features=["error recovery"],
        pages=[
            PageSpec([TextObj.simple(101, "第一页", 20, 30)]),
            PageSpec([], missing=True),
            PageSpec([TextObj.simple(301, "第三页", 20, 30)]),
        ],
        texts=["第一页", "", "第三页"],
    ))
    return s


# --------------------------------------------------------------------------- packaging


class _GBKZipInfo(zipfile.ZipInfo):
    def _encodeFilenameFlags(self) -> tuple[bytes, int]:  # noqa: N802 - overrides zipfile
        return self.filename.encode("gbk"), self.flag_bits & ~0x800


def package(sample: Sample) -> dict[str, bytes]:
    parts = build_parts(sample)
    if sample.gbk_entry_names:
        parts = {name.replace("Doc_0/", "文档/"): data for name, data in parts.items()}
        parts["OFD.xml"] = parts["OFD.xml"].replace(b"Doc_0/", "文档/".encode("utf-8"))
    return parts


def write_zip(path: Path, parts: dict[str, bytes], gbk_names: bool) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            info = (_GBKZipInfo if gbk_names else zipfile.ZipInfo)(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compresslevel=9)


def read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def catalog(all_samples: list[Sample]) -> str:
    lines = [
        "# Catalog",
        "",
        "Generated by `generator/build_samples.py`; do not edit by hand.",
        "",
        "| Sample | Conforming | Exercises | Description |",
        "| --- | --- | --- | --- |",
    ]
    for sample in all_samples:
        conforming = "yes" if sample.conforming else "no"
        lines.append(f"| [{sample.name}](samples/{sample.name}.ofd) | {conforming} | {', '.join(sample.features)} | {sample.description} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify committed files match, change nothing")
    args = parser.parse_args(argv)

    all_samples = samples()
    for sample in all_samples:
        for index, page in enumerate(sample.pages):
            page.id = page.id or index + 1
    names = [sample.name for sample in all_samples]
    assert len(names) == len(set(names)), "duplicate sample names"
    samples_dir, expected_dir = ROOT / "samples", ROOT / "expected"
    problems: list[str] = []

    for sample in all_samples:
        parts = package(sample)
        expected = json.dumps(build_expected(sample), ensure_ascii=False, indent=2) + "\n"
        ofd_path, json_path = samples_dir / f"{sample.name}.ofd", expected_dir / f"{sample.name}.json"
        if args.check:
            if not ofd_path.exists() or _decoded(read_zip(ofd_path), sample.gbk_entry_names) != parts:
                problems.append(f"{ofd_path.relative_to(ROOT)} is out of date")
            if not json_path.exists() or json_path.read_text(encoding="utf-8") != expected:
                problems.append(f"{json_path.relative_to(ROOT)} is out of date")
        else:
            samples_dir.mkdir(exist_ok=True)
            expected_dir.mkdir(exist_ok=True)
            write_zip(ofd_path, parts, sample.gbk_entry_names)
            json_path.write_text(expected, encoding="utf-8", newline="\n")

    catalog_text = catalog(all_samples)
    catalog_path = ROOT / "CATALOG.md"
    if args.check:
        if not catalog_path.exists() or catalog_path.read_text(encoding="utf-8") != catalog_text:
            problems.append("CATALOG.md is out of date")
        known = {f"{name}.ofd" for name in names} | {f"{name}.json" for name in names}
        for directory in (samples_dir, expected_dir):
            for path in directory.glob("*"):
                if path.name not in known:
                    problems.append(f"{path.relative_to(ROOT)} is not produced by the generator")
    else:
        catalog_path.write_text(catalog_text, encoding="utf-8", newline="\n")

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("Run `python generator/build_samples.py` and commit the result.", file=sys.stderr)
        return 1
    print(f"{len(all_samples)} samples {'checked' if args.check else 'written'}.")
    return 0


def _decoded(parts: dict[str, bytes], gbk_names: bool) -> dict[str, bytes]:
    """Entry names as the generator wrote them (zipfile reads non-UTF-8 names as CP437)."""
    if not gbk_names:
        return parts
    return {name.encode("cp437").decode("gbk") if not name.isascii() else name: data for name, data in parts.items()}


def _fmt(value: object) -> str:
    if isinstance(value, tuple):
        return " ".join(_fmt(part) for part in value)
    if isinstance(value, float):
        return f"{value:g}" if value != int(value) else str(int(value))
    return str(value)


def _round(value: float) -> float:
    rounded = round(value, 4)
    return 0.0 if rounded == 0 else rounded


if __name__ == "__main__":
    sys.exit(main())
