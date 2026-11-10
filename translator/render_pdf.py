"""產出翻譯後的 PDF。依頁面種類（extract.page_mode）處理：

  native（原生文字頁）
    刪除要翻譯的英文文字物件（圖片、線條、色塊、背景都不動），在原位置寫入中文。
  raster（整頁圖片＋隱形 OCR 文字）
    把整頁畫成影像，要翻譯的文字位置用背景色蓋掉，再寫入中文。
  rebuild（整頁圖片＋文字層，不保留背景）
    白紙上貼回圖片、表格、獨立公式，再寫上中文與不翻的原文。
  empty（沒有文字層）
    原樣複製，無法翻譯（不做 OCR）。

共同規則：
  - 圖片、獨立公式、頁首、頁碼、註腳、參考文獻不翻譯；中文是真正的文字（可選取、搜尋）
  - 行內公式 ⟪n⟫：簡單的依原字大小與高度重建上下標；複雜的（分數、堆疊）貼原公式的小圖
  - 不重疊：每段只能延伸到下方或右方的空白處，已寫上的中文也算障礙；放不下就縮小字級；
    寫完後再逐頁檢查，有重疊就記在報告裡
"""
import html
import os
import re

import cv2
import numpy as np
import pymupdf

from extract import plain
from regions import page_raster, px_rect, raster_dpi

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
CSS = """
@font-face {font-family: sans; src: url(NotoSansTC-Regular.otf);}
@font-face {font-family: sans; font-weight: bold; src: url(NotoSansTC-Bold.otf);}
@font-face {font-family: serif; src: url(NotoSerifTC-Regular.otf);}
@font-face {font-family: serif; font-weight: bold; src: url(NotoSerifTC-SemiBold.otf);}
* {margin: 0; padding: 0;}
sup {font-size: 60%; vertical-align: super;}
sub {font-size: 60%; vertical-align: sub;}
"""
TRANSLATE_KINDS = {"heading", "caption", "body", "table"}
PICTURE_REGIONS = {"figure", "equation"}


# ---------- 行內公式 ----------

class MathImages:
    """複雜行內公式：從原檔裁下高解析度小圖，放進排版用的 archive。"""

    def __init__(self, src, archive):
        self.src, self.archive, self.done = src, archive, set()

    def name(self, key, entry):
        fname = f"math{key}.png"
        if fname not in self.done:
            page = self.src[entry["page"]]
            clip = pymupdf.Rect(entry["bbox"]) & page.rect
            self.archive.add(page.get_pixmap(clip=clip, dpi=400).tobytes("png"), fname)
            self.done.add(fname)
        return fname


def math_html(key, entry, images):
    if entry["complex"] and images is not None:
        b = entry["bbox"]
        return f'<img src="{images.name(key, entry)}" style="height:{b[3] - b[1]:.1f}pt"/>'
    out = []
    for text, level, italic in entry["parts"]:
        t = html.escape(text)
        if italic:
            t = f"<i>{t}</i>"
        if level < 0:
            t = f"<sub>{t}</sub>"
        elif level > 0:
            t = f"<sup>{t}</sup>"
        out.append(t)
    return "".join(out)


def to_html(zh, math=None, images=None):
    math = math or {}
    parts = re.split(r"(⟪\d+⟫|⟦[^⟧]*⟧)", zh)
    out = []
    for p in parts:
        if p.startswith("⟪"):
            key = p[1:-1]
            entry = math.get(key)
            out.append(math_html(key, entry, images) if entry else "")
        elif p.startswith("⟦"):
            out.append(f"<sup>{html.escape(p[1:-1])}</sup>")
        else:
            out.append(html.escape(p))
    return "".join(out)


# ---------- 顏色 ----------

def _bg_stats(img, r):
    """文字框內的背景色（文字只佔少數像素，所以取中位數）。"""
    x0, y0, x1, y1 = r
    px = img[y0:y1, x0:x1].reshape(-1, 3).astype(int)
    if not len(px):
        return np.array([255.0, 255.0, 255.0])
    return np.median(px, axis=0)


