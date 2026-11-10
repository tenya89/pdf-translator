"""產出可編輯的 DOCX：照閱讀順序排列，套用整齊一致的 Word 樣式（不模仿原檔的座標與分欄）。

  - A4 單欄；內容依原檔閱讀順序（跨欄頁先左欄後右欄）由上往下排
  - 標題依全文字級分三層，套「標題 1／2／3」樣式（可產生目錄、導覽窗格）
  - 內文統一字型、字級、行距；跨欄/跨頁切開的句子用整句譯文
  - 以「•」開頭的項目用 Word 項目符號清單
  - 圖片、獨立公式置中放回出現的位置；圖說套「標號」樣式
  - 表格重建成 Word 表格，表頭加底色，跨欄表頭合併儲存格，表格內照片放回儲存格
  - 行內公式：簡單的用上下標格式，複雜的貼原公式小圖
  - 註腳原文集中在文件最後的「註釋」；參考文獻照順序放（凸排、小字）
  - 頁尾頁碼
"""
import io
import re
import statistics

import pymupdf
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from extract import plain, reading_order

SRC = {"doc": None}  # 原始 PDF（複雜行內公式、圖片要從原檔裁圖）

PAGE_W, PAGE_H = 595.3, 841.9  # A4（pt）
MARGIN_LR, MARGIN_TB = Cm(2.3), Cm(2.3)
TEXT_W = PAGE_W - 2 * 2.3 / 2.54 * 72  # 版心寬度（pt）

BODY_FONT = ("Times New Roman", "PMingLiU")      # 內文：新細明體＋Times New Roman
HEAD_FONT = ("Arial", "Microsoft JhengHei")      # 標題：微軟正黑體＋Arial
BODY_PT, CAPTION_PT, TABLE_PT, SMALL_PT = 11, 9.5, 9, 9
HEADING_PT = {1: 18, 2: 14, 3: 12}
TEXT_COLOR = (30, 30, 30)
BULLET_RE = re.compile(r"^\s*[•●▪◦・‧∙]\s*")


# ---------- 樣式 ----------

def _set_style_font(style, fonts, size, bold=False, color=TEXT_COLOR):
    style.font.name = fonts[0]
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(*color)
    rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for k in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(k), fonts[0])
    rfonts.set(qn("w:eastAsia"), fonts[1])


def _setup_styles(doc):
    st = doc.styles
    _set_style_font(st["Normal"], BODY_FONT, BODY_PT)
    pf = st["Normal"].paragraph_format
    pf.space_before, pf.space_after = Pt(0), Pt(6)
    pf.line_spacing = 1.5
    for lvl, size in HEADING_PT.items():
        h = st[f"Heading {lvl}"]
        _set_style_font(h, HEAD_FONT, size, bold=True)
        h.font.italic = False
        hpf = h.paragraph_format
        hpf.space_before, hpf.space_after = Pt(18 if lvl == 1 else 12), Pt(8 if lvl == 1 else 6)
        hpf.line_spacing = 1.2
        hpf.keep_with_next = True
    cap = st["Caption"]
    _set_style_font(cap, BODY_FONT, CAPTION_PT, color=(80, 80, 80))
    cap.font.italic = False
    cap.paragraph_format.space_before, cap.paragraph_format.space_after = Pt(2), Pt(12)
    cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    lb = st["List Bullet"]
    _set_style_font(lb, BODY_FONT, BODY_PT)
    lb.paragraph_format.space_after = Pt(4)
    lb.paragraph_format.line_spacing = 1.5


def _font(run, size=None, bold=None, italic=None, fonts=None, color=None):
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if fonts:
        run.font.name = fonts[0]
        rpr = run._element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.append(rf)
        rf.set(qn("w:eastAsia"), fonts[1])
    if color is not None:
        run.font.color.rgb = RGBColor(*color)


# ---------- 文字與行內公式 ----------

def _add_math(par, entry, size):
    """行內公式：簡單的用上下標格式重建，複雜的貼原公式小圖。"""
    if entry["complex"] and SRC["doc"] is not None:
        page = SRC["doc"][entry["page"]]
        clip = pymupdf.Rect(entry["bbox"]) & page.rect
        png = io.BytesIO(page.get_pixmap(clip=clip, dpi=400).tobytes("png"))
        par.add_run().add_picture(png, height=Pt(min(clip.height * size / 9, size * 1.8)))
        return
    for text, level, ital in entry["parts"]:
        r = par.add_run(text)
        _font(r, italic=ital)
        if level < 0:
            r.font.subscript = True
        elif level > 0:
            r.font.superscript = True


