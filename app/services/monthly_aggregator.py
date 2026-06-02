"""
月次データを年度単位で集計する。

ルール：
- PL系（売上・原価・粗利・販管費・営業利益・経常利益・純利益）→ 合計
- B/S系（総資産・現預金・売掛・在庫・負債・自己資本 等）→ 期末値（fiscal_year内で最も新しい月）
- breakdown_json（販管費明細等）→ 合計（各キーごと）

戻り値：合成した「年度集計データ」のリスト。期間文字列は fiscal_year のラベルを使う。
"""
import json
import re
from typing import Dict, List, Optional, Tuple

# PL系（合計対象）
_PL_FIELDS = [
    "revenue", "cost_of_sales", "gross_profit", "selling_expenses",
    "operating_profit", "ordinary_profit", "net_profit",
]
# B/S系（期末値採用）
_BS_FIELDS = [
    "total_assets", "current_assets", "cash", "receivables", "inventory",
    "total_liabilities", "current_liabilities", "interest_bearing_debt", "equity",
    "employees",
]


def _month_index(period: str) -> int:
    """期間文字列から月（1-12）を取り出して年度内ソート用キーに"""
    m = re.search(r"(\d{1,2})\s*月", period or "")
    return int(m.group(1)) if m else 0


def _year_index(period: str) -> int:
    m = re.search(r"(\d{4})", period or "")
    return int(m.group(1)) if m else 0


def aggregate_monthly_to_fiscal_year(monthly_fds: List) -> Optional[Dict]:
    """同じ会計年度に属する月次 FinancialData のリスト → 集計1件 (dict)
    戻り値 None: 集計に失敗（データ不足等）
    """
    if not monthly_fds:
        return None

    # 月順にソート（西暦→月）
    sorted_fds = sorted(monthly_fds, key=lambda f: (_year_index(f.period), _month_index(f.period)))

    # PL系: 合計
    agg = {}
    for f in _PL_FIELDS:
        total = sum((getattr(fd, f, 0) or 0) for fd in sorted_fds)
        agg[f] = total

    # B/S系: 最終月の値を採用
    last_fd = sorted_fds[-1]
    for f in _BS_FIELDS:
        agg[f] = getattr(last_fd, f, 0) or 0

    # breakdown_json: 各キーごとに合計（PL明細）+ 最終月（BS明細）
    pl_detail_keys = ["selling_expenses_detail", "cost_of_sales_detail", "revenue_detail",
                       "non_operating_income", "non_operating_expenses"]
    bs_detail_keys = ["current_assets_detail", "current_liabilities_detail",
                       "fixed_assets_detail", "fixed_liabilities_detail"]
    agg_breakdown = {}

    # PL明細は合計
    for key in pl_detail_keys:
        merged = {}
        for fd in sorted_fds:
            try:
                bd = json.loads(getattr(fd, "breakdown_json", "{}") or "{}")
            except Exception:
                bd = {}
            detail = bd.get(key) or {}
            for k, v in detail.items():
                if isinstance(v, (int, float)):
                    merged[k] = merged.get(k, 0) + v
        if merged:
            agg_breakdown[key] = merged

    # BS明細は最終月
    try:
        last_bd = json.loads(getattr(last_fd, "breakdown_json", "{}") or "{}")
        for key in bs_detail_keys:
            if last_bd.get(key):
                agg_breakdown[key] = last_bd[key]
    except Exception:
        pass

    agg["breakdown_json"] = json.dumps(agg_breakdown, ensure_ascii=False) if agg_breakdown else "{}"

    # 期間ラベル: 最終月の fiscal_year を採用
    fy = getattr(last_fd, "fiscal_year", "") or getattr(last_fd, "period", "")
    agg["period"] = fy
    agg["_aggregated_from_count"] = len(sorted_fds)
    agg["_aggregated_first"] = sorted_fds[0].period
    agg["_aggregated_last"] = sorted_fds[-1].period
    agg["client_id"] = getattr(last_fd, "client_id", None)

    return agg


def group_monthly_by_fiscal_year(monthly_fds: List) -> Dict[str, List]:
    """月次データを fiscal_year ごとにグルーピング"""
    groups = {}
    for fd in monthly_fds:
        fy = getattr(fd, "fiscal_year", "") or ""
        if not fy:
            continue
        groups.setdefault(fy, []).append(fd)
    return groups


