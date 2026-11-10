"""找出頁面上的圖片與表格區域（在翻譯之前執行）。

規則：
  - 圖片（figure）：原樣保留，裡面的文字不翻譯、不修改
  - 表格（table）：裡面的文字照常翻譯
判斷方式（不用 OCR、不用 LLM；原生文字頁與圖片頁都一樣）：
  1. 把整頁畫成影像，挖掉所有文字行的位置，剩下的非背景像素就是圖形、線條
  2. 相連的圖形合成區域，附近的短標籤也算進區域；上下排列的多條橫線（三線表）也算一個區域
  3. 圖說以 Table 開頭、由橫線組成、或有多條格線、或文字排成多欄多列 → 表格；其餘 → 圖片
"""
import re

import cv2
import numpy as np
import pymupdf

def raster_dpi(page):
    """頁面影像的解析度：整頁是掃描圖時沿用原圖解析度（150–300），否則 150。"""
    best = 150
    area = page.rect.width * page.rect.height
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if (x1 - x0) * (y1 - y0) > 0.8 * area and x1 > x0:
            best = max(best, min(300, round(im["width"] / (x1 - x0) * 72)))
    return best


def page_raster(page, dpi=150):
    """把整頁畫成 RGB 影像（隱形的 OCR 文字不會畫出來）。回傳影像、pt→像素比例、左上角座標。"""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    return arr, pix.width / page.rect.width, pix.height / page.rect.height, (page.rect.x0, page.rect.y0)


def header_bottom(img, segs, sy, page_h):
    """頁首區下緣（pt）：頁首文字的底部，若底下接著色塊橫條，延伸到橫條結束。"""
    hb = max((s["bbox"][3] for s in segs if s["kind"] == "header"), default=0)
    bg = np.median(img.reshape(-1, 3), axis=0)
    busy = (np.abs(img.astype(int) - bg).max(axis=2) > 35).mean(axis=1) > 0.5  # 大半列都是非背景色
    if not busy[0] and hb == 0:
        return 0
    y = int(hb * sy)
    limit = int(0.12 * page_h * sy)
    while y < min(limit, len(busy)) and busy[y]:
        y += 1
    if hb == 0:
        return y / sy
    return max(hb, y / sy) + 2


def px_rect(b, sx, sy, off, pad, shape):
    x0 = int(max(0, (b[0] - off[0]) * sx - pad))
    y0 = int(max(0, (b[1] - off[1]) * sy - pad))
    x1 = int(min(shape[1], (b[2] - off[0]) * sx + pad))
    y1 = int(min(shape[0], (b[3] - off[1]) * sy + pad))
    return x0, y0, x1, y1


def _near(a, b, m):
    return a[0] - m < b[2] and b[0] - m < a[2] and a[1] - m < b[3] and b[1] - m < a[3]


def _union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _merge(boxes, gap):
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _near(boxes[i], boxes[j], gap):
                    boxes[i] = _union(boxes[i], boxes.pop(j))
                    changed = True
                    break
            if changed:
                break
    return boxes


def _center_in(seg_bbox, box, m=1):
    cx, cy = (seg_bbox[0] + seg_bbox[2]) / 2, (seg_bbox[1] + seg_bbox[3]) / 2
    return box[0] - m <= cx <= box[2] + m and box[1] - m <= cy <= box[3] + m


def _ruling_lines(img_bin, r, sx):
    """區域內橫跨 70% 寬度以上的水平線數量（表格格線）。"""
    x0, y0, x1, y1 = r
    roi = img_bin[y0:y1, x0:x1]
    if roi.size == 0:
        return 0
    rows = (roi.mean(axis=1) > 0.7)
    # 相鄰的列算同一條線
    return int(np.sum(rows[1:] & ~rows[:-1]) + (1 if rows[:1].any() else 0))


def _grid_shape(segs_in):
    """區域內文字的欄數與列數（左緣 x 與上緣 y 分群）。"""
    def clusters(vals, gap):
        vals = sorted(vals)
        n = 1 if vals else 0
        for a, b in zip(vals, vals[1:]):
            if b - a > gap:
                n += 1
        return n
    texty = [s for s in segs_in if re.search(r"[A-Za-z]{2}", s["text"])]
    return clusters([s["bbox"][0] for s in texty], 25), clusters([s["bbox"][1] for s in texty], 6)


def _text_cover(segs_in, b):
    area = (b[2] - b[0]) * (b[3] - b[1])
    return sum((l["bbox"][2] - l["bbox"][0]) * (l["bbox"][3] - l["bbox"][1]) for s in segs_in for l in s["lines"]) / area