def _sample_colors(img, segs, sx, sy, off, fill):
    """取每段的背景色與文字色；fill=True 時順便用背景色蓋掉原文（圖片頁用）。"""
    out = img.copy() if fill else img
    for s in segs:
        if s["kind"] not in TRANSLATE_KINDS or not s.get("zh"):
            continue
        seg_r = px_rect(s["bbox"], sx, sy, off, 3, img.shape)
        bg = _bg_stats(img, seg_r)
        cols = []
        for ln in s["lines"]:
            r = px_rect(ln["bbox"], sx, sy, off, 3, img.shape)
            roi = img[r[1]:r[3], r[0]:r[2]].astype(int)
            if roi.size == 0:
                continue
            dist = np.abs(roi - bg).max(axis=2)
            m = dist > 25
            if m.sum() > 5:
                strong = dist >= np.percentile(dist[m], 90)
                cols.append(np.median(roi[strong], axis=0))
        if fill:
            out[seg_r[1]:seg_r[3], seg_r[0]:seg_r[2]] = bg.astype(np.uint8)
        s["bg"] = bg.tolist()
        if "color" not in s:
            s["color"] = np.median(cols, axis=0).tolist() if cols else [0, 0, 0]
    return out


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, v))) for v in c)


def _saturation(c):
    mx, mn = max(c), min(c)
    return 0 if mx == 0 else (mx - mn) / mx


def _readable(c):
    """在白紙上看得見的顏色：太淺的（例如原本壓在深色底上的白字）改成深灰。"""
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    return list(c) if lum < 170 else [60, 60, 60]


def _seg_color(img, s, sx, sy, off):
    """取任一段文字的顏色（不翻的文字重寫時用）。"""
    r = px_rect(s["bbox"], sx, sy, off, 2, img.shape)
    roi = img[r[1]:r[3], r[0]:r[2]].reshape(-1, 3).astype(int)
    if not len(roi):
        return [0, 0, 0]
    bg = np.median(roi, axis=0)
    dist = np.abs(roi - bg).max(axis=1)
    if dist.max() < 25:
        return [0, 0, 0]
    return np.median(roi[dist >= np.percentile(dist[dist > 25], 90)], axis=0).tolist()


# ---------- 排版 ----------

def _style(s):
    """字型：原生頁照原文的襯線/無襯線與粗體；圖片頁沒有字型資訊，標題依顏色判斷。"""
    color = s.get("color", [0, 0, 0])
    size = s["size"] * 0.92  # 中文字看起來比同字級的英文大
    if s.get("mode") == "native":
        fam = "serif" if s.get("serif") else "sans"
        bold = bool(s.get("bold"))
    elif s["kind"] == "heading":
        fam = "serif" if _saturation(color) > 0.25 else "sans"
        bold = True
    else:
        fam, bold = "sans", False
    weight = "bold" if bold else "normal"
    align = "center" if s.get("centered") else "left"
    if s["kind"] == "heading":
        return dict(family=fam, weight=weight, size=size, lh=1.25, align=align)
    if s["kind"] in ("caption", "table"):
        return dict(family=fam, weight=weight, size=size, lh=1.3, align=align)
    # 不用左右對齊：中英混排時排版引擎只能拉寬英文旁的空白，會出現很大的空隙
    return dict(family=fam, weight=weight, size=size, lh=1.45, align=align)


def _mark_centered(segs):
    """置中的標題、圖說：段落中心在版面中央，且左邊明顯內縮。"""
    body = [s for s in segs if s["kind"] == "body" and len(s["lines"]) >= 2]
    if not body:
        return
    left = min(s["bbox"][0] for s in body)
    right = max(s["bbox"][2] for s in body)
    for s in segs:
        x0, _, x1, _ = s["bbox"]
        lines_c = [(l["bbox"][0] + l["bbox"][2]) / 2 for l in s["lines"]]
        if (abs((x0 + x1) / 2 - (left + right) / 2) < 6 and x0 - left > 10
                and max(lines_c) - min(lines_c) < 6 and s["kind"] in ("heading", "caption", "body")):
            s["centered"] = (left, right)


