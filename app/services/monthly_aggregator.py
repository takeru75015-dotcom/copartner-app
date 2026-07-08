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
        # ID 設計:
        # - DB の本物の FinancialData は id>0
        # - 集計値は負の id を使い、複数年度を扱っても **fiscal_year ごとに一意** にする。
        #   こうしないと下流の `h.id == fd.id` 走査（前期検出）が他年度の集計値で
        #   先にマッチし、growth_rates・expense_deltas が誤った期と比較される。
        _fy = data.get("period", "") or ""
        _client = data.get("client_id") or 0
        # 安定ハッシュ（メモリ番地を避ける）。client × fiscal_year でユニーク。
        _key = f"{_client}::{_fy}"
        self.id = -1 - (abs(hash(_key)) % 10_000_000)
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


def aggregate_partial_year(monthly_fds: List) -> Optional[Dict]:
    """期中（n<12）の月次データを「nヶ月累計」として集計する。
    AggregatedFinancial と違い、年率換算しない。÷n は呼び出し側で行う。

    戻り値: aggregate_monthly_to_fiscal_year と同形 + 期中メタ情報
    """
    if not monthly_fds:
        return None
    agg = aggregate_monthly_to_fiscal_year(monthly_fds)
    if not agg:
        return None
    # nヶ月分情報を明示
    agg["_partial_n_months"] = agg.get("_aggregated_from_count", 0)
    agg["_is_partial"] = True
    return agg


def get_yoy_period_data(target_partial_agg: Dict, all_fds: List) -> Optional[Dict]:
    """期中スナップショット（target_partial_agg）の **前年同期間** データを取得し集計。

    例: 当期4-6月の3ヶ月累計 → 前年4-6月の3ヶ月累計を返す。
    戻り値: aggregate_monthly_to_fiscal_year と同形（前年同期間累計）、取れなければ None
    """
    if not target_partial_agg:
        return None
    first_period = target_partial_agg.get("_aggregated_first", "")
    last_period = target_partial_agg.get("_aggregated_last", "")
    if not first_period or not last_period:
        return None

    # 月（1-12）の範囲を抽出
    first_month = _month_index(first_period)
    last_month = _month_index(last_period)
    target_year = _year_index(first_period)
    if first_month == 0 or last_month == 0 or target_year == 0:
        return None

    # 前年同期間の月リスト（年またぎ対応：last < first なら年またぎ）
    prev_year = target_year - 1
    yoy_fds = []
    for f in all_fds:
        if getattr(f, "period_type", "") != "monthly":
            continue
        fp = getattr(f, "period", "") or ""
        fy_year = _year_index(fp)
        fy_month = _month_index(fp)
        if fy_month == 0:
            continue
        # 月の範囲判定（年またぎ無し: first<=last の場合）
        if first_month <= last_month:
            if fy_year == prev_year and first_month <= fy_month <= last_month:
                yoy_fds.append(f)
        else:
            # 年またぎ（例: first=11月、last=2月）
            if (fy_year == prev_year and fy_month >= first_month) or \
               (fy_year == prev_year + 1 and fy_month <= last_month):
                yoy_fds.append(f)

    if not yoy_fds:
        return None
    # ★ 前年同期間の月数が target と「ぴったり一致」しないなら不完全データ扱い（None 返却）
    # 少ない場合（前年データ欠落）: 短い期間との比較で歪む
    # 多い場合（当期データ欠落 or 重複行）: 長い期間との比較で誤った成長率になる
    target_n = target_partial_agg.get("_aggregated_from_count", 0) or len(yoy_fds)
    if target_n > 0 and len(yoy_fds) != target_n:
        return None
    yoy_agg = aggregate_monthly_to_fiscal_year(yoy_fds)
    if yoy_agg:
        yoy_agg["_is_yoy_comparison"] = True
    return yoy_agg


