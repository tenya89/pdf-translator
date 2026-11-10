"""從系統的 Noto CJK .ttc 抽出繁體中文（TC）字型到 translator/fonts/。

Ubuntu / Debian 先安裝：sudo apt install fonts-noto-cjk fonts-noto-cjk-extra
用法：python translator/setup_fonts.py [ttc 所在資料夾] [輸出資料夾]
"""
import os
import sys

from fontTools.ttLib import TTCollection

SRC = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/opentype/noto"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

WANTED = {
    "NotoSansCJK-Regular.ttc": "NotoSansTC-Regular.otf",
    "NotoSansCJK-Bold.ttc": "NotoSansTC-Bold.otf",
    "NotoSerifCJK-Regular.ttc": "NotoSerifTC-Regular.otf",
    "NotoSerifCJK-SemiBold.ttc": "NotoSerifTC-SemiBold.otf",
}

os.makedirs(OUT, exist_ok=True)
for ttc, otf in WANTED.items():
    path = os.path.join(SRC, ttc)
    if not os.path.exists(path):
        sys.exit(f"找不到 {path}，請先安裝 fonts-noto-cjk / fonts-noto-cjk-extra")
    for font in TTCollection(path).fonts:
        family = font["name"].getDebugName(1) or ""
        if family.endswith(" TC") or " TC " in family:
            font.save(os.path.join(OUT, otf))
            print(f"{otf} ← {ttc}（{family}）")
            break
    else:
        sys.exit(f"{ttc} 裡沒有 TC 字型")