def _hover(a, b):
    return min(a[2], b[2]) - max(a[0], b[0])


def _vover(a, b):
    return min(a[3], b[3]) - max(a[1], b[1])


def _free_bottom(s, blockers, clean, sx, sy, off, limit):
    """段落下方可延伸到哪裡：碰到其他文字、圖片、已寫上的中文或非空白圖案就停。
    下一段的框若跟本段重疊（OCR 框常這樣），本段就縮到下一段上緣為止。"""
    x0, y0, x1, y1 = s["bbox"]
    stop = limit
    for b in blockers:
        if b is s["bbox"]:
            continue
        if _hover((x0, y0, x1, y1), b) > 2 and b[1] > y0 + 2:
            stop = min(stop, b[1] - 2)
    bg = np.array(s.get("bg", [255, 255, 255]))
    if stop > y1:
        r = px_rect((x0, y1, x1, stop), sx, sy, off, 0, clean.shape)
        if r[3] > r[1]:
            roi = np.abs(clean[r[1]:r[3], r[0]:r[2]].astype(int) - bg).max(axis=2)
            busy = np.where((roi > 30).mean(axis=1) > 0.003)[0]
            if len(busy):
                stop = min(stop, y1 + busy[0] / sy - 2)
    return max(stop, y0 + 0.5 * (y1 - y0))


def _free_right(bbox, blockers, page_w):
    x0, y0, x1, y1 = bbox
    stop = min(page_w - 15, x0 + 2 * (x1 - x0) + 20)
    for b in blockers:
        if b is bbox:
            continue
        if b[0] >= x1 - 1 and _vover(bbox, b) > 1:
            stop = min(stop, b[0] - 4)
    return max(x1, stop)


def _centered_span(s, blockers):
    """置中段落可用的左右範圍：以原本中心為準，左右對稱延伸，碰到旁邊的東西就停。"""
    x0, y0, x1, y1 = s["bbox"]
    left, right = s["centered"]
    cx = (x0 + x1) / 2
    half = min(cx - left, right - cx)
    for b in blockers:
        if b is s["bbox"] or _vover(s["bbox"], b) <= 1:
            continue
        # 中心在左邊的障礙物限制左側，在右邊的限制右側（包含跟本段部分重疊的圖片）
        if (b[0] + b[2]) / 2 < cx:
            half = min(half, cx - b[2] - 3)
        else:
            half = min(half, b[0] - cx - 3)
    half = max(half, (x1 - x0) / 2)
    return cx - half, cx + half


def write_text(page, segs, clean, sx, sy, off, archive, limit_bottom, regions, images):
    """依序寫入中文；回傳記錄與每段實際佔用的範圍。"""
    log, placed = [], []
    _mark_centered(segs)
    fixed = [s["bbox"] for s in segs] + [r["bbox"] for r in regions if r["type"] in PICTURE_REGIONS]
    for s in segs:
        if s["kind"] not in TRANSLATE_KINDS or not s.get("zh"):
            continue
        blockers = [b for b in fixed if b is not s["bbox"]] + placed
        st = _style(s)
        x0, y0, x1, y1 = s["bbox"]
        bottom = _free_bottom(s, blockers, clean, sx, sy, off, limit_bottom)
        if s.get("centered"):
            x0, x1 = _centered_span(s, blockers)
        elif len(s["lines"]) == 1 and s["kind"] != "table":
            x1 = _free_right(s["bbox"], blockers, page.rect.width)  # 單行可往右延伸，避免被擠成兩行
        rect = pymupdf.Rect(x0, y0 - 1, x1 + 2, bottom)
        style = (f"font-family:{st['family']};font-weight:{st['weight']};font-size:{st['size']:.2f}pt;"
                 f"line-height:{st['lh']};color:{_hex(s.get('color', [0, 0, 0]))};text-align:{st['align']};")
        body = f'<div style="{style}">{to_html(s["zh"], s.get("math"), images)}</div>'
        spare, scale = page.insert_htmlbox(rect, body, css=CSS, archive=archive, scale_low=0)
        used = (rect.x0, rect.y0, rect.x1, rect.y1 - max(spare, 0))
        placed.append(used)
        s["placed"] = used
        if spare < 0:
            log.append(f"{s['id']}：文字放不下")
        elif scale < 0.7:
            log.append(f"{s['id']}：字級縮小為 {scale:.0%}")
    return log


