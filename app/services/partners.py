"""
F-08: 提携パートナー（保険・銀行）への送客導線
  - 財務プロファイルから、適切な提携先へのリード機会を抽出
  - 北村先生フィードバック（2026-05-01）の収益化導線アイデア由来
"""
from typing import Dict, List


def match_finance_partners(fd, ebitda: dict = None, working_capital: dict = None) -> List[Dict]:
    """
    財務状況から提携パートナー（銀行/保険/リース）への紹介機会を抽出。

    Returns:
        [
          {
            "partner_type": "銀行借換" / "法人保険" / "リファイナンス" / "リース",
            "title": "...",
            "trigger_reason": "...",  # なぜ今、これが刺さるか
            "estimated_benefit": "...",  # 期待される金額インパクト
            "next_step": "...",
          }
        ]
    """
    out = []
    revenue = getattr(fd, "revenue", 0) or 0
    operating_profit = getattr(fd, "operating_profit", 0) or 0
    net_profit = getattr(fd, "net_profit", 0) or 0
    interest_bearing_debt = getattr(fd, "interest_bearing_debt", 0) or 0
    equity = getattr(fd, "equity", 0) or 0
    total_assets = getattr(fd, "total_assets", 0) or 0
    cash = getattr(fd, "cash", 0) or 0
    employees = getattr(fd, "employees", 0) or 0

    # === 1. 借入リファイナンス機会 ===
    # 有利子負債÷EBITDA が3-7倍（リスケ手前）→ 金利見直し・期間延長で月次キャッシュ改善
    if ebitda and ebitda.get("debt_to_ebitda"):
        d2e = ebitda["debt_to_ebitda"]
        if 3 <= d2e <= 7 and interest_bearing_debt > 0:
            # 0.5%金利下げで年間 0.005 × 借入額 万円 改善（概算）
            est_save = round(interest_bearing_debt * 0.005, 0)
            out.append({
                "partner_type": "銀行借換・条件交渉",
                "title": "借入の金利見直し・期間延長交渉サポート",
                "trigger_reason": f"有利子負債÷EBITDA = {d2e}倍。リスケ手前ゾーン。複数行打診で金利・期間条件の改善余地あり。",
                "estimated_benefit": f"金利0.5%下げ想定で年 約{est_save}万円のキャッシュ改善",
                "next_step": "提携金融機関3-5行への一括打診 → 最有利条件をピックアップ",
                "fee_model": "成功報酬型（改善金利×残期間×借入額の数%）",
            })

    # === 2. 黒字社向けの法人保険・退職金準備 ===
    # 営業利益>1000万円 かつ 純利益>0 → 退職金準備・福利厚生型保険で節税×将来準備
    if operating_profit > 1000 and net_profit > 0:
        out.append({
            "partner_type": "法人保険（退職金準備）",
            "title": "役員退職金・福利厚生プランの設計",
            "trigger_reason": f"営業利益 {operating_profit:,.0f}万円の黒字着地。納税前に退職金準備・福利厚生型保険で節税×将来準備の二段構え可能。",
            "estimated_benefit": "実効税率33%換算で年 200-500万円の課税繰延（規模により変動）",
            "next_step": "提携保険代理店（独立系）から3社プラン比較 → 必要保障額に応じた組み合わせ提案",
            "fee_model": "保険会社からの代理店手数料のみ（顧客側は保険料のみ）",
        })

    # === 3. 設備投資余力ある黒字社 → リース・割賦 ===
    # 営業利益>500万円 かつ 現預金が月商1.5ヶ月以上 → 設備投資の選択肢を広げる
    monthly_revenue = revenue / 12 if revenue else 0
    if operating_profit > 500 and cash > monthly_revenue * 1.5 and revenue > 5000:
        out.append({
            "partner_type": "リース・割賦",
            "title": "設備投資のリース／割賦スキーム提案",
            "trigger_reason": "黒字＋現預金余裕ありの条件下で、設備投資をキャッシュアウト最小化で実行する選択肢。",
            "estimated_benefit": "中小企業経営強化税制と組み合わせで即時償却 → 当期の節税効果を最大化",
            "next_step": "提携リース会社 2-3社の見積比較 → 税制優遇との合算でROI試算",
            "fee_model": "リース会社からの紹介手数料",
        })

    # === 4. 債務超過・自己資本不足 → 増資・私募債コンサル ===
    if equity < 0 or (total_assets > 0 and equity / total_assets < 0.10):
        out.append({
            "partner_type": "資本性ローン・増資",
            "title": "資本性ローン／第三者割当増資の検討",
            "trigger_reason": (
                "債務超過水準" if equity < 0
                else f"自己資本比率 {equity/total_assets*100:.1f}% は銀行警戒ゾーン。"
            ) + "通常融資より資本性資金で財務基盤を立て直す選択肢が現実的。",
            "estimated_benefit": "資本性ローンは負債計上だが資本扱い → 自己資本比率改善で次の融資余力を回復",
            "next_step": "提携の認定支援機関経由で日本政策金融公庫の資本性ローン適用可否を確認",
            "fee_model": "認定支援機関の成功報酬型（着手金あり）",
        })

    return out
