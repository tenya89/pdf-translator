"""呼叫 Gemma（OpenAI 相容介面）翻譯，並套用華美辭典。

流程：
  1. 每段比對辭典，把命中的詞條寫進 prompt
  2. 一次送一頁的多個段落，要求以 JSON 回傳
  3. 檢查：註腳標記、強制詞條、殘留英文；不合格的段落單獨重翻
  4. OpenCC 確保繁體
  5. 結果存快取，重跑時不用重翻
"""
import concurrent.futures as cf
import hashlib
import json
import os
import re
import time

import httpx
import opencc

from glossary import satisfied

PROMPT_VERSION = "v7"
def _load_simplified_only():
    """從 OpenCC 字典找出「純簡體字」：繁體候選中不包含自己的字（例如 发→發、髮）。
    干、了、台、里、后 這類字自己也在候選中，代表在繁體中也正確，不轉換。"""
    path = os.path.join(os.path.dirname(opencc.__file__), "dictionary", "STCharacters.txt")
    table = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, _, v = line.rstrip("\n").partition("\t")
            cands = v.split()
            if len(k) == 1 and cands and k not in cands:
                table[k] = cands[0]
    return table


_SIMP_ONLY = _load_simplified_only()


def to_traditional(zh):
    """只把純簡體字換成繁體，其他字不動。"""
    return "".join(_SIMP_ONLY.get(ch, ch) for ch in zh)

SYSTEM = """你是專業譯者，將英文文件翻譯成台灣慣用的繁體中文，用詞要符合文件所屬的專業領域。
文件背景：{context}

翻譯規則：
1. 譯文要通順、專業，使用台灣用語，標點使用全形中文標點。
2. 「⟦數字⟧」是註腳標記，必須原樣保留在對應位置，不可刪除或改寫。
3. 「⟪數字⟫」是數學公式或符號，必須原樣保留（含編號），放在譯文中對應的位置；不可改寫、展開或翻譯。
   譯文中不可出現 LaTeX 語法（例如 $...$、\\alpha、\\rightarrow）。
4. 人名（作者、人物）一律保留英文原文，不要翻譯或音譯成中文（例如 Lee Hsi-min、Chao-Ying CHANG 照原樣）。
   機構名、報告名、計畫名可譯成中文，並在後面以括號保留英文原文；常見縮寫（如 PLA、CSIS、USV、LLM）保留英文。
   一般名詞與術語不要附英文括號。「Taiwan」譯為「台灣」（不用「臺灣」）。
5. 數字、金額、年份、單位照原文保留。
6. 原文可能來自 OCR 辨識，可能有單字黏在一起或缺字（例如 ministerWellingtonKoo），請依語意理解後翻譯。
7. 每段的 kind 表示用途：heading 是標題、caption 是圖說、table 是表格儲存格、body 是內文。標題與表格內容要簡潔；表格儲存格空間有限，其中的縮寫保持原樣，不要展開。
8. 段落若附有 abbreviations，表示本文對該縮寫的定義，翻譯該縮寫時優先採用（譯成對應的中文名稱，可保留縮寫）；這些定義只適用於該段中的縮寫本身。
9. 只輸出 JSON：{{"translations":[{{"id":"...","zh":"..."}}]}}，不要其他文字。"""

GLOSSARY_INTRO = """
【華美辭典詞條】
「必用」的詞條必須使用指定譯法（有多個譯法時擇一最符合語境者）；
「參考」的詞條依語境選用最適合的譯法；
「縮寫」列出辭典中的可能意思，只有與上下文相符時才採用，否則依上下文自行翻譯。
"""


def abbr_pattern(a):
    """縮寫的比對規則：前後不能接大寫字母或數字；前面可接小寫（OCR 常把 the ODC 黏成 theODC）。"""
    return rf"(?<![A-Z0-9]){re.escape(a)}(?![A-Za-z0-9])"


def _fmt_hits(hits, doc_abbr=None):
    doc_abbr = doc_abbr or {}
    must, ref, abbr, seen = [], [], [], set()
    for h in hits:
        key = (h["kind"], h["src"].lower())
        if key in seen:
            continue
        seen.add(key)
        if h["kind"] == "phrase":
            must.append(f"- {h['src']} → {'／'.join(h['zh'])}")
        elif h["kind"] == "word":
            ref.append(f"- {h['src']} → {'／'.join(h['zh'])}")
        elif h["src"] not in doc_abbr:
            opts = "；".join(f"{e} = {z}" if e else z for e, z in h["options"])
            abbr.append(f"- {h['src']}：{opts}")

    out = ""
    if must:
        out += "必用：\n" + "\n".join(must) + "\n"
    if ref:
        out += "參考：\n" + "\n".join(ref) + "\n"
    if abbr:
        out += "縮寫：\n" + "\n".join(abbr) + "\n"
    return (GLOSSARY_INTRO + out) if out else ""


