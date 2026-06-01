"""
クライアントHP自動取得 + AI抽出サービス
URLを与えると、トップページ＋会社概要/事業紹介ページから事業情報を抽出。
取得結果は Client.web_extracted_json (JSON文字列) に保存し、分析時に business_context として使う。
"""
import json
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# 取得を試みる関連ページのパス
_CANDIDATE_PATHS = [
    "/", "/about", "/about/", "/about-us", "/about-us/",
    "/company", "/company/", "/corporate", "/corporate/",
    "/service", "/service/", "/services", "/services/",
    "/business", "/business/", "/profile", "/profile/",
    "/info", "/access",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 CoPartnerBot/1.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en;q=0.8",
}


def _normalize_url(url: str) -> str:
    """URLを正規化（スキーム補完）"""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _fetch_one(url: str, timeout: float = 8.0) -> Optional[str]:
    """1ページ取得。失敗時は None。HTMLを返す"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "").lower()
        if resp.status_code >= 400 or "html" not in ct:
            return None
        return resp.text[:300000]  # 300KB cap
    except Exception:
        return None


def _extract_text(html: str) -> str:
    """HTMLから本文テキストを抽出（script/style除去）"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    # タイトル + 本文
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    text = soup.get_text(separator="\n", strip=True)
    # 空行・重複空白除去
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    body = "\n".join(lines)
    if title:
        return f"【タイトル】{title}\n\n{body}"
    return body


def fetch_website_text(url: str, max_pages: int = 5) -> Dict:
    """指定URLとその関連ページ（about/company/service等）を取得して結合。
    戻り値: {
        "url": "...",
        "pages_fetched": [...],
        "combined_text": "...",  # 最大 30KB 程度
        "error": None or "..."
    }
    """
    base = _normalize_url(url)
    if not base:
        return {"url": "", "pages_fetched": [], "combined_text": "", "error": "URL未指定"}

    parsed = urlparse(base)
    if not parsed.netloc:
        return {"url": base, "pages_fetched": [], "combined_text": "", "error": "URLの形式が不正"}

    origin = f"{parsed.scheme}://{parsed.netloc}"
    pages_to_try = []
    # まずは入力URLそのもの
    pages_to_try.append(base)
    # 関連パスも候補に
    for path in _CANDIDATE_PATHS:
        cand = urljoin(origin + "/", path.lstrip("/"))
        if cand != base and cand not in pages_to_try:
            pages_to_try.append(cand)

    fetched = []
    chunks = []
    for u in pages_to_try:
        if len(fetched) >= max_pages:
            break
        html = _fetch_one(u)
        if not html:
            continue
        text = _extract_text(html)
        if len(text) < 80:  # ほぼ空ページは捨てる
            continue
        fetched.append(u)
        chunks.append(f"━━━ {u} ━━━\n{text[:6000]}")  # 1ページ6KB cap
        time.sleep(0.3)  # 軽いインターバル

    combined = "\n\n".join(chunks)[:30000]  # 全体30KB cap

    if not combined:
        return {"url": base, "pages_fetched": [], "combined_text": "", "error": "ページを取得できませんでした（接続失敗 or 全ページが空）"}

    return {
        "url": base,
        "pages_fetched": fetched,
        "combined_text": combined,
        "error": None,
    }


