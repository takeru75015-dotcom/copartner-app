"""
補助金・助成金マッチングサービス
分析結果（業種・規模・課題タグ）から適合補助金を推薦する。
"""
import json
from pathlib import Path
from typing import Dict, List

_DATA_PATH = Path(__file__).parent.parent / "data" / "subsidies.json"
_subsidies_cache = None


def _load() -> List[Dict]:
    global _subsidies_cache
    if _subsidies_cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _subsidies_cache = json.load(f)
    return _subsidies_cache


def _extract_employees_from_text(text: str) -> int:
    """事業構成テキストから従業員数を正規表現で抽出"""
    if not text:
        return 0
    import re
    # 「従業員150人」「社員約30名」「スタッフ20名」「正社員XX」
    patterns = [
        r"従業員[数約]*\s*([0-9,]+)\s*[人名]",
        r"社員[数約]*\s*([0-9,]+)\s*[人名]",
        r"スタッフ\s*([0-9,]+)\s*[人名]",
        r"正社員\s*([0-9,]+)\s*[人名]",
        r"([0-9,]+)\s*人体制",
        r"([0-9,]+)\s*[名人]の従業員",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return 0


def _estimate_employees(fd, breakdown: dict, industry: str = "", business_details: str = "") -> int:
    """従業員数を取得（優先順位：DB > 事業構成テキスト > 人件費から推定）"""
    db_emp = getattr(fd, "employees", 0) or 0
    if db_emp > 0:
        return db_emp

    # 事業構成テキストから抽出
    text_emp = _extract_employees_from_text(business_details)
    if text_emp > 0:
        return text_emp

    if not breakdown:
        return 0

    se = breakdown.get("selling_expenses_detail") or {}
    cos = breakdown.get("cost_of_sales_detail") or {}

    # 給料・賞与系（社員給与）
    salary = 0
    for k, v in {**se, **cos}.items():
        if not isinstance(v, (int, float)):
            continue
        if any(lk in k for lk in ["給料手当", "給与手当", "給料", "雑給", "賞与"]):
            salary += v

    if salary <= 0:
        return 0

    # 業種別の平均年収（万円）
    industry_avg = {
        "IT": 600, "ソフトウェア": 600, "情報": 580, "サービス": 500,
        "製造": 450, "金属": 450, "機械": 480,
        "卸売": 480, "卸": 480, "商社": 600,
        "小売": 380, "飲食": 320,
        "建設": 480, "工事": 460,
        "運輸": 420, "物流": 420,
        "不動産": 550,
        "医療": 460, "福祉": 360,
    }
    avg_salary_man = 450  # デフォルト
    for keyword, val in industry_avg.items():
        if keyword in (industry or ""):
            avg_salary_man = val
            break

    estimated = int(salary / avg_salary_man)
    return max(estimated, 1)


def _extract_issue_tags(fd, breakdown: dict, result: dict, business_details: str = "") -> List[str]:
    """財務データ・分析結果から課題タグを抽出"""
    tags = set()

    revenue = getattr(fd, "revenue", 0) or 0
    op = getattr(fd, "operating_profit", 0) or 0
    cos = getattr(fd, "cost_of_sales", 0) or 0
    receivables = getattr(fd, "receivables", 0) or 0
    inventory = getattr(fd, "inventory", 0) or 0
    employees = getattr(fd, "employees", 0) or 0

    # 売上減少
    growth_rates = (result or {}).get("growth_rates") or {}
    if growth_rates.get("revenue") is not None and growth_rates["revenue"] < -5:
        tags.add("売上減少")

    # 粗利率低下（業界比較で判定）
    benchmark = (result or {}).get("benchmark") or {}
    for c in benchmark.get("comparisons", []):
        metric = c.get("metric", "")
        if "粗利率" in metric and c.get("rank") in ("below_median", "bottom"):
            tags.add("粗利率低下")
        if "営業利益率" in metric and c.get("rank") == "bottom":
            tags.add("収益性低下")

    # 売掛・在庫滞留
    if revenue and receivables and (receivables / revenue * 365) > 60:
        tags.add("売掛滞留")
    if cos and inventory and (inventory / cos * 365) > 60:
        tags.add("在庫滞留")

    # 人件費比率（販管費内訳から）
    if breakdown:
        se_detail = breakdown.get("selling_expenses_detail") or {}
        labor_keys = ["給料手当", "役員報酬", "人件費", "賞与", "法定福利費"]
        labor_total = sum(
            v for k, v in se_detail.items()
            if isinstance(v, (int, float)) and any(lk in k for lk in labor_keys)
        )
        if revenue and labor_total / revenue > 0.30:
            tags.add("人件費")
            tags.add("人件費比率高")

    # 業務効率系（一般的な課題）
    tags.add("業務効率化")
    tags.add("販路開拓")

    # 賃上げ要件（規模で判定）
    if employees and 5 <= employees <= 100:
        tags.add("賃上げ")

    # 事業承継キーワード（business_details から）
    if business_details and any(k in business_details for k in ["承継", "後継", "相続", "売却", "M&A", "退任"]):
        tags.add("事業承継")

    # EC・DX キーワード
    if business_details and any(k in business_details for k in ["EC", "ネット販売", "オンライン", "DX", "デジタル"]):
        tags.add("EC強化")
        tags.add("DX")

    return list(tags)


def _match_score(subsidy: Dict, fd, employees: int, issue_tags: List[str], business_details: str) -> float:
    """補助金との適合スコア（0-100）"""
    score = 0.0

    # 規模条件
    target_size = subsidy.get("target_size") or {}
    max_emp = target_size.get("max_employees", 9999)
    if employees and employees > max_emp:
        return 0  # 規模超過

    # 業種マッチング
    targets = subsidy.get("target_industries", [])
    if "全業種" in targets:
        score += 20
    # それ以外の業種マッチは business_details に依存（簡易判定）

    # キーワードマッチング（business_details）
    keywords = subsidy.get("match_keywords", [])
    for kw in keywords:
        if business_details and kw in business_details:
            score += 8

    # 課題タグマッチング（最重要）
    sub_tags = set(subsidy.get("issue_tags", []))
    matched = sub_tags & set(issue_tags)
    score += len(matched) * 15

    # 上限
    return min(score, 100)


def match_subsidies(fd, breakdown: dict = None, result: dict = None,
                    business_details: str = "", industry: str = "",
                    limit: int = 8) -> List[Dict]:
    """
    財務データと分析結果から、適合補助金リストを返す。
    各補助金には match_reason（適合理由）を付ける。
    """
    subsidies = _load()
    db_emp = getattr(fd, "employees", 0) or 0
    text_emp = _extract_employees_from_text(business_details)
    estimated_emp = _estimate_employees(fd, breakdown or {}, industry, business_details)
    employees = db_emp if db_emp > 0 else (text_emp if text_emp > 0 else estimated_emp)
    issue_tags = _extract_issue_tags(fd, breakdown or {}, result or {}, business_details)

    matched = []
    for s in subsidies:
        score = _match_score(s, fd, employees, issue_tags, business_details)
        if score <= 0:
            continue
        # 適合理由
        reasons = []
        sub_tags = set(s.get("issue_tags", []))
        common = sub_tags & set(issue_tags)
        if common:
            reasons.append(f"課題に該当: {' / '.join(list(common)[:3])}")
        keywords = s.get("match_keywords", [])
        kw_match = [kw for kw in keywords if business_details and kw in business_details]
        if kw_match:
            reasons.append(f"事業構成に該当: {' / '.join(kw_match[:3])}")
        if not reasons:
            reasons.append("業種・規模条件で適合")

        matched.append({
            **s,
            "match_score": round(score, 1),
            "match_reasons": reasons,
        })

    matched.sort(key=lambda x: -x["match_score"])
    if db_emp > 0:
        emp_source = "DB登録値"
    elif text_emp > 0:
        emp_source = "事業構成テキストから抽出"
    elif estimated_emp > 0:
        emp_source = "人件費から推定"
    else:
        emp_source = "不明（要入力）"

    return {
        "issue_tags": issue_tags,
        "items": matched[:limit],
        "employees_used": employees,
        "employees_source": emp_source,
    }