def _rule_groups(nonbg, sx, sy, off, segs):
    """上下排列的長橫線（三線表）。回傳每組的範圍。"""
    k = max(10, int(60 * sx))
    horiz = cv2.morphologyEx(nonbg, cv2.MORPH_OPEN, np.ones((1, k), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(horiz)
    rules = []
    for i in range(1, n):
        x, y, w, h, _ = st[i]
        if w / sx >= 80 and h / sy <= 3:
            rules.append([x / sx + off[0], y / sy + off[1], (x + w) / sx + off[0], (y + h) / sy + off[1]])
    rules.sort(key=lambda r: r[1])
    # 1. 長度相近的長橫線分成一組（表格的上、中、下框線）
    groups = []
    for r in rules:
        g = next((g for g in reversed(groups) if r[1] - g["last"][3] < 150 and
                  min(r[2], g["box"][2]) - max(r[0], g["box"][0]) > 0.7 * max(r[2] - r[0], g["box"][2] - g["box"][0])),
                 None)
        if g:
            g["box"] = _union(g["box"], r)
            g["last"] = r
            g["n"] += 1
        else:
            groups.append({"box": list(r), "last": r, "n": 1})
    # 2. 夾在同一組長線之間的短線（只畫在部分欄位下的線）算同一個表格；其他短線（箭頭等）不算
    groups = [g for g in groups if g["n"] >= 2]
    out = []
    for g in groups:
        inside = [s for s in segs if _center_in(s["bbox"], g["box"])]
        ncol, _ = _grid_shape(inside) if inside else (0, 0)
        if len(inside) >= 2 and ncol >= 2:
            out.append(g["box"])
    return out


def _is_paragraph(s):
    """真正的內文段落：三行以上、而且行夠長（圖例、標籤清單的行都很短）。"""
    if s["kind"] != "body" or len(s["lines"]) < 3:
        return False
    widths = sorted(l["bbox"][2] - l["bbox"][0] for l in s["lines"])
    return widths[len(widths) // 2] > 120


def _equation_regions(segs, regions):
    """把同一條獨立公式的碎片（Σ、分數上下兩行、編號 (1)）合成一個公式區域，原樣保留。"""
    figs = [r["bbox"] for r in regions]
    eq = [s for s in segs if s["kind"] == "math" and not any(_center_in(s["bbox"], f) for f in figs)]
    boxes = _merge([list(s["bbox"]) for s in eq], 12)
    # 同一列上的公式編號、標點也算進去
    tail = re.compile(r"^\(?\d{1,3}[a-z]?\)?[.,;]?$|^[,.;]$")
    for b in boxes:
        for s in segs:
            if s["kind"] not in ("keep", "math"):
                continue
            sb = s["bbox"]
            vover = min(b[3], sb[3]) - max(b[1], sb[1])
            if (_center_in(sb, b, 3) or (vover > 0.5 * (sb[3] - sb[1]) and tail.match(s["text"].strip())
                                          and -2 < sb[0] - b[2] < 150)):
                b[:] = _union(b, sb)
                s["kind"] = "math"
    boxes = _merge(boxes, 6)
    return [{"bbox": [round(float(v), 1) for v in b], "type": "equation", "images": []} for b in boxes]


def analyze(doc, page, segs):
    """在 segs（dict 清單）上標記：kind 改為 figtext（圖中文字，不翻）或 table（表格文字）。
    回傳 (regions, header_bottom)；regions = [{"bbox":[x0,y0,x1,y1], "type":"figure"|"table", "images":[...]}]。"""
    img, sx, sy, off = page_raster(page)
    bg = np.median(img.reshape(-1, 3), axis=0)
    nonbg = (np.abs(img.astype(int) - bg).max(axis=2) > 35).astype(np.uint8)
    hb = header_bottom(img, segs, sy, page.rect.height)

    foot = [s["bbox"][1] for s in segs if s["kind"] in ("footnote", "pagenum")]
    foot_top = min(foot) if foot else page.rect.height
    nonbg[:max(0, int((hb - off[1]) * sy))] = 0
    nonbg[max(0, int((foot_top - 2 - off[1]) * sy)):] = 0
    lines_only = nonbg.copy()
    # 表格橫線常緊貼文字行，要在挖掉文字之前找
    ruled = _rule_groups(nonbg, sx, sy, off, segs)
    for s in segs:  # 挖掉文字
        for ln in s["lines"]:
            r = px_rect(ln["bbox"], sx, sy, off, 2, img.shape)
            nonbg[r[1]:r[3], r[0]:r[2]] = 0

    k = max(3, int(8 * sx))
    closed = cv2.morphologyEx(nonbg, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed)
    boxes = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if w / sx > 25 and h / sy > 25:
            boxes.append([x / sx + off[0], y / sy + off[1], (x + w) / sx + off[0], (y + h) / sy + off[1]])
    boxes = _merge(boxes + [list(b) for b in ruled], 12)

    # 把圖形內、或緊貼圖形的短標籤算進區域
    # 多行的內文段落只有真的落在圖形本體內才算（避免區域擴大後連鎖吃進旁邊的段落）
    core = [list(b) for b in boxes]
    cand = [s for s in segs if s["kind"] in ("body", "heading", "keep")]
    for _ in range(3):
        for b in boxes:
            for s in cand:
                sb = s["bbox"]
                if _is_paragraph(s):  # 內文段落：要在這個區域裡，而且落在某個圖形本體內
                    ok = _center_in(sb, b) and any(_center_in(sb, c) for c in core)
                elif len(s["lines"]) >= 3:  # 多行短文字（圖例、標籤清單）：在區域內，或貼在旁邊且高度大半重疊
                    vover = min(sb[3], b[3]) - max(sb[1], b[1])
                    ok = _center_in(sb, b) or (_near(b, sb, 22) and vover > 0.5 * (sb[3] - sb[1]))
                else:  # 短標籤：在區域內、與區域相交，或緊鄰且大部分落在區域的左右範圍內（橫跨出去的大標題不算）
                    inside_x = min(sb[2], b[2]) - max(sb[0], b[0]) >= 0.6 * (sb[2] - sb[0])
                    tiny = (sb[2] - sb[0]) < 60  # 緊貼圖邊的零碎小標籤（座標軸文字等）
                    ok = _center_in(sb, b) or _near(b, sb, 0) or (_near(b, sb, 22) and inside_x) \
                        or (tiny and _near(b, sb, 6))
                if ok:
                    b[:] = _union(b, s["bbox"])
        boxes = _merge(boxes, 15)

    regions = []
    for b in boxes:
        if (b[2] - b[0]) * (b[3] - b[1]) < 3000:
            continue
        # 找最近的圖說
        caps = [s for s in segs if s["kind"] == "caption"
                and min(b[2], s["bbox"][2]) - max(b[0], s["bbox"][0]) > 0
                and (-8 <= s["bbox"][1] - b[3] < 40 or -8 <= b[1] - s["bbox"][3] < 40)]
        cap = caps[0]["text"] if caps else ""
        r = px_rect(b, sx, sy, off, 0, img.shape)
        inside = [s for s in segs if s["kind"] in ("body", "heading", "keep") and _center_in(s["bbox"], b)]
        ncol, nrow = _grid_shape(inside)
        cover = _text_cover(inside, b)
        is_ruled = any(_center_in(rb, b) for rb in ruled)
        # 表格：圖說寫 Table；或沒有 Figure 圖說，但由橫線組成、有多條格線、或文字排成多欄多列且密度高
        if re.match(r"^Table\s*\d", cap, re.I):
            typ = "table"
        elif is_ruled and not re.match(r"^(Figure|Fig\.|Exhibit)\s*\d", cap, re.I):
            typ = "table"
        elif not re.match(r"^(Figure|Fig\.|Exhibit)\s*\d", cap, re.I) and len(inside) >= 4 and (
                _ruling_lines(lines_only, r, sx) >= 4 or (ncol >= 3 and nrow >= 3 and cover > 0.15)):
            typ = "table"  # 沒有圖說時：要有足夠的文字（QR code 之類的方格圖不算表格）
        else:
            typ = "figure"
        regions.append({"bbox": [round(float(v), 1) for v in b], "type": typ})

    # 表格裡的照片（非文字、非格線的圖塊），DOCX 要放回對應的儲存格
    n2, _, st2, _ = cv2.connectedComponentsWithStats(cv2.morphologyEx(nonbg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)))
    for reg in regions:
        reg["images"] = []
        if reg["type"] != "table":
            continue
        for i in range(1, n2):
            x, y, w, h, _ = st2[i]
            bb = [x / sx + off[0], y / sy + off[1], (x + w) / sx + off[0], (y + h) / sy + off[1]]
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            if bw > 20 and bh > 12 and bw < 0.6 * (reg["bbox"][2] - reg["bbox"][0]) and _center_in(bb, reg["bbox"]):
                reg["images"].append([round(float(v), 1) for v in bb])

    regions += _equation_regions(segs, regions)

    for s in segs:
        for reg in regions:
            if reg["type"] == "equation":
                continue
            # 內文段落要真的落在圖形本體內才算圖中文字（跟吸收時的規則一致）
            if _is_paragraph(s) and reg["type"] == "figure" \
                    and not any(_center_in(s["bbox"], c) for c in core):
                continue
            if s["kind"] in ("body", "heading", "keep") and _center_in(s["bbox"], reg["bbox"]):
                s["kind"] = "figtext" if reg["type"] == "figure" else ("table" if s["kind"] != "keep" else "keep")
                s["region"] = regions.index(reg)
    return regions, round(float(hb), 1)
