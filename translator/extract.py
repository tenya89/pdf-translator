"""從 PDF 的文字層取出文字段落，並判斷每段的類型。

支援的頁面（自動判斷，見 page_mode）：
  native  原生文字頁：文字是真正畫出來的字
  raster  圖片頁：整頁是圖片，上面疊一層隱形的 OCR 文字（別人事先做好的，我們不做 OCR）
  empty   沒有文字層，無法翻譯

讀取以「字元」為單位，才能：
  - 濾掉控制字元（例如 TeX 數學字型的大括號被記成 \\x10）
  - 修復 OCR 碎片（re uirement + 漂走的 q → requirement；Stockp + iling → Stockpiling）
  - 認出數學字型，把行內公式換成 ⟪n⟫ 標記，並記下每個字的大小、高度，之後重建上下標

段落類型：
  header     頁首（頁面最上方的單行），不翻
  pagenum    頁首頁尾的頁碼與其他單行，不翻
  footnote   頁尾註腳，不翻
  reference  參考文獻，不翻
  math       獨立成行的公式，原樣保留
  heading    標題
  caption    圖說、表說（Figure 1. / Table 1.）
  body       內文
  keep       純數字、符號、網址，不翻
  side       直式文字（側邊浮水印等），不翻
（之後 regions 會再把圖片裡的字改成 figtext、表格裡的字改成 table）
"""
import re
import statistics
import unicodedata

SUP_RE = re.compile(r"\s*\d{1,3}(?:[,–-]\d{1,3})*\s*")
SERIF_HINT = re.compile(r"times|serif|roman|georgia|garamond|minion|cambria|palatino|book|mincho|ming|song|"
                        r"libertine|charter|computer ?modern|^cmr|^lmr|nimbusrom|termes|stix", re.I)
FOOTNOTE_RE = re.compile(r"^\d{1,3}\s?(?:\.\s?|\s)[A-Z“\"]")
REFERENCES_RE = re.compile(r"^(?:\d+\.?\s*)?(references|bibliography|works cited|literature cited|參考文獻)\s*$",
                           re.I)
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")
PUA_RE = re.compile(r"[\ue000-\uf8ff\ufe00-\ufe0f]")  # 私用區字元、變體選擇字元
MATH_FONT = re.compile(r"math|^(cm|lm)(mi|sy|ex|bsy)|^ms(am|bm)|^stix|symbol|^eu(fm|sm|rm|ex)|esint|^rsfs|"
                       r"^(n|r)?tx(sy|ex|mi)|^ztmcm|^xits|asana|^mt(mi|sy|ex)", re.I)
OPS = set("=+−-–×·*/<>≤≥≈∼~()[]{}|,.:;'′″0123456789 ∑∏∫√∂∇±∓→←↔∞")

# 全文件的行內公式：編號 → 資訊（文字、上下標結構、是否複雜、位置）
MATH = {}


def page_mode(page):
    """判斷頁面種類：native / raster / empty。"""
    trace = page.get_texttrace()
    if not trace:
        return "empty"
    visible = sum(len(t["chars"]) for t in trace if t["type"] != 3 and t.get("opacity", 1) > 0)
    hidden = sum(len(t["chars"]) for t in trace) - visible
    return "native" if visible >= hidden else "raster"


