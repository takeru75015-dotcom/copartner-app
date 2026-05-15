"""
F-09: アフィリエイトマッチング
  - affiliates.json から自社の課題タグ・業種・売上規模に合致する商品を抽出
  - referral_code をURLに自動付与
  - AI出力の actions / revenue_ideas / cost_ideas / tax_savings_advice に紐付け
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

_DATA_PATH = Path(__file__).parent.parent / "data" / "affiliates.json"
_cache: Optional[List[Dict]] = None


def _load_affiliates() -> List[Dict]:
    global _cache
    if _cache is not None:
        return _cache
    with open(_DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _cache = [a for a in data.get("affiliates", []) if a.get("active", True)]
    return _cache


def _build_url(aff: Dict, referral_code: str = "") -> str:
    base = aff.get("url_base", "")
    return base.replace("{ref}", referral_code or "default")


def _matches(aff: Dict, issue_tags: List[str], revenue: float,
             industry: str, is_profitable: bool) -> tuple[bool, int]:
    """マッチ判定。マッチしたらスコア（0-100）も返す"""
    score = 0

    # 1. trigger_tags が issue_tags に1つでも含まれるか
    aff_tags = set(aff.get("trigger_tags", []))
    user_tags = set(issue_tags or [])
    matched_tags = aff_tags & user_tags
    if not matched_tags:
        return False, 0
    score += min(len(matched_tags) * 20, 50)  # タグ一致は最大50点

    # 2. 売上規模レンジ
    rev_min = aff.get("target_revenue_min", 0)
    rev_max = aff.get("target_revenue_max", 9999999)
    if not (rev_min <= revenue <= rev_max):
        # レンジ外でも完全に弾かず、減点扱い（規模感の参考情報として残す）
        score -= 15
    else:
        score += 25

    # 3. 業種マッチ（"*" は全業種OK）
    target_industry = aff.get("target_industry", "*")
    if target_industry != "*" and industry and target_industry not in industry:
        score -= 10
    else:
        score += 10

    # 4. 黒字専用商品（節税系）は黒字社のみ
    if aff.get("tax_save_relevant") and not is_profitable:
        return False, 0  # 赤字社に節税商品出さない

    # 5. trust_score を加算（最大15点）
    score += int(aff.get("trust_score", 3)) * 3

    return score >= 30, max(0, min(score, 100))


def match_affiliates_for_issue(issue_tags: List[str], fd, industry: str = "",
                                referral_code: str = "", limit: int = 3) -> List[Dict]:
    """
    課題タグに合致するアフィ商品を抽出。

    Args:
        issue_tags: 課題タグのリスト（例: ["経費仕訳工数大", "黒字社"]）
        fd: FinancialData （revenue, operating_profit, net_profit を参照）
        industry: 業種テキスト
        referral_code: 紹介税理士のID（URLに埋め込み）
        limit: 最大返却件数

    Returns:
        [
          {
            "id": "freee_corp",
            "name": "freee 法人プラン",
            "category": "会計SaaS",
            "match_score": 75,
            "match_reasons": ["経費仕訳工数大 にマッチ"],
            "description": "...",
            "url": "https://...?ref=tax_001",
            "vendor": "freee株式会社"
          }
        ]
    """
    revenue = getattr(fd, "revenue", 0) or 0
    operating_profit = getattr(fd, "operating_profit", 0) or 0
    net_profit = getattr(fd, "net_profit", 0) or 0
    is_profitable = operating_profit > 0 and net_profit > 0

    affiliates = _load_affiliates()
    candidates = []
    for aff in affiliates:
        ok, score = _matches(aff, issue_tags, revenue, industry, is_profitable)
        if not ok:
            continue
        # マッチ理由
        matched = set(aff.get("trigger_tags", [])) & set(issue_tags or [])
        candidates.append({
            "id": aff["id"],
            "name": aff["name"],
            "vendor": aff.get("vendor", ""),
            "category": aff["category"],
            "match_score": score,
            "match_reasons": [f"{t} にマッチ" for t in list(matched)[:3]],
            "description": aff.get("description", ""),
            "url": _build_url(aff, referral_code),
            "tax_save_relevant": aff.get("tax_save_relevant", False),
        })

    # スコア降順
    candidates.sort(key=lambda x: x["match_score"], reverse=True)
    return candidates[:limit]


def derive_issue_tags_from_result(result: Dict, fd) -> List[str]:
    """
    AI 結果と財務データから、アフィマッチ用の issue_tags を導出。
    （AI が明示的に出さなくても、財務状況から推定）
    """
    tags = []

    revenue = getattr(fd, "revenue", 0) or 0
    operating_profit = getattr(fd, "operating_profit", 0) or 0
    net_profit = getattr(fd, "net_profit", 0) or 0
    employees = getattr(fd, "employees", 0) or 0
    receivables = getattr(fd, "receivables", 0) or 0
    selling_expenses = getattr(fd, "selling_expenses", 0) or 0

    # 黒字判定
    if operating_profit > 0 and net_profit > 0:
        tags.append("黒字社")
        tags.append("節税")
        if operating_profit > 1000:
            tags.append("個人住民税控除")
            tags.append("役員退職金準備")
            tags.append("法人税軽減")  # 企業版ふるさと納税等
            tags.append("CSR")

    # 売掛金多い → 支払サイト改善・キャッシュフロー
    if revenue > 0 and receivables / revenue * 365 > 60:
        tags.append("支払サイト改善")
        tags.append("キャッシュフロー改善")

    # 経費仕訳工数（推定：販管費明細が薄い、または売上規模に対して人件費が多い）
    breakdown = getattr(fd, "breakdown", None)
    has_thin_breakdown = (
        not breakdown or
        (isinstance(breakdown, dict) and
         len(breakdown.get("selling_expenses_detail", [])) < 5)
    )
    if has_thin_breakdown and revenue > 3000:
        tags.append("経費仕訳工数大")
        tags.append("法人カード未導入")

    # 福利厚生 / 離職対策
    if employees >= 10 and operating_profit > 500:
        tags.append("福利厚生未整備")
    if employees >= 30:
        tags.append("離職率高")
        tags.append("採用強化")

    # 成長機会（営業利益率が業界中央値超 → 採用拡大の余地）
    bench = result.get("benchmark", {}) if isinstance(result, dict) else {}
    if operating_profit > 0 and bench.get("overall_position") in ("top25", "above_median"):
        tags.append("成長機会")
        tags.append("採用強化")

    # 高所得社長（売上1億超＋黒字 → アメックス系・経営幹部採用）
    if revenue > 10000 and operating_profit > 1000:
        tags.append("高所得社長")
        if employees >= 20:
            tags.append("経営幹部採用")

    # 黒字社で設備投資余力 → 太陽光・経営強化税制
    if operating_profit > 1000 and revenue > 10000:
        tags.append("設備投資")
        tags.append("中小企業経営強化税制")

    # BCP（事業継続）視点：黒字＋従業員多い → 太陽光の災害時電源価値
    if operating_profit > 500 and employees >= 10:
        tags.append("BCP")

    # 補助金活用フラグ：成長機会タグがあれば補助金代行へ送れる
    if "成長機会" in tags or "採用強化" in tags:
        tags.append("補助金活用")
        tags.append("DX投資")

    # 法務未整備推察：従業員10人以上で契約書整備需要
    if employees >= 10:
        tags.append("法務未整備")
        tags.append("契約書整備")

    # 業務委託・BPO 候補：人件費が業界平均比で高い時
    bench = result.get("benchmark", {}) if isinstance(result, dict) else {}
    if bench.get("overall_position") in ("below_median", "bottom") and employees >= 5:
        tags.append("属人化解消")

    # 営業代行・Webマーケ候補：新規開拓ニーズ＋営業体制が弱いと推察される場合
    # - 売上が横ばい/減少 or 顧客集中度高い → 新規開拓必要
    # - 売上規模に対して営業人数が見えない（人件費明細から推測困難）→ 営業体制弱い可能性
    issues_list = result.get("issues", []) if isinstance(result, dict) else []
    issues_text = " ".join(issues_list) if isinstance(issues_list, list) else ""
    if any(kw in issues_text for kw in ["顧客集中", "依存", "横ばい", "減少", "新規開拓", "営業"]):
        tags.append("新規開拓")
        tags.append("売上向上")
        tags.append("営業体制弱い")
        tags.append("顧客分散")
        # B2B寄り判定（業種に建設・卸・製造・コンサル等）
        if any(kw in (industry_text or "") for kw in ["建設", "卸", "製造", "コンサル", "調査", "測量", "IT", "ソフトウェア", "サービス"]) \
           if 'industry_text' in dir() else False:
            tags.append("B2B")
    # 単純に建設・調査・専門サービス業ならB2B強制
    # （argsで industry が来てないので呼び出し側で結合する設計だが、ここでは結果のbenchmark.industry_nameを使う）
    ind_name = bench.get("industry_name", "") if isinstance(bench, dict) else ""
    if any(kw in ind_name for kw in ["建設", "卸", "製造", "コンサル", "サービス業", "情報通信"]):
        tags.append("B2B")

    # Web集客弱い：売上規模に対してマーケティング費が少ない（販管費明細で「広告宣伝費」が薄い）
    breakdown = getattr(fd, "breakdown_dict", None) or {}
    sed = breakdown.get("selling_expenses_detail", []) if isinstance(breakdown, dict) else []
    ad_total = 0
    for item in sed if isinstance(sed, list) else []:
        name = str(item.get("name", "") if isinstance(item, dict) else "")
        if "広告" in name or "宣伝" in name or "マーケ" in name or "販促" in name:
            try:
                ad_total += float(item.get("amount", 0))
            except Exception:
                pass
    if revenue > 5000 and ad_total < revenue * 0.005:  # 売上の0.5%未満なら Web 集客弱い
        tags.append("Web集客弱い")

    return list(dict.fromkeys(tags))  # 重複除去（順序保持）


def attach_affiliates_to_result(result: Dict, fd, industry: str = "",
                                  referral_code: str = "") -> Dict:
    """
    AI 分析結果にアフィ推薦を埋め込んで返す。
    結果に result["recommended_affiliates"] と
    各 actions/revenue_ideas/cost_ideas/tax_savings_advice に products を追加。
    """
    issue_tags = derive_issue_tags_from_result(result, fd)
    result["affiliate_issue_tags"] = issue_tags

    # 全体推薦（5件まで）
    result["recommended_affiliates"] = match_affiliates_for_issue(
        issue_tags, fd, industry, referral_code, limit=5
    )

    return result
