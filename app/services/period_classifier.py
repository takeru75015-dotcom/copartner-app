"""
期間文字列から「月次データ / 年次データ」を判定し、所属会計年度を推定する。

判定例：
  "2024年3月期"     → annual, fiscal_year="2024年3月期"
  "2024年3月"        → monthly, fiscal_year="2024年3月期"（決算月は社内設定 or デフォルト）
  "2025年8月期"     → annual
  "2025年8月"        → monthly, fiscal_year="2025年8月期"（決算月8月の会社）
  "令和6年3月期"    → annual
  "R6.3月"           → monthly
  "2025/05"          → monthly
"""
import re
from typing import Optional, Tuple


def classify_period(period: str, fiscal_close_month: int = None) -> Tuple[str, str]:
    """期間文字列を判定。
    戻り値: (period_type, fiscal_year)
      - period_type: 'annual' or 'monthly' or ''（判定不能）
      - fiscal_year: 所属する会計年度の文字列（月次の場合のみ意味あり、annualは period そのまま）

    fiscal_close_month: 会社の決算月（不明なら None。自動推定）
    """
    if not period:
        return ("", "")
    p = period.strip()

    # 「期」が含まれる → annual
    if "期" in p:
        return ("annual", p)

    # 数字パターン抽出
    # パターン1: "2024年3月" / "令和6年3月" / "R6.3月"
    # パターン2: "2024/03" / "2024-03"
    year, month = _extract_year_month(p)
    if year is None or month is None:
        return ("", "")

    # ここまで来たら月次データ
    # fiscal_year を推定：fiscal_close_month が分かればそれを基準、不明なら 3月期 をデフォルト
    if fiscal_close_month is None:
        # 学習サンプル：月次データの直近月を集めて統計から推定するロジックは別関数で
        fiscal_close_month = 3  # デフォルト 3月期

    fy = _compute_fiscal_year_label(year, month, fiscal_close_month)
    return ("monthly", fy)


def _extract_year_month(s: str) -> Tuple[Optional[int], Optional[int]]:
    """文字列から (西暦年, 月) を抽出"""
    # 令和X年Y月
    m = re.search(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月", s)
    if m:
        return (2018 + int(m.group(1)), int(m.group(2)))
    m = re.search(r"R\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return (2018 + int(m.group(1)), int(m.group(2)))
    # 平成X年Y月
    m = re.search(r"平成\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月", s)
    if m:
        return (1988 + int(m.group(1)), int(m.group(2)))
    m = re.search(r"H\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return (1988 + int(m.group(1)), int(m.group(2)))
    # YYYY年MM月
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # YYYY/MM or YYYY-MM
    m = re.search(r"(\d{4})[/\-](\d{1,2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (None, None)


def _compute_fiscal_year_label(year: int, month: int, fiscal_close_month: int) -> str:
    """西暦年・月・決算月から「YYYY年M月期」のラベルを返す。
    例：fiscal_close_month=8（8月決算）、2024年5月 → 2024年8月期（2023/9〜2024/8）
        fiscal_close_month=8、2024年9月 → 2025年8月期（2024/9〜2025/8）
        fiscal_close_month=3、2024年4月 → 2025年3月期（2024/4〜2025/3）
    """
    if month <= fiscal_close_month:
        # 同じ年度
        return f"{year}年{fiscal_close_month}月期"
    else:
        # 翌年度
        return f"{year+1}年{fiscal_close_month}月期"


def normalize_to_seireki(period: str) -> str:
    """期間文字列を西暦表記に正規化。
    例:
      "令和7年2月期（第50期）"   → "2025年2月期（第50期）"
      "令和6年8月期"              → "2024年8月期"
      "R6.3月"                    → "2024年3月"
      "平成31年3月期"             → "2019年3月期"
      "2024年3月期"               → "2024年3月期"（変化なし）
      "令和5年3月1日～令和6年2月29日（第49期）" → "2023年3月1日～2024年2月29日（第49期）"
    """
    if not period:
        return period
    s = period

    def _reiwa(m):
        year = 2018 + int(m.group(1))
        return f"{year}年"
    def _heisei(m):
        year = 1988 + int(m.group(1))
        return f"{year}年"
    def _showa(m):
        year = 1925 + int(m.group(1))
        return f"{year}年"

    # 令和X年 → YYYY年
    s = re.sub(r"令和\s*(\d{1,2})\s*年", _reiwa, s)
    s = re.sub(r"R\s*(\d{1,2})[\.\s年]", lambda m: f"{2018 + int(m.group(1))}年", s)
    # 平成X年 → YYYY年
    s = re.sub(r"平成\s*(\d{1,2})\s*年", _heisei, s)
    s = re.sub(r"H\s*(\d{1,2})[\.\s年]", lambda m: f"{1988 + int(m.group(1))}年", s)
    # 昭和X年 → YYYY年
    s = re.sub(r"昭和\s*(\d{1,2})\s*年", _showa, s)
    s = re.sub(r"S\s*(\d{1,2})[\.\s年]", lambda m: f"{1925 + int(m.group(1))}年", s)

    # 「YYYY年M月D日～YYYY年M月D日（第N期）」→「YYYY年M月期（第N期）」に短縮
    # 終了側の年月だけ拾い、第N期があれば残す
    range_pattern = re.compile(
        r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*[～~〜\-]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*\d{1,2}\s*日(\s*（[^）]+）)?"
    )
    m = range_pattern.search(s)
    if m:
        end_year = m.group(1)
        end_month = int(m.group(2))
        suffix = m.group(3) or ""  # （第49期）等
        s = range_pattern.sub(f"{end_year}年{end_month}月期{suffix}", s)

    # 「YYYY年M月～YYYY年M月」（日なし）も対応
    range_pattern_md = re.compile(
        r"\d{4}\s*年\s*\d{1,2}\s*月\s*[～~〜\-]\s*(\d{4})\s*年\s*(\d{1,2})\s*月(\s*（[^）]+）)?"
    )
    m2 = range_pattern_md.search(s)
    if m2:
        end_year = m2.group(1)
        end_month = int(m2.group(2))
        suffix = m2.group(3) or ""
        s = range_pattern_md.sub(f"{end_year}年{end_month}月期{suffix}", s)

    return s


def guess_fiscal_close_month(monthly_periods: list) -> int:
    """月次データ群から決算月を推定。
    - 最も多く現れる「年末位置」+1 で推定
    - データが少ない場合はデフォルト3月
    """
    if not monthly_periods:
        return 3
    from collections import Counter
    months = []
    for p in monthly_periods:
        _, m = _extract_year_month(p or "")
        if m:
            months.append(m)
    if not months:
        return 3
    # 一番遅い月 = 決算月の候補（その月までデータがあれば）
    # シンプル戦略：年度内の最後の月 = 決算月とする
    # 各年度（西暦）でグループ化、各年の最終月を集める
    from collections import defaultdict
    year_last = defaultdict(int)
    for p in monthly_periods:
        y, m = _extract_year_month(p or "")
        if y and m:
            if m > year_last[y]:
                year_last[y] = m
    if not year_last:
        return 3
    # 最終月の最頻値
    last_months = list(year_last.values())
    counter = Counter(last_months)
    return counter.most_common(1)[0][0]
