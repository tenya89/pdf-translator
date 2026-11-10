"""PDF 英翻繁中（套用華美辭典），輸出 PDF、DOCX 與檢查報告。

用法：
  .venv/bin/python translator/run.py 原檔.pdf                       # 整份
  .venv/bin/python translator/run.py 原檔.pdf --pages 7-10 --name ch1
金鑰與端點讀自專案根目錄的 .env（GEMMA_BASE_URL、GEMMA_API_KEY）。
"""
import argparse
import json
import os
import sys
import time

import pymupdf

sys.path.insert(0, os.path.dirname(__file__))
from extract import extract_page, link_continuations, mark_references, page_mode, plain  # noqa: E402
from glossary import Glossary, doc_abbreviations, satisfied  # noqa: E402
from render_docx import build as build_docx  # noqa: E402
from render_pdf import TRANSLATE_KINDS, render  # noqa: E402
import regions  # noqa: E402
from translate import Translator, split_translation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def parse_pages(spec, n):
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a) - 1, int(b or a)))
    return [p for p in out if 0 <= p < n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="", help="頁碼（從 1 起算），例如 7-10；省略則處理整份")
    ap.add_argument("--name", default="", help="輸出檔名後綴，例如 ch1")
    ap.add_argument("--dict", default="", help="華美辭典 CSV 資料夾（省略則讀 .env 的 GLOSSARY_DIR）")
    ap.add_argument("--out", default=os.path.join(ROOT, "output"))
    ap.add_argument("--model", default="gemma31-nothink")
    ap.add_argument("--context", default="")
    ap.add_argument("--mode", choices=["auto", "rebuild"], default="auto",
                    help="auto：自動判斷（預設）；rebuild：圖片型頁面不保留原背景，只在白紙上重排文字、圖片、表格")
    args = ap.parse_args()

    load_env(os.path.join(ROOT, ".env"))
    args.dict = args.dict or os.environ.get("GLOSSARY_DIR", "")
    if not args.dict:
        sys.exit("請用 --dict 或 .env 的 GLOSSARY_DIR 指定華美辭典 CSV 資料夾")
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    # 檔名加上頁數，例如 _ch1_p7-10；多段頁碼 7-10,14 會變成 _p7-10_14
    pages_tag = ("_p" + args.pages.replace(" ", "").replace(",", "_")) if args.pages else ""
    suffix = (f"_{args.name}" if args.name else "") + pages_tag
    t0 = time.time()

    src = pymupdf.open(args.pdf)
    pages = parse_pages(args.pages, src.page_count) if args.pages else list(range(src.page_count))
    print(f"處理第 {pages[0] + 1}–{pages[-1] + 1} 頁")

    # 1. 解析：判斷每頁種類（原生文字 / 圖片＋OCR 文字層 / 沒有文字），取出段落
    modes = {p: page_mode(src[p]) for p in pages}
    if args.mode == "rebuild":
        modes = {p: ("rebuild" if m == "raster" else m) for p, m in modes.items()}
    seg_objs = [extract_page(src[p], p, modes[p]) for p in pages]
    page_w = src[pages[0]].rect.width
    mark_references(seg_objs, page_w)
    link_continuations(seg_objs, page_w)
    segs = {p: [s.to_dict() for s in so] for p, so in zip(pages, seg_objs)}
    by_id = {s["id"]: s for p in pages for s in segs[p]}
    # 找出圖片與表格：圖片裡的字不翻，表格裡的字照翻
    regs, header_h = {}, {}
    for p in pages:
        regs[p], header_h[p] = regions.analyze(src, src[p], segs[p]) if modes[p] != "empty" else ([], 0)
        kinds = {"native": "原生文字", "raster": "整頁圖片＋文字層", "rebuild": "整頁圖片＋文字層（重建）",
                 "empty": "沒有文字層"}
        names = {"figure": "圖片", "table": "表格", "equation": "公式"}
        found = "、".join(names[r["type"]] for r in regs[p]) or "無圖表"
        print(f"  第 {p + 1} 頁：{kinds[modes[p]]}；{found}")

    # 落在翻譯段落範圍內的零碎符號（OCR 漂出來的括號、標點）併入該段，不單獨處理
    for p in pages:
        paras = [s for s in segs[p] if s["kind"] in TRANSLATE_KINDS and len(s["lines"]) >= 2]
        for s in segs[p]:
            if s["kind"] == "keep" and len(s["text"]) <= 3:
                cx, cy = (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
                if any(b["bbox"][0] <= cx <= b["bbox"][2] and b["bbox"][1] <= cy <= b["bbox"][3] for b in paras):
                    s["kind"] = "absorbed"

    # 2. 組翻譯單位（跨欄的句子合成一段一起翻）
    units, used = [], set()
    for p in pages:
        for s in segs[p]:
            if s["kind"] not in TRANSLATE_KINDS or s["id"] in used:
                continue
            chain = [s]
            while chain[-1]["cont_next"] in by_id and by_id[chain[-1]["cont_next"]]["kind"] == "body":
                chain.append(by_id[chain[-1]["cont_next"]])
            used.update(c["id"] for c in chain)
            units.append({"id": s["id"], "kind": s["kind"], "text": " ".join(c["text"] for c in chain),
                          "parts": [c["id"] for c in chain]})

    # 3. 翻譯
    g = Glossary(args.dict)
    full_text = " ".join(src[i].get_text().replace("\n", " ") for i in range(src.page_count))
    doc_abbr = doc_abbreviations(full_text)
    print(f"全文找到 {len(doc_abbr)} 個作者定義的縮寫")
    tr = Translator(os.environ["GEMMA_BASE_URL"], os.environ["GEMMA_API_KEY"], args.model, g,
                    os.path.join(args.out, "cache.json"), doc_abbr=doc_abbr)
    # 文件背景由模型讀前幾頁自動產生；也可用 --context 自行指定
    tr.context = args.context or tr.describe(" ".join(src[i].get_text() for i in range(min(6, src.page_count))))
    print(f"文件背景：{tr.context}")
    res = tr.translate([dict({k: u[k] for k in ("id", "kind", "text")}, plain=plain(u["text"])) for u in units])
    for u in units:
        zh = res[u["id"]]["zh"]
        for sid, piece in zip(u["parts"], split_translation(zh, [by_id[i]["text"] for i in u["parts"]])):
            by_id[sid]["zh"] = piece
        # DOCX 照順序排版，跨欄/跨頁的句子用整句譯文，放在第一段
        if len(u["parts"]) > 1:
            by_id[u["parts"][0]]["zh_full"] = zh
            for sid in u["parts"][1:]:
                by_id[sid]["cont_of"] = u["parts"][0]

    # 4. PDF
    pdf_out = os.path.join(args.out, f"{stem}{suffix}_zh.pdf")
    print("產生 PDF …")
    render_log = render(args.pdf, pages, segs, regs, modes, pdf_out)

    # 5. DOCX
    docx_out = os.path.join(args.out, f"{stem}{suffix}_zh.docx")
    print("產生 DOCX …")
    build_docx(args.pdf, pages, segs, regs, header_h, modes, docx_out)

    # 6. 報告
    report_path = os.path.join(args.out, f"{stem}{suffix}_report.md")
    write_report(report_path, units, res, render_log, regs, args, time.time() - t0)
    with open(os.path.join(args.out, f"{stem}{suffix}_segments.json"), "w", encoding="utf-8") as f:
        json.dump({"segments": segs, "regions": regs}, f, ensure_ascii=False, indent=1, default=str)
    print(f"完成（{time.time() - t0:.0f} 秒）\n  {pdf_out}\n  {docx_out}\n  {report_path}")


def write_report(path, units, res, render_log, regs, args, secs):
    must_total = must_ok = 0
    applied, problems = {}, []
    for u in units:
        r = res[u["id"]]
        for h in r["hits"]:
            if h["kind"] != "phrase":
                continue
            must_total += 1
            ok = satisfied(h, r["zh"])
            must_ok += ok
            key = (h["src"].lower(), "／".join(h["zh"]))
            a = applied.setdefault(key, [0, 0])
            a[0] += 1
            a[1] += ok
        if r["problems"]:
            problems.append((u, r))
    L = [f"# 翻譯檢查報告", "",
         f"- 模型：{args.model}　頁數：{args.pages or "全部"}　耗時：{secs:.0f} 秒",
         f"- 翻譯段數：{len(units)}；需人工複查：{len(problems)} 段",
         f"- 辭典必用詞條：命中 {must_total} 次，譯文有使用辭典譯法 {must_ok} 次", ""]
    L += ["## 需要人工複查的段落", ""]
    if not problems:
        L.append("無。")
    for u, r in problems:
        L += [f"### {u['id']}（{u['kind']}）", f"- 問題：{'；'.join(r['problems'])}",
              f"- 原文：{u['text']}", f"- 譯文：{r['zh']}", ""]
    flat = [u["id"] for u in units if res[u["id"]].get("note")]
    if flat:
        L += ["", "## 公式改以純文字呈現的段落", "",
              "模型多次漏掉公式標記，這些段落改用純文字公式翻譯（內容完整，但上下標格式消失）：" + "、".join(flat)]
    L += ["", "## 辭典必用詞條使用情形", "", "| 原文 | 辭典譯法 | 出現 | 已套用 |", "|---|---|---|---|"]
    for (en, zh), (n, ok) in sorted(applied.items()):
        L.append(f"| {en} | {zh} | {n} | {ok} |")
    L += ["", "## 版面", ""]
    L += [f"- {m}" for m in render_log] or ["- 全部文字都放得下。"]
    for p, rs in regs.items():
        nf = sum(r["type"] == "figure" for r in rs)
        nt = sum(r["type"] == "table" for r in rs)
        if rs:
            L.append(f"- 第 {p + 1} 頁：圖片 {nf} 個（原樣保留、不翻譯），表格 {nt} 個（已翻譯）")
    L += ["", "## 逐段對照", ""]
    for u in units:
        L += [f"**{u['id']}**　{u['text']}", "", f"> {res[u['id']]['zh']}", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