def _add_text(par, zh, size=None, bold=None, math=None, fonts=None):
    for part in re.split(r"(⟦[^⟧]*⟧|⟪\d+⟫)", zh):
        if not part:
            continue
        if part.startswith("⟪"):
            entry = (math or {}).get(part[1:-1])
            if entry:
                _add_math(par, entry, size or BODY_PT)
            continue
        r = par.add_run(part[1:-1] if part.startswith("⟦") else part)
        _font(r, size=size, bold=bold, fonts=fonts)
        if part.startswith("⟦"):
            r.font.superscript = True


def _crop_png(pdf_page, bbox, dpi=220):
    clip = pymupdf.Rect(bbox) & pdf_page.rect
    return io.BytesIO(pdf_page.get_pixmap(clip=clip, dpi=dpi).tobytes("png"))


def _in(b, box):
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return box[0] - 1 <= cx <= box[2] + 1 and box[1] - 1 <= cy <= box[3] + 1


# ---------- 全文共用的判斷 ----------

def _heading_levels(segs_by_page, pages):
    """依全文標題字級分三層：最大的一群是標題 1，其次標題 2，其餘標題 3。"""
    sizes = sorted({round(s["size"]) for p in pages for s in segs_by_page[p]
                    if s["kind"] == "heading" and s.get("zh")}, reverse=True)
    groups = []
    for sz in sizes:  # 字級相差 1.5pt 以內算同一層（OCR 字級有誤差）
        if groups and groups[-1][-1] - sz <= 1.5:
            groups[-1].append(sz)
        else:
            groups.append([sz])
    level = {}
    for i, g in enumerate(groups):
        for sz in g:
            level[sz] = min(i + 1, 3)
    return level


def _page_scale(segs, regs, pw):
    """原檔版心寬度，用來把圖片等比例縮到 Word 版心。"""
    xs = [s["bbox"] for s in segs if s["kind"] in ("body", "heading", "caption")] + [r["bbox"] for r in regs]
    if not xs:
        return 1.0
    width = max(b[2] for b in xs) - min(b[0] for b in xs)
    return min(1.0, TEXT_W / width) if width > 0 else 1.0


# ---------- 主程式 ----------

class _Item:
    def __init__(self, kind, bbox, seg=None, region=None):
        self.kind, self.bbox, self.seg, self.region = kind, bbox, seg, region


def build(src_pdf, pages, segs_by_page, regions_by_page, header_h, modes, out_path):
    src = pymupdf.open(src_pdf)
    SRC["doc"] = src
    doc = Document()
    _setup_styles(doc)
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Pt(PAGE_W), Pt(PAGE_H)
    sec.left_margin = sec.right_margin = MARGIN_LR
    sec.top_margin = sec.bottom_margin = MARGIN_TB
    _page_number_footer(sec)

    levels = _heading_levels(segs_by_page, pages)
    n_h1 = sum(1 for p in pages for s in segs_by_page[p]
               if s["kind"] == "heading" and s.get("zh") and levels.get(round(s["size"])) == 1)
    footnotes = []
    first_content = True

    for pno in pages:
        spage = src[pno]
        pw = spage.rect.width
        segs = segs_by_page[pno]
        regs = regions_by_page.get(pno, [])
        footnotes += [s for s in segs if s["kind"] == "footnote"]

        if modes.get(pno) == "empty":
            # 沒有文字層：整頁以圖片放入
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(_crop_png(spage, spage.rect), width=Pt(TEXT_W))
            continue

        scale = _page_scale(segs, regs, pw)
        items = [_Item(r["type"], r["bbox"], region=r) for r in regs]
        items += [_Item("seg", s["bbox"], s) for s in segs
                  if s["kind"] in ("heading", "caption", "body") and s.get("zh") and not s.get("cont_of")
                  and not any(_in(s["bbox"], r["bbox"]) for r in regs)]
        refs = [s for s in segs if s["kind"] == "reference"]
        for col in ([r for r in refs if (r["bbox"][0] + r["bbox"][2]) / 2 < pw / 2],
                    [r for r in refs if (r["bbox"][0] + r["bbox"][2]) / 2 >= pw / 2]):
            if col:
                xs0, ys0, xs1, ys1 = zip(*(s["bbox"] for s in col))
                items.append(_Item("refs", (min(xs0), min(ys0), max(xs1), max(ys1)), {"refs": col}))

        for kind, left, right in reading_order(items, pw, key=lambda it: it.bbox):
            for it in left + right:
                is_h1 = it.kind == "seg" and it.seg["kind"] == "heading" \
                    and levels.get(round(it.seg["size"])) == 1
                _emit(doc, it, spage, segs, levels, scale,
                      page_break=is_h1 and n_h1 >= 3 and not first_content)
                first_content = False

    if footnotes:
        _emit_footnotes(doc, footnotes)
    doc.save(out_path)


