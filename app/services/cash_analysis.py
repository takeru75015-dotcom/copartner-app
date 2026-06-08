"""
キャッシュ分析サービス
  - バーンレート（毎月いくら燃えているか）
  - 運転資本指標（CCC, 売上債権回転率 等）
  - 資金ショート予測（runway）
"""
from typing import Dict


def compute_cf_buckets(fd, breakdown: Dict = None, historical_data: list = None) -> Dict:
    """
    CFを4バケツ（営業CF / 投資CF / 財務CF / フリーCF）で推計する。
    決算書のCF計算書がないため、間接法で推定。

    Returns:
      {
        "operating_cf": <営業CF推定>,
        "investing_cf": <投資CF推定>,
        "financing_cf": <財務CF推定>,
        "free_cf": <フリーCF = 営業CF + 投資CF>,
        "cash_change": <現預金増減>,
        "notes": ["推計の前提を1-3個"]
      }
    """
    notes = []

    # 減価償却（breakdownから取れれば利用、無ければ 0）
    depreciation = 0
    if breakdown and isinstance(breakdown, dict):
        sed = breakdown.get("selling_expenses_detail", []) or []
        if isinstance(sed, list):
            for item in sed:
                if isinstance(item, dict):
                    name = str(item.get("name", ""))
                    if "減価償却" in name or "償却" in name:
                        try:
                            depreciation += float(item.get("amount", 0))
                        except Exception:
                            pass

    # 運転資本変動（前期と比較）
    prev = None
    if historical_data:
        sorted_hd = sorted(historical_data, key=lambda x: x.period or "")
        for h in sorted_hd:
            if h.id == fd.id:
                break
            prev = h

    wc_change = 0
    if prev:
        dRecv = (getattr(fd, "receivables", 0) or 0) - (getattr(prev, "receivables", 0) or 0)
        dInv = (getattr(fd, "inventory", 0) or 0) - (getattr(prev, "inventory", 0) or 0)
        # 簡略：買掛金は流動負債で代用（精密版は別途）
        wc_change = -(dRecv + dInv)  # 売掛・在庫増は CF マイナス
        notes.append(f"運転資本変動（売掛+在庫の増減）を反映：{wc_change:+,.0f}万円")
    else:
        notes.append("前期データなしのため運転資本変動は0扱い")

    # 営業CF ≒ 当期純利益 + 減価償却 + 運転資本変動
    net_profit = getattr(fd, "net_profit", 0) or 0
    operating_cf = net_profit + depreciation + wc_change

    # 投資CF：固定資産増減（前期比）でざっくり推定。今回は depreciation 同額をマイナス
    # （※実額は固定資産台帳で別途）
    investing_cf = -depreciation if depreciation > 0 else 0
    if depreciation > 0:
        notes.append("投資CFは減価償却同額のマイナスで簡易推定（実額は固定資産台帳要）")

    # 財務CF：有利子負債の前期比（増えれば+、減れば-）
    financing_cf = 0
    if prev:
        d_debt = (getattr(fd, "interest_bearing_debt", 0) or 0) - (getattr(prev, "interest_bearing_debt", 0) or 0)
        financing_cf = d_debt

    # フリーCF = 営業CF + 投資CF
    free_cf = operating_cf + investing_cf

    # 現預金増減
    cash_change = None
    if prev:
        cash_change = (getattr(fd, "cash", 0) or 0) - (getattr(prev, "cash", 0) or 0)

    # ★ 期中（partial_aggregated）の場合、cash_change は n ヶ月の変化なので
    # 月割り計算の分母として n を持たせる（UI側で÷n表示に使う）
    _period_type = getattr(fd, "period_type", "") or ""
    if _period_type == "partial_aggregated":
        _period_divisor = max(getattr(fd, "_partial_n_months", 0) or 1, 1)
        _period_label = f"期中{_period_divisor}ヶ月"
    else:
        _period_divisor = 12
        _period_label = "年間"

    return {
        "operating_cf": round(operating_cf, 0),
        "investing_cf": round(investing_cf, 0),
        "financing_cf": round(financing_cf, 0),
        "free_cf": round(free_cf, 0),
        "cash_change": round(cash_change, 0) if cash_change is not None else None,
        "depreciation": round(depreciation, 0),
        "wc_change": round(wc_change, 0),
        "notes": notes,
        "_period_divisor": _period_divisor,  # 月割り計算用（年次=12、期中=n）
        "_period_label": _period_label,
    }