class Seg:
    def __init__(self, page, idx, lines):
        self.page = page
        self.id = f"p{page + 1}_{idx}"
        self.lines = lines  # [{"bbox", "text", "size", "bold", "serif", "color", "n_math", "n_chars"}]
        self.kind = "body"
        self.cont_next = None  # 段落接續到下一欄/下一頁時，記下一段的 id

    @property
    def bbox(self):
        xs0, ys0, xs1, ys1 = zip(*(l["bbox"] for l in self.lines))
        return (min(xs0), min(ys0), max(xs1), max(ys1))

    @property
    def size(self):
        """字級。OCR 的字級有時不準，多行時改用行距推估（行距約為字級 1.08 倍以上）。"""
        ocr = statistics.median(l["size"] for l in self.lines)
        if len(self.lines) >= 3:
            ys = [l["bbox"][3] for l in self.lines]
            pitch = statistics.median(b - a for a, b in zip(ys, ys[1:]))
            if pitch > 0:
                return round(min(ocr, pitch / 1.08), 2)
        return ocr

    def _major(self, key):
        vals = [l[key] for l in self.lines for _ in range(max(1, len(l["text"])))]
        return max(set(vals), key=vals.count) if vals else None

    @property
    def text(self):
        out = ""
        for l in self.lines:
            t = l["text"]
            if not out:
                out = t
            elif out.endswith("-") and t[:1].islower():
                out = out + t  # 斷字：well- / defended
            else:
                out = out + " " + t
        return re.sub(r"\s+", " ", out).strip()

    def to_dict(self):
        text = self.text
        keys = re.findall(r"⟪(\d+)⟫", text)
        return {"id": self.id, "page": self.page, "kind": self.kind, "bbox": self.bbox,
                "size": self.size, "text": text, "lines": self.lines, "cont_next": self.cont_next,
                "bold": self._major("bold"), "serif": self._major("serif"), "src_color": self._major("color"),
                "math": {k: MATH[int(k)] for k in keys if int(k) in MATH}}


def _rgb(c):
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


def _font_name(f):
    return f.split("+")[-1]


def _is_math_span(s):
    if MATH_FONT.search(_font_name(s["font"])):
        return True
    return any(0x1D400 <= ord(c["c"]) <= 0x1D7FF for c in s["chars"])


# ---------- 讀取字元 ----------

def _read_blocks(page):
    """讀出每個區塊、每一行的字元（含位置、字級、字型、是否為數學字型）。"""
    blocks, sid = [], 0
    for b in page.get_text("rawdict")["blocks"]:
        if b["type"] != 0:
            continue
        lines = []
        for l in b["lines"]:
            chars = []
            for s in l["spans"]:
                sid += 1
                is_math = _is_math_span(s)
                for c in s["chars"]:
                    ch = c["c"]
                    ctrl = bool(CTRL_RE.match(ch))
                    if PUA_RE.match(ch):
                        ch = ""
                    chars.append({"c": "" if ctrl else ch, "bbox": c["bbox"], "oy": c["origin"][1],
                                  "size": s["size"], "font": s["font"], "flags": s["flags"], "color": s["color"],
                                  "math": is_math, "ctrl": ctrl, "sid": sid, "frag": False})
            if any(ch["c"].strip() or ch["ctrl"] for ch in chars):
                lines.append({"chars": chars, "bbox": l["bbox"], "vertical": abs(l["dir"][1]) > 0.5})
        if lines:
            blocks.append(lines)
    return blocks


def _visible(chars):
    return [c for c in chars if c["c"].strip()]


def _cx(b):
    return (b[0] + b[2]) / 2


def _cy(b):
    return (b[1] + b[3]) / 2


def _line_size(line):
    vis = [c["size"] for c in _visible(line["chars"]) if not c["frag"]]
    return statistics.median(vis) if vis else 10