def _page_number_footer(sec):
    p = sec.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    _font(run, size=9, color=(110, 110, 110))


def _emit(doc, it, spage, segs, levels, scale, page_break=False):
    if it.kind in ("figure", "equation"):
        b = it.bbox
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(4 if it.kind == "figure" else 6)
        p.paragraph_format.keep_with_next = it.kind == "figure"
        w = min((b[2] - b[0]) * scale, TEXT_W)
        p.add_run().add_picture(_crop_png(spage, b), width=Pt(w))
        return
    if it.kind == "table":
        _emit_table(doc, it.bbox, [s for s in segs if _in(s["bbox"], it.bbox)],
                    it.region.get("images", []), spage, scale)
        return
    if it.kind == "refs":
        refs = it.seg["refs"]
        for t in _split_references(refs):
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.left_indent, pf.first_line_indent = Pt(22), Pt(-22)  # 凸排
            pf.space_after, pf.line_spacing = Pt(3), 1.15
            _add_text(p, t, size=SMALL_PT)
        return

    s = it.seg
    zh = s.get("zh_full") or s["zh"]
    if s["kind"] == "heading":
        lvl = levels.get(round(s["size"]), 3)
        p = doc.add_paragraph(style=f"Heading {lvl}")
        p.paragraph_format.page_break_before = page_break
        _add_text(p, zh, math=s.get("math"))
        return
    if s["kind"] == "caption":
        p = doc.add_paragraph(style="Caption")
        _add_text(p, zh, math=s.get("math"))
        return
    # 內文；以「•」開頭的拆成項目清單
    if BULLET_RE.match(zh):
        for item in [x for x in re.split(r"\s*[•●▪◦・‧∙]\s*", zh) if x.strip()]:
            p = doc.add_paragraph(style="List Bullet")
            _add_text(p, item.strip(), math=s.get("math"))
        return
    p = doc.add_paragraph()
    short_lines = len(s["lines"]) >= 2 and statistics.median(
        l["bbox"][2] - l["bbox"][0] for l in s["lines"]) < 150
    if s.get("centered"):
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif len(s["lines"]) >= 3 and not short_lines:
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _add_text(p, zh, math=s.get("math"))


# ---------- 註腳、參考文獻 ----------

def _emit_footnotes(doc, foots):
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("註釋")
    by_page = {}
    for f in foots:
        by_page.setdefault(f["page"], []).append(f)
    for page in sorted(by_page):
        for t in _split_footnotes(by_page[page]):
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.left_indent, pf.first_line_indent = Pt(18), Pt(-18)
            pf.space_after, pf.line_spacing = Pt(2), 1.15
            _add_text(p, t, size=SMALL_PT)


def _split_footnotes(foots):
    """註腳區的所有行依位置重排（OCR 有時把行首字母拆成獨立區塊），再依「數字.」拆成一條一條。"""
    text = _rows_text(foots)
    parts = re.split(r"(?=(?:^|\s)\d{1,3}\s?\.\s?[A-Z“\"])", text)
    return [p.strip() for p in parts if p.strip()]