def compute_working_capital(fd, payables_days: float = 45.0) -> Dict:
    """
    運転資本指標を計算。
    payables_days は買掛金回転期間（DB未保持のため仮定値。既定45日）。
    後続でヒアリング回答から正確値を上書き可能。
    """
    result = {}

    # ★ 期中スナップショット対応: revenue/cost_of_sales が n ヶ月累計の場合、
    # 365/n*12 倍したものを年率換算として扱う（÷n を分子に補正）
    _period_type = getattr(fd, "period_type", "") or ""
    if _period_type == "partial_aggregated":
        _n = max(getattr(fd, "_partial_n_months", 0) or 1, 1)
        _annual_factor = 12.0 / _n  # n月累計 → 年率換算
    else:
        _annual_factor = 1.0
    result["_period_factor_used"] = _annual_factor
    result["_period_n_months"] = _n if _period_type == "partial_aggregated" else 12

    # 売上債権回転（年率換算売上 ÷ 売掛金）
    if getattr(fd, "revenue", 0) and getattr(fd, "receivables", 0):
        _rev_annualized = fd.revenue * _annual_factor
        turnover = _rev_annualized / fd.receivables
        result["receivables_turnover"] = round(turnover, 2)
        result["receivables_days"] = round(365 / turnover, 1)

    # 在庫回転（年率換算売上原価 ÷ 棚卸資産）
    if getattr(fd, "cost_of_sales", 0) and getattr(fd, "inventory", 0):
        _cos_annualized = fd.cost_of_sales * _annual_factor
        turnover = _cos_annualized / fd.inventory
        result["inventory_turnover"] = round(turnover, 2)
        result["inventory_days"] = round(365 / turnover, 1)

    # 買掛金回転期間（DB未保持。既定45日。後で上書き可）
    result["payables_days_assumed"] = payables_days

    # CCC（Cash Conversion Cycle）＝ 売上債権日数 + 在庫日数 - 買掛金日数
    r_days = result.get("receivables_days")
    i_days = result.get("inventory_days")
    if r_days is not None and i_days is not None:
        result["ccc_days"] = round(r_days + i_days - payables_days, 1)

    # 負債比率（総負債 ÷ 純資産）
    if getattr(fd, "total_liabilities", 0) and getattr(fd, "equity", 0):
        result["debt_equity_ratio"] = round(fd.total_liabilities / fd.equity * 100, 1)

    # 月商キャッシュ倍率（現預金 ÷ 月商）
    # ★ 期中の場合 revenue が n ヶ月累計なので ÷n、年次なら ÷12
    if getattr(fd, "revenue", 0) > 0 and getattr(fd, "cash", 0) > 0:
        _rev_divisor = (getattr(fd, "_partial_n_months", 0) or 12) if _period_type == "partial_aggregated" else 12
        monthly_revenue = fd.revenue / _rev_divisor
        result["cash_to_monthly_revenue"] = round(fd.cash / monthly_revenue, 2)
        result["cash_months_of_sales"] = round(fd.cash / monthly_revenue, 1)

    # 各指標の健全性ラベル
    def _label(metric, value):
        if metric == "receivables_days":
            if value <= 30: return "good"
            if value <= 60: return "ok"
            return "bad"
        if metric == "inventory_days":
            if value <= 30: return "good"
            if value <= 60: return "ok"
            return "bad"
        if metric == "ccc_days":
            if value <= 30: return "good"
            if value <= 90: return "ok"
            return "bad"
        if metric == "debt_equity_ratio":
            if value <= 100: return "good"
            if value <= 200: return "ok"
            return "bad"
        return "neutral"

    result["labels"] = {
        k: _label(k, result[k])
        for k in ("receivables_days", "inventory_days", "ccc_days", "debt_equity_ratio")
        if k in result
    }

    return result


def compute_ebitda(fd, breakdown: Dict = None) -> Dict:
    """
    EBITDA（営業利益 + 減価償却費）を計算。
    breakdown_json の販管費内訳から「減価償却費」を自動取得。
    """
    result = {"depreciation": 0, "ebitda": None, "ebitda_margin": None,
              "debt_to_ebitda": None, "ebitda_to_interest": None}

    op = getattr(fd, "operating_profit", 0) or 0

    # 減価償却費を breakdown から拾う
    depreciation = 0
    if breakdown:
        se = breakdown.get("selling_expenses_detail") or {}
        for key, val in se.items():
            if isinstance(val, (int, float)):
                if "減価償却" in key or "償却" in key:
                    depreciation += val
        # 売上原価明細にも減価償却が入る会社あり
        cos = breakdown.get("cost_of_sales_detail") or {}
        for key, val in cos.items():
            if isinstance(val, (int, float)):
                if "減価償却" in key or "償却" in key:
                    depreciation += val

    result["depreciation"] = depreciation
    if depreciation > 0:
        ebitda = op + depreciation
        result["ebitda"] = round(ebitda, 1)
        if getattr(fd, "revenue", 0) > 0:
            result["ebitda_margin"] = round(ebitda / fd.revenue * 100, 1)

        # 有利子負債 / EBITDA（金融機関が気にする指標：5倍超で危険ライン）
        # ★ 期中の EBITDA は n ヶ月累計なので、debt/EBITDA は年率換算した EBITDA で比較
        _period_type = getattr(fd, "period_type", "") or ""
        if _period_type == "partial_aggregated":
            _n = max(getattr(fd, "_partial_n_months", 0) or 1, 1)
            ebitda_annualized = ebitda * (12.0 / _n)
        else:
            ebitda_annualized = ebitda
        debt = getattr(fd, "interest_bearing_debt", 0) or 0
        if debt > 0 and ebitda_annualized > 0:
            result["debt_to_ebitda"] = round(debt / ebitda_annualized, 2)

    return result


