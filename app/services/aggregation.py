"""
科目集約サービス
  - 決算書の細かい勘定科目（給与・賞与・法定福利費 等）を、
    社長向け説明用に「人件費」「販売関連費」等のグループに集約する。

北村先生 #07（2026-05-16 FB）対応:
  「役員報酬、給与、賞与、社保まとめて人件費にしたりしてます」
  「中小の社長に説明するのに簡易化は必須」
"""
from typing import Dict, List, Tuple


# グループ定義: (グループ名, キーワードのリスト)
# 順序は重要 — 上のグループに含まれる科目は下のグループでは拾わない
GROUP_DEFINITIONS: List[Tuple[str, List[str]]] = [
    ("人件費", ["人件", "給料", "給与", "賞与", "役員報酬", "法定福利", "福利厚生", "退職", "社会保険", "労災", "雇用保険"]),
    ("外注・委託費", ["外注", "業務委託", "委託費"]),
    ("地代家賃・賃借料", ["地代家賃", "家賃", "賃借", "リース"]),
    ("販売促進・広告費", ["広告", "宣伝", "販売促進", "販促"]),
    ("接待・会議費", ["接待", "交際", "会議"]),
    ("物流・通信費", ["荷造", "運賃", "通信", "発送"]),
    ("旅費交通費", ["旅費", "交通"]),
    ("消耗品・事務用品", ["消耗品", "事務用品", "新聞図書"]),
    ("修繕・保守費", ["修繕", "保守", "メンテナンス"]),
    ("水道光熱費", ["水道", "光熱", "電気", "ガス"]),
    ("保険料", ["保険"]),
    ("支払手数料", ["支払手数料", "振込手数料", "決済手数料"]),
    ("租税公課", ["租税", "公課", "印紙"]),
    ("減価償却費", ["減価償却"]),
    ("研修・採用費", ["研修", "教育", "採用", "求人"]),
]


def _classify(kamoku: str) -> str:
    """1つの科目名がどのグループに属するかを返す。該当しなければ「その他」。"""
    for group_name, keywords in GROUP_DEFINITIONS:
        for kw in keywords:
            if kw in kamoku:
                return group_name
    return "その他"


def aggregate_breakdown(breakdown: Dict[str, float]) -> Dict[str, float]:
    """
    {科目名: 金額} を {グループ名: 合計金額} に集約する。

    Args:
        breakdown: 例 {"給料手当": 1084.5, "役員報酬": 306.0, "外注費": 382.9, ...}

    Returns:
        例 {"人件費": 1437.7, "外注・委託費": 382.9, ...}
        金額の小さい順ではなく、合計の大きい順（降順）で返す。
    """
    if not breakdown:
        return {}

    aggregated: Dict[str, float] = {}
    for kamoku, amount in breakdown.items():
        if amount is None:
            continue
        group = _classify(kamoku)
        aggregated[group] = aggregated.get(group, 0.0) + float(amount)

    # 金額の大きい順にソートして返す（表示順がそのまま重要度順になる）
    return dict(sorted(aggregated.items(), key=lambda x: x[1], reverse=True))


def aggregate_with_breakdown(breakdown: Dict[str, float]) -> Dict[str, Dict]:
    """
    グループ集約 + 各グループの内訳（元の科目）を保持して返す。
    UI で「人件費 1,437万」を展開すると「給料1,084 / 役員報酬306 / 福利125 / ...」が見えるようにする。

    Returns:
        {
          "人件費": {"total": 1437.7, "items": {"給料手当": 1084.5, "役員報酬": 306.0, ...}},
          "外注・委託費": {"total": 382.9, "items": {"外注費": 382.9}},
          ...
        }
        合計金額の降順。
    """
    if not breakdown:
        return {}

    result: Dict[str, Dict] = {}
    for kamoku, amount in breakdown.items():
        if amount is None:
            continue
        group = _classify(kamoku)
        if group not in result:
            result[group] = {"total": 0.0, "items": {}}
        result[group]["total"] += float(amount)
        result[group]["items"][kamoku] = float(amount)

    return dict(sorted(result.items(), key=lambda x: x[1]["total"], reverse=True))
