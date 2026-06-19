"""
打ち手（節税・手取り最適化・売上拡大）の影響計算エンジン。

設計: deliverables/copartner_product_todo_20260618/ の 02(型)・03(数式)・05(優先順位)。
原則:
  - 税は提案ごとに足さず、合算後の税引前利益から1回だけ再計算する
  - 確定（節税）と 仮定（売上施策）は混ぜず、2本のライン（確定 / 仮定込み）で出す
  - 効果は「会社のニーズ(税/現金/手取り/利益)」で並びが変わる
  - 手取りは精度を装わずレンジ（約X〜Y万）で出す
  - 金額・税額は全て『万円』単位（DB財務値に統一）。税率・制度限度額は概算（要最新確認）
マスタデータは data/tax_proposals.json。計算ロジックは calc_id ごとに本ファイルで実装。
"""
import json
import os

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tax_proposals.json")

NEEDS = [
    {"key": "perf", "label": "業績を上げたい（売上↑・コスト↓）"},
    {"key": "tax", "label": "法人税を減らしたい"},
    {"key": "takehome", "label": "社長個人の手取りを増やしたい"},
    {"key": "cash", "label": "現金を手元に残したい"},
]


def _load() -> dict:
    with open(_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _round10(x: float) -> int:
    """10万円単位に丸める。※金額の単位は万円（DBの保存単位に合わせる）。"""
    return int(round(x / 10.0)) * 10


def _coerce_amount(v) -> float:
    """金額を安全に float 化（不正値は0）。外部入力の防御。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(v: float, lo, hi) -> float:
    """提案ごとの下限/上限（万円）でクランプ。None は無制限。"""
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def build_context(fd) -> dict:
    """FinancialData から計算に必要な現状値を取り出す。税引前は経常利益を基準。
    ※ DBの財務値・本エンジンの金額・税額は全て『万円』単位（claude_client と統一）。"""
    pretax = float(getattr(fd, "ordinary_profit", 0) or 0)
    if pretax == 0:
        pretax = float(getattr(fd, "net_profit", 0) or 0)  # フォールバック（ざっくり）
    revenue = float(getattr(fd, "revenue", 0) or 0)
    gross = float(getattr(fd, "gross_profit", 0) or 0)
    margin = (gross / revenue) if revenue > 0 else 0.3  # 粗利率（取れなければ30%と仮定）
    return {
        "revenue": revenue,
        "operating_profit": float(getattr(fd, "operating_profit", 0) or 0),
        "pretax": pretax,
        "cash": float(getattr(fd, "cash", 0) or 0),
        "equity": float(getattr(fd, "equity", 0) or 0),
        "gross_margin": max(0.0, min(1.0, margin)),
        "interest_bearing_debt": float(getattr(fd, "interest_bearing_debt", 0) or 0),
    }


def estimate_loan_repay(ctx, params) -> int:
    """年間の元本返済額の推定（万円）。決算書には正確値が無いため有利子負債÷想定年数でざっくり。"""
    debt = ctx.get("interest_bearing_debt", 0) or 0
    years = params.get("loan_estimate_years", 7) or 7
    if debt <= 0:
        return 0
    return max(0, _round10(debt / years))


def _corp_tax(pretax, t_corp) -> float:
    """法人税等のざっくり試算（黒字部分のみ・繰越欠損や均等割は未考慮）。"""
    return max(0.0, pretax) * t_corp


def _suggest_default(rule, ctx, max_v):
    pretax = ctx["pretax"]
    if rule == "pretax_ratio_0.2":
        return max(0, _round10(pretax * 0.2)) if pretax > 0 else 0
    if rule == "pretax_ratio_0.2_cap":
        v = max(0, _round10(pretax * 0.2)) if pretax > 0 else 0
        return min(v, max_v or 240)
    if rule == "revenue_ratio_0.05":
        return max(0, _round10(ctx["revenue"] * 0.05))
    if rule == "revenue_ratio_0.01":
        return max(0, _round10(ctx["revenue"] * 0.01))
    if rule and rule.startswith("fixed_"):
        return int(rule.split("_", 1)[1])
    return 0


def _impact(p, amount, params, ctx) -> dict:
    """1提案・金額amountの影響。p は提案dict（calc_id, cost_ratio 等を参照）。
    pretax_delta: 法人の税引前への差分 / cash_direct: 法人の直接現金支出(税効果除く)
    personal_tax_saving: 個人の節税(+が得) / personal_takehome: 非課税の手取り増
    salary_gross: 役員報酬の額面変化（手取り内訳用）
    """
    calc_id = p["calc_id"]
    t_per = params["t_personal"]
    shaho = params["shaho_rate"]
    a = float(amount or 0)
    zero = {"pretax_delta": 0.0, "cash_direct": 0.0, "personal_tax_saving": 0.0,
            "personal_takehome": 0.0, "salary_gross": 0.0}
    if calc_id == "kessan_bonus":
        return {**zero, "pretax_delta": -(1 + shaho) * a, "cash_direct": -(1 + shaho) * a}
    if calc_id == "sec_kyosai":
        return {**zero, "pretax_delta": -a, "cash_direct": -a}
    if calc_id == "small_kyosai":  # 個人の所得控除。法人には影響なし
        return {**zero, "personal_tax_saving": a * t_per}
    if calc_id == "yakuin_hosyu":  # +で増額: 法人損金増・個人は額面増（手取り内訳で精算）
        return {**zero, "pretax_delta": -(1 + shaho) * a, "cash_direct": -(1 + shaho) * a,
                "salary_gross": a}
    if calc_id == "ryohi_kitei":  # 日当: 会社の損金・受取側は非課税
        return {**zero, "pretax_delta": -a, "cash_direct": -a, "personal_takehome": a}
    if calc_id == "rev_growth":  # 売上拡大（仮定）: 粗利−コスト が利益に
        cost_ratio = float(p.get("cost_ratio", 0.2))
        m = ctx.get("gross_margin", 0.3)
        profit = a * (m - cost_ratio)
        # 売上は売掛で遅れる→短期の現金はコスト先行のみ（保守的）
        return {**zero, "pretax_delta": profit, "cash_direct": -a * cost_ratio}
    if calc_id == "cost_cut":  # コスト削減（仮定）: 削減分がそのまま利益・現金改善に
        return {**zero, "pretax_delta": a, "cash_direct": a}
    return zero


def _base_view(ctx, params) -> dict:
    tax = _corp_tax(ctx["pretax"], params["t_corp"])
    return {
        "revenue": round(ctx["revenue"]),
        "operating_profit": round(ctx["operating_profit"]),
        "pretax": round(ctx["pretax"]),
        "corp_tax": round(tax),
        "net": round(ctx["pretax"] - tax),
        "cash": round(ctx["cash"]),
        "equity": round(ctx["equity"]),
    }


_PUBLIC_FIELDS = ("id", "calc_id", "title", "category", "target", "certainty",
                  "effort", "unit", "input_label", "description", "explanation", "caveats")


def _calc_explain(p, amount, params, ctx, imp, corp_tax_saving):
    """各提案の『前提＋計算式＋結果』を文字列リストで返す（根拠の見える化・仮の前提を明示）。"""
    tc = int(round(params["t_corp"] * 100))
    tp = int(round(params["t_personal"] * 100))
    sh = int(round(params["shaho_rate"] * 100))
    bl = int(round(params["personal_burden_low"] * 100))
    bh = int(round(params["personal_burden_high"] * 100))
    a = round(amount)
    ce = round(imp["cash_direct"] + corp_tax_saving)
    cid = p["calc_id"]
    if cid == "kessan_bonus":
        return [f"前提: 法人税率 約{tc}%（概算）",
                f"賞与{a}万＋社保約{sh}%＝損金 → 法人税 −{corp_tax_saving}万",
                f"会社の現金: 約{ce}万（賞与は出ていく）"]
    if cid == "sec_kyosai":
        return [f"前提: 法人税率 約{tc}%（概算）",
                f"掛金{a}万＝全額損金 → 法人税 −{corp_tax_saving}万",
                "※解約時は課税（実質は課税の繰延＋積立）"]
    if cid == "small_kyosai":
        return [f"前提: 個人税率 約{tp}%（概算・所得で変動）",
                f"掛金{a}万＝全額所得控除 → 個人税 −{round(imp['personal_tax_saving'])}万",
                "※法人の決算には影響しない（社長個人の節税）"]
    if cid == "yakuin_hosyu":
        return [f"前提: 法人税率{tc}% / 個人負担 約{bl}〜{bh}%（概算）",
                f"額面±{a}万 → 法人税 {'+' if corp_tax_saving >= 0 else ''}{corp_tax_saving}万",
                "個人の手取りは下の『手取りレンジ』参照"]
    if cid == "ryohi_kitei":
        return [f"前提: 法人税率 約{tc}%（概算）",
                f"日当{a}万＝損金 → 法人税 −{corp_tax_saving}万",
                f"社長: 非課税で手取り +{a}万（給与より税・社保が軽い）"]
    if cid == "rev_growth":
        m = int(round(ctx.get("gross_margin", 0.3) * 100))
        cr = int(round(float(p.get("cost_ratio", 0.2)) * 100))
        gp = round(amount * ctx.get("gross_margin", 0.3))
        co = round(amount * float(p.get("cost_ratio", 0.2)))
        return [f"前提: 粗利率 約{m}% / コスト率 約{cr}%（仮定）",
                f"売上+{a}万 → 粗利{gp}万 − コスト{co}万 ＝ 利益+{round(imp['pretax_delta'])}万",
                "※当たるかは不確実・売掛で入金は遅れる"]
    if cid == "cost_cut":
        return ["前提: 削減を継続できる想定（仮定）",
                f"コスト−{a}万 → 営業利益+{a}万（その分 税は増える）"]
    return []


def list_proposals(ctx) -> dict:
    """各提案に推奨デフォルト額と単独impactを付けて返す（UI初期表示用）。"""
    data = _load()
    params = data["params"]
    tax_now = _corp_tax(ctx["pretax"], params["t_corp"])
    out = []
    for p in data["proposals"]:
        default = _suggest_default(p.get("default_rule"), ctx, p.get("max"))
        imp = _impact(p, default, params, ctx)
        tax_after = _corp_tax(ctx["pretax"] + imp["pretax_delta"], params["t_corp"])
        corp_tax_saving = tax_now - tax_after
        item = {k: p[k] for k in _PUBLIC_FIELDS}
        item["default_amount"] = default
        item["min"] = p.get("min")
        item["max"] = p.get("max")
        item["single_impact"] = {
            "corp_tax_saving": round(corp_tax_saving),
            "cash_effect": round(imp["cash_direct"] + corp_tax_saving),
            "personal_tax_saving": round(imp["personal_tax_saving"]),
            "personal_takehome": round(imp["personal_takehome"]),
            "profit_effect": round(imp["pretax_delta"]),
        }
        item["calc_explain"] = _calc_explain(p, default, params, ctx, imp, round(corp_tax_saving))
        out.append(item)
    return {
        "params": params,
        "base": _base_view(ctx, params),
        "proposals": out,
        "needs": NEEDS,
        "loan_repay_estimate": estimate_loan_repay(ctx, params),
    }


def rank(ctx, need_key="tax") -> list:
    """ニーズ別に並べ替え。スコア = 期待効果(効果×確実性) × 実行しやすさ。"""
    proposals = list_proposals(ctx)["proposals"]
    cert_w = {"確定": 1.0, "仮定": 0.4}

    def effect(p):
        si = p["single_impact"]
        if need_key == "cash":
            return si["cash_effect"]
        if need_key == "takehome":
            return si["personal_takehome"] + max(0, si["personal_tax_saving"])
        if need_key == "perf":   # 業績改善＝営業利益インパクト（売上↑・コスト↓が上位、節税は下がる）
            return si["profit_effect"]
        return si["corp_tax_saving"] + max(0, si["personal_tax_saving"])  # tax

    for p in proposals:
        e = effect(p)
        w = cert_w.get(p["certainty"], 0.5)
        p["rank_effect"] = round(e)
        p["score"] = round(e * w * (p["effort"] / 5.0))
    proposals.sort(key=lambda x: x["score"], reverse=True)
    return proposals


def _personal_breakdown(salary_gross, takehome_nontax, kyosai_saving, params):
    """社長個人への影響をレンジで返す（額面→税・社保→手取り）。精度は装わずレンジ。"""
    lo = params.get("personal_burden_low", 0.25)
    hi = params.get("personal_burden_high", 0.40)
    # 役員報酬の額面変化分に対する手取り（負担はレンジ）
    deduct_low = salary_gross * lo
    deduct_high = salary_gross * hi
    take_low = salary_gross - deduct_high + takehome_nontax + kyosai_saving
    take_high = salary_gross - deduct_low + takehome_nontax + kyosai_saving
    return {
        "salary_gross": round(salary_gross),
        "deduct_low": round(min(deduct_low, deduct_high)),
        "deduct_high": round(max(deduct_low, deduct_high)),
        "nontax_takehome": round(takehome_nontax),
        "kyosai_saving": round(kyosai_saving),
        "takehome_low": round(min(take_low, take_high)),
        "takehome_high": round(max(take_low, take_high)),
    }


def _collect(ctx, selections):
    """selections を確定/仮定に分けて集計。外部入力なので防御。"""
    data = _load()
    params = data["params"]
    by_id = {p["id"]: p for p in data["proposals"]}  # 識別は id（calc_idは計算タイプで共有あり）
    if not isinstance(selections, list):
        selections = []
    agg = {
        "confirmed": {"pretax": 0.0, "cash": 0.0},
        "assumed": {"pretax": 0.0, "cash": 0.0},
        "personal_tax": 0.0, "takehome_nontax": 0.0, "salary_gross": 0.0,
    }
    for s in selections:
        if not isinstance(s, dict):
            continue
        p = by_id.get(s.get("id") or s.get("calc_id"))
        if p is None:
            continue
        amount = _clamp(_coerce_amount(s.get("amount")), p.get("min"), p.get("max"))
        imp = _impact(p, amount, params, ctx)
        bucket = "assumed" if p.get("certainty") == "仮定" else "confirmed"
        agg[bucket]["pretax"] += imp["pretax_delta"]
        agg[bucket]["cash"] += imp["cash_direct"]
        agg["personal_tax"] += imp["personal_tax_saving"]
        agg["takehome_nontax"] += imp["personal_takehome"]
        agg["salary_gross"] += imp["salary_gross"]
    return params, agg


def _line(ctx, params, add_pretax, add_cash):
    """ある打ち手集合を適用した着地（税は合算後に1回再計算）。"""
    t_corp = params["t_corp"]
    pretax_after = ctx["pretax"] + add_pretax
    tax_before = _corp_tax(ctx["pretax"], t_corp)
    tax_after = _corp_tax(pretax_after, t_corp)
    corp_tax_saving = tax_before - tax_after
    cash_after = ctx["cash"] + add_cash + corp_tax_saving
    net_after = pretax_after - tax_after
    base_net = ctx["pretax"] - tax_before
    # 本モデルでは打ち手の影響は全て営業段階（販管費/売上/原価）＝営業外を持たない。
    # 営業利益も税引前と同じ“表示デルタ”で動かす（丸め差を出さず営業外を一定に保つ）。
    pretax_delta_disp = round(pretax_after) - round(ctx["pretax"])
    return {
        "revenue": round(ctx["revenue"]),
        "operating_profit": round(ctx["operating_profit"]) + pretax_delta_disp,
        "pretax": round(pretax_after),
        "corp_tax": round(tax_after),
        "net": round(net_after),
        "cash": round(cash_after),
        "equity": round(ctx["equity"] + (net_after - base_net)),
        "_corp_tax_saving": round(corp_tax_saving),
        "_cash_effect": round(cash_after - ctx["cash"]),
    }


def simulate(ctx, selections) -> dict:
    """確定ラインと仮定込みラインの2本を返す。税は各ラインで合算後に1回再計算。"""
    params, agg = _collect(ctx, selections)
    base = _base_view(ctx, params)
    confirmed = _line(ctx, params, agg["confirmed"]["pretax"], agg["confirmed"]["cash"])
    all_pretax = agg["confirmed"]["pretax"] + agg["assumed"]["pretax"]
    all_cash = agg["confirmed"]["cash"] + agg["assumed"]["cash"]
    assumed = _line(ctx, params, all_pretax, all_cash)
    personal = _personal_breakdown(agg["salary_gross"], agg["takehome_nontax"],
                                   agg["personal_tax"], params)
    has_assumption = abs(agg["assumed"]["pretax"]) > 0 or abs(agg["assumed"]["cash"]) > 0
    return {
        "base": base,
        "after": confirmed,                 # 確定ライン（堅い）
        "after_with_assumptions": assumed,  # 仮定込みライン
        "has_assumption": has_assumption,
        "summary": {
            "corp_tax_saving": confirmed["_corp_tax_saving"],
            "cash_effect": confirmed["_cash_effect"],
            "personal_tax_saving": round(agg["personal_tax"]),
            "personal_takehome": round(agg["takehome_nontax"]),
        },
        "personal": personal,
    }


def project_years(ctx, selections, years=3, growth=0.0, loan_repay=0.0) -> dict:
    """複数年のキャッシュ推移（概算）。確定施策は継続適用。
    簡略: 純利益≒営業CF、納税は同年、元本返済 loan_repay を毎年控除。"""
    params, agg = _collect(ctx, selections)
    t_corp = params["t_corp"]
    try:
        years = int(years)
    except (TypeError, ValueError):
        years = 3
    years = max(1, min(5, years))
    g = _coerce_amount(growth)
    repay = max(0.0, _coerce_amount(loan_repay))
    add_pretax = agg["confirmed"]["pretax"]  # 確定施策は継続
    rows = []
    cash = ctx["cash"]
    for t in range(0, years + 1):
        pretax_t = ctx["pretax"] * ((1 + g) ** t) + (add_pretax if t >= 1 else 0.0)
        tax_t = _corp_tax(pretax_t, t_corp)
        net_t = pretax_t - tax_t
        if t >= 1:
            cash = cash + net_t - repay
        rows.append({
            "year": t,
            "pretax": round(pretax_t),
            "corp_tax": round(tax_t),
            "net": round(net_t),
            "cash": round(cash),
        })
    return {
        "rows": rows,
        "assumptions": {"growth": g, "loan_repay": round(repay), "years": years},
        "note": "概算。純利益≒営業CF・納税は同年・減価償却/運転資本/賞与の継続性は簡略。",
    }
