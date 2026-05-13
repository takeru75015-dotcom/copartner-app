"""
F-03: 業界ベンチマーク比較サービス
  - industries.csv から業界別の中央値・上位25% を取得
  - 決算書データと比較して判定結果を返す
  - 業種名の曖昧マッチング対応
"""
import csv
from pathlib import Path
from typing import Dict, List, Optional

_DATA_PATH = Path(__file__).parent.parent / "data" / "industries.csv"
_industries_cache: Optional[List[Dict]] = None


def _load_industries() -> List[Dict]:
    """industries.csv をメモリにロード（キャッシュ）"""
    global _industries_cache
    if _industries_cache is not None:
        return _industries_cache

    rows = []
    with open(_DATA_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 数値カラムをfloat化
            for k in row:
                if k not in ("industry_code", "industry_name", "category", "source_note"):
                    try:
                        row[k] = float(row[k])
                    except (ValueError, TypeError):
                        row[k] = 0.0
            rows.append(row)
    _industries_cache = rows
    return rows


def find_industry(industry_text: str) -> Dict:
    """
    業種テキスト（ユーザー入力）から最も近い業種レコードを返す。
    見つからなければ "その他（汎用）" を返す。
    """
    industries = _load_industries()

    if not industry_text:
        return _get_default(industries)

    query = industry_text.strip().lower()

    # 完全一致
    for row in industries:
        if query == row["industry_name"].lower():
            return row

    # 部分一致（業種名にクエリが含まれる or 逆）
    for row in industries:
        name_lc = row["industry_name"].lower()
        if query in name_lc or name_lc.replace("業", "") in query:
            return row

    # キーワードマッチ（カテゴリ）
    keyword_map = {
        "飲食": "R001",
        "レストラン": "R001",
        "食品": "E001",
        "食料品": "R002",
        "コーヒー": "W001",
        "カフェ": "R001",
        "製造": "E002",
        "機械": "E002",
        "金属": "E003",
        "小売": "R004",
        "衣料": "R003",
        "アパレル": "R003",
        "卸": "W001",
        "商社": "W002",
        "建設": "C001",
        "工務店": "C001",
        "工事": "C002",
        "it": "I001",
        "ソフトウェア": "I001",
        "システム": "I002",
        "web": "I001",
        "広告": "S001",
        "マーケティング": "S001",
        "派遣": "S002",
        "コンサル": "S003",
        "運送": "T001",
        "物流": "T001",
        "不動産": "P001",
        "賃貸": "P001",
        "医療": "H001",
        "介護": "H001",
    }

    for keyword, code in keyword_map.items():
        if keyword in query:
            for row in industries:
                if row["industry_code"] == code:
                    return row

    return _get_default(industries)


def _get_default(industries: List[Dict]) -> Dict:
    for row in industries:
        if row["industry_code"] == "X999":
            return row
    return industries[0] if industries else {}


def classify_scale_tier(revenue: float) -> Dict:
    """売上高で規模帯を判定。
    小：< 1億円 / 中：1-10億円 / 大：10億円超
    返り値の adjustments は中央値・上位25% を規模補正するための係数。
    """
    if revenue < 10000:  # 万円単位なので 1億円 = 10000万円
        return {
            "tier": "小規模",
            "tier_label": "小規模（年商1億円未満）",
            "tier_range": "年商 〜1億円",
            "margin_mult": 0.7,    # 小規模は利益率低めの傾向
            "equity_add": -5.0,    # 自己資本比率は低い
        }
    elif revenue < 100000:  # 10億円
        return {
            "tier": "中規模",
            "tier_label": "中規模（年商1〜10億円）",
            "tier_range": "年商 1〜10億円",
            "margin_mult": 1.0,
            "equity_add": 0.0,
        }
    else:
        return {
            "tier": "大規模",
            "tier_label": "大規模（年商10億円超）",
            "tier_range": "年商 10億円〜",
            "margin_mult": 1.3,    # 大規模は規模の経済で利益率高め
            "equity_add": 5.0,
        }


def compare_to_benchmark(fd, industry_text: str) -> Dict:
    """
    財務データと業界ベンチマークを比較して判定結果を返す。

    Returns:
        {
          "industry_name": "飲食店",
          "source_note": "...",
          "comparisons": [
            {
              "metric": "粗利率",
              "self_value": 34.2,
              "unit": "%",
              "median": 62.0,
              "top25": 70.5,
              "rank": "bottom",   # "top25" / "above_median" / "below_median" / "bottom"
              "gap_to_median": -27.8,
              "comment": "業界中央値を大きく下回る。仕入原価または価格設定に改善余地"
            },
            ...
          ],
          "overall_position": "below_median"
        }
    """
    ind = find_industry(industry_text)
    if not ind:
        return {"industry_name": "不明", "comparisons": [], "overall_position": "unknown"}

    # 規模帯判定（北村先生FB対応：規模別の参考中央値も併記）
    scale = classify_scale_tier(getattr(fd, "revenue", 0) or 0)
    m_mult = scale["margin_mult"]
    eq_add = scale["equity_add"]

    # 自社の指標計算
    gross_margin = (fd.gross_profit / fd.revenue * 100) if fd.revenue else 0
    operating_margin = (fd.operating_profit / fd.revenue * 100) if fd.revenue else 0

    comparisons = []

    # 粗利率
    gm_comp = _build_comparison(
        metric="粗利率（売上から仕入れを引いた利益の割合）",
        self_value=gross_margin,
        median=ind["gross_margin_median"],
        top25=ind["gross_margin_top25"],
        unit="%",
        higher_is_better=True,
    )
    gm_comp["scale_adjusted_median"] = round(ind["gross_margin_median"] * m_mult, 1)
    gm_comp["scale_tier"] = scale["tier_label"]
    comparisons.append(gm_comp)

    # 営業利益率（黒字/赤字分離で比較）
    is_profitable = operating_margin > 0
    profit_med = ind.get("operating_margin_profit_median") or ind["operating_margin_median"]
    loss_med = ind.get("operating_margin_loss_median") or 0.0
    if is_profitable:
        # 黒字企業 → 黒字企業のみの中央値と比較（赤字社混入の平均は使わない）
        op_comp = _build_comparison(
            metric="営業利益率（黒字企業のみの中央値と比較）",
            self_value=operating_margin,
            median=profit_med,
            top25=ind["operating_margin_top25"],
            unit="%",
            higher_is_better=True,
        )
        op_comp["benchmark_note"] = (
            f"黒字企業のみの業界中央値 {profit_med}%（赤字企業の中央値は {loss_med}%）。"
            "全体平均は赤字社で押し下げられるため、黒字社は黒字社内で比較が妥当。"
        )
        op_comp["scale_adjusted_median"] = round(profit_med * m_mult, 1)
        op_comp["scale_tier"] = scale["tier_label"]
        comparisons.append(op_comp)
    else:
        # 赤字企業 → 赤字企業の中央値と比較（赤字社の中でどの位置か）
        op_comp = _build_comparison(
            metric="営業利益率（赤字企業群の中での位置）",
            self_value=operating_margin,
            median=loss_med,
            top25=0.0,  # 赤字脱却ライン
            unit="%",
            higher_is_better=True,
        )
        op_comp["benchmark_note"] = (
            f"赤字企業の業界中央値 {loss_med}%。"
            f"黒字脱却の目安は 0% 超え、業界平均並みは {profit_med}%。"
            "赤字社内での位置づけと、脱却ラインを意識した二段階目標設定が有効。"
        )
        op_comp["scale_adjusted_median"] = round(loss_med * m_mult, 1)
        op_comp["scale_tier"] = scale["tier_label"]
        comparisons.append(op_comp)

    # 自己資本比率（B/Sデータがあれば）
    total_assets = getattr(fd, "total_assets", 0) or 0
    equity = getattr(fd, "equity", 0) or 0
    if total_assets > 0 and equity > 0:
        equity_ratio = equity / total_assets * 100
        eq_comp = _build_comparison(
            metric="自己資本比率（借入に頼らない自力経営の度合い）",
            self_value=equity_ratio,
            median=ind["equity_ratio_median"],
            top25=ind["equity_ratio_top25"],
            unit="%",
            higher_is_better=True,
        )
        eq_comp["scale_adjusted_median"] = round(ind["equity_ratio_median"] + eq_add, 1)
        eq_comp["scale_tier"] = scale["tier_label"]
        comparisons.append(eq_comp)

    # 売上債権回転期間（業界ベンチマークなしの独立判定）
    receivables = getattr(fd, "receivables", 0) or 0
    if receivables > 0 and fd.revenue > 0:
        rec_days = receivables / fd.revenue * 365
        # 一般目安: 30日以下=良、45日=普通、60日超=要改善、90日超=危険
        comparisons.append({
            "metric": "売上債権回転期間（売掛金が現金になるまでの日数）",
            "self_value": round(rec_days, 1),
            "median": 45.0,
            "top25": 30.0,
            "unit": "日",
            "rank": (
                "top25" if rec_days <= 30
                else "above_median" if rec_days <= 45
                else "below_median" if rec_days <= 60
                else "bottom"
            ),
            "gap_to_median": round(rec_days - 45.0, 1),
            "comment": (
                "回収が早く資金繰りに好影響" if rec_days <= 30
                else "標準的な回収ペース" if rec_days <= 45
                else "やや回収が遅い。取引条件の見直しを検討" if rec_days <= 60
                else f"⚠️ {rec_days:.0f}日は明確に遅い。EC/小売なら異常、BtoB卸でも要改善"
            ),
        })

    # 在庫回転期間（業界ベンチマークなしの独立判定）
    inventory = getattr(fd, "inventory", 0) or 0
    cost_of_sales = getattr(fd, "cost_of_sales", 0) or 0
    if inventory > 0 and cost_of_sales > 0:
        inv_days = inventory / cost_of_sales * 365
        comparisons.append({
            "metric": "在庫回転期間（仕入れから売れるまでの日数）",
            "self_value": round(inv_days, 1),
            "median": 45.0,
            "top25": 30.0,
            "unit": "日",
            "rank": (
                "top25" if inv_days <= 30
                else "above_median" if inv_days <= 45
                else "below_median" if inv_days <= 60
                else "bottom"
            ),
            "gap_to_median": round(inv_days - 45.0, 1),
            "comment": (
                "在庫効率が高い" if inv_days <= 30
                else "標準的な在庫水準" if inv_days <= 45
                else "やや在庫過多。滞留在庫をリスト化して圧縮検討" if inv_days <= 60
                else f"⚠️ {inv_days:.0f}日は滞留在庫の疑い。値引販売・廃棄損で整理を"
            ),
        })

    # 負債比率（自己資本比率よりインパクトが大きい場合が多いので重視）
    total_liabilities = getattr(fd, "total_liabilities", 0) or 0
    equity = getattr(fd, "equity", 0) or 0
    if total_liabilities > 0 and equity > 0:
        debt_equity = total_liabilities / equity * 100
        comparisons.append({
            "metric": "負債比率（借金が自己資金の何倍か）",
            "self_value": round(debt_equity, 1),
            "median": 100.0,
            "top25": 50.0,
            "unit": "%",
            "rank": (
                "top25" if debt_equity <= 50
                else "above_median" if debt_equity <= 100
                else "below_median" if debt_equity <= 200
                else "bottom"
            ),
            "gap_to_median": round(debt_equity - 100.0, 1),
            "comment": (
                "借金が少なく財務極めて健全" if debt_equity <= 50
                else "健全水準" if debt_equity <= 100
                else "借入がやや多い。金利負担・返済計画要確認" if debt_equity <= 200
                else f"⚠️ {debt_equity:.0f}%は借入過大。金融機関交渉・返済計画見直しが急務"
            ),
        })

    # 流動比率（業界ベンチマークなしの独立判定）
    current_assets = getattr(fd, "current_assets", 0) or 0
    current_liabilities = getattr(fd, "current_liabilities", 0) or 0
    if current_assets > 0 and current_liabilities > 0:
        current_ratio = current_assets / current_liabilities * 100
        # 流動比率の"深読み": 売掛・在庫が大きい場合は警告
        has_slow_assets = False
        if receivables and fd.revenue:
            if (receivables / fd.revenue * 365) > 60:
                has_slow_assets = True
        if inventory and cost_of_sales:
            if (inventory / cost_of_sales * 365) > 60:
                has_slow_assets = True

        if has_slow_assets:
            comment = "⚠️ 流動比率の数値は高いが、売掛金・在庫が滞留しており見た目倒れの可能性。現金化の順序を立て直す必要あり"
            rank = "below_median"
        else:
            rank = (
                "top25" if current_ratio >= 200
                else "above_median" if current_ratio >= 150
                else "below_median" if current_ratio >= 100
                else "bottom"
            )
            comment = (
                "健全水準（200%超）" if current_ratio >= 200
                else "健全な支払い余力あり" if current_ratio >= 150
                else "余力はあるが中央値を下回る" if current_ratio >= 100
                else "⚠️ 短期支払能力に不安。資金繰り要注意"
            )

        comparisons.append({
            "metric": "流動比率（短期支払い余力）",
            "self_value": round(current_ratio, 1),
            "median": 150.0,
            "top25": 200.0,
            "unit": "%",
            "rank": rank,
            "gap_to_median": round(current_ratio - 150.0, 1),
            "comment": comment,
        })

    # 全体ポジション（ランクの平均）
    rank_scores = {"top25": 4, "above_median": 3, "below_median": 2, "bottom": 1}
    avg_score = sum(rank_scores.get(c["rank"], 2) for c in comparisons) / max(len(comparisons), 1)
    if avg_score >= 3.5:
        overall = "top25"
    elif avg_score >= 2.5:
        overall = "above_median"
    elif avg_score >= 1.5:
        overall = "below_median"
    else:
        overall = "bottom"

    return {
        "industry_code": ind["industry_code"],
        "industry_name": ind["industry_name"],
        "source_note": ind["source_note"],
        "comparisons": comparisons,
        "overall_position": overall,
        "scale_tier": scale["tier"],
        "scale_tier_label": scale["tier_label"],
        "scale_tier_range": scale["tier_range"],
    }


def _build_comparison(metric: str, self_value: float, median: float, top25: float,
                     unit: str, higher_is_better: bool = True) -> Dict:
    """1指標のベンチマーク比較結果を作る"""
    gap = self_value - median

    if higher_is_better:
        if self_value >= top25:
            rank = "top25"
            comment = f"業界上位25%の水準。強みとして維持"
        elif self_value >= median:
            rank = "above_median"
            comment = f"業界中央値を上回る。標準的な水準"
        elif self_value >= (median + (median - top25)):
            rank = "below_median"
            comment = f"業界中央値を下回る。改善余地あり"
        else:
            rank = "bottom"
            comment = f"業界平均を大きく下回る。優先的に改善が必要"
    else:
        # 低い方が良い指標（現状なし、将来用）
        if self_value <= top25:
            rank = "top25"
            comment = f"業界上位25%の水準"
        elif self_value <= median:
            rank = "above_median"
            comment = f"業界中央値以下で良好"
        else:
            rank = "below_median"
            comment = f"業界中央値を超過"

    return {
        "metric": metric,
        "self_value": round(self_value, 1),
        "median": median,
        "top25": top25,
        "unit": unit,
        "rank": rank,
        "gap_to_median": round(gap, 1),
        "comment": comment,
    }


def extract_competitive_strengths(benchmark: Dict, fd=None) -> List[Dict]:
    """
    業界ベンチマークから「他社と比較した強み」を抽出して言語化する。
    rank が "top25" または "above_median" の項目を強みとして整理。

    Returns:
        [
          {
            "title": "粗利率が業界上位25%",
            "metric": "粗利率",
            "self_value": "42.5%",
            "industry_median": "32.5%",
            "industry_top25": "42.0%",
            "gap": "+10.0pt",
            "rank": "top25",
            "narrative": "業界の上位25%水準。同業他社と比べて...",
          }
        ]
    """
    out = []
    rank_order = {"top25": 0, "above_median": 1}
    comparisons = [c for c in benchmark.get("comparisons", [])
                   if c.get("rank") in rank_order]
    comparisons.sort(key=lambda c: rank_order.get(c["rank"], 9))

    for c in comparisons:
        metric = c["metric"].split("（")[0]
        rank = c["rank"]
        gap = c.get("gap_to_median", 0)
        self_val = c.get("self_value")
        median = c.get("median")
        top25 = c.get("top25")
        unit = c.get("unit", "")

        if rank == "top25":
            narrative = (
                f"{metric}は業界上位25%水準。"
                f"同業の半分以上はあなたの数字に届いていません。"
                f"「{metric} {self_val}{unit}」は社長との会話で誇れる材料。"
            )
        else:  # above_median
            narrative = (
                f"{metric}は業界中央値を上回る。"
                f"同業の半分より良い数字。"
                f"上位25%（{top25}{unit}）まではあと {top25 - self_val:+.1f}{unit}。"
            )

        out.append({
            "title": f"{metric}が業界{'上位25%' if rank == 'top25' else '中央値超'}",
            "metric": metric,
            "self_value": f"{self_val}{unit}",
            "industry_median": f"{median}{unit}",
            "industry_top25": f"{top25}{unit}",
            "gap": f"{gap:+.1f}{unit}",
            "rank": rank,
            "narrative": narrative,
        })

    return out


def format_benchmark_text(benchmark: Dict) -> str:
    """
    LLM プロンプトに差し込むテキスト形式の業界比較情報を返す。
    analyze_financials() のプロンプトに追加することで、AI がベンチマークを踏まえた提案を出せる。
    """
    if not benchmark.get("comparisons"):
        return "業界ベンチマーク情報なし"

    lines = [f"【業界ベンチマーク：{benchmark['industry_name']}】"]
    for c in benchmark["comparisons"]:
        rank_label = {
            "top25": "✅ 上位25%",
            "above_median": "⬆️ 中央値超",
            "below_median": "⚠️ 中央値下",
            "bottom": "🔴 下位",
        }.get(c["rank"], "")
        lines.append(
            f"  {c['metric']}: 自社 {c['self_value']}{c['unit']} "
            f"(業界中央値 {c['median']}{c['unit']}, 上位25% {c['top25']}{c['unit']}) "
            f"→ {rank_label}"
        )
        if c.get("benchmark_note"):
            lines.append(f"    補足: {c['benchmark_note']}")
    lines.append(f"  出典: {benchmark['source_note']}")
    return "\n".join(lines)