class PartialYearSnapshot:
    """期中スナップショット（n<12 ヶ月累計）の薄いラッパ。AggregatedFinancialと同インターフェース。"""
    def __init__(self, data: dict):
        _fy = data.get("period", "") or ""
        _client = data.get("client_id") or 0
        # nヶ月分も含めてキーをユニークに（同FYでも月数が変わるケースで衝突を防ぐ）
        _n = data.get("_partial_n_months", 0)
        _first = data.get("_aggregated_first", "")
        _last = data.get("_aggregated_last", "")
        _key = f"{_client}::{_fy}::partial::{_n}::{_first}-{_last}"
        self.id = -10_000_000 - (abs(hash(_key)) % 10_000_000)
        self.client_id = data.get("client_id")
        # period 表示は「2026年2月期（期中4ヶ月時点・4-7月累計）」風に
        _period_label = data.get("period", "")
        if _n and _first and _last:
            _period_label = f"{_period_label}（期中{_n}ヶ月時点・{_first}〜{_last}累計）"
        self.period = _period_label
        self.period_type = "partial_aggregated"
        self.fiscal_year = data.get("period", "")
        for f in _PL_FIELDS + _BS_FIELDS:
            setattr(self, f, data.get(f, 0))
        self.prev_revenue = 0
        self.prev_operating_profit = 0
        self.breakdown_json = data.get("breakdown_json", "{}")
        self._aggregated_from_count = data.get("_aggregated_from_count", 0)
        self._aggregated_first = data.get("_aggregated_first", "")
        self._aggregated_last = data.get("_aggregated_last", "")
        self._partial_n_months = data.get("_partial_n_months", 0)
        self._is_partial = True


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
    # 設計方針（Takeru判断 2026-06-07）:
    # - **年次データがあるFYは年次を優先**、月次集計は捨てる（決算書が公式・税理士のスタンス）
    # - 年次データが無いFYのみ月次集計を採用
    #   - n=12 → AggregatedFinancial（年次相当、period_type='annual_aggregated'）
    #   - n<12 → PartialYearSnapshot（期中累計、period_type='partial_aggregated'）
    # - 月次データそのものは monthly_chart_data 用に呼び出し側で別途保持
    partial_count = 0
    # annual.period に fy が含まれるかでマッチング（"2024年2月期" vs "2024年2月期（第49期）"）
    annual_fy_set = set()
    for a in annual_fds:
        a_period = getattr(a, "period", "") or ""
        # fiscal_year 属性があればそれを使う、なければ period 文字列ベース
        a_fy = getattr(a, "fiscal_year", "") or a_period
        annual_fy_set.add(a_fy)
        # 念のため：「2024年2月期」のような prefix も追加（period が「2024年2月期（第49期）」のケース対応）
        import re as _re_local
        _m = _re_local.match(r"(\d{4}年\s*\d{1,2}月期)", a_period)
        if _m:
            annual_fy_set.add(_m.group(1))

    import re as _re_fy_norm
    def _normalize_fy(s: str) -> str:
        """fiscal_year ラベルから '2026年2月期' のような正規形を抽出"""
        if not s:
            return ""
        m = _re_fy_norm.match(r"(\d{4}年\s*\d{1,2}月期)", s)
        return m.group(1) if m else s

    for fy, fds in monthly_groups.items():
        # ★ 年次データがあるFYは月次集計をスキップ（年次を優先）
        # monthly側の fy が「2026年2月期（第51期）」のケースに備えて正規化してから比較
        fy_norm = _normalize_fy(fy)
        if fy in annual_fy_set or fy_norm in annual_fy_set:
            # データ品質チェックのため乖離だけ記録（採用はしない）
            agg_data_for_check = aggregate_monthly_to_fiscal_year(fds)
            if agg_data_for_check:
                n_check = agg_data_for_check.get("_aggregated_from_count", len(fds))
                if n_check >= 12:
                    matching_annual = next((a for a in annual_fds
                                            if (getattr(a, "fiscal_year", "") or getattr(a, "period", "")).startswith(fy.split("（")[0])),
                                            None)
                    if matching_annual:
                        cmp_result = compare_with_annual(agg_data_for_check, matching_annual)
                        if cmp_result["discrepancies"]:
                            discrepancies[fy] = cmp_result["discrepancies"]
            continue  # 月次集計は採用しない

        agg_data = aggregate_monthly_to_fiscal_year(fds)
        if not agg_data:
            continue
        n = agg_data.get("_aggregated_from_count", len(fds))
        if n >= 12:
            agg_obj = AggregatedFinancial(agg_data)
        else:
            # 期中スナップショット
            agg_data["_partial_n_months"] = n
            agg_data["_is_partial"] = True
            agg_obj = PartialYearSnapshot(agg_data)
            partial_count += 1
        aggregated.append(agg_obj)
        fy_with_monthly.append(fy)
        used_annual_fy.add(fy)

    # 月次集計でカバーされない年次データだけを残す
    remaining_annual = [a for a in annual_fds if getattr(a, "period", "") not in used_annual_fy]

    effective_fds = aggregated + remaining_annual
    # period 順にソート（YYYY 年順）
    effective_fds.sort(key=lambda f: (_year_index(getattr(f, "period", "")), _month_index(getattr(f, "period", ""))))

    meta = {
        "monthly_count": len(monthly_fds),
        "annual_count": len(annual_fds),
        "aggregated_count": len(aggregated),
        "partial_count": partial_count,  # 期中スナップショットの数
        "fiscal_years_with_monthly": fy_with_monthly,
        "discrepancies_by_fy": discrepancies,
    }
    return effective_fds, meta
