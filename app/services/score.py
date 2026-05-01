"""
財務健全性スコアの固定計算ロジック。
AI 任せだとブレるので、Python 側で4軸 × 25点 = 100点満点で計算する。
"""
from typing import Dict


def compute_health_score(fd, benchmark: dict = None, ebitda: dict = None,
                          historical_data: list = None) -> Dict:
    """
    4軸スコア合計：
      - 収益性（25点）：粗利率・営業利益率の業界比較
      - 安全性（25点）：自己資本比率・負債比率・流動比率
      - 効率性（25点）：売上債権回転・在庫回転（業種が在庫もつ場合）
      - 成長性（25点）：売上成長率・利益成長率
    """
    revenue = getattr(fd, "revenue", 0) or 0
    cos = getattr(fd, "cost_of_sales", 0) or 0
    op = getattr(fd, "operating_profit", 0) or 0
    gross = getattr(fd, "gross_profit", 0) or 0
    prev_rev = getattr(fd, "prev_revenue", 0) or 0
    prev_op = getattr(fd, "prev_operating_profit", 0) or 0
    cash = getattr(fd, "cash", 0) or 0
    receivables = getattr(fd, "receivables", 0) or 0
    inventory = getattr(fd, "inventory", 0) or 0
    total_assets = getattr(fd, "total_assets", 0) or 0
    total_liab = getattr(fd, "total_liabilities", 0) or 0
    cur_assets = getattr(fd, "current_assets", 0) or 0
    cur_liab = getattr(fd, "current_liabilities", 0) or 0
    equity = getattr(fd, "equity", 0) or 0

    # ベンチマーク参照（業界中央値）
    bm = {}
    if benchmark and benchmark.get("comparisons"):
        for c in benchmark["comparisons"]:
            metric = c.get("metric") or ""
            if "粗利率" in metric:
                bm["gross"] = c.get("median", 0)
            elif "営業利益率" in metric:
                bm["operating"] = c.get("median", 0)

    breakdown = {}

    # ① 収益性（25点）
    score_profit = 0
    if revenue:
        gm_ratio = gross / revenue * 100 if gross else 0
        om_ratio = op / revenue * 100 if op else 0
        bm_g = bm.get("gross", 30)
        bm_o = bm.get("operating", 5)
        # 粗利率：業界中央値で 8点、+10pt超過で 13点
        if gm_ratio >= bm_g + 10: score_profit += 13
        elif gm_ratio >= bm_g: score_profit += 9
        elif gm_ratio >= bm_g * 0.7: score_profit += 5
        elif gm_ratio > 0: score_profit += 2
        # 営業利益率
        if om_ratio >= bm_o + 5: score_profit += 12
        elif om_ratio >= bm_o: score_profit += 9
        elif om_ratio >= 0: score_profit += 4
        # 赤字なら 0
    breakdown["profitability"] = score_profit

    # ② 安全性（25点）
    score_safety = 0
    if total_assets and equity:
        equity_ratio = equity / total_assets * 100
        if equity_ratio >= 50: score_safety += 10
        elif equity_ratio >= 30: score_safety += 7
        elif equity_ratio >= 10: score_safety += 4
        elif equity_ratio > 0: score_safety += 1
    if total_liab and equity > 0:
        debt_eq = total_liab / equity * 100
        if debt_eq <= 50: score_safety += 8
        elif debt_eq <= 100: score_safety += 6
        elif debt_eq <= 200: score_safety += 3
        else: score_safety += 0
    elif equity <= 0:
        # 債務超過 — 安全性壊滅
        score_safety = 0
    if cur_liab and cur_assets:
        cr = cur_assets / cur_liab * 100
        if cr >= 200: score_safety += 7
        elif cr >= 150: score_safety += 5
        elif cr >= 100: score_safety += 3
        else: score_safety += 0
    breakdown["safety"] = min(score_safety, 25)

    # ③ 効率性（25点）— 売上債権・在庫の回転
    score_eff = 0
    has_inventory = inventory > 0
    if revenue and receivables:
        rec_days = receivables / revenue * 365
        if rec_days <= 30: score_eff += (12 if has_inventory else 18)
        elif rec_days <= 45: score_eff += (9 if has_inventory else 14)
        elif rec_days <= 60: score_eff += (6 if has_inventory else 9)
        elif rec_days <= 90: score_eff += (3 if has_inventory else 4)
        else: score_eff += 0
    if has_inventory and cos:
        inv_days = inventory / cos * 365
        if inv_days <= 30: score_eff += 13
        elif inv_days <= 45: score_eff += 10
        elif inv_days <= 60: score_eff += 6
        elif inv_days <= 90: score_eff += 3
        else: score_eff += 0
    elif not has_inventory and not (revenue and receivables):
        # データ不十分なら満点扱いせずデフォルト
        score_eff = 12
    breakdown["efficiency"] = min(score_eff, 25)

    # ④ 成長性（25点）
    score_growth = 0
    if prev_rev and revenue:
        rev_g = (revenue - prev_rev) / abs(prev_rev) * 100
        if rev_g >= 20: score_growth += 13
        elif rev_g >= 10: score_growth += 11
        elif rev_g >= 0: score_growth += 8
        elif rev_g >= -10: score_growth += 4
        else: score_growth += 0
    if prev_op is not None and op is not None and prev_op != 0:
        op_g = (op - prev_op) / abs(prev_op) * 100
        if op_g >= 20: score_growth += 12
        elif op_g >= 0: score_growth += 9
        elif op_g >= -20: score_growth += 4
        else: score_growth += 0
    elif prev_op == 0 and op > 0:
        score_growth += 12  # 黒字転換
    breakdown["growth"] = min(score_growth, 25)

    # ⑤ 数字の動きペナルティ（実態反映）
    penalties = []
    # 売掛膨張：前期比で売上の伸び率より売掛の伸び率が大きく上回る
    prev_data = None
    if historical_data:
        for h in sorted(historical_data, key=lambda x: x.period or ""):
            if h.id == fd.id:
                break
            prev_data = h
    if prev_data:
        if prev_data.revenue and revenue and prev_data.receivables and receivables:
            rev_g = (revenue - prev_data.revenue) / abs(prev_data.revenue) * 100
            rec_g = (receivables - prev_data.receivables) / abs(prev_data.receivables) * 100
            if rec_g - rev_g > 20:
                # 売掛が売上を 20pt 以上上回って伸びてる → 売上の質が悪い
                eff_penalty = min(10, int((rec_g - rev_g) / 5))
                breakdown["efficiency"] = max(0, breakdown["efficiency"] - eff_penalty)
                breakdown["safety"] = max(0, breakdown["safety"] - 3)
                penalties.append(f"売掛+{rec_g:.0f}% vs 売上+{rev_g:.0f}%（乖離+{rec_g-rev_g:.0f}pt） → 効率性 -{eff_penalty}")

        if prev_data.cost_of_sales and cos and prev_data.inventory and inventory:
            cos_g = (cos - prev_data.cost_of_sales) / abs(prev_data.cost_of_sales) * 100
            inv_g = (inventory - prev_data.inventory) / abs(prev_data.inventory) * 100
            if inv_g - cos_g > 20:
                inv_penalty = min(8, int((inv_g - cos_g) / 5))
                breakdown["efficiency"] = max(0, breakdown["efficiency"] - inv_penalty)
                penalties.append(f"在庫+{inv_g:.0f}% vs 原価+{cos_g:.0f}%（乖離+{inv_g-cos_g:.0f}pt） → 効率性 -{inv_penalty}")

        # 営業利益+ なのに現預金が大きく減
        if op > 0 and prev_data.cash and cash:
            cash_g = (cash - prev_data.cash) / abs(prev_data.cash) * 100
            if cash_g < -10:
                breakdown["safety"] = max(0, breakdown["safety"] - 5)
                penalties.append(f"営業黒字なのに現預金{cash_g:.0f}%減 → 安全性 -5")

    # 業界比較ペナルティ
    if bm.get("gross"):
        gm_ratio = gross / revenue * 100 if revenue and gross else 0
        if gm_ratio < bm["gross"] - 10:
            breakdown["profitability"] = max(0, breakdown["profitability"] - 5)
            penalties.append(f"粗利率 {gm_ratio:.1f}% は業界中央値 {bm['gross']:.0f}% より-10pt超 → 収益性 -5")

    # 負債比率ペナルティ
    if total_liab and equity > 0:
        debt_eq = total_liab / equity * 100
        if debt_eq > 200:
            breakdown["safety"] = max(0, breakdown["safety"] - 8)
            penalties.append(f"負債比率 {debt_eq:.0f}% （借金が自己資金の{debt_eq/100:.1f}倍） → 安全性 -8")

    # 売掛・在庫日数の異常ペナルティ（業界水準超）
    if revenue and receivables:
        rec_days = receivables / revenue * 365
        if rec_days > 90:
            breakdown["efficiency"] = max(0, breakdown["efficiency"] - 5)
            penalties.append(f"売掛回収 {rec_days:.0f}日（90日超） → 効率性 -5")
    if inventory and cos:
        inv_days = inventory / cos * 365
        if inv_days > 90:
            breakdown["efficiency"] = max(0, breakdown["efficiency"] - 3)
            penalties.append(f"在庫回転 {inv_days:.0f}日（90日超） → 効率性 -3")

    total = sum(breakdown.values())
    total = max(0, min(100, total))

    # ラベル
    if total >= 80:
        label, color = "優良", "#22c55e"
    elif total >= 60:
        label, color = "良好", "#3b82f6"
    elif total >= 40:
        label, color = "注意", "#f59e0b"
    else:
        label, color = "危険", "#ef4444"

    return {
        "score": total,
        "score_label": label,
        "score_color": color,
        "breakdown": breakdown,
        "penalties": penalties,  # 適用されたペナルティ理由
    }