def compute_burn_rate(fd, breakdown: Dict = None) -> Dict:
    """
    バーンレート関連指標を計算。
      - 月次営業利益／損失
      - 月次営業CF（≒ 営業利益 + 減価償却費）★ 真のキャッシュ創出力
      - 推定月次借入返済（有利子負債 ÷ 10年返済と仮定）
      - 実質バーン／月（営業CF - 返済）
      - 黒字化に必要な年間改善額
      - 資金ショートまでの月数（runway）

    🚨 修正前は real_burn = 営業利益 - 返済 だったが、減価償却を加算しないと
    「黒字なのにキャッシュアウト」と誤判定する（運輸・製造業に多い）。
    """
    result = {
        "operating_monthly": None,
        "debt_repayment_monthly_est": None,
        "real_burn_monthly": None,
        "operating_cf_monthly": None,
        "depreciation_monthly": None,
        "breakeven_required_yearly": None,
        "runway_months": None,
        "is_burning": False,
    }

    op = getattr(fd, "operating_profit", 0) or 0
    cash = getattr(fd, "cash", 0) or 0
    debt = getattr(fd, "interest_bearing_debt", 0) or 0

    # ★ 期中スナップショット対応: 期中なら op/depreciation は n ヶ月累計なので ÷n
    _period_type = getattr(fd, "period_type", "") or ""
    if _period_type == "partial_aggregated":
        _divisor = max(getattr(fd, "_partial_n_months", 0) or 1, 1)
    else:
        _divisor = 12
    result["_period_divisor_used"] = _divisor

    # 月次営業損益
    op_monthly = round(op / _divisor, 1)
    result["operating_monthly"] = op_monthly

    # 減価償却（販管費明細から拾う。なければ ebitda 算出のために 0 のまま）
    depreciation = 0
    if breakdown:
        se = (breakdown.get("selling_expenses_detail") or {})
        for k, v in se.items():
            if not isinstance(v, (int, float)) or k.startswith("__"):
                continue
            if any(kw in k for kw in ["減価償却", "償却費"]):
                depreciation += v
        # 売上原価側にもあれば（製造業）
        cos = (breakdown.get("cost_of_sales_detail") or {})
        for k, v in cos.items():
            if not isinstance(v, (int, float)) or k.startswith("__"):
                continue
            if any(kw in k for kw in ["減価償却", "償却費"]):
                depreciation += v
    dep_monthly = round(depreciation / _divisor, 1) if depreciation else 0
    result["depreciation_monthly"] = dep_monthly

    # 月次営業CF（簡易EBITDA ベース）＝ 営業利益 + 減価償却
    op_cf_monthly = round(op_monthly + dep_monthly, 1)
    result["operating_cf_monthly"] = op_cf_monthly

    # 推定月次返済額（長期借入=10年想定の粗推定）
    if debt > 0:
        result["debt_repayment_monthly_est"] = round(debt / 10 / 12, 1)

    # 実質バーン（営業CF - 返済）★ 修正：減価償却込み
    real_burn = op_cf_monthly
    if result["debt_repayment_monthly_est"]:
        real_burn = op_cf_monthly - result["debt_repayment_monthly_est"]
    result["real_burn_monthly"] = round(real_burn, 1)

    # 黒字化に必要な年間改善
    # ★ 期中（partial_aggregated）の op は n月累計なので、年間ベースに換算（×12/n）
    if op < 0:
        if _period_type == "partial_aggregated":
            _op_annualized = op * (12.0 / _divisor)
            result["breakeven_required_yearly"] = round(-_op_annualized, 1)
        else:
            result["breakeven_required_yearly"] = round(-op, 1)
        result["is_burning"] = True
    if real_burn < 0:
        result["repayment_covered_yearly_gap"] = round(-real_burn * 12, 1)

    # Runway（real_burn が負＝キャッシュアウト時のみ）
    if real_burn < 0 and cash > 0:
        result["runway_months"] = round(cash / (-real_burn), 1)

    return result