def _repair_fragments(blocks):
    """OCR 碎片修復（只用現成的字元座標）：
    1. 1～3 個字母的小片段，落在某一行的空格處 → 填回去；緊貼在行首左邊 → 接到行首
    2. 同一高度、緊貼在一起的兩段（Stockp + iling）→ 接成一段"""
    lines = [(bi, ln) for bi, bl in enumerate(blocks) for ln in bl]

    def is_frag(ln):
        vis = _visible(ln["chars"])
        return 0 < len(vis) <= 3 and all(c["c"].isalpha() or c["c"] in "-'’" for c in vis) \
            and not any(c["math"] for c in vis)

    drop = set()
    for i, (bi, f) in enumerate(lines):
        if not is_frag(f):
            continue
        placed = []
        for fc in _visible(f["chars"]):
            fx, fy = _cx(fc["bbox"]), _cy(fc["bbox"])
            best = None
            for j, (bj, g) in enumerate(lines):
                if j == i or j in drop or is_frag(g):
                    continue
                gb, gs = g["bbox"], _line_size(g)
                if gb[1] - 2 <= fy <= gb[3] + 2 and gb[0] - 1.5 * gs <= fx <= gb[2] + 0.5 * gs:
                    d = abs(_cy(gb) - fy)
                    if best is None or d < best[0]:
                        best = (d, j)
            if best is None:
                continue
            placed.append(fc)
            g = lines[best[1]][1]
            chars = g["chars"]
            idx = next((k for k, c in enumerate(chars) if _cx(c["bbox"]) > fx), len(chars))
            new = dict(fc, frag=True, bbox=(fc["bbox"][0], g["bbox"][1], fc["bbox"][2], g["bbox"][3]))
            # 落在空格上就取代空格，否則插入
            for k in (idx - 1, idx):
                if 0 <= k < len(chars) and not chars[k]["c"].strip() \
                        and chars[k]["bbox"][0] - 1 <= fx <= chars[k]["bbox"][2] + 1:
                    chars[k] = new
                    break
            else:
                chars.insert(idx, new)
            g["bbox"] = (min(g["bbox"][0], fc["bbox"][0]), g["bbox"][1], max(g["bbox"][2], fc["bbox"][2]),
                         g["bbox"][3])
        f["chars"] = [c for c in f["chars"] if not any(c is p for p in placed)]
        if not _visible(f["chars"]):
            drop.add(i)

    # 同一高度、緊鄰的片段接回同一行：
    #   OCR 把一行切成左右兩塊（materials. | Current production…）
    #   字級不同的下標讓 PDF 把一行拆開（where A | color and A | entity denote…）
    #   單字被切斷（Stockp | iling）時不加空白
    changed = True
    while changed:
        changed = False
        alive = [j for j in range(len(lines)) if j not in drop]
        for i in alive:
            a = lines[i][1]
            av = _visible(a["chars"])
            if not av:
                continue
            ab = a["bbox"]
            best = None
            for j in alive:
                if j == i or j in drop:
                    continue
                b = lines[j][1]
                bv = _visible(b["chars"])
                if not bv:
                    continue
                bb = b["bbox"]
                h = min(ab[3] - ab[1], bb[3] - bb[1])
                vover = min(ab[3], bb[3]) - max(ab[1], bb[1])
                gap = bb[0] - ab[2]
                size = max(_line_size(a), _line_size(b))
                if vover > 0.5 * h and -0.4 * size <= gap <= 0.8 * size and bb[0] > ab[0] \
                        and (best is None or gap < best[0]):
                    best = (gap, j, size)
            if best is None:
                continue
            gap, j, size = best
            b = lines[j][1]
            bv = _visible(b["chars"])
            word_cut = av[-1]["c"].isalpha() and bv[0]["c"].islower() and gap <= 0.3 * size
            joiner = []
            if not word_cut and gap > 0.15 * size and a["chars"][-1]["c"] != " " and b["chars"][0]["c"] != " ":
                joiner = [dict(av[-1], c=" ", frag=True, math=False, ctrl=False, sid=-1)]
            a["chars"] = a["chars"] + joiner + b["chars"]
            bb = b["bbox"]
            a["bbox"] = (min(ab[0], bb[0]), min(ab[1], bb[1]), max(ab[2], bb[2]), max(ab[3], bb[3]))
            drop.add(j)
            changed = True
            break

    out = [[] for _ in blocks]
    for i, (bi, ln) in enumerate(lines):
        if i not in drop:
            out[bi].append(ln)
    return [bl for bl in out if bl]


# ---------- 組成一行文字（含上標註腳、行內公式） ----------

