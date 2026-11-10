"""華美辭典載入與比對。

辭典檔：
  full_*.csv  欄位 english,chinese          （一般詞條）
  abbr_*.csv  欄位 abbr,expansion,chinese   （縮寫）

比對分三類：
  phrase  多字片語（含縮寫的全名），強制套用，翻完會檢查
  word    單字詞條，常有多種意思，提供給模型依語境選用
  abbr    縮寫，常有多種意思，只當候選，與上下文不符時忽略
"""
import csv
import glob
import os
import re

import ahocorasick
from wordfreq import zipf_frequency

# 單字詞頻高於此值視為常用字，不套辭典（例如 can、about、control）
WORD_ZIPF_MAX = 4.3
# 片語中每個字都高於此值時略過（例如 will be）
PHRASE_ZIPF_ALL = 5.5


def _common(word, limit):
    return zipf_frequency(word, "en") >= limit

_SPLIT = re.compile(r"[，,；;]")


def clean_zh(s):
    s = re.sub(r"\s+", "", s.strip())
    return s.replace("（", "(").replace("）", ")")


def senses(zh):
    """把「部隊，兵力，力」拆成多個意思。"""
    return [x for x in (clean_zh(p) for p in _SPLIT.split(zh)) if x]


def variants(sense):
    """「彈藥(艙)庫」可接受：彈藥(艙)庫、彈藥艙庫、彈藥庫。"""
    v = {sense, sense.replace("(", "").replace(")", ""), re.sub(r"\([^)]*\)", "", sense)}
    return {x for x in v if x}


class Glossary:
    def __init__(self, folder):
        self.entries = {}  # 小寫英文 -> {"en": 原寫法, "zh": [意思...]}
        self.abbr = {}  # 縮寫 -> [(全名, 中文)]
        for f in sorted(glob.glob(os.path.join(folder, "full_*.csv"))):
            with open(f, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    self._add(r.get("english", ""), r.get("chinese", ""))
        for f in sorted(glob.glob(os.path.join(folder, "abbr_*.csv"))):
            with open(f, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    a, exp, zh = (r.get(k, "").strip() for k in ("abbr", "expansion", "chinese"))
                    if not a or not zh:
                        continue
                    self.abbr.setdefault(a, []).append((exp, clean_zh(zh)))
                    if exp:
                        self._add(exp, zh)

        self._ac = ahocorasick.Automaton()
        for k in self.entries:
            self._ac.add_word(k, k)
        self._ac.make_automaton()
        self._ac_abbr = ahocorasick.Automaton()
        for a in self.abbr:
            # 只收含大寫、至少兩個字元的縮寫，避免 "a"、"in" 之類誤判
            if len(a) >= 2 and any(c.isupper() for c in a):
                self._ac_abbr.add_word(a, a)
        self._ac_abbr.make_automaton()

    def _add(self, en, zh):
        en = re.sub(r"\s+", " ", en.strip())
        if not en or not zh.strip() or len(en) < 2:
            return
        e = self.entries.setdefault(en.lower(), {"en": en, "zh": []})
        for s in senses(zh):
            if s not in e["zh"]:
                e["zh"].append(s)

    @staticmethod
    def _boundary_ok(text, start, end):
        before = text[start - 1] if start > 0 else " "
        after = text[end] if end < len(text) else " "
        return not before.isalnum() and not after.isalnum()

    def match(self, text):
        """回傳文字中命中的詞條（長詞優先、不重疊）。"""
        low = text.lower()
        cands = []
        for end, key in self._ac.iter(low):
            start = end - len(key) + 1
            e = end + 1
            # 容許複數：missile -> missiles
            for suf in ("", "s", "es"):
                if low[e:e + len(suf)] == suf and self._boundary_ok(low, start, e + len(suf)):
                    kind = "phrase" if " " in key or "-" in key else "word"
                    cands.append((start, e + len(suf), kind, key))
                    break
        for end, a in self._ac_abbr.iter(text):
            start = end - len(a) + 1
            if self._boundary_ok(text, start, end + 1):
                cands.append((start, end + 1, "abbr", a))

        cands.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
        taken, hits = [], []
        for s, e, kind, key in cands:
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            surface = text[s:e]
            if kind == "word" and _common(key, WORD_ZIPF_MAX):
                continue
            if kind == "phrase" and all(_common(w, PHRASE_ZIPF_ALL) for w in re.split(r"[\s-]+", key) if w):
                continue
            if kind == "abbr":
                hits.append({"kind": kind, "src": surface, "options": self.abbr[key][:8]})
            else:
                hits.append({"kind": kind, "src": surface, "en": self.entries[key]["en"],
                             "zh": self.entries[key]["zh"]})
        hits.sort(key=lambda h: text.find(h["src"]))
        return hits


def satisfied(hit, zh_text):
    """譯文是否用了辭典的其中一個譯法。"""
    if hit["kind"] == "abbr":
        opts = [z for _, z in hit["options"]]
        sens = [s for z in opts for s in senses(z)]
    else:
        sens = hit["zh"]
    return any(v in zh_text for s in sens for v in variants(s))


_DEF_RE = re.compile(
    r"((?:[A-Z][\w’'\-]*|of|and|the|for|on|in)(?:\s+(?:[A-Z][\w’'\-]*|of|and|the|for|on|in)){0,9})"
    r"[”\"]?\s*\(([A-Z][A-Za-z&]{1,9})\)")


def doc_abbreviations(text):
    """找出文中「全名 (縮寫)」的定義，例如 Overall Defense Concept (ODC)。"""
    found = {}
    for m in _DEF_RE.finditer(text):
        words, ab = m.group(1).split(), m.group(2)
        letters = [c for c in ab if c.isupper()]
        # 從尾端往前取，直到大寫字首能拼出縮寫
        for k in range(len(words), 0, -1):
            cand = words[-k:]
            caps = "".join(w[0] for w in cand if w[0].isupper())
            every = "".join(w[0] for w in cand).upper()
            if cand[0][0].isupper() and "".join(letters) in (caps, every):
                found.setdefault(ab, " ".join(cand))
                break
    return found
