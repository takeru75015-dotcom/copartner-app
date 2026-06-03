"""
比較損益推移表PDFから月次データを抽出するサービス。
ファイル名やテキスト内容から「月次推移表」かを判定し、
Claude Vision で月次12ヶ月分を抽出する。
"""
import base64
import re
from typing import Dict, List


# ファイル名に含まれていれば「月次推移表」と判定するキーワード
_MONTHLY_HINT_KEYWORDS = [
    "月次", "推移", "比較損益", "月別", "月次推移", "比較推移", "month",
]


def is_monthly_trend_file(filename: str) -> bool:
    """ファイル名から「月次推移表」かを判定"""
    if not filename:
        return False
    fn = filename.lower()
    fn_orig = filename
    for kw in _MONTHLY_HINT_KEYWORDS:
        if kw in fn or kw in fn_orig:
            return True
    return False


def _guess_fiscal_year_from_filename(filename: str) -> str:
    """ファイル名から会計年度を推定。例: '比較損益推移表_R7.2期_50期.pdf' → '2025年2月期'"""
    if not filename:
        return ""
    # 「R<数字>.<数字>期」パターン（令和X年Y月期）
    m = re.search(r"R\s*(\d{1,2})[\.\s](\d{1,2})\s*期", filename)
    if m:
        year = 2018 + int(m.group(1))
        month = int(m.group(2))
        return f"{year}年{month}月期"
    # 「令和X年Y月期」
    m = re.search(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月期", filename)
    if m:
        year = 2018 + int(m.group(1))
        month = int(m.group(2))
        return f"{year}年{month}月期"
    # 「YYYY年M月期」
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月期", filename)
    if m:
        return f"{int(m.group(1))}年{int(m.group(2))}月期"
    # 「第N期」だけある場合は会計年度判定不能
    return ""


def extract_monthly_from_pdf(pdf_bytes: bytes, filename: str,
                              expected_fy: str = "") -> Dict:
    """比較損益推移表PDFからAI(Vision)で月次12ヶ月分を抽出。
    戻り値: {"fiscal_year": "...", "months": [{month_label, revenue, cost_of_sales, ...}], "error": None}
    """
    if not expected_fy:
        expected_fy = _guess_fiscal_year_from_filename(filename)

    # 決算月をfyから抽出
    close_month = 3
    m = re.search(r"(\d{1,2})\s*月期", expected_fy)
    if m:
        close_month = int(m.group(1))

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    prompt = f"""このPDFは「比較損益推移表」または「月次推移表」で、各月の財務数値が月次列で並んでいます。
12ヶ月分の月次データを JSON 配列で抽出してください。

会計年度: {expected_fy or '不明（PDFから推定）'}
決算月: {close_month}月（つまり {close_month+1 if close_month < 12 else 1}月開始 〜 翌{close_month}月終了の12ヶ月）

【出力JSON】
{{
  "fiscal_year": "{expected_fy}",
  "fiscal_close_month": {close_month},
  "months": [
    {{
      "month_label": "<例：2024年3月、2024年4月、2024年5月…（決算月+1から開始、翌年決算月まで）>",
      "revenue": <月次売上 万円>,
      "cost_of_sales": <月次売上原価 万円>,
      "gross_profit": <月次粗利 万円>,
      "selling_expenses": <月次販管費 万円>,
      "operating_profit": <月次営業利益 万円>
    }}
  ]
}}

【ルール】
- 単位は「万円」に統一（PDFが千円なら÷10、円なら÷10000）
- 月の年指定が重要：会計年度{expected_fy}なら、決算月+1 から開始
  例: 2025年2月期なら → 2024年3月, 2024年4月, ..., 2024年12月, 2025年1月, 2025年2月
- PDFに合計列・前期列・差分列が含まれている場合、月次列のみを抽出
- 月次列が読めない月はスキップ可
- JSON のみ。前置き・コードブロック禁止"""

    try:
        from ..claude_client import _get_claude, _call_claude_with_retry, _parse_json_response, CLAUDE_MODEL
    except Exception as e:
        return {"months": [], "error": f"Claude モジュール読み込み失敗: {e}"}

    try:
        client = _get_claude()
        msg = _call_claude_with_retry(
            client, model=CLAUDE_MODEL, max_tokens=8000,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        text = msg.content[0].text if msg.content else ""
        result = _parse_json_response(text)
        result.setdefault("fiscal_year", expected_fy)
        result.setdefault("months", [])
        result["error"] = None
        return result
    except Exception as e:
        return {"months": [], "fiscal_year": expected_fy, "error": str(e)}