def _parse_json(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    return json.loads(m.group(0) if m else s)


LATEX_SYMBOLS = {
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒", "leftrightarrow": "↔",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ", "epsilon": "ε", "theta": "θ",
    "lambda": "λ", "mu": "μ", "sigma": "σ", "Sigma": "Σ", "rho": "ρ", "tau": "τ", "phi": "φ", "omega": "ω",
    "pi": "π", "times": "×", "cdot": "·", "pm": "±", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "approx": "≈", "sim": "∼", "in": "∈", "sum": "∑", "infty": "∞", "nabla": "∇", "partial": "∂",
}


def delatex(zh):
    """模型偶爾會把符號寫成 LaTeX（$\\rightarrow$），自動轉回一般符號。"""
    def cmd(m):
        return LATEX_SYMBOLS.get(m.group(1), m.group(0))
    out = re.sub(r"\\(?:text|mathrm|mathit|mathbf|operatorname)\{([^{}]*)\}", r"\1", zh)
    out = re.sub(r"\\([A-Za-z]+)", cmd, out)
    out = re.sub(r"\$([^$]{1,40})\$", lambda m: m.group(1).strip(), out)
    out = re.sub(r"_\{([^{}]*)\}|_([A-Za-z0-9])", lambda m: m.group(1) or m.group(2), out)
    return out


def check(item, zh):
    """回傳問題清單；空的代表合格。"""
    problems = []
    src_marks = re.findall(r"⟦[^⟧]*⟧", item["text"])
    lost = [m for m in src_marks if m not in zh]
    if lost:
        problems.append(f"遺漏註腳標記 {' '.join(lost)}")
    src_math = re.findall(r"⟪\d+⟫", item["text"])
    lost_math = [m for m in src_math if zh.count(m) != item["text"].count(m)]
    if lost_math or set(re.findall(r"⟪\d+⟫", zh)) - set(src_math):
        problems.append(f"公式標記不符（應保留 {' '.join(src_math)}）")
    if re.search(r"\\[A-Za-z]{2,}|\$[^$\s][^$]*\$", zh):
        problems.append("出現 LaTeX 語法")
    missing = [h for h in item["hits"] if h["kind"] == "phrase" and not satisfied(h, zh)]
    if missing:
        problems.append("未使用辭典譯法：" + "、".join(f"{h['src']}→{'／'.join(h['zh'])}" for h in missing))
    if re.search(r"(?:\b[a-z]{3,}\b[\s,]+){4,}", re.sub(r"[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*)+", "", zh)):
        problems.append("疑似漏翻（殘留連續英文）")
    # 原文有一般英文單字（小寫字母組成）卻沒翻成中文；只有縮寫、代號的不算
    if not re.search(r"[一-鿿]", zh) and re.search(r"\b[a-z]{3,}\b", item["text"]):
        problems.append("沒有中文")
    return problems


class Translator:
    def __init__(self, base_url, api_key, model, glossary, cache_path, context="", workers=4, doc_abbr=None):
        self.doc_abbr = doc_abbr or {}
        self.client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                   timeout=180)
        self.model, self.g, self.context, self.workers = model, glossary, context, workers
        self.cache_path = cache_path
        self.cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}

    def _key(self, item):
        defs = {a: f for a, f in self.doc_abbr.items() if re.search(abbr_pattern(a), item["text"])}
        raw = json.dumps([PROMPT_VERSION, self.model, item["kind"], item["text"], defs], ensure_ascii=False)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _chat(self, system, user, temperature=0.2):
        body = {"model": self.model, "temperature": temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(4):
            try:
                r = self.client.post("/chat/completions", json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError) as e:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def _call(self, items, extra=""):
        system = SYSTEM.format(context=self.context) + _fmt_hits([h for it in items for h in it["hits"]], self.doc_abbr) + extra
        segs = []
        for it in items:
            d = {"id": it["id"], "kind": it["kind"], "text": it["text"]}
            defs = {a: f for a, f in self.doc_abbr.items() if re.search(abbr_pattern(a), it["text"])}
            if defs:
                d["abbreviations"] = defs  # 只附給含有該縮寫的段落
            segs.append(d)
        user = json.dumps({"segments": segs}, ensure_ascii=False)
        for attempt in range(3):
            try:
                data = _parse_json(self._chat(system, user, 0.2 if attempt == 0 else 0.4))
                return {t["id"]: t["zh"].strip() for t in data["translations"]}
            except (ValueError, KeyError, TypeError):
                continue
        return {}

    def describe(self, sample):
        """請模型讀文件開頭，用一句話寫出文件背景，放進翻譯 prompt（不需人工填寫）。"""
        key = "context:" + hashlib.sha1((self.model + sample).encode()).hexdigest()
        if key not in self.cache:
            sys_p = "你是文件分析助理。請用一到兩句繁體中文（台灣用語）說明這份文件的標題、出版機構與主題領域，只輸出說明。"
            self.cache[key] = self._chat(sys_p, sample[:6000]).strip()
            self._save()
        return self.cache[key]

    def _batch(self, items):
        """翻一批；不合格的單獨重翻最多兩次。"""
        got = self._call(items)
        results = {}
        for it in items:
            zh = delatex(got.get(it["id"], ""))
            tries = 0
            probs = check(it, zh) if zh else ["沒有回傳"]
            while probs and tries < 2:
                tries += 1
                extra = "\n【上次翻譯有問題，請修正】\n" + "\n".join(probs) + (f"\n上次譯文：{zh}" if zh else "")
                zh2 = delatex(self._call([it], extra).get(it["id"], ""))
                p2 = check(it, zh2) if zh2 else ["沒有回傳"]
                if zh2 and len(p2) <= len(probs):
                    zh, probs = zh2, p2
            note = ""
            # 公式標記一直被漏掉：改用「公式已換成純文字」的版本翻譯，確保內容不缺（失去上下標格式）
            if any(p.startswith("公式標記") for p in probs) and it.get("plain") and it["plain"] != it["text"]:
                flat = dict(it, text=it["plain"])
                zh3 = delatex(self._call([flat]).get(it["id"], ""))
                if zh3:
                    zh, probs = zh3, check(flat, zh3)
                    note = "公式改以純文字呈現"
            conv = to_traditional(zh)
            results[it["id"]] = {"zh": conv, "problems": probs, "opencc_changed": conv != zh, "retries": tries,
                                 "note": note}
        return results

    def translate(self, items, batch_chars=2500, progress=print):
        """items: [{"id","kind","text"}]；回傳 {id: {"zh","problems","hits",...}}。"""
        for it in items:
            it["hits"] = self.g.match(it["text"])
        def stale(it):
            """快取裡的結果若公式標記有遺失（又還沒改用純文字版），重新翻譯。"""
            r = self.cache.get(self._key(it))
            if r is None:
                return True
            return not r.get("note") and any(p.startswith("公式標記") for p in check(it, r["zh"]))
        todo = [it for it in items if stale(it)]
        batches, cur, n = [], [], 0
        for it in todo:
            if cur and (n + len(it["text"]) > batch_chars or len(cur) >= 20):
                batches.append(cur)
                cur, n = [], 0
            cur.append(it)
            n += len(it["text"])
        if cur:
            batches.append(cur)
        progress(f"共 {len(items)} 段，需翻譯 {len(todo)} 段（{len(batches)} 批），其餘取自快取")

        done = 0
        with cf.ThreadPoolExecutor(self.workers) as ex:
            for fut in cf.as_completed([ex.submit(self._batch, b) for b in batches]):
                res = fut.result()
                for it in todo:
                    if it["id"] in res:
                        self.cache[self._key(it)] = res[it["id"]]
                done += 1
                progress(f"  批次 {done}/{len(batches)} 完成")
                self._save()
        out = {}
        for it in items:
            r = dict(self.cache[self._key(it)])
            r["zh"] = to_traditional(delatex(r["zh"]))
            r["hits"] = it["hits"]
            checked = dict(it, text=it["plain"]) if r.get("note") == "公式改以純文字呈現" else it
            r["problems"] = check(checked, r["zh"])  # 每次都用目前的規則重新檢查
            out[it["id"]] = r
        return out

    def _save(self):
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cache_path)


def split_translation(zh, parts_src):
    """一句話跨兩欄時，把譯文依原文長度比例切回兩段，切點盡量落在標點後。"""
    if len(parts_src) == 1:
        return [zh]
    total = sum(len(p) for p in parts_src)
    out, start = [], 0
    for i, p in enumerate(parts_src[:-1]):
        target = start + round(len(zh) * len(p) / total)
        best = None
        for d in range(0, 25):
            for pos in (target + d, target - d):
                if start < pos < len(zh) and zh[pos - 1] in "，。；：、）」』！？":
                    best = pos
                    break
            if best:
                break
        best = best or min(max(target, start + 1), len(zh) - 1)
        # 不要切在 ⟪n⟫ 或 ⟦n⟧ 標記中間
        for o, c in (("⟪", "⟫"), ("⟦", "⟧")):
            left = zh.rfind(o, start, best)
            if left != -1 and zh.find(c, left) >= best:
                best = zh.find(c, left) + 1
        out.append(zh[start:best])
        start = best
    out.append(zh[start:])
    return out