def _math_entry(chars, base_size, base_oy, pno):
    """依每個字的大小與高度判斷上下標；結構太複雜的標記為 complex（之後改用原公式圖片）。"""
    vis = [c for c in chars if c["c"].strip() or c["ctrl"]]
    complex_ = any(c["ctrl"] for c in vis) or any(c["size"] > 1.25 * base_size for c in vis) \
        or any(c["size"] < 0.6 * base_size for c in vis)
    parts = []
    for c in chars:
        ch = unicodedata.normalize("NFKC", c["c"])
        if not ch:
            continue
        level = 0
        if c["size"] < 0.85 * base_size and ch.strip():
            level = -1 if c["oy"] > base_oy + 0.12 * base_size else 1
        italic = bool(c["flags"] & 2) or bool(re.search(r"mi\b|mi\d|italic|-it", _font_name(c["font"]), re.I))
        italic = italic and ch.isalpha()
        if parts and parts[-1][1] == level and parts[-1][2] == italic:
            parts[-1][0] += ch
        else:
            parts.append([ch, level, italic])
    # 上下堆疊的字（分數、Σ 上下限）也算複雜
    for a in vis:
        for b in vis:
            if a is b:
                continue
            ov = min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0])
            w = min(a["bbox"][2] - a["bbox"][0], b["bbox"][2] - b["bbox"][0])
            if w > 0 and ov > 0.5 * w and abs(a["oy"] - b["oy"]) > 0.5 * base_size:
                complex_ = True
    xs0, ys0, xs1, ys1 = zip(*(c["bbox"] for c in vis)) if vis else ([0], [0], [0], [0])
    if parts:  # 頭尾空白不算公式
        parts[0][0] = parts[0][0].lstrip()
        parts[-1][0] = parts[-1][0].rstrip()
        parts = [p for p in parts if p[0]]
    plain = "".join(p[0] for p in parts)
    return {"text": plain.strip(), "parts": [p for p in parts], "complex": complex_,
            "bbox": [min(xs0) - 0.5, min(ys0) - 0.5, max(xs1) + 0.5, max(ys1) + 0.5], "page": pno}


