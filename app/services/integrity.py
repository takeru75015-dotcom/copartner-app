"""
財務データの整合性チェック。
抽出後に「数字が合ってるか」を機械的に検証する。
"""
import json
from typing import Dict, List


def check_integrity(fd) -> List[Dict]:
    """
    財務データの整合性をチェックし、違和感がある項目を warning リストで返す。
    各 warning は { severity, message, suggestion } の dict。
    """
    warnings = []

    rev = getattr(fd, "revenue", 0) or 0
    cos = getattr(fd, "cost_of_sales", 0) or 0
    gp = getattr(fd, "gross_profit", 0) or 0
    se = getattr(fd, "selling_expenses", 0) or 0
    op = getattr(fd, "operating_profit", 0) or 0
    op2 = getattr(fd, "ordinary_profit", 0) or 0
    np_ = getattr(fd, "net_profit", 0) or 0

    ta = getattr(fd, "total_assets", 0) or 0
    ca = getattr(fd, "current_assets", 0) or 0
    cash = getattr(fd, "cash", 0) or 0
    rec = getattr(fd, "receivables", 0) or 0
    inv = getattr(fd, "inventory", 0) or 0
    tl = getattr(fd, "total_liabilities", 0) or 0
    cl = getattr(fd, "current_liabilities", 0) or 0
    eq = getattr(fd, "equity", 0) or 0

    # ========== P/L 整合性 ==========
    # 売上 - 原価 = 粗利（5%以内の差は許容）
    if rev > 0 and cos > 0 and gp > 0:
        expected_gp = rev - cos
        gap = abs(expected_gp - gp)
        if gap > max(rev * 0.05, 100):
            warnings.append({
                "severity": "high",
                "category": "PL整合性",
                "message": f"粗利が一致しない：売上 {rev:,.0f} − 原価 {cos:,.0f} = {expected_gp:,.0f} だが、登録値 {gp:,.0f}（差 {gap:,.0f}万）",
                "suggestion": "売上・原価・粗利のいずれかに抽出ミスの可能性。決算書原本で確認を推奨。",
            })

    # 粗利 - 販管費 = 営業利益
    if gp > 0 and se > 0:
        expected_op = gp - se
        gap = abs(expected_op - op)
        if gap > max(rev * 0.05, 100):
            warnings.append({
                "severity": "medium",
                "category": "PL整合性",
                "message": f"営業利益が一致しない：粗利 {gp:,.0f} − 販管費 {se:,.0f} = {expected_op:,.0f} だが、登録値 {op:,.0f}（差 {gap:,.0f}万）",
                "suggestion": "販管費 or 営業利益の抽出ミスの可能性。",
            })

    # ========== B/S 貸借バランス ==========
    if ta > 0 and tl > 0 and eq:
        expected_ta = tl + eq
        gap = abs(expected_ta - ta)
        if gap > max(ta * 0.05, 100):
            warnings.append({
                "severity": "high",
                "category": "B/S貸借",
                "message": f"貸借が合わない：負債 {tl:,.0f} + 純資産 {eq:,.0f} = {expected_ta:,.0f} だが、総資産 {ta:,.0f}（差 {gap:,.0f}万）",
                "suggestion": "総資産・負債合計・純資産のいずれかに抽出ミス。決算書BSで確認。",
            })

    # ========== 流動資産の内訳 vs 合計 ==========
    try:
        bd = json.loads(fd.breakdown_json or "{}")
    except Exception:
        bd = {}

    cad = bd.get("current_assets_detail") or {}
    cad_sum = sum(v for k, v in cad.items() if not k.startswith("__") and isinstance(v, (int, float)))
    if ca > 0 and cad_sum > 0:
        gap = abs(ca - cad_sum)
        if gap > max(ca * 0.05, 100):
            warnings.append({
                "severity": "medium",
                "category": "流動資産整合性",
                "message": f"流動資産の合計と内訳が合わない：内訳合計 {cad_sum:,.0f} vs 登録合計 {ca:,.0f}（差 {gap:,.0f}万）",
                "suggestion": "未分類項目の可能性 or 一部抽出漏れ。",
            })

    # ========== 個別項目の整合性（在庫・売掛） ==========
    if cad:
        # 棚卸資産候補
        inv_keys = ["商品", "製品", "原材料", "仕掛品", "貯蔵品", "半製品"]
        inv_breakdown_sum = sum(v for k, v in cad.items() if isinstance(v, (int, float)) and any(ik in k for ik in inv_keys))
        if inv > 0 and inv_breakdown_sum > 0:
            gap = abs(inv_breakdown_sum - inv)
            if gap > max(inv * 0.10, 50):
                warnings.append({
                    "severity": "medium",
                    "category": "在庫の合算ミス",
                    "message": f"棚卸資産 {inv:,.0f}万 と内訳合算 {inv_breakdown_sum:,.0f}万 にズレ。商品・製品・原材料・仕掛品・貯蔵品 を全部足してない可能性",
                    "suggestion": "「内訳から再計算」ボタンで自動修正できます。",
                })

        # 売掛金候補
        rec_keys = ["売掛金", "受取手形", "電子記録債権"]
        rec_breakdown_sum = sum(v for k, v in cad.items() if isinstance(v, (int, float)) and any(rk in k for rk in rec_keys))
        if rec > 0 and rec_breakdown_sum > 0:
            gap = abs(rec_breakdown_sum - rec)
            if gap > max(rec * 0.10, 50):
                warnings.append({
                    "severity": "medium",
                    "category": "売掛金の合算ミス",
                    "message": f"売掛金 {rec:,.0f}万 と内訳合算 {rec_breakdown_sum:,.0f}万 にズレ。売掛金+受取手形+電子記録債権 を全部足してない可能性",
                    "suggestion": "「内訳から再計算」ボタンで自動修正できます。",
                })

    # ========== 業種知識ベースの妥当性 ==========
    # 売上規模に対する粗利率の妥当性（極端値）
    if rev > 0 and gp > 0:
        gm_ratio = gp / rev * 100
        if gm_ratio > 95:
            warnings.append({
                "severity": "low",
                "category": "粗利率異常",
                "message": f"粗利率が {gm_ratio:.1f}% と異常に高い。原価が抽出できていない可能性。",
                "suggestion": "サービス業なら正常。卸・小売・製造業なら原価明細を確認。",
            })
        elif gm_ratio < 5 and rev > 1000:
            warnings.append({
                "severity": "low",
                "category": "粗利率異常",
                "message": f"粗利率が {gm_ratio:.1f}% と異常に低い。原価の数字が大きすぎる可能性。",
                "suggestion": "原価の単位（千円/万円/百万円）を確認。",
            })

    # 売掛日数 90日超
    if rev > 0 and rec > 0:
        rec_days = rec / rev * 365
        if rec_days > 180:
            warnings.append({
                "severity": "low",
                "category": "売掛異常",
                "message": f"売掛回収期間が {rec_days:.0f}日（{rec_days/30:.1f}ヶ月）と異常に長い。",
                "suggestion": "売掛金の単位ミス or 売上が小さすぎる可能性。",
            })

    return warnings