def extract_business_info_with_ai(combined_text: str, company_name: str = "") -> Dict:
    """取得テキストから AI で事業情報を構造化抽出。
    戻り値: {
        "business_summary": "...",
        "main_services": [...],
        "target_customers": "...",
        "price_range": "...",
        "strengths": [...],
        "regions": [...],
        "company_age": "...",
        "employees": "...",
        "raw_notes": "...",
    }
    失敗時は {"error": "..."}
    """
    if not combined_text:
        return {"error": "テキストが空のためスキップ"}

    try:
        from ..claude_client import _get_claude, _call_claude_with_retry, _parse_json_response, CLAUDE_MODEL
    except Exception as e:
        return {"error": f"Claude モジュール読み込み失敗: {e}"}

    prompt = f"""以下は「{company_name or '対象企業'}」のホームページから取得したテキストです。
税理士が顧問先の事業を理解するために、以下の項目を JSON で抽出してください。

【出力JSON】
{{
  "business_summary": "<2-3文。事業全体の概要>",
  "main_services": ["<提供サービス・商品 上位3-5件>"],
  "target_customers": "<主要な顧客層。BtoB/BtoC、業種、規模>",
  "price_range": "<料金・価格帯。サイトに明記がなければ '不明'>",
  "strengths": ["<強み・特徴 2-4件>"],
  "regions": ["<対応エリア・拠点>"],
  "company_age": "<創業年・社歴。不明なら ''>",
  "employees": "<従業員数。不明なら ''>",
  "key_facts": ["<その他、決算分析に役立つ事実 2-5件>"],
  "raw_notes": "<取得テキストから読み取れた重要な追記事項>"
}}

【ルール】
- JSON のみ出力。前置き・後置き不要
- 推測ではなく、テキストに記載がある事実のみ
- 不明な項目は空文字 '' または空配列 []
- すべて日本語で

【ホームページテキスト】
{combined_text}
"""

    try:
        client = _get_claude()
        msg = _call_claude_with_retry(
            client,
            model=CLAUDE_MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else ""
        return _parse_json_response(text)
    except Exception as e:
        return {"error": f"AI抽出失敗: {e}"}


def extract_from_website(url: str, company_name: str = "") -> Dict:
    """URLから取得+AI抽出を1コールで実行（メインAPI）。
    戻り値: {
        "url": "...",
        "fetched_at": "ISO datetime",
        "pages_fetched": [...],
        "extracted": {...} or {"error": "..."},
    }
    """
    fetched = fetch_website_text(url)
    if fetched.get("error"):
        return {
            "url": fetched["url"],
            "fetched_at": datetime.utcnow().isoformat(),
            "pages_fetched": [],
            "extracted": {"error": fetched["error"]},
        }

    extracted = extract_business_info_with_ai(
        fetched["combined_text"], company_name=company_name
    )
    return {
        "url": fetched["url"],
        "fetched_at": datetime.utcnow().isoformat(),
        "pages_fetched": fetched["pages_fetched"],
        "extracted": extracted,
    }


def format_for_business_context(web_json: dict) -> str:
    """Client.web_extracted_json を analyze_financials の business_context に注入する形式に変換"""
    if not web_json:
        return ""
    try:
        if isinstance(web_json, str):
            web_json = json.loads(web_json)
    except Exception:
        return ""

    ex = (web_json or {}).get("extracted") or {}
    if ex.get("error"):
        return ""

    lines = ["\n【🌐 ホームページから自動取得した事業情報】"]
    if ex.get("business_summary"):
        lines.append(f"事業概要: {ex['business_summary']}")
    if ex.get("main_services"):
        lines.append(f"主要サービス: {' / '.join(ex['main_services'])}")
    if ex.get("target_customers"):
        lines.append(f"主要顧客: {ex['target_customers']}")
    if ex.get("price_range"):
        lines.append(f"料金帯: {ex['price_range']}")
    if ex.get("strengths"):
        lines.append(f"強み: {' / '.join(ex['strengths'])}")
    if ex.get("regions"):
        lines.append(f"対応エリア: {' / '.join(ex['regions'])}")
    if ex.get("company_age"):
        lines.append(f"社歴: {ex['company_age']}")
    if ex.get("employees"):
        lines.append(f"従業員数: {ex['employees']}")
    if ex.get("key_facts"):
        lines.append(f"その他: {' / '.join(ex['key_facts'][:3])}")
    fetched_at = web_json.get("fetched_at", "")
    if fetched_at:
        lines.append(f"（取得日時: {fetched_at[:10]}）")

    return "\n".join(lines) + "\n"