def compare_with_annual(monthly_agg: Dict, annual_fd) -> Dict:
    """月次集計と年次データの乖離をチェック（同じ fiscal_year 内）
    戻り値: {"discrepancies": [{"field": "revenue", "monthly": ..., "annual": ..., "pct": ...}], ...}
    """
    if not annual_fd:
        return {"discrepancies": []}
    discrepancies = []
    for f in ["revenue", "cost_of_sales", "selling_expenses", "operating_profit"]:
        m_val = monthly_agg.get(f, 0)
        a_val = getattr(annual_fd, f, 0) or 0
        if m_val == 0 and a_val == 0:
            continue
        diff = m_val - a_val
        pct = (diff / abs(a_val) * 100) if a_val else None
        if pct is not None and abs(pct) > 5:
            discrepancies.append({
                "field": f, "monthly_total": m_val, "annual": a_val,
                "diff": diff, "pct": round(pct, 1),
            })
    return {"discrepancies": discrepancies}


class AggregatedFinancial:
    """月次集計結果を FinancialData っぽく扱うための薄いラッパ"""
    def __init__(self, data: dict):
        self.id = -1  # 仮ID
        self.client_id = data.get("client_id")
        self.period = data.get("period", "")
        self.period_type = "annual_aggregated"  # 「月次から集計した年次」と分かるよう専用ラベル
        self.fiscal_year = data.get("period", "")
        for f in _PL_FIELDS + _BS_FIELDS:
            setattr(self, f, data.get(f, 0))
        self.prev_revenue = 0
        self.prev_operating_profit = 0
        self.breakdown_json = data.get("breakdown_json", "{}")
        self._aggregated_from_count = data.get("_aggregated_from_count", 0)
        self._aggregated_first = data.get("_aggregated_first", "")
        self._aggregated_last = data.get("_aggregated_last", "")


def build_effective_historical_data(all_fds: list) -> Tuple[list, dict]:
    """月次データを年度別に集計し、月次優先で historical_data を再構築する。

    戻り値:
      (effective_fds, meta)
        effective_fds: 分析に使う FinancialData / AggregatedFinancial の混在リスト
                       同じ fiscal_year に月次があれば月次集計を採用、なければ年次そのまま
        meta: {
          "monthly_count": int,
          "annual_count": int,
          "aggregated_count": int,
          "fiscal_years_with_monthly": [fy_label],
          "discrepancies_by_fy": {fy: [...]},
        }
    """
    monthly_fds = [f for f in all_fds if (getattr(f, "period_type", "") == "monthly")]
    annual_fds = [f for f in all_fds if (getattr(f, "period_type", "") != "monthly")]

    monthly_groups = group_monthly_by_fiscal_year(monthly_fds)
    aggregated = []
    fy_with_monthly = []
    discrepancies = {}
    used_annual_fy = set()

    # 月次グループから集計
    for fy, fds in monthly_groups.items():
        if len(fds) < 3:  # 月次が3件未満なら集計せずスキップ
            continue
        agg_data = aggregate_monthly_to_fiscal_year(fds)
        if not agg_data:
            continue
        agg_obj = AggregatedFinancial(agg_data)
        aggregated.append(agg_obj)
        fy_with_monthly.append(fy)
        used_annual_fy.add(fy)
        # 同じ FY の annual と比較
        matching_annual = next((a for a in annual_fds if (getattr(a, "period", "") == fy)), None)
        if matching_annual:
            cmp_result = compare_with_annual(agg_data, matching_annual)
            if cmp_result["discrepancies"]:
                discrepancies[fy] = cmp_result["discrepancies"]

    # 月次集計でカバーされない年次データだけを残す
    remaining_annual = [a for a in annual_fds if getattr(a, "period", "") not in used_annual_fy]

    effective_fds = aggregated + remaining_annual
    # period 順にソート（YYYY 年順）
    effective_fds.sort(key=lambda f: (_year_index(getattr(f, "period", "")), _month_index(getattr(f, "period", ""))))

    meta = {
        "monthly_count": len(monthly_fds),
        "annual_count": len(annual_fds),
        "aggregated_count": len(aggregated),
        "fiscal_years_with_monthly": fy_with_monthly,
        "discrepancies_by_fy": discrepancies,
    }
    return effective_fds, meta