def _split_references(refs):
    """參考文獻依位置重組，再依 [1]、1. 之類的編號拆成一條一條。"""
    text = _rows_text(refs)
    parts = re.split(r"(?=\[\d{1,3}\]\s)", text)
    if len(parts) <= 1:
        parts = re.split(r"(?=(?:^|\s)\d{1,3}\.\s[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


def _rows_text(segs):
    """把多段文字的所有行依位置排成一列一列再接起來（同一列左右分開的片段會接回去）。"""
    math = {}
    for s in segs:
        math.update(s.get("math") or {})
    lines = sorted((l for s in segs for l in s["lines"]), key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2)
    rows = []
    for l in lines:
        cy = (l["bbox"][1] + l["bbox"][3]) / 2
        if rows and abs(cy - rows[-1][0]) < 3:
            rows[-1][1].append(l)
        else:
            rows.append([cy, [l]])
    text = " ".join("".join(x["text"] if i == 0 or x["bbox"][0] - r[i - 1]["bbox"][2] < 1.5 else " " + x["text"]
                            for i, x in enumerate(r))
                    for r in (sorted(row, key=lambda l: l["bbox"][0]) for _, row in rows))
    return plain(text, math)


# ---------- 表格 ----------

def _shade(cell, hex_color):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcpr.append(shd)


def _emit_table(doc, box, cells, images=(), spage=None, scale=1.0):
    """依儲存格位置重建 Word 表格：左緣分欄、垂直重疊分列；跨欄的儲存格合併。"""
    cells = [c for c in cells if c.get("zh") or c["kind"] == "keep"]
    if not cells:
        return
    width = box[2] - box[0]
    wide = [c for c in cells if (c["bbox"][2] - c["bbox"][0]) > 0.6 * width]
    normal = [c for c in cells if c not in wide]

    rows = []  # 每列：[y0, y1, [cells]]
    for c in sorted(cells, key=lambda c: c["bbox"][1]):
        y0, y1 = c["bbox"][1], c["bbox"][3]
        if c not in wide:
            for r in rows:
                if r[2] and r[2][0] not in wide and min(r[1], y1) - max(r[0], y0) > 0.3 * min(r[1] - r[0], y1 - y0):
                    r[0], r[1] = min(r[0], y0), max(r[1], y1)
                    r[2].append(c)
                    break
            else:
                rows.append([y0, y1, [c]])
        else:
            rows.append([y0, y1, [c]])
    rows.sort(key=lambda r: r[0])

    # 分欄：用格子最多的那幾列，把左右範圍互相重疊的格子歸成同一欄
    # （看左右範圍而不是左緣，靠右對齊的數字、置中的文字都能分對）
    counts = [len(r[2]) for r in rows if r[2][0] not in wide]
    top = max(counts) if counts else 1
    basis = [c for r in rows if r[2][0] not in wide and len(r[2]) >= max(2, 0.8 * top) for c in r[2]] or normal
    cols = []
    for x0, x1 in sorted((c["bbox"][0], c["bbox"][2]) for c in basis):
        if cols and x0 < cols[-1][1] - 1:
            cols[-1][1] = max(cols[-1][1], x1)
        else:
            cols.append([x0, x1])
    if not cols:
        cols = [[box[0], box[2]]]

    def col_span(c):
        """儲存格涵蓋的欄：與該欄範圍重疊的都算（跨欄表頭，例如 Single 橫跨四欄）。"""
        hit = [i for i, (a, b) in enumerate(cols)
               if min(b, c["bbox"][2]) - max(a, c["bbox"][0]) > 0.3 * min(b - a, c["bbox"][2] - c["bbox"][0])]
        if not hit:  # 落在欄與欄之間：歸到最近的一欄
            cx = (c["bbox"][0] + c["bbox"][2]) / 2
            i = min(range(len(cols)), key=lambda k: abs((cols[k][0] + cols[k][1]) / 2 - cx))
            return i, i
        return hit[0], hit[-1]

    ncols = len(cols)

    # 表頭候選：表格最上面、沒有純數字儲存格的列（最多 3 列；最上面的說明列另計）
    cand = 0
    for ri, (_, _, rc) in enumerate(rows[:5]):
        if all(c["kind"] != "keep" for c in rc) and (ri - sum(1 for r in rows[:ri] if r[2][0] in wide) < 3):
            cand = ri + 1
        else:
            break
    # 表頭中只有第一欄有字的列（例如上下置中、橫跨兩列表頭的「方法」），併入下一列表頭
    merged_into = set()
    for ri in range(cand - 1):
        rc, nxt = rows[ri][2], rows[ri + 1][2]
        if rc and rc[0] not in wide and all(col_span(c)[0] == 0 for c in rc) \
                and not any(col_span(c)[0] == 0 for c in nxt) and nxt[0] not in wide:
            nxt.extend(rc)
            rc.clear()
            merged_into.add(id(nxt))
    cand -= sum(1 for r in rows[:cand] if not r[2])
    rows = [r for r in rows if r[2]]
    # 表頭：最上面的說明列＋第一列；之後的列要第一欄是空的（子表頭）或剛併入跨列表頭，才算表頭
    header_rows = 0
    first_normal = True
    for ri, (_, _, rc) in enumerate(rows[:cand]):
        if rc[0] in wide and len(rc) == 1:
            header_rows = ri + 1
        elif first_normal:
            header_rows, first_normal = ri + 1, False
        elif id(rc) in merged_into or not any(col_span(c)[0] == 0 for c in rc):
            header_rows = ri + 1
        else:
            break

    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, (_, _, rc) in enumerate(rows):
        row = table.rows[ri]
        if len(rc) == 1 and rc[0] in wide:
            cell = row.cells[0].merge(row.cells[-1]) if ncols > 1 else row.cells[0]
            _fill(cell, [rc[0]])
            continue
        spans = {id(c): col_span(c) for c in rc}
        if ri < header_rows:
            # 表頭的空欄歸給最近的表頭文字（第一欄除外），形成跨欄（例如 Single 橫跨四欄）
            occupied = {i for a, b in spans.values() for i in range(a, b + 1)}
            for k in range(1, ncols):
                if k in occupied:
                    continue
                kc = (cols[k][0] + cols[k][1]) / 2
                cands = [c for c in rc if spans[id(c)][0] > 0 or spans[id(c)][1] > 0]
                if not cands:
                    continue
                c = min(cands, key=lambda c: abs((c["bbox"][0] + c["bbox"][2]) / 2 - kc))
                a, b = spans[id(c)]
                if all(i not in occupied or a <= i <= b for i in range(min(a, k), max(b, k) + 1)):
                    spans[id(c)] = (min(a, k), max(b, k))
        by_col = {}
        for c in sorted(rc, key=lambda c: c["bbox"][1]):
            by_col.setdefault(spans[id(c)], []).append(c)
        used = set()
        for (first, last), cs in sorted(by_col.items()):
            if first in used:
                continue
            cell = row.cells[first]
            if last > first and not any(i in used for i in range(first, last + 1)):
                cell = cell.merge(row.cells[last])
                used.update(range(first, last + 1))
            else:
                used.add(first)
            _fill(cell, cs, bold=ri < header_rows)
    for ri in range(header_rows):
        for cell in table.rows[ri].cells:
            _shade(cell, "E8E8E8")
    # 表格中的照片：依位置放進對應的儲存格（原圖，不翻譯）
    for im in images:
        cy = (im[1] + im[3]) / 2
        ri = min(range(len(rows)), key=lambda i: 0 if rows[i][0] - 2 <= cy <= max(rows[i][1], rows[i + 1][0] if i + 1 < len(rows) else 1e9)
                 else min(abs(cy - rows[i][0]), abs(cy - rows[i][1])) + 1)
        ci = min(range(len(cols)), key=lambda k: abs((cols[k][0] + cols[k][1]) / 2 - (im[0] + im[2]) / 2))
        par = table.rows[ri].cells[ci].add_paragraph()
        cell_w = TEXT_W / ncols * 0.9
        par.add_run().add_picture(_crop_png(spage, im), width=Pt(min((im[2] - im[0]) * scale, cell_w)))
    gap = doc.add_paragraph()
    gap.paragraph_format.space_after = Pt(6)


def _fill(cell, cs, bold=False):
    p = cell.paragraphs[0]
    for i, c in enumerate(cs):
        if i:
            p = cell.add_paragraph()
        pf = p.paragraph_format
        pf.space_before, pf.space_after = Pt(1), Pt(1)
        pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
        _add_text(p, c.get("zh") or plain(c["text"], c.get("math")), size=TABLE_PT, bold=bold or None,
                  math=c.get("math"))