def _build_line(line, pno):
    chars = line["chars"]
    spans = []
    for c in chars:
        if spans and spans[-1]["sid"] == c["sid"] and spans[-1]["math"] == c["math"]:
            spans[-1]["chars"].append(c)
        else:
            spans.append({"sid": c["sid"], "math": c["math"], "chars": [c]})
    for s in spans:
        s["text"] = "".join(c["c"] for c in s["chars"])
        s["size"] = s["chars"][0]["size"]

    body = [c for c in chars if c["c"].strip() and not c["math"] and not c["frag"]]
    sizes = sorted(c["size"] for c in body) or sorted(c["size"] for c in chars)
    med = sizes[len(sizes) // 2]
    oys = sorted(c["oy"] for c in body if c["size"] >= 0.9 * med) or [line["bbox"][3]]
    base_oy = oys[len(oys) // 2]

    # 夾在兩段公式之間、只有運算符號的片段也算公式（例如 α = β 中間的 =）
    for k in range(1, len(spans) - 1):
        if not spans[k]["math"] and spans[k - 1]["math"] and spans[k + 1]["math"] \
                and all(ch in OPS for ch in spans[k]["text"]):
            spans[k]["math"] = True
    # 緊貼在公式旁、字比較小的文字是公式的上下標（例如 L_binding 的 binding、ΔE_00 的 00、i-th 的 th）
    changed = True
    while changed:
        changed = False
        for k, sp in enumerate(spans):
            if sp["math"] or not sp["text"].strip() or sp["size"] >= 0.85 * med:
                continue
            for nb in (k - 1, k + 1):
                if 0 <= nb < len(spans) and spans[nb]["math"]:
                    near = spans[nb]["chars"][-1 if nb < k else 0]["bbox"]
                    mine = sp["chars"][0 if nb < k else -1]["bbox"]
                    if abs(mine[0] - near[2]) < 0.6 * med or abs(near[0] - mine[2]) < 0.6 * med:
                        sp["math"] = True
                        changed = True
                        break

    parts, n_math, k = [], 0, 0
    while k < len(spans):
        s = spans[k]
        if s["math"]:
            run = []
            while k < len(spans) and spans[k]["math"]:
                run += spans[k]["chars"]
                k += 1
            # 前後的空白留在外面
            entry = _math_entry(run, med, base_oy, pno)
            n_math += sum(1 for c in run if c["c"].strip())
            if re.search(r"[A-Za-z0-9Ͱ-Ͽ]", entry["text"]) or entry["complex"]:
                key = len(MATH) + 1
                MATH[key] = entry
                parts.append(f" ⟪{key}⟫ " if entry["text"] else f"⟪{key}⟫")
            else:
                parts.append(entry["text"])  # 單純符號（→、=）直接當文字
            continue
        t = s["text"]
        if t.strip() and SUP_RE.fullmatch(t) and s["size"] < 0.8 * med:
            parts.append(f"⟦{t.strip()}⟧")  # 註腳上標
        else:
            parts.append(t)
        k += 1
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    text = re.sub(r"\s+⟦", "⟦", text)
    text = re.sub(r"([（(\[])\s+⟪", r"\1⟪", text)
    text = re.sub(r"⟫\s+([)）\],.;:])", r"⟫\1", text)
    if not text:
        return None
    main_chars = body or [c for c in chars if c["c"].strip()] or chars
    fonts = [c["font"] for c in main_chars]
    main_font = max(set(fonts), key=fonts.count)
    flags = [c["flags"] for c in main_chars if c["font"] == main_font][0]
    colors = [c["color"] for c in main_chars]
    vis_boxes = [c["bbox"] for c in chars if c["c"].strip() or c["ctrl"]]
    lb = line["bbox"]
    if vis_boxes:
        lb = (min(b[0] for b in vis_boxes), lb[1], max(b[2] for b in vis_boxes), lb[3])
    return {"bbox": tuple(round(v, 2) for v in lb), "text": text, "size": round(med, 2),
            "bold": bool(flags & 16) or "bold" in main_font.lower(),
            "serif": bool(SERIF_HINT.search(_font_name(main_font))) or bool(flags & 4),
            "color": _rgb(max(set(colors), key=colors.count)),
            "n_math": n_math, "n_chars": len([c for c in chars if c["c"].strip()]),
            "vertical": line.get("vertical", False)}


# ---------- 組成段落 ----------

def _same_para(a, b, max_gap=0.9, pitch=None):
    """b 是否接在 a 下面、屬於同一段。
    pitch 有值（圖片頁）時不看 OCR 字級（常常不準），改看行距是否正常。"""
    ax0, ay0, ax1, ay1 = a["bbox"]
    bx0, by0, bx1, by1 = b["bbox"]
    if pitch:
        # 大字標題的行距本來就比內文大，用兩行本身的字級放寬
        pitch = max(pitch, 1.15 * min(a["size"], b["size"]))
        sz = pitch / 1.15
        step = by1 - ay1
        if not (0.5 * pitch <= step <= 1.45 * pitch):
            return False
    else:
        sz = max(a["size"], b["size"])
        if not (0.8 < a["size"] / b["size"] < 1.25):
            return False
        if by0 - ay1 > max_gap * sz or by1 - ay1 < 0.5 * min(a["size"], b["size"]):
            return False  # 間距太大，或兩行在同一高度（左右並排）
    overlap = min(ax1, bx1) - max(ax0, bx0)
    if overlap <= 0:
        return False
    left_aligned = overlap > 0.3 * min(ax1 - ax0, bx1 - bx0) and abs(ax0 - bx0) < 3 * sz
    centered = abs((ax0 + ax1) / 2 - (bx0 + bx1) / 2) < 0.6 * sz  # 置中的多行標題、圖說
    return left_aligned or centered


def _page_pitch(blocks):
    steps = []
    for bl in blocks:
        for a, b in zip(bl, bl[1:]):
            d = b["bbox"][3] - a["bbox"][3]
            if 0.6 * a["size"] < d < 2 * a["size"]:
                steps.append(d)
    return statistics.median(steps) if steps else None


def extract_page(page, pno, mode="native"):
    raw = _repair_fragments(_read_blocks(page))
    blocks = []
    for bl in raw:
        lines = [x for x in (_build_line(l, pno) for l in bl) if x]
        if lines:
            blocks.append(lines)
    pitch = _page_pitch(blocks) if mode == "raster" else None

    segs, idx = [], 0
    for blines in blocks:
        cur = []
        for ln in blines:
            if cur and not _same_para(cur[-1], ln, pitch=pitch):
                segs.append(Seg(pno, idx, cur))
                idx += 1
                cur = []
            cur.append(ln)
        if cur:
            segs.append(Seg(pno, idx, cur))
            idx += 1
    # 同一段被拆在不同區塊時（例如清單第一行、置中標題第二行），位置與行距銜接的就合回同一段
    merged = []
    for sg in segs:
        a, b = (merged[-1].lines[-1], sg.lines[0]) if merged else (None, None)
        if merged:
            step = b["bbox"][3] - a["bbox"][3]
            pp = max(pitch, 1.15 * min(a["size"], b["size"])) if pitch else None
            normal = step < (1.3 * pp if pp else 1.4 * min(a["size"], b["size"]))
            if _same_para(a, b, pitch=pitch) and normal and a["bold"] == b["bold"]:
                merged[-1].lines += sg.lines
                continue
        merged.append(sg)
    merged = _absorb_overlapping(merged)
    for i, sg in enumerate(merged):
        sg.id = f"p{pno + 1}_{i}"
    _classify(merged, page.rect.height)
    return merged


def _absorb_overlapping(segs):
    """OCR 框常互相重疊：只有一兩行、大半落在另一段範圍內的片段，併入那一段（依行的位置排序），
    不單獨處理，避免兩段中文寫在同一個位置。"""
    def area(b):
        return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

    changed = True
    while changed:
        changed = False
        for a in segs:
            if len(a.lines) > 2:
                continue
            ab = a.bbox
            for b in segs:
                if b is a or area(b.bbox) <= area(ab):
                    continue
                bb = b.bbox
                inter = max(0, min(ab[2], bb[2]) - max(ab[0], bb[0])) * max(0, min(ab[3], bb[3]) - max(ab[1], bb[1]))
                if area(ab) and inter / area(ab) > 0.5:
                    b.lines = _order_lines(b.lines + a.lines)
                    segs.remove(a)
                    changed = True
                    break
            if changed:
                break
    return segs


def _order_lines(lines):
    """依位置排序：高度重疊一半以上的算同一列，同一列由左到右。"""
    rows = []
    for l in sorted(lines, key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2):
        for row in rows:
            r = row[0]["bbox"]
            h = min(r[3] - r[1], l["bbox"][3] - l["bbox"][1])
            if min(r[3], l["bbox"][3]) - max(r[1], l["bbox"][1]) > 0.5 * h:
                row.append(l)
                break
        else:
            rows.append([l])
    return [l for row in rows for l in sorted(row, key=lambda l: l["bbox"][0])]


def _classify(segs, page_h):
    sizes = [l["size"] for s in segs for l in s.lines for _ in range(len(l["text"]))]
    body = statistics.median(sizes) if sizes else 10

    # 註腳區：頁面下方、字小、以「數字. 文字」或「數字 文字」開頭的第一段以下
    foot_top = page_h
    for s in segs:
        # OCR 有時把註腳編號辨識成上標小字（⟦47⟧. Andrew…），判斷時忽略上標標記
        if s.bbox[1] > 0.7 * page_h and s.size < 0.9 * body and FOOTNOTE_RE.match(re.sub(r"[⟦⟧]", "", s.text)):
            foot_top = min(foot_top, s.bbox[1])

    for s in segs:
        x0, y0, x1, y1 = s.bbox
        t = s.text
        n_math = sum(l["n_math"] for l in s.lines)
        n_chars = sum(l["n_chars"] for l in s.lines) or 1
        words = len(re.findall(r"[A-Za-z]{3,}", re.sub(r"⟪\d+⟫", " ", t)))
        if any(l.get("vertical") for l in s.lines):
            s.kind = "side"  # 直式文字（例如側邊的 arXiv 浮水印），不翻
        elif len(s.lines) == 1 and (y1 <= 0.075 * page_h or (y1 <= 0.1 * page_h and s.size < 0.95 * body)):
            s.kind = "header"  # 頁眉：頁面最上方的單行（較低的位置要字比內文小才算）
        elif y0 >= 0.93 * page_h and len(s.lines) == 1 and not re.match(r"^(Figure|Fig\.|Table)\s*\d", t, re.I):
            s.kind = "pagenum"
        elif y0 >= foot_top - 1 and s.size < 0.9 * body:
            s.kind = "footnote"
        elif n_math and (n_math / n_chars >= 0.5 or (words <= 3 and len(s.lines) <= 4)):
            s.kind = "math"  # 獨立成行的公式
        elif re.match(r"^(Figure|Fig\.|Table|Exhibit|Chart)\s*\d", t, re.I):
            s.kind = "caption"
        elif not re.search(r"[A-Za-z]{2}", re.sub(r"⟪\d+⟫", " ", t)) or re.fullmatch(
                r"(?:doi:\s*|https?://|www\.)\S+|\S+@\S+\.\S+|ISBN[\s:]*[\d-]+", t, re.I):
            s.kind = "keep"  # 純數字、符號、網址、doi、email，不翻
        elif len(s.lines) <= 3 and not t[:1].islower() and "⟦" not in t and len(t) < 160 and (
                s.size >= 1.12 * body or (s._major("bold") and s.size >= 0.95 * body and not t.endswith("."))):
            s.kind = "heading"
        else:
            s.kind = "body"


def mark_references(pages_segs, page_w):
    """「References」標題之後（含後續頁面）的內容視為參考文獻，不翻譯。"""
    on = False
    for segs in pages_segs:
        for _, left, right in reading_order(segs, page_w):
            for s in left + right:
                if s.kind in ("header", "pagenum", "footnote"):
                    continue
                if REFERENCES_RE.match(s.text):
                    on = True
                    continue
                if on:
                    # 參考文獻後面若出現新的大標題（例如附錄），就結束
                    if s.kind == "heading" and s.size >= 1.12 * statistics.median(x.size for x in segs):
                        on = False
                    else:
                        s.kind = "reference"


def reading_order(items, page_w, key=lambda it: it.bbox):
    """依閱讀順序排序：跨欄的元素把頁面切成幾段，每段內先左欄再右欄。"""
    mid = page_w / 2

    def spanning(it):
        x0, _, x1, _ = key(it)
        return x0 < mid - 20 and x1 > mid + 20

    def cy(it):
        b = key(it)
        return (b[1] + b[3]) / 2

    spans = sorted([it for it in items if spanning(it)], key=lambda it: key(it)[1])
    others = [it for it in items if not spanning(it)]
    bands, prev = [], -1e9
    for sp in spans + [None]:
        lim = key(sp)[1] if sp is not None else 1e9
        band = [it for it in others if prev <= cy(it) < lim]
        left = sorted([it for it in band if (key(it)[0] + key(it)[2]) / 2 < mid], key=lambda it: key(it)[1])
        right = sorted([it for it in band if (key(it)[0] + key(it)[2]) / 2 >= mid], key=lambda it: key(it)[1])
        if band:
            bands.append(("cols", left, right))
        if sp is not None:
            bands.append(("span", [sp], []))
            prev = cy(sp)
    return bands


def link_continuations(pages_segs, page_w):
    """段落被切到下一欄或下一頁時（上一段沒結束、下一段小寫開頭），標記成同一句。"""
    ordered = []
    for segs in pages_segs:
        for kind, left, right in reading_order([s for s in segs if s.kind == "body"], page_w):
            ordered += left + right
    for a, b in zip(ordered, ordered[1:]):
        ta = re.sub(r"⟦[^⟧]*⟧$", "", a.text).rstrip()
        if ta and ta[-1] not in ".?!:;\"”’)" and b.text[:1].islower() and len(a.lines) >= 2:
            a.cont_next = b.id


def plain(text, math=None):
    """把 ⟪n⟫ 換回公式的純文字、⟦n⟧ 換回數字（給不翻譯的原文使用）。"""
    def sub(m):
        e = (math or {}).get(m.group(1)) or MATH.get(int(m.group(1)))
        return e["text"] if e else ""
    return re.sub(r"⟪(\d+)⟫", sub, text).replace("⟦", "").replace("⟧", "")
