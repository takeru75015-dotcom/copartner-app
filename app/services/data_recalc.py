"""
breakdown_json の内訳から、在庫・売掛・現金などの主要数字を再計算する。
PDF/Excel 抽出時に「inventory に貯蔵品が入ってない」「売掛に受取手形が入ってない」等の
取り漏れを修正する。
"""
import json
from typing import Dict, Tuple


# 各カテゴリのキーワード（部分一致）
INVENTORY_KEYS = ["商品", "製品", "原材料", "仕掛品", "貯蔵品", "半製品", "未成工事支出金", "棚卸資産"]
RECEIVABLES_KEYS = ["売掛金", "受取手形", "電子記録債権", "売上債権"]
CASH_KEYS = ["現金", "預金", "現金及び預金", "現金預金"]
INTEREST_DEBT_KEYS = ["短期借入金", "長期借入金", "1年内返済", "1年以内返済", "社債", "リース債務"]


def _sum_by_keys(detail: Dict, keys: list) -> Tuple[float, list]:
    """detail dict から keys に部分一致する項目の合計と内訳"""
    total = 0
    matched = []
    for k, v in (detail or {}).items():
        if k.startswith("__") or not isinstance(v, (int, float)):
            continue
        for kw in keys:
            if kw in k:
                total += v
                matched.append((k, v))
                break
    return total, matched


def recalc_from_breakdown(fd) -> Dict:
    """
    fd.breakdown_json から在庫・売掛・現金を再計算し、
    既存の値より大きければ上書き候補として返す。実際の更新は呼び出し側で。
    """
    try:
        bd = json.loads(fd.breakdown_json or "{}")
    except Exception:
        return {"changed": False, "details": []}

    ca = bd.get("current_assets_detail") or {}
    cl = bd.get("current_liabilities_detail") or {}
    fl = bd.get("fixed_liabilities_detail") or {}

    changes = {}
    details = []

    # 在庫
    inv_total, inv_items = _sum_by_keys(ca, INVENTORY_KEYS)
    cur_inv = getattr(fd, "inventory", 0) or 0
    if inv_total > 0 and abs(inv_total - cur_inv) > max(cur_inv * 0.05, 10):
        changes["inventory"] = inv_total
        details.append(f"在庫: {cur_inv:,.0f}万 → {inv_total:,.0f}万（{', '.join(f'{k}:{v:,.0f}' for k,v in inv_items)}）")

    # 売掛金
    rec_total, rec_items = _sum_by_keys(ca, RECEIVABLES_KEYS)
    cur_rec = getattr(fd, "receivables", 0) or 0
    if rec_total > 0 and abs(rec_total - cur_rec) > max(cur_rec * 0.05, 10):
        changes["receivables"] = rec_total
        details.append(f"売掛: {cur_rec:,.0f}万 → {rec_total:,.0f}万")

    # 現金
    cash_total, _ = _sum_by_keys(ca, CASH_KEYS)
    cur_cash = getattr(fd, "cash", 0) or 0
    if cash_total > 0 and abs(cash_total - cur_cash) > max(cur_cash * 0.05, 10):
        changes["cash"] = cash_total
        details.append(f"現金: {cur_cash:,.0f}万 → {cash_total:,.0f}万")

    # 有利子負債（流動＋固定）
    debt_cur, _ = _sum_by_keys(cl, INTEREST_DEBT_KEYS)
    debt_fix, _ = _sum_by_keys(fl, INTEREST_DEBT_KEYS)
    debt_total = debt_cur + debt_fix
    cur_debt = getattr(fd, "interest_bearing_debt", 0) or 0
    if debt_total > 0 and abs(debt_total - cur_debt) > max(cur_debt * 0.05, 10):
        changes["interest_bearing_debt"] = debt_total
        details.append(f"有利子負債: {cur_debt:,.0f}万 → {debt_total:,.0f}万")

    return {"changed": len(changes) > 0, "changes": changes, "details": details}


def apply_recalc(fd) -> Dict:
    """fd に対して再計算結果を実際に適用する"""
    result = recalc_from_breakdown(fd)
    for attr, val in (result.get("changes") or {}).items():
        setattr(fd, attr, val)
    return result
