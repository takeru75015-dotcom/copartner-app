"""
販管費・売上原価の費目を自動でグループ集計するサービス。
AI に詳細な内訳と同時に「グループ集計後の構造」を渡して、分析の解像度を上げる。

グループ定義は中小企業の決算書で出現する標準的な費目を踏まえて設計。
"""
from typing import Dict, List, Tuple, Optional


# グループ → そのグループに該当する費目名のキーワード
# キーワードは部分一致（in 演算子）で判定
_EXPENSE_GROUPS = [
    ("人件費", [
        "給与", "給料", "賞与", "役員報酬", "雑給",
        "法定福利", "福利厚生", "退職金", "退職給付",
        "通勤費", "通勤交通費",
    ]),
    ("旅費交通費", [
        "旅費", "出張", "交通費",  # 「通勤交通費」は上で先にマッチさせる
    ]),
    ("物件費", [
        "地代家賃", "賃借料", "リース料",
        "水道光熱", "電気料", "ガス料", "水道料",
        "通信費", "電話", "回線",
        "修繕費", "修繕維持費", "保守料",
    ]),
    ("販促費", [
        "広告", "宣伝", "販売促進", "販促",
        "接待交際", "交際費", "会議費", "会員費",
    ]),
    ("外注・委託費", [
        "外注", "業務委託", "支払手数料", "支払報酬",
        "コンサル", "顧問料", "システム利用料", "ソフトウェア",
    ]),
    ("車両・運送費", [
        "燃料", "ガソリン", "車両", "運搬費",
        "リース料_車両", "高速道路", "車検",
        "自動車保険",
    ]),
    ("管理費", [
        "消耗品", "事務用品", "図書研修", "新聞図書",
        "事務用消耗", "備品",
    ]),
    ("租税公課・保険料", [
        "租税", "公租公課", "公課",
        "保険料",  # 「自動車保険」は上で先にマッチさせる
    ]),
    ("研究・開発・教育費", [
        "研究開発", "研究費", "開発費",
        "教育", "研修",
    ]),
    ("減価償却費", [
        "減価償却", "償却費",
    ]),
    ("貸倒関連", [
        "貸倒", "貸倒引当", "貸倒損失",
    ]),
]


def _match_group(item_name: str) -> Optional[str]:
    """費目名から所属グループを判定。最初にマッチしたグループを返す。"""
    if not item_name:
        return None
    for group_name, keywords in _EXPENSE_GROUPS:
        for kw in keywords:
            if kw in item_name:
                return group_name
    return None


def aggregate_expenses(detail: Dict) -> Dict:
    """販管費明細（{費目名: 金額}）をグループ集計する。
    戻り値:
    {
      "groups": {
        "人件費": {"total": 32_500, "entries": [("給与手当", 25_864), ...]},
        "物件費": {...},
        ...
        "その他": {"total": ..., "entries": [...]}  # どのグループにも入らなかった費目
      },
      "total": 70_730,  # 全合計
      "groups_sorted": [("人件費", 32_500), ("車両・運送費", 8_900), ...]  # 金額降順
    }
    """
    if not isinstance(detail, dict):
        return {"groups": {}, "total": 0, "groups_sorted": []}

    # ノイズキー（__で始まる内部キー等）除外
    clean = {
        k: v for k, v in detail.items()
        if not k.startswith("__") and isinstance(v, (int, float)) and v != 0
    }

    groups: Dict[str, Dict] = {}
    for item, amount in clean.items():
        group = _match_group(item) or "その他"
        if group not in groups:
            groups[group] = {"total": 0.0, "entries": []}
        groups[group]["total"] += amount
        groups[group]["entries"].append((item, amount))

    total = sum(g["total"] for g in groups.values())

    # 金額降順
    groups_sorted = sorted(
        [(name, g["total"]) for name, g in groups.items()],
        key=lambda x: -x[1]
    )

    # items を金額順にソート
    for g in groups.values():
        g["entries"].sort(key=lambda x: -x[1])
        g["total"] = round(g["total"], 1)

    return {
        "groups": groups,
        "total": round(total, 1),
        "groups_sorted": groups_sorted,
    }


def format_expense_groups_for_prompt(detail: Dict, label: str = "販管費",
                                       revenue: float = 0) -> str:
    """グループ集計結果をプロンプト埋め込み用テキストに整形"""
    agg = aggregate_expenses(detail)
    if not agg["groups"]:
        return ""

    lines = [f"\n◆ {label} グループ集計（人件費・物件費等にまとめた集計値。提案の主軸にすべき）"]
    lines.append(f"  合計: {agg['total']:,.0f}万円" +
                 (f"（売上比 {agg['total']/revenue*100:.1f}%）" if revenue else ""))
    for group_name, group_total in agg["groups_sorted"]:
        ratio = (group_total / agg['total'] * 100) if agg['total'] else 0
        ratio_rev = (group_total / revenue * 100) if revenue else 0
        rev_text = f" / 売上比{ratio_rev:.1f}%" if revenue else ""
        lines.append(f"  - {group_name}: {group_total:,.0f}万円（{label}内{ratio:.1f}%{rev_text}）")
        # 上位アイテム3つまで内訳表示
        items = agg["groups"][group_name]["entries"][:3]
        for item_name, item_amt in items:
            lines.append(f"      └ {item_name}: {item_amt:,.0f}万円")
    return "\n".join(lines)


def compute_group_delta(prev_detail: Dict, curr_detail: Dict) -> List[Dict]:
    """前期と当期のグループ集計を比較。各グループの増減を返す。
    戻り値: [{"group": "人件費", "prev": 30000, "curr": 32500, "delta": 2500, "pct": 8.3}, ...]
    金額の絶対差が大きい順にソート。
    """
    prev_agg = aggregate_expenses(prev_detail)
    curr_agg = aggregate_expenses(curr_detail)
    all_groups = set(prev_agg["groups"].keys()) | set(curr_agg["groups"].keys())
    rows = []
    for g in all_groups:
        p = prev_agg["groups"].get(g, {}).get("total", 0)
        c = curr_agg["groups"].get(g, {}).get("total", 0)
        delta = c - p
        if p == 0 and c == 0:
            continue
        pct = (delta / abs(p) * 100) if p else None
        rows.append({
            "group": g,
            "prev": round(p, 1),
            "curr": round(c, 1),
            "delta": round(delta, 1),
            "pct": round(pct, 1) if pct is not None else None,
        })
    rows.sort(key=lambda x: -abs(x["delta"]))
    return rows


def format_group_delta_for_prompt(prev_detail: Dict, curr_detail: Dict,
                                    label: str = "販管費") -> str:
    """グループ別前期比をプロンプト埋め込み用に整形"""
    rows = compute_group_delta(prev_detail, curr_detail)
    if not rows:
        return ""
    lines = [f"\n◆ {label} グループ別 前期比増減（増減原因の特定用。費目をまとめた粒度で見る）"]
    for r in rows:
        pct_text = f"{r['pct']:+.1f}%" if r['pct'] is not None else "新規"
        lines.append(
            f"  - {r['group']}: 前期 {r['prev']:,.0f} → 今期 {r['curr']:,.0f}万円"
            f"（差 {r['delta']:+,.0f} / {pct_text}）"
        )
    return "\n".join(lines)
