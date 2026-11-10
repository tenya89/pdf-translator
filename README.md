# PDF 英翻繁中（華美辭典）

通用、全自動的流程：輸入任何有文字層的 PDF，輸出中文 PDF、DOCX 與檢查報告。
翻譯只用 Gemma 4，其餘是程式處理，不需要人工介入、不做 OCR。

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo apt install fonts-noto-cjk fonts-noto-cjk-extra   # 若系統還沒有
.venv/bin/python translator/setup_fonts.py              # 產生 translator/fonts/
cp .env.example .env                                    # 填入 Gemma 端點與金鑰
```

華美辭典 CSV 不在 repo 內，用 `--dict` 指定資料夾。

## 執行

```bash
cd pdf-translator
.venv/bin/python translator/run.py 檔案.pdf                        # 整份
.venv/bin/python translator/run.py 檔案.pdf --pages 7-10 --name ch1  # 指定頁數
```

- `--pages`：頁碼（從 1 起算），例如 `7-10` 或 `7-10,14`；省略則整份
- `--name`：輸出檔名後綴
- `--dict`：華美辭典 CSV 資料夾（省略則讀 `.env` 的 `GLOSSARY_DIR`）
- `--model`：預設 `gemma31-nothink`
- 端點、金鑰、辭典位置讀自 `.env`（`GEMMA_BASE_URL`、`GEMMA_API_KEY`、`GLOSSARY_DIR`）

輸出在 `output/`：`檔名[_名稱][_p頁數]_zh.pdf`、`_zh.docx`、`_report.md`（檢查報告）。
翻譯結果存在 `output/cache.json`，重跑時不會重翻相同段落。

## 頁面種類（自動判斷）

| 種類 | 例子 | PDF 的處理方式 |
|---|---|---|
| 原生文字 | 期刊論文、Word 轉出的 PDF | 刪除英文文字物件後寫入中文；圖片、線條、色塊、背景都不動 |
| 整頁圖片＋文字層 | 掃描後做過 OCR 的報告 | 在圖上用底色蓋掉英文再寫中文；加 `--mode rebuild` 則不保留背景，白紙上重排 |
| 沒有文字層 | 純掃描檔 | 無法翻譯（不做 OCR），原樣輸出並在報告註明 |

## DOCX 的排法

DOCX 不模仿原檔的座標與分欄，而是照閱讀順序排列、套用一致的 Word 樣式，方便編輯：
A4 單欄；標題依字級分三層套「標題 1／2／3」（可產生目錄）；內文新細明體＋Times New Roman 11pt、1.5 倍行距；
「•」開頭的項目用項目符號清單；圖片與獨立公式置中、圖說套「標號」樣式；表格表頭加底色、跨欄表頭合併儲存格；
註腳原文集中在文件最後的「註釋」；參考文獻凸排；頁尾頁碼。原檔外觀請看 PDF。

## 內容規則

| 內容 | 處理 |
|---|---|
| 內文、標題、圖說 | 翻譯，寫回原位置；字型粗細、襯線、顏色、置中照原文 |
| 表格 | 翻譯；DOCX 重建成 Word 表格，表格內照片放回儲存格 |
| 圖片、圖表 | 不翻譯，原圖放回原位置 |
| 頁首、頁碼、註腳、參考文獻、網址、doi、直式浮水印 | 不翻譯，保留原樣 |
| 獨立成行的公式 | 不翻譯，原樣保留（DOCX 以原公式圖片放回） |
| 行內公式、符號 | 簡單的依原字大小與高度重建上下標；分數等複雜結構貼原公式小圖 |
| 英文姓名 | 保留英文，不翻譯 |

讀取時的自動修復（只用 PDF 現成的字元座標，不做 OCR）：
- 濾掉控制字元（例如 TeX 公式字型的大括號被記成 `\x10`）
- OCR 碎片放回原位（re uirement + q → requirement；Stockp + iling → Stockpiling）
- 同一行被拆開的片段接回去；分段改看對齊與行距，不依賴不準的 OCR 字級

不重疊：每段中文只能延伸到下方或右方的空白處，已寫上的中文也算障礙，放不下才縮小字級；
寫完後逐頁檢查重疊，有的話列在報告的「版面」一節。

華美辭典：多字片語強制套用（翻完檢查，沒用上會自動重翻）；不常見的單字給模型參考；
縮寫優先採用文件本身的定義（例如 Overall Defense Concept (ODC)），其次才看辭典。

## 模組

- `extract.py`：判斷頁面種類、讀文字層、組成段落並分類
- `regions.py`：找出圖片與表格區域
- `glossary.py`：辭典載入與比對
- `translate.py`：呼叫 Gemma、檢查、重試、快取
- `render_pdf.py` / `render_docx.py`：輸出
- `run.py`：主程式與檢查報告