def check_overlaps(segs, regions):
    """寫完一頁後檢查：中文與中文、中文與留下的原文、中文與圖片/公式是否重疊。"""
    zh = [(s["id"], s["placed"]) for s in segs if s.get("placed")]
    others = [(s["id"], s["bbox"]) for s in segs if not s.get("placed")
              and s["kind"] in ("header", "pagenum", "footnote", "reference", "keep", "side")]
    others += [(f"{r['type']}", r["bbox"]) for r in regions if r["type"] in PICTURE_REGIONS]
    log = []

    def area(a, b):
        return max(0, _hover(a, b)) * max(0, _vover(a, b))

    for i, (ia, a) in enumerate(zh):
        for ib, b in zh[i + 1:] + others:
            inter = area(a, b)
            small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
            if inter > 4 and inter / small > 0.05:
                log.append(f"{ia} 與 {ib} 重疊")
    return log


def _limit_bottom(segs, page_h):
    foot = [s["bbox"][1] for s in segs if s["kind"] in ("footnote", "pagenum")]
    return (min(foot) if foot else page_h) - 4


# ---------- 三種頁面 ----------

def _render_native(src, pno, segs, regions, out, archive, images):
    out.insert_pdf(src, from_page=pno, to_page=pno)
    page = out[-1]
    todo = [s for s in segs if s["kind"] in TRANSLATE_KINDS and s.get("zh")]
    # 複雜行內公式可能含有線條（分數線），先把完全在公式範圍內的線條一起刪掉
    complex_rects = [pymupdf.Rect(e["bbox"]) for s in todo for e in (s.get("math") or {}).values() if e["complex"]]
    if complex_rects:
        for r in complex_rects:
            page.add_redact_annot(r, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for s in todo:
        s["mode"] = "native"
        s["color"] = list(s.get("src_color") or (0, 0, 0))
        for ln in s["lines"]:
            # 稍微內縮，避免刪到相鄰行的字
            page.add_redact_annot(pymupdf.Rect(ln["bbox"]) + (0.5, 1, -0.5, -1), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    clean, sx, sy, off = page_raster(page, 100)
    _sample_colors(clean, todo, sx, sy, off, fill=False)
    return write_text(page, segs, clean, sx, sy, off, archive, _limit_bottom(segs, page.rect.height), regions,
                      images)


def _render_raster(src, pno, segs, regions, out, archive, images):
    page = src[pno]
    img, sx, sy, off = page_raster(page, raster_dpi(page))
    for s in segs:
        s["mode"] = "raster"
    clean = _sample_colors(img, segs, sx, sy, off, fill=True)
    npage = out.new_page(width=page.rect.width, height=page.rect.height)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(clean, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
    npage.insert_image(npage.rect, stream=buf.tobytes())
    log = write_text(npage, segs, clean, sx, sy, off, archive, _limit_bottom(segs, page.rect.height), regions,
                     images)
    # 沒翻譯的文字保留隱形文字層，仍可搜尋、選取
    for s in segs:
        if s["kind"] in TRANSLATE_KINDS and s.get("zh"):
            continue
        for ln in s["lines"]:
            b = ln["bbox"]
            npage.insert_text((b[0], b[3] - 0.2 * ln["size"]), plain(ln["text"], s.get("math")),
                              fontsize=ln["size"], render_mode=3)
    return log


def _render_rebuild(src, pno, segs, regions, out, archive, images):
    """不保留原背景：白紙上貼回圖片、表格、獨立公式，再寫上中文與不翻的原文。"""
    page = src[pno]
    img, sx, sy, off = page_raster(page, raster_dpi(page))
    for s in segs:
        s["mode"] = "raster"
    clean = _sample_colors(img, segs, sx, sy, off, fill=True)  # 表格裡的英文清掉
    npage = out.new_page(width=page.rect.width, height=page.rect.height)
    canvas = np.full_like(img, 255)  # 用來判斷文字可延伸的空白處

    for r in regions:
        src_img = img if r["type"] in PICTURE_REGIONS else clean
        x0, y0, x1, y1 = px_rect(r["bbox"], sx, sy, off, 2, img.shape)
        crop = src_img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        canvas[y0:y1, x0:x1] = crop
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
        npage.insert_image(pymupdf.Rect(x0 / sx + off[0], y0 / sy + off[1], x1 / sx + off[0], y1 / sy + off[1]),
                           stream=buf.tobytes())

    in_table = {i for i, r in enumerate(regions) if r["type"] == "table"}
    for s in segs:
        if s["kind"] in TRANSLATE_KINDS and s.get("zh"):
            if s.get("region") not in in_table:
                s["bg"] = [255, 255, 255]
            s["color"] = _readable(s.get("color", [0, 0, 0]))
    log = write_text(npage, segs, canvas, sx, sy, off, archive, _limit_bottom(segs, page.rect.height), regions,
                     images)

    # 不翻的文字（頁首、頁碼、註腳、參考文獻、純數字）改用真正的文字重寫在原位置
    for s in segs:
        if s["kind"] in TRANSLATE_KINDS and s.get("zh") or s["kind"] in ("figtext", "math", "side") \
                or s.get("region") is not None:
            continue
        if s["kind"] == "keep" and re.fullmatch(r"[A-Za-z ]{1,3}", s["text"]):
            continue  # OCR 殘片（被拆開的單一字母），重寫會疊在中文上
        color = _hex(_readable(_seg_color(img, s, sx, sy, off)))
        for ln in s["lines"]:
            b = ln["bbox"]
            text = to_html(ln["text"], s.get("math"), images)
            style = f"font-family:sans;font-size:{ln['size'] * 0.9:.2f}pt;line-height:1.1;color:{color};"
            npage.insert_htmlbox(pymupdf.Rect(b[0], b[1] - 1, max(b[2] + 30, b[0] + 20), b[3] + 4),
                                 f'<div style="{style}">{text}</div>', css=CSS, archive=archive, scale_low=0)
    return log


def render(src_path, pages, segs_by_page, regions_by_page, modes, out_path):
    src = pymupdf.open(src_path)
    out = pymupdf.open()
    archive = pymupdf.Archive(FONT_DIR)
    images = MathImages(src, archive)
    log = []
    for pno in pages:
        mode = modes[pno]
        if mode == "empty":
            out.insert_pdf(src, from_page=pno, to_page=pno)
            log.append(f"第 {pno + 1} 頁 沒有文字層（可能是掃描圖），無法翻譯，原樣保留")
            continue
        fn = {"native": _render_native, "raster": _render_raster, "rebuild": _render_rebuild}[mode]
        segs, regs = segs_by_page[pno], regions_by_page[pno]
        msgs = fn(src, pno, segs, regs, out, archive, images) + check_overlaps(segs, regs)
        log += [f"第 {pno + 1} 頁 {m}" for m in msgs]
    out.subset_fonts(fallback=True)  # 用 fontTools 精簡字型（MuPDF 內建的處理不了這套 CJK 字型）
    out.save(out_path, garbage=4, deflate=True)
    return log
