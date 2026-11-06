"""构造可审查的小型 Tagged PDF，明确控制结构顺序和绘制内容。"""

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NullObject,
    NumberObject,
    TextStringObject,
)


def pdf_dictionary(**values):
    """把测试中已明确类型的字段转换为 PDF 字典。"""
    return DictionaryObject({NameObject("/" + key): value for key, value in values.items()})


def serialized(value):
    """使用 pypdf 的字符串编码生成合法内容流操作数。"""
    output = BytesIO()
    value.write_to_stream(output)
    return output.getvalue()


class TaggedPdfBuilder:
    """生成带 ParentTree 的测试文档，支持独立页面和 Form 标记作用域。"""

    def __init__(self, pages=1):
        """建立共享字体、文档结构根和空页面。"""
        self.writer = PdfWriter()
        self.font = self.writer._add_object(
            pdf_dictionary(Type=NameObject("/Font"), Subtype=NameObject("/Type1"), BaseFont=NameObject("/Helvetica"))
        )
        self.resources = pdf_dictionary(Font=pdf_dictionary(F1=self.font))
        self.root = pdf_dictionary(Type=NameObject("/StructTreeRoot"))
        self.root_ref = self.writer._add_object(self.root)
        self.pages = [self.writer.add_blank_page(300, 400) for _ in range(pages)]
        self.commands = [bytearray() for _ in self.pages]
        self.next_mcid = [0 for _ in self.pages]
        self.parents = {index: [] for index in range(pages)}
        for index, page in enumerate(self.pages):
            page[NameObject("/StructParents")] = NumberObject(index)
            page[NameObject("/Resources")] = pdf_dictionary(Font=pdf_dictionary(F1=self.font))

    def text(self, text, *, page=0, x=20, y=350, actual=None, named=False, invisible=False, artifact=False):
        """绘制一段文字并返回其有页归属的 MCR。"""
        mcid = self.next_mcid[page]
        self.next_mcid[page] += 1
        properties = pdf_dictionary(MCID=NumberObject(mcid))
        if actual is not None:
            properties[NameObject("/ActualText")] = TextStringObject(actual)
        if named:
            name = NameObject(f"/Pr{mcid}")
            resources = self.pages[page]["/Resources"]
            resources.setdefault(NameObject("/Properties"), DictionaryObject())[name] = properties
            encoded = serialized(name)
        else:
            encoded = serialized(properties)
        tag = b"/Artifact" if artifact else b"/P"
        self.commands[page].extend(
            tag
            + b" "
            + encoded
            + b" BDC BT /F1 12 Tf "
            + (b"3 Tr " if invisible else b"")
            + f"{x} {y} Td ".encode()
            + serialized(TextStringObject(text))
            + b" Tj ET EMC\n"
        )
        return pdf_dictionary(Type=NameObject("/MCR"), Pg=self.pages[page].indirect_reference, MCID=NumberObject(mcid))

    def figure(self, *, page=0, x=20, y=50):
        """绘制一个有标记的矢量区域，检验无文字图像的裁图路径。"""
        mcid = self.next_mcid[page]
        self.next_mcid[page] += 1
        self.commands[page].extend(f"/Figure <</MCID {mcid}>> BDC 0.2 0.4 0.8 rg {x} {y} 50 40 re f EMC\n".encode())
        return pdf_dictionary(Type=NameObject("/MCR"), Pg=self.pages[page].indirect_reference, MCID=NumberObject(mcid))

    def form(self, text="Form text"):
        """创建使用独立 MCID 零号的 Form 内容流。"""
        stream = DecodedStreamObject()
        stream.set_data(b"/P <</MCID 0>> BDC BT /F1 12 Tf 0 10 Td " + serialized(TextStringObject(text)) + b" Tj ET EMC")
        parent_key = len(self.parents)
        self.parents[parent_key] = []
        stream.update(
            pdf_dictionary(
                Type=NameObject("/XObject"),
                Subtype=NameObject("/Form"),
                BBox=ArrayObject([NumberObject(v) for v in (0, 0, 100, 30)]),
                Resources=self.resources,
                StructParents=NumberObject(parent_key),
            )
        )
        return self.writer._add_object(stream)

    def place_form(self, form, *, page=0, x=20, y=200):
        """在指定页绘制 Form，复用流时仍形成独立实际调用实例。"""
        name = NameObject(f"/Fm{form.idnum}")
        self.pages[page]["/Resources"].setdefault(NameObject("/XObject"), DictionaryObject())[name] = form
        self.commands[page].extend(f"q 1 0 0 1 {x} {y} cm {name} Do Q\n".encode())
        return pdf_dictionary(Type=NameObject("/MCR"), Pg=self.pages[page].indirect_reference, Stm=form, MCID=NumberObject(0))

    def node(self, role, children=(), *, page=0, actual=None, alt=None, attributes=None, mathml=None):
        """建立结构节点并维护子节点父指针和内容的 ParentTree 所属关系。"""
        value = pdf_dictionary(
            Type=NameObject("/StructElem"),
            S=NameObject("/" + role),
            P=self.root_ref,
            Pg=self.pages[page].indirect_reference,
            K=ArrayObject(list(children)),
        )
        if actual is not None:
            value[NameObject("/ActualText")] = TextStringObject(actual)
        if alt is not None:
            value[NameObject("/Alt")] = TextStringObject(alt)
        if attributes:
            value[NameObject("/A")] = attributes
        if mathml:
            embedded = DecodedStreamObject()
            embedded.set_data(mathml)
            specification = pdf_dictionary(
                Type=NameObject("/Filespec"),
                F=TextStringObject("formula.mml"),
                EF=pdf_dictionary(F=self.writer._add_object(embedded)),
            )
            value[NameObject("/AF")] = ArrayObject([self.writer._add_object(specification)])
        reference = self.writer._add_object(value)
        for child in children:
            item = child.get_object()
            if item.get("/Type") == "/StructElem":
                item[NameObject("/P")] = reference
            elif item.get("/Type") == "/MCR":
                owner = item.get("/Stm", item["/Pg"]).get_object()
                key = int(owner["/StructParents"])
                mcid = int(item["/MCID"])
                parents = self.parents[key]
                parents.extend(NullObject() for _ in range(mcid + 1 - len(parents)))
                parents[mcid] = reference
        return reference

    def finish(self, children):
        """物化页面内容及结构根，返回确定性 PDF 字节。"""
        for page, commands in zip(self.pages, self.commands, strict=True):
            stream = DecodedStreamObject()
            stream.set_data(bytes(commands))
            page[NameObject("/Contents")] = self.writer._add_object(stream)
        self.root[NameObject("/K")] = ArrayObject(list(children))
        numbers = ArrayObject()
        for key, owners in sorted(self.parents.items()):
            numbers.extend((NumberObject(key), ArrayObject(owners)))
        self.root[NameObject("/ParentTree")] = self.writer._add_object(pdf_dictionary(Nums=numbers))
        self.writer._root_object[NameObject("/StructTreeRoot")] = self.root_ref
        self.writer._root_object[NameObject("/MarkInfo")] = pdf_dictionary(Marked=BooleanObject(True))
        output = BytesIO()
        self.writer.write(output)
        return output.getvalue()
