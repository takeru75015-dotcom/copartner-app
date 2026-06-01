"""
AI Provider 抽象化レイヤー
環境変数 AI_PROVIDER で Gemini / Claude を切替可能
  - AI_PROVIDER=gemini (デフォルト、無料枠・速い)
  - AI_PROVIDER=claude (高精度、先生デモ・本番向け)
"""
import os
import json
from pathlib import Path
from dotenv import load_dotenv
from .database import FinancialData
from .services.benchmark import compare_to_benchmark, format_benchmark_text, extract_competitive_strengths
from .services.cash_analysis import compute_working_capital, compute_burn_rate, compute_ebitda
from .services.score import compute_health_score
from .services.subsidy import match_subsidies
from .services.partners import match_finance_partners
from .services.affiliate_match import attach_affiliates_to_result

# app/.env を明示的にロード（カレントディレクトリに依存しない）
# override=True で既存環境変数を上書き（Git Bashなどシェル側に残った旧値を防ぐ）
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# 遅延初期化
_gemini_model = None
_claude_client = None


def _get_gemini():
    """Gemini クライアントの遅延初期化"""
    global _gemini_model
    if _gemini_model is None:
        import google.generativeai as genai
        genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
        _gemini_model = genai.GenerativeModel(GEMINI_MODEL)
    return _gemini_model


def _get_claude():
    """Claude クライアントの遅延初期化"""
    global _claude_client
    if _claude_client is None:
        from anthropic import Anthropic
        _claude_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    return _claude_client


def _call_llm(prompt: str, provider: str = None) -> str:
    """
    LLM 抽象化呼び出し。
    provider引数で個別指定可。未指定なら環境変数 AI_PROVIDER に従う。
    """
    p = (provider or AI_PROVIDER).lower()

    if p == "claude":
        client = _get_claude()
        message = _call_claude_with_retry(
            client,
            model=CLAUDE_MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    else:  # gemini
        model = _get_gemini()
        response = model.generate_content(prompt)
        return response.text.strip()


def _parse_json_response(text: str) -> dict:
    """両プロバイダ共通の JSON 抽出。途中切れ・文法エラーでも可能な限り救済"""
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    # 試行1: そのままパース
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        pass

    # 試行2: 途中切れの復旧（末尾の壊れた部分を切り捨て）
    salvaged = _salvage_truncated_json(text)
    if salvaged:
        try:
            return json.loads(salvaged)
        except json.JSONDecodeError:
            pass

    # 試行3: 文法ミスの自動修復（末尾カンマ、未エスケープ改行等）
    repaired = _repair_common_json_errors(text)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    # 試行4: 修復 + 途中切れ復旧
    if repaired:
        salvaged2 = _salvage_truncated_json(repaired)
        if salvaged2:
            try:
                return json.loads(salvaged2)
            except json.JSONDecodeError:
                pass

    # ここまで失敗：元エラーを投げる
    json.loads(text)  # 例外を再発火させて detail を出す
    return {}  # unreachable


def _repair_common_json_errors(text: str) -> str | None:
    """AI生成JSONの典型的な構文エラーを修復。
    - 末尾カンマ（オブジェクト/配列の最後）
    - キーと値の間の改行/空白を許容
    - 中括弧の直前のカンマ余り
    - ダブルクォート内の生改行（文字列途中改行）を \\n に置換
    """
    import re as _re
    s = text

    # 末尾カンマ: ,} や ,] パターンを削除（複数回適用）
    for _ in range(5):
        new = _re.sub(r',(\s*[}\]])', r'\1', s)
        if new == s:
            break
        s = new

    # ダブルクォート文字列の中に生の改行が入っているケースを修復
    # 雑だが効果的：奇数番目のクォート区間内の \n を \\n に置換
    out = []
    in_str = False
    escape = False
    for ch in s:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == '\\':
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch == '\n':
            out.append('\\n')
            continue
        if in_str and ch == '\r':
            out.append('\\r')
            continue
        out.append(ch)
    s = ''.join(out)

    return s if s != text else None


# ---------------------------------------------------------------------------
# 会社の状態診断（規模 × 健全性 × 禁止打ち手）
# ---------------------------------------------------------------------------
# AI に「規模に合わない提案（売上1億の会社に IPO 等）」「無理な課題創出」を
# させないために、Python 側で事前に会社のタイプを判定してプロンプトに渡す。
_SCALE_LABEL = {
    "tiny": "零細（売上1億円未満）",
    "small": "小規模（売上1-3億円）",
    "mid": "中規模（売上3-10億円）",
    "upper_mid": "中堅（売上10-30億円）",
    "large": "大規模（売上30億円超）",
}


def _diagnose_company_state(fd, ebitda: dict, burn: dict, wc: dict) -> dict:
    """規模・健全性・買収余力・禁止提案リストを返す。"""
    revenue = getattr(fd, "revenue", 0) or 0
    cash = getattr(fd, "cash", 0) or 0
    op_cf_monthly = (burn or {}).get("operating_monthly") or 0
    op_cf_annual = op_cf_monthly * 12
    debt_to_ebitda = (ebitda or {}).get("debt_to_ebitda") or 99
    ebitda_margin = (ebitda or {}).get("ebitda_margin") or 0
    cash_months = (wc or {}).get("cash_months_of_sales") or 0
    operating_profit = getattr(fd, "operating_profit", 0) or 0
    equity = getattr(fd, "equity", 0) or 0
    total_assets = getattr(fd, "total_assets", 0) or 1
    equity_ratio = (equity / total_assets * 100) if total_assets else 0

    # 規模バケット（万円単位）
    if revenue < 10000:
        scale = "tiny"
    elif revenue < 30000:
        scale = "small"
    elif revenue < 100000:
        scale = "mid"
    elif revenue < 300000:
        scale = "upper_mid"
    else:
        scale = "large"

    # 健全性
    if cash_months > 3 and op_cf_annual > 0 and debt_to_ebitda < 2:
        health = "excess_cash"
    elif op_cf_annual <= 0 or cash_months < 1.5:
        health = "struggling"
    else:
        health = "balanced"

    # 買収余力（現金 + 営業CF2年分。万円）
    buyout_capacity = cash + max(0, op_cf_annual * 2)

    # 禁止提案リスト
    forbidden = []
    if scale in ("tiny", "small"):
        forbidden += ["IPO", "上場準備", "大型M&A（買収額3億円超）"]
    if scale == "mid":
        forbidden += ["IPO（時期尚早）"]
    if buyout_capacity < 7000:  # 7,000万円
        forbidden += ["他社買収（買収余力不足）", "M&A買収側"]
    # IPO 適格性（売上5億超 × 営業利益率7%超 × 自己資本比率30%超 すべて満たさないと NG）
    ipo_ok = (revenue >= 50000 and ebitda_margin >= 7 and equity_ratio >= 30
              and operating_profit > 0)
    if not ipo_ok and "IPO" not in forbidden:
        forbidden.append("IPO（適格性未達）")
    # 健全性悪化時は攻め系も封じる
    if health == "struggling":
        forbidden += ["他社買収", "M&A買収側", "IPO", "大型新規事業（投資3,000万円超）"]

    # 事業承継・M&A売却は社長年齢/後継者の事前ヒアリング必須（このタイミングでは禁止）
    forbidden += [
        "事業承継（社長年齢・後継者ヒアリング前）",
        "M&A売却（社長意向ヒアリング前）",
    ]

    # 重複除去
    forbidden = list(dict.fromkeys(forbidden))

    return {
        "scale": scale,
        "scale_label": _SCALE_LABEL[scale],
        "health": health,
        "health_label": {
            "excess_cash": "余剰キャッシュ過多（攻めモード）",
            "struggling": "経営苦境（守りモード）",
            "balanced": "バランス（複合モード）",
        }[health],
        "buyout_capacity_man_yen": int(buyout_capacity),
        "forbidden_proposals": forbidden,
        "ipo_eligible": ipo_ok,
        "_metrics": {
            "revenue": revenue,
            "cash": cash,
            "op_cf_annual": int(op_cf_annual),
            "cash_months": cash_months,
            "debt_to_ebitda": debt_to_ebitda,
            "ebitda_margin": ebitda_margin,
            "equity_ratio": round(equity_ratio, 1),
        },
    }


def _diagnose_business_phase(fd, historical_data: list, breakdown: dict) -> dict:
    """3年トレンドから経営フェーズを判定する。
    - 成長期: 売上↑↑ + 営業利益↑
    - 投資期: 売上→ + 減価償却↑↑（固定資産↑ or 現金不変）★ 営業利益悪化を「戦略的」と扱う
    - 回収期: 売上↑ + 減価償却→ + 営業利益↑
    - 停滞期: 売上→ + 営業利益→
    - 再建期: 売上↓ + 営業利益↓ + 現金↓
    """
    if not historical_data or len(historical_data) < 2:
        return {"phase": "unknown", "label": "判定不能（履歴2期以上必要）", "evidence": []}

    sorted_hd = sorted(historical_data, key=lambda x: x.period or "")
    # 直近3期に絞る
    recent = sorted_hd[-3:]
    if len(recent) < 2:
        return {"phase": "unknown", "label": "判定不能", "evidence": []}

    # 減価償却を各期から取り出す
    def _dep_of(fd_x) -> float:
        try:
            bd = json.loads(getattr(fd_x, "breakdown_json", "{}") or "{}")
        except Exception:
            return 0.0
        total = 0.0
        for section in ("selling_expenses_detail", "cost_of_sales_detail"):
            d = bd.get(section) or {}
            for k, v in d.items():
                if not isinstance(v, (int, float)) or k.startswith("__"):
                    continue
                if any(kw in k for kw in ["減価償却", "償却費"]):
                    total += v
        return total

    metrics = []
    for h in recent:
        metrics.append({
            "period": h.period,
            "revenue": h.revenue or 0,
            "op": h.operating_profit or 0,
            "cash": h.cash or 0,
            "fixed_assets": (h.total_assets or 0) - (h.current_assets or 0),
            "depreciation": _dep_of(h),
        })

    # 成長率
    evidence = []
    if len(metrics) >= 2:
        prev, curr = metrics[-2], metrics[-1]
        rev_g = ((curr["revenue"] - prev["revenue"]) / abs(prev["revenue"]) * 100) if prev["revenue"] else 0
        op_g = ((curr["op"] - prev["op"]) / abs(prev["op"]) * 100) if prev["op"] else 0
        dep_g = ((curr["depreciation"] - prev["depreciation"]) / max(abs(prev["depreciation"]), 1) * 100) if prev["depreciation"] else (999 if curr["depreciation"] > 0 else 0)
        cash_diff = curr["cash"] - prev["cash"]
        fa_diff = curr["fixed_assets"] - prev["fixed_assets"]
        evidence.append(f"売上成長率（直前期→当期）: {rev_g:+.1f}%")
        evidence.append(f"営業利益成長率: {op_g:+.1f}%")
        evidence.append(f"減価償却費の増減: {dep_g:+.1f}%（{prev['depreciation']:,.0f}→{curr['depreciation']:,.0f}万円）")
        evidence.append(f"現預金の増減: {cash_diff:+,.0f}万円")
        evidence.append(f"固定資産（総資産-流動資産）の増減: {fa_diff:+,.0f}万円")

        # フェーズ判定（優先順位順）
        phase = "stable"

        # 投資期: 減価償却が大幅増（+50%以上 or 前期0→当期 1,000万円超）かつ 現金が極端に減ってない
        big_dep_jump = (
            (prev["depreciation"] > 0 and curr["depreciation"] / max(prev["depreciation"], 1) >= 1.5)
            or (prev["depreciation"] < 500 and curr["depreciation"] >= 1000)
        )
        if big_dep_jump and cash_diff > -prev["cash"] * 0.30:
            phase = "investment"

        # 再建期: 売上↓ かつ 営業利益↓ かつ 現金↓
        elif rev_g < -5 and op_g < -10 and cash_diff < 0:
            phase = "rebuilding"

        # 成長期: 売上↑10%超 かつ 営業利益↑
        elif rev_g > 10 and op_g > 0:
            phase = "growth"

        # 回収期: 売上↑ かつ 減価償却→（ほぼ横ばい） かつ 営業利益↑
        elif rev_g > 5 and abs(dep_g) < 20 and op_g > 0:
            phase = "harvest"

        # 停滞期: 売上ほぼ横ばい かつ 営業利益ほぼ横ばい
        elif abs(rev_g) < 5 and abs(op_g) < 20:
            phase = "stagnation"

    labels = {
        "investment": "投資期（営業利益悪化は戦略的判断。課題化禁止）",
        "growth": "成長期（売上拡大期）",
        "harvest": "回収期（過去投資の利益化）",
        "stagnation": "停滞期（横ばい・打ち手探索）",
        "rebuilding": "再建期（売上・利益・現金 三重悪化）",
        "stable": "通常運営",
        "unknown": "判定不能",
    }
    return {
        "phase": phase,
        "label": labels.get(phase, phase),
        "evidence": evidence,
        "metrics_recent": metrics,
    }


# 業界別の必要運転資金（月商の何ヶ月分か）
_INDUSTRY_WORKING_CAPITAL_MONTHS = {
    "運輸": 2.5, "運送": 2.5, "物流": 2.5, "バス": 2.5, "タクシー": 2.5,
    "製造": 2.5, "工業": 2.5, "メーカー": 2.5,
    "建設": 3.0, "建築": 3.0, "土木": 3.0, "工事": 3.0,
    "卸売": 2.0, "商社": 2.0,
    "小売": 1.0, "EC": 1.0, "通販": 1.0,
    "飲食": 1.0, "外食": 1.0, "レストラン": 1.0,
    "サービス": 1.5, "コンサル": 1.5, "IT": 1.5, "ソフトウェア": 1.5, "SaaS": 1.5,
    "不動産": 2.0, "宿泊": 1.5, "ホテル": 1.5,
    "医療": 1.5, "介護": 1.5, "教育": 1.0,
}


def _get_required_working_months(industry: str) -> float:
    """業界名から必要運転資金月数を判定（曖昧マッチ）"""
    if not industry:
        return 1.5
    industry_lc = industry.lower()
    for key, months in _INDUSTRY_WORKING_CAPITAL_MONTHS.items():
        if key in industry or key.lower() in industry_lc:
            return months
    return 1.5  # default


def _compute_idle_cash(fd, burn: dict, wc: dict, industry: str = "") -> dict:
    """余剰キャッシュ（=寝てるカネ）を業界別倍率で算出する。
    余剰 = 現預金 - 必要運転資金(業界別月商×倍率) - 借入1年返済分
    """
    cash = getattr(fd, "cash", 0) or 0
    revenue = getattr(fd, "revenue", 0) or 0
    monthly_revenue = revenue / 12 if revenue else 0

    months = _get_required_working_months(industry)
    required_working = monthly_revenue * months
    debt_repay_yearly = ((burn or {}).get("debt_repayment_monthly_est") or 0) * 12

    idle = cash - required_working - debt_repay_yearly

    return {
        "cash_total": int(cash),
        "industry_used": industry or "(不明)",
        "required_months": months,
        "required_working_capital": int(required_working),
        "debt_repay_buffer_yearly": int(debt_repay_yearly),
        "idle_cash": int(idle),
        "idle_cash_label": (
            "潤沢（次の戦略投資の予算として活用必須・寝かせ過ぎ）" if idle >= monthly_revenue * 1.5
            else "中程度（節税×小型投資の余地あり）" if idle >= monthly_revenue * 0.5
            else "限定的（守り優先）" if idle >= 0
            else "不足（資金繰り注意）"
        ),
    }


def _format_idle_cash_for_prompt(idle_info: dict) -> str:
    """余剰キャッシュ情報をプロンプト用に整形"""
    if not idle_info or idle_info.get("idle_cash") is None:
        return ""
    return f"""
【💰 余剰キャッシュ分析（寝てるカネの活用可能額）】
- 現預金合計: {idle_info['cash_total']:,}万円
- 業界判定: {idle_info['industry_used']} → 必要運転資金は月商の {idle_info['required_months']} ヶ月分とする
- 必要運転資金: {idle_info['required_working_capital']:,}万円
- 借入1年分の返済バッファ: {idle_info['debt_repay_buffer_yearly']:,}万円
- ★ 余剰キャッシュ: {idle_info['idle_cash']:,}万円（{idle_info['idle_cash_label']}）

【余剰キャッシュ活用ルール（社長が「キャッシュ寝てる」と感じる規模なら必須提案）】
余剰が 5,000万円超 の場合、growth_opportunities に「余剰キャッシュ活用シナリオ」を **複数の選択肢で並列提示** すること。
社長は「何に使うか決めかねている」状態が多いので、複数の選択肢を比較できる形にする。

【選択肢の候補（規模・業界・健全性に応じて4-6件選び、それぞれ rationale + impact を書く）】
1. **新規事業 / FC加盟**（category=new_business or franchise）
   - 既存事業とシナジーある分野で1,000-5,000万円規模の小型新規
   - FC加盟は初期投資ハードル低く、本業の余力を活かしやすい
2. **小型M&A（買収側）**（category=m_and_a）
   - 同業・隣接業の小規模事業を買収（5,000万〜2億円規模）
   - 🚨 **事業承継・引継ぎ補助金**（M&A型・経営革新型）の活用で買収費用の最大6/10（上限800万）が補助される旨を必ず明記
   - 自社事業の地理拡大・横展開に有効。買収余力が買収額の70%以下なら提案しない
3. **有価証券投資 / 運用**（category=investment）
   - 余剰の一部（30-50%程度）を中期運用に回す（社債・投信・株式）
   - ただし本業に直結しない投資はリスク・税務を踏まえ慎重に。「すぐ使わないが3-5年後に使う予定」の資金が対象
4. **不動産投資**（category=investment）
   - 自社事業との関連性次第。運輸・物流業なら駐車場・倉庫・整備工場の取得は事業連動で有効
   - 純粋な収益不動産は本業から離れるためリスク・利回り（4-6%）を明示。借入併用前提も検討
5. **設備投資強化**（category=investment）
   - 現投資期の効果を最大化する追加投資（既存事業の拡張・効率化）
   - 中小企業経営強化税制・即時償却の活用で節税効果も
6. **借入返済前倒し**（category=finance）
   - 金利水準が高い借入（特に金利3%超）なら前倒し返済で金利負担削減
   - 金利が低い（1%台）借入は手元現金を維持し別投資に回す方が合理的
7. **社員還元・採用強化**（category=employee_return）
   - 決算賞与・福利厚生・採用予算（業界人手不足の場合）
8. **節税×投資のセット**（category=tax_optimization + investment）
   - 設備投資 + 即時償却 / 倒産防止共済 等

【選択肢提示のフォーマット】
- 各選択肢に **「投資額レンジ」「想定リターン」「リスク」「想定回収期間」** を明記
- 最後に **rank=1 として「複数選択肢を比較する社内会議の設計」** を提示
  例：「次回月次会議でA-Dの選択肢を比較し、社長と一緒に方向性を決定」

【🚨 整合性チェック】
- 余剰がマイナスなら「資金繰り改善」を rank1 で課題化（攻めの提案は出さない）
- 投資期の場合は「新規大型投資」と「既存投資の効果検証」のバランスを取る
- 業界特性を必ず反映（運輸業に「飲食店向けFC」を提案するのはNG。隣接業優先）
"""
def _format_business_phase_for_prompt(phase_info: dict) -> str:
    """経営フェーズ判定結果をプロンプト用に整形"""
    phase = phase_info.get("phase", "unknown")
    if phase == "unknown":
        return ""
    label = phase_info.get("label", "")
    evidence = phase_info.get("evidence", [])

    text = [
        "",
        "【📈 経営フェーズ判定（3年トレンド分析・必ず読め）】",
        f"フェーズ: **{label}**",
        "判定根拠:",
    ]
    for e in evidence:
        text.append(f"  - {e}")

    if phase == "investment":
        text.extend([
            "",
            "🎯 **投資期で必須の打ち手（growth_opportunities に必ず1件以上含める）**",
            "1. **投資効果の見える化（CFO代行 or データ可視化BPO の紹介）**（category=cfo_outsource、executor=referral_partner）",
            "   投資した資産の稼働率・売上貢献・コスト削減効果を月次で追跡。",
            "   🚨 税理士が自分でやるのではなく、**CFO代行サービス（月10-30万円）** か **データ可視化BPO（kintone代行/Excel Pro、月3-10万）** を税理士が紹介し、社内データ入力＋外部分析で運用。",
            "   actions の最初は『社内で Excel/SaaS でやれるか確認、難しければ CFO代行 or BPO を紹介』にする",
            "2. **次の投資判断 or 余剰キャッシュ活用**（category=new_business / franchise / investment / m_and_a）",
            "   今期も現金は積み上がる傾向。次に何に投資するか／投資せず還元するかの選択肢を提示",
            "",
            "🚨 **投資期の重要ルール（誤判定を防ぐ）**",
            "営業利益の前期比悪化は **戦略的投資の結果である可能性が高い**（過去に投資した資産の減価償却本格化）。",
            "ただし「本当に戦略的な投資だったか」「想定通りの回収シナリオがあるか」は **社長への確認が必須**。",
            "AIが勝手に決め打ちせず、以下の流れで整理せよ：",
            "",
            "✅ やるべきこと：",
            "  1. **observation_notes に『投資期と推定される旨』と『要確認事項』を必ず1件以上書く**",
            "     例：「営業利益▲75% は減価償却+7,917万円が主因。戦略的投資かどうかを社長に確認」",
            "  2. **hearing_sheet.growth_opportunity に投資判断ヒアリングを必須化**：",
            "     - 何に投資したか（バス・設備・システム）",
            "     - 投資の回収シナリオ（売上増/コスト削減/新規事業）はあるか",
            "     - 当初想定通りに進んでいるか",
            "  3. **prioritized_problems に『営業利益悪化』を書く場合は、必ず以下の3要素を含む**：",
            "     - fact に EBITDA を併記して『現金創出力は維持』を明示",
            "     - confirmation_needed に「投資判断の戦略性」「回収計画の有無」を含める",
            "     - title は「悪化」「急減」を避け、「投資の回収検証」など中立的に",
            "  4. 単なる「営業利益が悪化」だけを課題化するのは禁止。必ず投資視点の文脈を添える",
            "",
            "✅ growth_opportunities の方向性：",
            "  - 『投資の回収を加速する打ち手』（稼働率向上・営業強化）",
            "  - 『次の投資判断の意思決定支援』（KPI設計・効果検証）",
            "  - 『投資による減価償却節税効果の最大化』（即時償却の活用）",
        ])
    elif phase == "rebuilding":
        text.append("→ struggling 寄りの判定。再建打ち手を優先")
    elif phase == "growth":
        text.append("→ 成長加速・組織拡大・人材投資が中心テーマ")
    elif phase == "harvest":
        text.append("→ 過去投資の回収局面。利益還元・次の投資判断")
    elif phase == "stagnation":
        text.append("→ 横ばい打破の打ち手探索。差別化・新規開拓・効率化")

    return "\n".join(text) + "\n"


def _format_state_for_prompt(state: dict) -> str:
    """diagnose の結果をプロンプト埋め込み用テキストに整形"""
    forbidden_text = " / ".join(state["forbidden_proposals"]) if state["forbidden_proposals"] else "（なし）"
    confirmations = state.get("required_confirmations") or []
    confirmations_block = ""
    if confirmations:
        confirmations_block = "\n📋 社長への確認が必須な事項（hearing_sheet と observation_notes に必ず反映）:\n"
        for c in confirmations:
            confirmations_block += f"  - {c}\n"
    return f"""【🚨 会社の現状診断（Python事前計算 / 必ず守れ）】
規模: {state['scale_label']}
健全性: {state['health_label']}
買収余力: {state['buyout_capacity_man_yen']:,}万円（=現金+営業CF2年分）
🚫 出してはいけない提案: {forbidden_text}{confirmations_block}

【規模別の打ち手ガイド】
- 零細（<1億）: 節税・規程整備・少額の社員還元・補助金が中心。IPO/大型M&A/大型投資 NG
- 小規模（1-3億）: 節税・人材1-2名採用・小型設備・福利厚生・FC加盟検討。IPO/他社買収 NG
- 中規模（3-10億）: 新規事業・人材拡大・小型M&A（買収余力次第）・事業承継。IPO 時期尚早
- 中堅（10-30億）: 上場準備検討・M&A・新規事業・組織整備
- 大規模（30億超）: 全部OK

【🚨 規模整合の自己検証（必須）】
- forbidden_proposals に含まれる提案を growth_opportunities に書いたら無効。出さない方がマシ
- 売上1億の会社に「IPO検討」を書いていないか？
- 現金1,000万円の会社に「他社買収」を書いていないか？
- これらが出ていたら quality_check.warnings に「規模不整合」を記録し、その提案を削除"""


def _salvage_truncated_json(text: str) -> str | None:
    """
    出力が途中で切れた JSON を、直前の完全な状態まで巻き戻して修復。
    文字列途中、配列途中、オブジェクト途中で切れているケースに対応。
    """
    # 最後の `}` までを残す（オブジェクト最後完結部）
    last_brace = text.rfind('}')
    if last_brace < 0:
        return None
    candidate = text[:last_brace + 1]

    # 括弧の open/close 数を数えて、不足分を閉じる
    opens_curly = candidate.count('{')
    closes_curly = candidate.count('}')
    opens_square = candidate.count('[')
    closes_square = candidate.count(']')

    # 文字列内の括弧は数えない方が正確だが、ざっくり対処
    missing_curly = max(0, opens_curly - closes_curly)
    missing_square = max(0, opens_square - closes_square)

    # 末尾のカンマを除去
    candidate = candidate.rstrip().rstrip(',')
    # 配列 > オブジェクトの順で閉じる
    candidate += ']' * missing_square + '}' * missing_curly
    return candidate


# ---------------------------------------------------------------------------
# PDF 抽出スキーマ（テキスト版・Vision版で共通利用）
# ---------------------------------------------------------------------------
_PDF_EXTRACTION_SCHEMA = """**重要：決算書には本表（損益計算書・貸借対照表）の他に、明細書・内訳書（販管費明細、売上原価明細、売上内訳 等）が含まれていることが多いです。これらの内訳も必ず抽出してください。** 提案の精度を左右する最重要データです。

【🚨 抽出後の自己検証（必須）】
JSON を返す前に、以下を自己検証してください：
1. **粗利 ≒ 売上 − 原価** か（5%以内）
2. **営業利益 ≒ 粗利 − 販管費** か
3. **流動資産合計 ≒ 流動資産内訳の合算** か（5%以内）
4. **負債+純資産 ≒ 総資産** か（貸借バランス）
5. **棚卸資産は全項目の合算か**（商品・製品・原材料・仕掛品・貯蔵品 を**全部足したか**）
6. **売掛金は売上債権全項目の合算か**（売掛金+受取手形+電子記録債権）
7. **単位は万円で統一されているか**（千円・百万円の混在ミスがないか）
不整合があれば、validation フィールドに具体的に記載してください。

以下のJSON形式で抽出してください。単位は万円に統一してください。
千円単位の場合は10で割る、円単位の場合は10000で割る、百万円の場合は100倍してください。
数値が見つからない場合は 0 にしてください。

{
  "period": "<決算期 例: 2024年3月期>",
  "revenue": <売上高（万円）>,
  "cost_of_sales": <売上原価（万円）>,
  "gross_profit": <売上総利益（万円）>,
  "selling_expenses": <販管費（万円）>,
  "operating_profit": <営業利益（万円）>,
  "ordinary_profit": <経常利益（万円）>,
  "net_profit": <当期純利益（万円）>,
  "prev_revenue": <前期売上高（万円、あれば）>,
  "prev_operating_profit": <前期営業利益（万円、あれば）>,

  "total_assets": <総資産（万円、B/S にあれば）>,
  "current_assets": <流動資産（万円）>,
  "cash": <現金及び預金（万円）>,
  "receivables": <売上債権の合計（売掛金+受取手形+電子記録債権を**全部足す**）（万円）>,
  "inventory": <棚卸資産の合計（商品+製品+原材料+仕掛品+貯蔵品+半製品 等を**全部足す**。1項目だけにしない）（万円）>,
  "total_liabilities": <負債合計（万円）>,
  "current_liabilities": <流動負債（万円）>,
  "interest_bearing_debt": <有利子負債：長期借入金+短期借入金+社債（万円）>,
  "equity": <純資産・自己資本（万円）>,

  "employees": <従業員数（人数、見つからなければ 0）>,

  "breakdown": {
    "selling_expenses_detail": {
      "__comment__": "販管費明細書から項目別に抽出。人件費・役員報酬・給料手当・法定福利費・賃借料・広告宣伝費・通信費・旅費交通費・水道光熱費・消耗品費・支払手数料・外注費・減価償却費 等",
      "<項目名>": <金額（万円）>
    },
    "cost_of_sales_detail": {
      "__comment__": "売上原価明細書があれば。期首棚卸・当期仕入・期末棚卸・外注加工費 等",
      "<項目名>": <金額（万円）>
    },
    "revenue_detail": {
      "__comment__": "売上内訳（部門別・商品別・顧客区分別）があれば",
      "<セグメント名>": <金額（万円）>
    },
    "non_operating_income": {
      "__comment__": "営業外収益の内訳（受取利息・雑収入・受取家賃 等）",
      "<項目名>": <金額（万円）>
    },
    "non_operating_expenses": {
      "__comment__": "営業外費用の内訳（支払利息・雑損失 等）",
      "<項目名>": <金額（万円）>
    },
    "current_assets_detail": {
      "__comment__": "流動資産の内訳を全て抽出。現金及び預金・受取手形・売掛金・有価証券・商品（製品）・仕掛品・原材料・貯蔵品・前払費用・前渡金（前払金）・未収入金・未収収益・短期貸付金・仮払金・繰延税金資産（流動）・その他流動資産・貸倒引当金（マイナス値）",
      "<項目名>": <金額（万円）>
    },
    "fixed_assets_detail": {
      "__comment__": "固定資産の主要項目（有形・無形・投資その他）の内訳。建物・機械装置・土地・ソフトウェア・投資有価証券・関係会社株式・長期貸付金・繰延税金資産（固定）・敷金保証金 等",
      "<項目名>": <金額（万円）>
    },
    "current_liabilities_detail": {
      "__comment__": "流動負債の内訳を全て抽出。支払手形・買掛金・短期借入金・1年以内返済予定長期借入金・未払金・未払費用・未払法人税等・未払消費税・前受金・預り金・賞与引当金 等",
      "<項目名>": <金額（万円）>
    },
    "fixed_liabilities_detail": {
      "__comment__": "固定負債の内訳。長期借入金・社債・退職給付引当金・繰延税金負債（固定） 等",
      "<項目名>": <金額（万円）>
    }
  },

  "confidence": "<抽出の確信度: 高/中/低>",
  "validation": {
    "pl_consistent": <true/false  P/L整合性 OK か>,
    "bs_balanced": <true/false  貸借バランス OK か>,
    "ca_breakdown_matches": <true/false  流動資産合計と内訳合計が一致するか>,
    "inventory_summed": <true/false  棚卸資産を全項目合算したか>,
    "receivables_summed": <true/false  売上債権を全項目合算したか>,
    "unit_consistent": <true/false  単位が万円で統一されているか>,
    "issues": ["<不整合があれば具体的に列挙>"]
  }
}"""


# ---------------------------------------------------------------------------
# F-01-V: PDFバイナリ → 財務データ抽出（Claude Vision による画像PDF対応）
# ---------------------------------------------------------------------------
def extract_ledger_summary_from_pdf(pdf_bytes: bytes, filename: str, doc_type: str = "ledger") -> str:
    """
    総勘定元帳・固定資産台帳・補助元帳のPDFから AI 分析に使える要点を抽出。
    北村先生FB対応：質問が多すぎるので、元帳・台帳を読ませて自動回答化する。
    """
    import base64
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    if doc_type == "fixed_assets":
        focus = """【固定資産台帳の読み方】
- 主要な固定資産（金額の大きい順 TOP10）
- 取得時期・取得価額・現在簿価・耐用年数
- 償却方法と当期償却額
- 用途（事業に直接使用 / 役員社宅 / 投資用 / 遊休資産）
- 古い資産で減価償却が終わっているもの（簿価1円や少額）
- 売却・除却の検討対象になりそうな遊休資産
- 修繕費が多い資産（買い替え検討の示唆）"""
    elif doc_type == "aux_ledger":
        focus = """【補助元帳の読み方】
- 売掛金の取引先別残高（上位10社、回収状況、長期滞留先）
- 買掛金の取引先別残高（仕入先集中度、支払条件）
- 借入金の銀行別残高・金利・返済期日
- 役員借入金・役員貸付金の有無と金額
- 仮払金・仮受金・前払費用の中身（不明瞭な計上の有無）"""
    else:  # ledger 総勘定元帳
        focus = """【総勘定元帳の読み方】
- 販管費の主要科目（月別推移、特に大きな変動）
- 旅費交通費・接待交際費・会議費の合計と頻度（営業活動の活発度）
- 外注費・業務委託費の内訳（どんな業務を外注しているか）
- 保険料・福利厚生費の構成（法人保険の有無）
- 役員報酬の月額・賞与の有無
- 異常な仕訳・期末調整の大きな計上"""

    prompt = f"""このPDFは中小企業の {doc_type}（{filename}）です。
税理士が顧問先の社長と面談するために、決算書だけでは見えない以下の情報を抽出してください。

{focus}

出力ルール：
- プレーンテキスト・日本語、JSONではない
- 見出し+箇条書きで構造化
- 数字は万円単位（元データが円ならそのまま記載＋括弧で万円換算）
- 不明な点は推測せず「不明」と書く
- 全体で 3000 文字以内
- 機密情報（個人名・口座番号）は出力しない
"""

    client = _get_claude()
    message = _call_claude_with_retry(
        client,
        model=CLAUDE_MODEL,
        max_tokens=6000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return message.content[0].text.strip()


def extract_business_context_from_pdf(pdf_bytes: bytes, filename: str) -> str:
    """
    会社概要・事業計画・KPI進捗・顧客別売上・商品別売上の補足資料を読み取り、
    business_details に追記できるテキストを生成する（Claude Vision 利用）。
    PDF / PNG / JPG / WEBP / GIF に対応。
    """
    import base64

    fname_lower = filename.lower()
    is_image = any(fname_lower.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])
    file_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    if is_image:
        media_type = "image/png"
        if fname_lower.endswith(".jpg") or fname_lower.endswith(".jpeg"):
            media_type = "image/jpeg"
        elif fname_lower.endswith(".webp"):
            media_type = "image/webp"
        elif fname_lower.endswith(".gif"):
            media_type = "image/gif"
        source_block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_b64}}
    else:
        source_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}}

    prompt = f"""この資料は会社の補足資料です（会社概要・事業計画・KPI進捗・**顧客別売上**・**商品別売上**・商品カタログ・ピッチ資料 等）。
以下の観点で要点を抽出し、プレーンテキストで返してください（JSONではなく日本語の自由文）。
各項目は「見出し: 内容」の形式で改行区切り。事実ベースのみ、不明な点は書かない。

【特に重視する抽出観点】
- **顧客別売上TOP10**（社名・金額・構成比をわかる範囲で）→ 顧客集中度・特定取引先依存を判定
- **商品別売上TOP10**（商品名・金額・構成比）→ 主力商品の偏り・依存度
- **販路別・地域別・事業別の売上構成**
- 事業内容（何を作り/売っているか）
- 主な顧客層（BtoB/BtoC、法人・個人、地域）
- 販路・チャネル（EC、実店舗、卸、代理店 等）
- 主力商品・ブランド・こだわり
- KPI 進捗（会員数、リピート率、客単価 等）
- 事業の強み・差別化ポイント
- 今後の計画・目標

【出力ルール】
- 表形式の数字は「項目名: 〇〇万円（〇〇％）」の形で並べる
- 個人名は伏字（〇〇さん）
- 数字は元データの単位で記載＋万円換算（元データが円なら）
- 全体で 3000 文字以内

ファイル名: {filename}"""

    client = _get_claude()
    message = _call_claude_with_retry(
        client,
        model=CLAUDE_MODEL,
        max_tokens=6000,
        messages=[{
            "role": "user",
            "content": [
                source_block,
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return message.content[0].text.strip()


def generate_owner_pdf_content(result: dict, fd, client_name: str, period: str) -> dict:
    """
    分析結果を「社長プレゼン用」のナラティブに再翻訳。
    既存の result（prioritized_problems, owner_message 等）をベースに、
    ストーリー仕立て・易しい言葉で再生成する。
    トークン消費は約5-8k input + 2-3k output。
    """
    import json as _json
    # 入力を最小限に絞る（トークン節約）
    cs = result.get("company_state") or {}
    distilled = {
        "company": client_name,
        "period": period,
        "industry": result.get("benchmark", {}).get("industry_name", ""),
        "company_state": {
            "scale": cs.get("scale_label"),
            "health": cs.get("health_label"),
            "forbidden_proposals": cs.get("forbidden_proposals"),
        },
        "key_insight": result.get("key_insight", ""),
        "owner_message": result.get("owner_message", ""),
        "owner_what_to_do": result.get("owner_what_to_do", ""),
        "summary": result.get("summary", ""),
        "strengths": (result.get("strengths") or [])[:3],
        "issues": (result.get("issues") or [])[:3],
        "prioritized_problems": [],
        "growth_opportunities": (result.get("growth_opportunities") or [])[:5],
        "competitive_strengths": [],
    }
    for p in (result.get("prioritized_problems") or [])[:3]:
        distilled["prioritized_problems"].append({
            "rank": p.get("rank"),
            "title": p.get("title"),
            "fact": p.get("fact"),
            "hypothesis": p.get("hypothesis"),
            "confirmation_needed": p.get("confirmation_needed"),
            "detail": p.get("detail"),
            "solutions": [
                {
                    "title": s.get("title"),
                    "first_step": s.get("first_step"),
                    "why": s.get("why"),
                    "timeframe": s.get("timeframe"),
                    "impact_min": s.get("impact_min"),
                    "impact_max": s.get("impact_max"),
                    "impact_basis": s.get("impact_basis"),
                }
                for s in (p.get("solutions") or [])
            ],
            "expected_outcome": p.get("expected_outcome"),
        })
    for cs in (result.get("competitive_strengths") or [])[:3]:
        distilled["competitive_strengths"].append({
            "title": cs.get("title"),
            "narrative": cs.get("narrative"),
        })

    # 主要数字
    distilled["numbers"] = {
        "revenue": getattr(fd, "revenue", 0),
        "operating_profit": getattr(fd, "operating_profit", 0),
        "net_profit": getattr(fd, "net_profit", 0),
        "cash": getattr(fd, "cash", 0),
        "equity_ratio": (getattr(fd, "equity", 0) / getattr(fd, "total_assets", 1) * 100) if getattr(fd, "total_assets", 0) else None,
        "interest_bearing_debt": getattr(fd, "interest_bearing_debt", 0),
    }

    prompt = f"""あなたは中小企業の社長に経営分析レポートを送付する **コンサルタント/CFO** です。
社長が一人で読んで自走できる、**ロジカル&エビデンスベース** の構造化レポートを書き出してください。
業界比較は精度が低いため使わず、**社内の絶対値と推移** で評価します。

【分析結果（要約済み）】
{_json.dumps(distilled, ensure_ascii=False, indent=2)}

【絶対ルール】
1. **専門用語は「用語（説明）」形式**: 専門用語を消さず、カッコで説明を併記する。
   ✅「営業利益（本業のもうけ）」「EBITDA（営業利益+減価償却＝本業の現金を生む力）」「自己資本比率（自分のお金で経営してる割合）」「CCC（現金が戻るサイクル）」「流動比率（短期の支払い余力）」「有利子負債（借入の残り）」
   ❌ 用語を完全削除して説明だけ書くのは幼稚に見えるので NG（「本業のもうけは～」より「営業利益（本業のもうけ）は～」が良い）
   初出時のみカッコ説明、同セクション2回目以降は用語のみで可
2. **数字は丸めて1〜2桁**: 2.6億 / 6,600万 / 45万 / ▲1,652万円 など
3. **業界比較禁止**: 業界中央値や上位25%との比較は使わない（精度低のため）。代わりに「絶対値（健全水準）」と「過去推移」で評価
4. **数字→因果→示唆**: 「売上77%が1社依存」→「同社減少で売上8割消失」→「依存度50%まで下げるには年間5,000万円超の新規受注が必要」
5. **不安煽り禁止**: 「危険・破綻」→ 「このペースが続くと〜年で〜の計算」
6. **論理整合**: 「依存度高い顧客から追加受注」のような矛盾NG
7. **打ち手は具体的**: 「効果金額・実行費用・期間」を必ず明示

【出力JSON】
{{
  "cover_subtitle": "<表紙サブタイトル30字以内。CFOらしく分析的に。例：『黒字化達成。次の経営課題は1社依存解消と現金体質強化』>",

  "section1_performance": {{
    "headline": "<1文。今期の業績ハイライトを数字で。例『2025年5月期は売上2.73億円（前期比+5%）、本業赤字▲1,652万円も雑収入1,870万円で経常黒字37万円』>",
    "summary_text": "<3-4文 / 300-450字。売上・粗利・営業利益・経常利益・純利益を順に解説。前期比と推移トレンドを含める。社長語で>",
    "trend_comment": "<1-2文。過去推移グラフが示すトレンドの含意。例『売上は3期連続で2.7億円前後で安定、本業は赤字幅を縮小しており黒字化が射程圏内』>"
  }},

  "section2_strengths": {{
    "headline": "<1文。強みを総括。例『売掛回収の早さと短期支払い余力の厚さが財務体質の支柱』>",
    "strengths_list": [
      {{"title": "<強み名>", "evidence": "<数字根拠>", "implication": "<経営インパクト1文>"}}
    ]
  }},

  "section3_issues": {{
    "headline": "<1文。3つの課題を総括。例『売上集中度・本業赤字・借入返済負担の3点が連動した経営課題群』>",
    "issues": [
      {{
        "rank": 1,
        "title": "<課題タイトル40字以内。🚨 断定的ネガティブ禁止（『未整備』『できていない』『不足』『欠如』『悪化』NG）。前向き表現で：『〜に改善余地』『〜の精度向上の機会』『〜の見直しタイミング』『〜の最適化余地』。例：『売上の77%が1社依存（取引分散の検討余地）』『投資資産の見える化に改善余地』>",
        "fact": "<事実 / 数字。例『売上2.73億円のうち77%（2.10億円）がいであ㈱1社』>",
        "causal": "<因果 / 影響。例『同社の発注減少で即座に売上7割喪失=経営危機』>",
        "implication": "<示唆 / どうすべきか。例『依存度を50%まで下げるには新規顧客から年間5,000万円超の受注が必要』>"
      }}
    ]
  }},

  "section4_proposals_per_issue": [
    {{
      "issue_rank": 1,
      "issue_title": "<対応する課題タイトル>",
      "intro": "<1文。打ち手の方向性>",
      "solutions": [
        {{
          "title": "<打ち手30字以内>",
          "expected_effect": "<効果金額。例『+1,500〜3,000万円/年』>",
          "cost": "<実行費用。例『月50万円×6ヶ月=300万円』>",
          "timeframe": "<期間。例『3-6ヶ月』>",
          "first_step": "<明日からの一歩40字以内>",
          "why": "<なぜ効くか1文>"
        }}
      ]
    }}
  ],

  "section5_tax_proposals": {{
    "applicable": <true/false。営業利益>0 or 経常利益>0 のとき true>,
    "headline": "<1文。例『黒字を活かした節税×投資判断で年200-400万円の課税繰延が可能』。applicable=false なら空文字>",
    "proposals": [
      {{
        "title": "<節税策名>",
        "tax_save": "<節税効果。例『▲100-200万円/年』>",
        "cost": "<必要支出。例『不要』『300万円』>",
        "note": "<1文の説明>"
      }}
    ]
  }},

  "section6_conclusion": {{
    "headline": "<1文。総括。例『黒字化は達成。次は依存度・現金体質の2軸改善で3年後の経営安定へ』>",
    "positive_summary": "<2-3文 / 200-300字。「これは良い」というポイントをまとめる>",
    "next_steps_summary": "<2-3文 / 200-300字。提案実行で見える未来像。数字でシナリオ>",
    "closing_message": "<1-2文 / 100-150字。締めは意思決定接続型で。汎用テンプレ禁止。例『次回面談では、まずA案とB案のどちらを優先するか、社長と一緒に決めさせてください』『3ヶ月後、本業の現金が＋500万円改善している絵を一緒に描きにいきましょう』>"
  }}
}}

件数ルール（厳守）:
- section3 issues = 3件
- **section4 各 issue の solutions: 入力 `prioritized_problems[].solutions` と完全に同数・同じ並び順。1件も間引かない／1件も追加しない。各 input solution の `title` / `first_step` / `why` を社長語にリライトしつつ、対応関係を必ず保つ。**（理由: 税理士が画面上で採択／削除した結果が入力なので、PDF はそれを忠実に反映する）
- section5 proposals = 2-4件（黒字時のみ）
JSON以外は出力しない。マークダウンのコードブロックも不要。"""

    client = _get_claude()
    message = _call_claude_with_retry(
        client,
        model=CLAUDE_MODEL,
        max_tokens=8000,  # v13: 構造化されたフィールドが増えたため拡大
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    return _parse_json_response(text)


def _call_claude_with_retry(client, **kwargs):
    """Claude API 呼び出し（rate_limit 時の自動リトライ付き）
    token/分のレート制限対策のため、最低60秒待機する設計"""
    import time
    max_retries = 5
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            is_rate = ('429' in err_str or 'rate_limit' in err_str or
                       'overloaded' in err_str.lower())
            if is_rate and attempt < max_retries - 1:
                # サーバから retry-after があれば優先利用
                retry_after = None
                try:
                    if hasattr(e, 'response') and e.response is not None:
                        ra = e.response.headers.get('retry-after')
                        if ra:
                            retry_after = int(ra)
                except Exception:
                    pass
                # token/分 制限は1分待たないと回復しないので最低65秒
                # サーバ指定があればそれ、無ければ65 + 段階的増加
                wait = retry_after if retry_after else (65 + 15 * attempt)
                wait = min(wait, 180)  # 上限3分
                print(f"[claude_client] rate limit hit, retry {attempt+1}/{max_retries} after {wait}s")
                time.sleep(wait)
                continue
            raise


def extract_financials_from_pdf_binary(pdf_bytes: bytes, filename: str) -> dict:
    """
    スキャンPDF（画像PDF）対応。Claude Vision にPDFを直接投げて数値抽出。
    pdfplumber でテキストが取れなかった場合の fallback。
    注意：Claude 専用（Geminiは別の実装が必要）。
    """
    import base64
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    _max_tokens = 16000

    prompt = f"""あなたは財務データ抽出の専門家です。
このPDFは決算書です。画像として読み取り、財務数値を抽出してください。
必ずJSON形式のみで回答してください。前置きや説明文、マークダウンのコードブロックは不要です。

**重要**：決算書PDFは通常、以下の順で複数ページにわたります。**すべてのページを読み**、以下を必ず探してください：
  1ページ目付近: 表紙・勘定科目要約
  中盤: 損益計算書(P/L)・貸借対照表(B/S)
  **後半（見落としやすい）: 販管費明細書・売上原価明細書・勘定科目内訳書**
  最後: 個別注記表

特に「**販売費及び一般管理費明細書**」または「**販管費明細書**」（人件費・役員報酬・給料手当・法定福利費・賃借料・広告宣伝費・通信費・旅費交通費・減価償却費 等が項目別に金額で並ぶページ）を必ず探し、**全項目を漏らさず** `breakdown.selling_expenses_detail` に入れてください。

ファイル名: {filename}

{_PDF_EXTRACTION_SCHEMA}"""

    client = _get_claude()
    message = _call_claude_with_retry(
        client,
        model=CLAUDE_MODEL,
        max_tokens=_max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = message.content[0].text.strip()
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# Excel → 財務データ抽出（複数期対応）
# ---------------------------------------------------------------------------
def extract_excel_text(excel_bytes: bytes) -> str:
    """Excelファイルを全シート・テキスト化"""
    from io import BytesIO
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(excel_bytes), data_only=True)
    lines = []
    for sheet in wb.worksheets:
        lines.append(f"\n=== シート: {sheet.title} ===")
        for row in sheet.iter_rows(values_only=True):
            if all(c is None or c == '' for c in row):
                continue
            lines.append(" | ".join("" if c is None else str(c) for c in row))
    return "\n".join(lines)


def extract_financials_from_excel(excel_bytes: bytes, filename: str, provider: str = None) -> dict:
    """
    Excelファイルから財務データを抽出。
    月次推移・科目別推移・予算管理など複数期が含まれる可能性があるので、
    JSON の `periods` 配列として複数期返せるスキーマにする。
    """
    excel_text = extract_excel_text(excel_bytes)

    # ファイル名からデータ種類のヒントを推測
    fname_lc = (filename or "").lower()
    is_budget = any(k in filename for k in ["予算", "事業計画", "計画", "Budget", "budget", "Plan", "資本政策"])
    is_monthly = any(k in filename for k in ["月次", "月別", "monthly", "資金繰り"])
    is_balance = any(k in filename for k in ["残高試算", "残高", "試算表"])

    file_hint = ""
    if is_budget:
        file_hint = "⚠️ ファイル名から判断するとこれは【予算 or 事業計画】データの可能性が高い。実績ではないなら data_type='budget' として返し、保存対象外マークを付ける"
    elif is_monthly:
        file_hint = "⚠️ ファイル名から判断するとこれは【月次推移】データ。各月を別期として返さず、必ず**通期合算（12ヶ月合計）を1期として**返すこと。data_type='annual'"
    elif is_balance:
        file_hint = "⚠️ ファイル名から判断するとこれは【残高試算表】。期末B/Sデータとして1期として抽出。data_type='annual'"
    else:
        file_hint = "通常の決算書ファイル。data_type='annual'"

    prompt = f"""あなたは財務データ抽出の専門家です。
以下の Excel テキスト全体（複数シート）から、**すべての期の**財務数値を抽出してください。
必ずJSON形式のみで回答してください。前置きや説明文、マークダウンのコードブロックは不要です。

【ファイル種類のヒント】
{file_hint}

**🚨 最重要ルール**：
- 月次データの場合：**必ず12ヶ月合計を1期として**返す（月別12レコードは絶対作らない）
- 予算・事業計画データの場合：data_type="budget" として返す（実績データと混ぜない）
- 四半期データ：通期 or 各四半期、通期優先
- **異常検知**：もし同じ期の売上が極端に小さい（年商の1/12とか）なら、それは月次データの誤登録の疑い → 通期合算する
- 複数年度のデータがあれば各年度を1期として periods 配列に入れる

ファイル名: {filename}

【Excel テキスト】
{excel_text[:80000]}

以下のJSON形式で抽出してください：
{{
  "periods": [
    {{
      "period": "<決算期 例: 2024年3月期>",
      "revenue": <売上高（万円）>,
      "cost_of_sales": <売上原価（万円）>,
      "gross_profit": <売上総利益（万円）>,
      "selling_expenses": <販管費（万円）>,
      "operating_profit": <営業利益（万円）>,
      "ordinary_profit": <経常利益（万円）>,
      "net_profit": <当期純利益（万円）>,
      "prev_revenue": <前期売上高（万円、あれば）>,
      "prev_operating_profit": <前期営業利益（万円、あれば）>,
      "total_assets": <総資産（万円、B/S にあれば）>,
      "current_assets": <流動資産（万円）>,
      "cash": <現金及び預金（万円）>,
      "receivables": <売上債権の合計（売掛金+受取手形+電子記録債権を**全部足す**）（万円）>,
      "inventory": <棚卸資産の合計（商品+製品+原材料+仕掛品+貯蔵品+半製品 等を**全部足す**。1項目だけにしない）（万円）>,
      "total_liabilities": <負債合計（万円）>,
      "current_liabilities": <流動負債（万円）>,
      "interest_bearing_debt": <有利子負債（万円）>,
      "equity": <純資産（万円）>,
      "employees": <従業員数>,
      "breakdown": {{
        "selling_expenses_detail": {{"<項目名>": <金額（万円）>}},
        "cost_of_sales_detail": {{"<項目名>": <金額（万円）>}},
        "revenue_detail": {{"<セグメント名>": <金額（万円）>}},
        "non_operating_income": {{"<項目名>": <金額>}},
        "non_operating_expenses": {{"<項目名>": <金額>}},
        "current_assets_detail": {{"<項目名>": <金額>}},
        "fixed_assets_detail": {{"<項目名>": <金額>}},
        "current_liabilities_detail": {{"<項目名>": <金額>}},
        "fixed_liabilities_detail": {{"<項目名>": <金額>}}
      }}
    }}
  ],
  "source_type": "<月次推移 / 四半期推移 / 年次決算 / 科目別推移 / 予算管理 / 残高試算表 / 混合>",
  "data_type": "<annual / monthly / budget>  (実績年次=annual, 月次=monthly, 予算=budget)",
  "should_save": <true/false  予算・事業計画は false>,
  "note": "<ファイル全体についてのコメント（例: '月次12ヶ月を通期集計して1期として抽出' 等）>",
  "confidence": "<高/中/低>"
}}

単位：万円に統一。千円単位は10で割る、円は10000で割る、百万円は100倍。見つからなければ 0。"""

    text = _call_llm(prompt, provider)
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# F-01: PDF → 財務データ抽出
# ---------------------------------------------------------------------------
def extract_financials_from_pdf_text(pdf_text: str, filename: str, provider: str = None) -> dict:
    """PDFから抽出したテキストをLLMで財務データに変換する（P/L + B/S + 内訳明細 対応）"""
    prompt = f"""あなたは財務データ抽出の専門家です。
以下の決算書テキスト（全ページ）から財務数値を抽出し、必ずJSON形式のみで回答してください。
前置きや説明文、マークダウンのコードブロックは不要です。

**重要**：決算書PDFは通常、以下の順で複数ページにわたります。すべてを探して抽出してください：
  1ページ目付近: 表紙・勘定科目要約
  中盤: 損益計算書(P/L)・貸借対照表(B/S)
  **後半（見落としがち）: 販管費明細書・売上原価明細書・勘定科目内訳書・株主資本等変動計算書**
  最後: 個別注記表

特に「**販売費及び一般管理費明細書**」（人件費・家賃・広告宣伝費 等の項目別金額が羅列されているページ）を必ず探し、**全項目を漏らさず** `breakdown.selling_expenses_detail` に入れてください。

ファイル名: {filename}

【決算書テキスト（{len(pdf_text)}文字）】
{pdf_text[:80000]}

{_PDF_EXTRACTION_SCHEMA}"""

    text = _call_llm(prompt, provider)
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# F-02 + F-04 + F-05: 単年財務分析（スコア + 課題 + 打ち手）
# ---------------------------------------------------------------------------
def analyze_financials(fd: FinancialData, client_name: str, industry: str,
                       business_details: str = "", hearing_answers: dict = None,
                       historical_data: list = None,
                       provider: str = None,
                       referral_code: str = "",
                       excluded_categories: list = None) -> dict:
    """
    単年財務データを LLM で分析。
    business_details: 社長が答えた事業構成情報
    hearing_answers: 業種ヒアリング質問への回答 dict
    historical_data: 同じクライアントの他期の FinancialData リスト（トレンド分析用）
    referral_code: アフィリンクに埋め込む税理士の紹介ID
    """
    # ★ BS抽出ミス自動補正（三角測量で安全な場合のみ上書き）
    # 単純な「DB値 vs 内訳合計」比較は危険（内訳に重複がある場合誤補正する）。
    # 「DB + 別カラム = 総額」と「内訳合計 + 別カラム = 総額」のどちらが整合するかで判定。
    try:
        _bd = json.loads(getattr(fd, "breakdown_json", "{}") or "{}")

        def _sum_detail(key: str) -> float:
            d = _bd.get(key) or {}
            return sum(v for k, v in d.items()
                       if not k.startswith("__") and isinstance(v, (int, float)) and v != 0)

        _cl_sum = _sum_detail("current_liabilities_detail")
        _ca_sum = _sum_detail("current_assets_detail")
        _fl_sum = _sum_detail("fixed_liabilities_detail")
        _fa_sum = _sum_detail("fixed_assets_detail")
        _cl_db = getattr(fd, "current_liabilities", 0) or 0
        _ca_db = getattr(fd, "current_assets", 0) or 0
        _tl = getattr(fd, "total_liabilities", 0) or 0
        _ta = getattr(fd, "total_assets", 0) or 0

        # --- 流動負債の三角測量補正 ---
        # 候補1: cl_db + fl_sum vs total_liabilities
        # 候補2: cl_sum + fl_sum vs total_liabilities
        # より整合する方を採用
        if _tl > 0 and _fl_sum > 0 and _cl_sum > 0 and _cl_db > 0:
            err_db = abs(_cl_db + _fl_sum - _tl)
            err_sum = abs(_cl_sum + _fl_sum - _tl)
            # 内訳合計版の誤差が DB版より大幅に小さい場合のみ上書き（DB値が固定負債と誤分類されてるケース）
            if err_sum < err_db and err_db > _tl * 0.20:
                print(f"[BS補正] fd_{fd.id} current_liabilities: DB={_cl_db:,.0f} → 内訳合計={_cl_sum:,.0f}（総負債との整合で内訳が正）", flush=True)
                fd.current_liabilities = _cl_sum

        # --- 流動資産の三角測量補正 ---
        if _ta > 0 and _fa_sum > 0 and _ca_sum > 0 and _ca_db > 0:
            err_db = abs(_ca_db + _fa_sum - _ta)
            err_sum = abs(_ca_sum + _fa_sum - _ta)
            if err_sum < err_db and err_db > _ta * 0.20:
                print(f"[BS補正] fd_{fd.id} current_assets: DB={_ca_db:,.0f} → 内訳合計={_ca_sum:,.0f}（総資産との整合で内訳が正）", flush=True)
                fd.current_assets = _ca_sum
            elif err_db < err_sum and err_sum > _ta * 0.20:
                # DBが正で内訳に重複あり → 警告のみ（AI に伝えて、AI が分析時に内訳を慎重に扱う）
                print(f"[BS警告] fd_{fd.id} current_assets_detail に重複の可能性: DB={_ca_db:,.0f} / 内訳合計={_ca_sum:,.0f}（DBが正）", flush=True)

        # --- 総負債の整合チェック ---
        if _cl_sum > 0 and _fl_sum > 0 and _tl > 0:
            _calc = _cl_sum + _fl_sum
            if abs(_calc - _tl) / max(_tl, _calc) > 0.10:
                print(f"[BS警告] fd_{fd.id} total_liabilities: DB={_tl:,.0f} vs 流動+固定内訳={_calc:,.0f}（乖離あり）", flush=True)

        # --- 総資産の整合チェック ---
        if _ca_sum > 0 and _fa_sum > 0 and _ta > 0:
            _calc_ta = _ca_sum + _fa_sum
            if abs(_calc_ta - _ta) / max(_ta, _calc_ta) > 0.15:
                print(f"[BS警告] fd_{fd.id} total_assets: DB={_ta:,.0f} vs 流動+固定内訳={_calc_ta:,.0f}（乖離あり）", flush=True)
    except Exception as _e:
        pass

    gross_margin = (fd.gross_profit / fd.revenue * 100) if fd.revenue else 0
    operating_margin = (fd.operating_profit / fd.revenue * 100) if fd.revenue else 0
    revenue_growth = (
        (fd.revenue - fd.prev_revenue) / fd.prev_revenue * 100
        if fd.prev_revenue
        else None
    )
    growth_text = (
        f"前期比 {revenue_growth:+.1f}%"
        if revenue_growth is not None
        else "前期データなし"
    )

    # 事業構成コンテキスト（社長からのヒアリング情報）
    if business_details:
        business_context = f"\n【事業構成（社長より）】\n{business_details}\n"
    else:
        business_context = ""

    if hearing_answers:
        business_context += "\n【追加ヒアリング回答】\n"
        for q, a in hearing_answers.items():
            business_context += f"  Q: {q}\n  A: {a}\n"

    # 内訳データ（販管費内訳・売上内訳等）
    breakdown_text = ""
    try:
        breakdown = json.loads(getattr(fd, "breakdown_json", "{}") or "{}")
    except Exception:
        breakdown = {}

    if breakdown:
        parts = ["\n【決算書の内訳明細（重要：提案の根拠として必ず使うこと）】"]

        # 販管費明細
        se_detail = breakdown.get("selling_expenses_detail") or {}
        se_detail = {k: v for k, v in se_detail.items()
                     if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        if se_detail:
            total_se = sum(se_detail.values())
            parts.append("◆ 販管費内訳（合計: {:,.0f}万円）".format(total_se))
            sorted_items = sorted(se_detail.items(), key=lambda x: -x[1])
            for name, amount in sorted_items:
                ratio = amount / fd.revenue * 100 if fd.revenue else 0
                ratio_se = amount / total_se * 100 if total_se else 0
                parts.append(f"  - {name}: {amount:,.0f}万円 "
                             f"（売上比 {ratio:.1f}%、販管費内シェア {ratio_se:.1f}%）")

        # 売上内訳
        rev_detail = breakdown.get("revenue_detail") or {}
        rev_detail = {k: v for k, v in rev_detail.items()
                      if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        if rev_detail:
            total_rev = sum(rev_detail.values())
            parts.append("◆ 売上内訳")
            for name, amount in sorted(rev_detail.items(), key=lambda x: -x[1]):
                ratio = amount / total_rev * 100 if total_rev else 0
                parts.append(f"  - {name}: {amount:,.0f}万円 ({ratio:.1f}%)")

        # 売上原価内訳
        cos_detail = breakdown.get("cost_of_sales_detail") or {}
        cos_detail = {k: v for k, v in cos_detail.items()
                      if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        if cos_detail:
            parts.append("◆ 売上原価内訳")
            for name, amount in sorted(cos_detail.items(), key=lambda x: -x[1]):
                parts.append(f"  - {name}: {amount:,.0f}万円")

        # 営業外収益・費用
        nop_in = breakdown.get("non_operating_income") or {}
        nop_in = {k: v for k, v in nop_in.items() if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        if nop_in:
            parts.append("◆ 営業外収益内訳")
            for name, amount in sorted(nop_in.items(), key=lambda x: -x[1]):
                parts.append(f"  - {name}: {amount:,.0f}万円")

        nop_ex = breakdown.get("non_operating_expenses") or {}
        nop_ex = {k: v for k, v in nop_ex.items() if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        if nop_ex:
            parts.append("◆ 営業外費用内訳")
            for name, amount in sorted(nop_ex.items(), key=lambda x: -x[1]):
                parts.append(f"  - {name}: {amount:,.0f}万円")

        # 流動資産内訳（整合性チェック付き）
        ca_detail = breakdown.get("current_assets_detail") or {}
        ca_detail = {k: v for k, v in ca_detail.items() if not k.startswith("__") and isinstance(v, (int, float))}
        if ca_detail:
            ca_sum = sum(ca_detail.values())
            parts.append(f"◆ 流動資産内訳（合計内訳: {ca_sum:,.0f}万円）")
            for name, amount in sorted(ca_detail.items(), key=lambda x: -x[1]):
                ratio = amount / ca_sum * 100 if ca_sum else 0
                ratio_rev = amount / fd.revenue * 100 if fd.revenue else 0
                parts.append(f"  - {name}: {amount:,.0f}万円（流動資産内{ratio:.1f}% / 売上比{ratio_rev:.1f}%）")
            # 整合性チェック
            if getattr(fd, "current_assets", 0):
                gap = fd.current_assets - ca_sum
                if abs(gap) > fd.current_assets * 0.05:  # 5%以上乖離
                    parts.append(f"  ⚠️ 流動資産合計({fd.current_assets:,.0f})と内訳合計({ca_sum:,.0f})に{gap:+,.0f}万円の乖離 → 未分類項目の可能性")

        # 流動負債内訳（整合性チェック付き）
        cl_detail = breakdown.get("current_liabilities_detail") or {}
        cl_detail = {k: v for k, v in cl_detail.items() if not k.startswith("__") and isinstance(v, (int, float))}
        if cl_detail:
            cl_sum = sum(cl_detail.values())
            parts.append(f"◆ 流動負債内訳（合計内訳: {cl_sum:,.0f}万円）")
            for name, amount in sorted(cl_detail.items(), key=lambda x: -x[1]):
                ratio = amount / cl_sum * 100 if cl_sum else 0
                parts.append(f"  - {name}: {amount:,.0f}万円（流動負債内{ratio:.1f}%）")
            if getattr(fd, "current_liabilities", 0):
                gap = fd.current_liabilities - cl_sum
                if abs(gap) > fd.current_liabilities * 0.05:
                    parts.append(f"  ⚠️ 流動負債合計({fd.current_liabilities:,.0f})と内訳合計({cl_sum:,.0f})に{gap:+,.0f}万円の乖離")

        # 固定負債内訳
        fl_detail = breakdown.get("fixed_liabilities_detail") or {}
        fl_detail = {k: v for k, v in fl_detail.items() if not k.startswith("__") and isinstance(v, (int, float))}
        if fl_detail:
            parts.append(f"◆ 固定負債内訳")
            for name, amount in sorted(fl_detail.items(), key=lambda x: -x[1]):
                parts.append(f"  - {name}: {amount:,.0f}万円")

        # 固定資産内訳
        fa_detail = breakdown.get("fixed_assets_detail") or {}
        fa_detail = {k: v for k, v in fa_detail.items() if not k.startswith("__") and isinstance(v, (int, float))}
        if fa_detail:
            parts.append(f"◆ 固定資産内訳")
            for name, amount in sorted(fa_detail.items(), key=lambda x: -x[1]):
                parts.append(f"  - {name}: {amount:,.0f}万円")

        # グループ集計（人件費・物件費・車両費等）を追加
        from .services.expense_grouping import format_expense_groups_for_prompt
        se_group_text = format_expense_groups_for_prompt(
            breakdown.get("selling_expenses_detail") or {},
            label="販管費",
            revenue=fd.revenue or 0,
        )
        if se_group_text:
            parts.append(se_group_text)
        cos_group_text = format_expense_groups_for_prompt(
            breakdown.get("cost_of_sales_detail") or {},
            label="売上原価",
            revenue=fd.revenue or 0,
        )
        if cos_group_text:
            parts.append(cos_group_text)

        if len(parts) > 1:
            breakdown_text = "\n".join(parts) + "\n"

    # ★ 費目別 前期比較（営業利益率悪化の原因特定用）
    cost_delta_text = ""
    if historical_data and len(historical_data) >= 2:
        try:
            sorted_hd = sorted(historical_data, key=lambda x: x.period or "")
            curr_fd = None
            prev_fd = None
            for h in sorted_hd:
                if h.id == fd.id:
                    curr_fd = h
                    break
                prev_fd = h
            if prev_fd and curr_fd:
                try:
                    prev_bd = json.loads(getattr(prev_fd, "breakdown_json", "{}") or "{}")
                    curr_bd = json.loads(getattr(curr_fd, "breakdown_json", "{}") or "{}")
                except Exception:
                    prev_bd, curr_bd = {}, {}

                def _delta_block(label: str, key: str):
                    prev = (prev_bd.get(key) or {})
                    curr = (curr_bd.get(key) or {})
                    prev = {k: v for k, v in prev.items() if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
                    curr = {k: v for k, v in curr.items() if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
                    keys = sorted(set(prev) | set(curr))
                    if not keys:
                        return None
                    rows = []
                    for k in keys:
                        pv = prev.get(k, 0)
                        cv = curr.get(k, 0)
                        delta = cv - pv
                        if pv == 0 and cv == 0:
                            continue
                        pct = (delta / abs(pv) * 100) if pv else None
                        pct_text = f"{pct:+.1f}%" if pct is not None else "新規"
                        rows.append((abs(delta), f"  - {k}: 前期 {pv:,.0f} → 今期 {cv:,.0f} 万円（差分 {delta:+,.0f} / {pct_text}）"))
                    # 差分の絶対値が大きい順
                    rows.sort(key=lambda x: -x[0])
                    return [f"◆ {label} 前期比増減"] + [r[1] for r in rows[:15]]

                delta_parts = []
                # 売上総利益・営業利益・経常利益の前期比から先に
                pl_summary = []
                for label, attr in [
                    ("売上", "revenue"), ("売上原価", "cost_of_sales"),
                    ("粗利", "gross_profit"), ("販管費", "selling_expenses"),
                    ("営業利益", "operating_profit"), ("経常利益", "ordinary_profit"),
                ]:
                    p = getattr(prev_fd, attr, 0) or 0
                    c = getattr(curr_fd, attr, 0) or 0
                    if p == 0 and c == 0:
                        continue
                    delta = c - p
                    pct = (delta / abs(p) * 100) if p else None
                    pct_text = f"{pct:+.1f}%" if pct is not None else "新規"
                    pl_summary.append(f"  - {label}: {p:,.0f} → {c:,.0f}（{delta:+,.0f} / {pct_text}）")
                if pl_summary:
                    delta_parts.append("◆ P/L 主要項目 前期比")
                    delta_parts.extend(pl_summary)

                for label, key in [
                    ("販管費", "selling_expenses_detail"),
                    ("売上原価", "cost_of_sales_detail"),
                    ("売上", "revenue_detail"),
                ]:
                    block = _delta_block(label, key)
                    if block:
                        delta_parts.extend(block)

                # グループ別前期比（人件費まとめ等）も追加
                from .services.expense_grouping import format_group_delta_for_prompt
                se_group_delta = format_group_delta_for_prompt(
                    prev_bd.get("selling_expenses_detail") or {},
                    curr_bd.get("selling_expenses_detail") or {},
                    label="販管費",
                )
                if se_group_delta:
                    delta_parts.append(se_group_delta)
                cos_group_delta = format_group_delta_for_prompt(
                    prev_bd.get("cost_of_sales_detail") or {},
                    curr_bd.get("cost_of_sales_detail") or {},
                    label="売上原価",
                )
                if cos_group_delta:
                    delta_parts.append(cos_group_delta)

                if delta_parts:
                    cost_delta_text = "\n【💡 費目別 前期比較（営業利益率悪化の原因特定用）】\n" + "\n".join(delta_parts) + "\n"
        except Exception as _e:
            cost_delta_text = ""

    # ★ BS整合性チェック＆自動補正（DBの current_liabilities が内訳合計と大きくズレてたら警告）
    bs_correction_text = ""
    try:
        cl_detail = (breakdown.get("current_liabilities_detail") or {})
        cl_detail_clean = {k: v for k, v in cl_detail.items()
                           if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        cl_detail_sum = sum(cl_detail_clean.values())
        cl_db = getattr(fd, "current_liabilities", 0) or 0
        fl_detail = (breakdown.get("fixed_liabilities_detail") or {})
        fl_detail_clean = {k: v for k, v in fl_detail.items()
                           if not k.startswith("__") and isinstance(v, (int, float)) and v != 0}
        fl_detail_sum = sum(fl_detail_clean.values())
        total_liab = getattr(fd, "total_liabilities", 0) or 0

        # ズレが 20% 超なら BS抽出ミス警告
        corrections = []
        if cl_detail_sum > 0 and cl_db > 0 and abs(cl_db - cl_detail_sum) / max(cl_db, cl_detail_sum) > 0.20:
            corrections.append(
                f"⚠️ 流動負債のDB値({cl_db:,.0f}万円) と 内訳合計({cl_detail_sum:,.0f}万円) が大きく乖離。\n"
                f"   内訳合計({cl_detail_sum:,.0f}万円)を正とみなして分析せよ。"
            )
            # 流動比率の再計算
            ca_db = getattr(fd, "current_assets", 0) or 0
            if ca_db > 0 and cl_detail_sum > 0:
                corrected_ratio = ca_db / cl_detail_sum * 100
                corrections.append(
                    f"   ★ 流動比率の補正値: {ca_db:,.0f}/{cl_detail_sum:,.0f} = {corrected_ratio:.0f}%（DB保存の{ca_db/cl_db*100 if cl_db else 0:.0f}%は誤り）"
                )

        if total_liab > 0:
            calc_total = cl_detail_sum + fl_detail_sum
            if calc_total > 0 and abs(calc_total - total_liab) / max(total_liab, calc_total) > 0.10:
                corrections.append(
                    f"⚠️ 総負債({total_liab:,.0f}) ≠ 流動+固定内訳合計({calc_total:,.0f})。BS抽出に分類ミスの可能性あり"
                )

        # 既存の保険・有価証券・共済の検出（節税重複防止）
        existing_assets = []
        fa = (breakdown.get("fixed_assets_detail") or {})
        for name, amt in fa.items():
            if not isinstance(amt, (int, float)) or amt == 0 or name.startswith("__"):
                continue
            if any(kw in name for kw in ["保険", "共済", "有価証券", "投資", "出資"]):
                existing_assets.append(f"  - {name}: {amt:,.0f}万円")
        for k in ["non_operating_expenses"]:
            block = breakdown.get(k) or {}
            for name, amt in block.items():
                if any(kw in name for kw in ["保険料"]) and isinstance(amt, (int, float)) and amt != 0:
                    existing_assets.append(f"  - {name}（営業外費用）: {amt:,.0f}万円")
        # 販管費明細にも保険料があるか
        se = (breakdown.get("selling_expenses_detail") or {})
        for name, amt in se.items():
            if "保険料" in name and isinstance(amt, (int, float)) and amt != 0:
                existing_assets.append(f"  - {name}（販管費）: {amt:,.0f}万円")

        parts = []
        if corrections:
            parts.append("【🚨 BS抽出の整合性チェック】")
            parts.extend(corrections)
        if existing_assets:
            parts.append("\n【🛡 既加入の保険・共済・有価証券（節税提案の重複確認用）】")
            parts.extend(existing_assets)
            parts.append("🚨 上記が **既加入の保険積立金 or 法人保険**を示唆している場合、以下を厳守：")
            parts.append("  1. 経営セーフティ共済の新規加入提案は **growth_opportunities / tax_savings_advice から削除** すること（既に類似機能の保険がある可能性が高い）")
            parts.append("  2. どうしても提案する場合は、verify_first に『既加入の◯◯万円の保険積立金と経営セーフティ共済の機能比較表を作成し、社長と相談』と明記")
            parts.append("  3. 既存保険の見直し・解約返戻金活用・名義変更を優先打ち手として提案")
        if parts:
            bs_correction_text = "\n" + "\n".join(parts) + "\n"
    except Exception:
        bs_correction_text = ""

    # B/S データ（まだ DB に無い場合は空文字）
    bs_parts = []
    for attr, label in [
        ("total_assets", "総資産"),
        ("current_assets", "流動資産"),
        ("cash", "現預金"),
        ("receivables", "売掛金"),
        ("inventory", "棚卸資産"),
        ("total_liabilities", "負債合計"),
        ("current_liabilities", "流動負債"),
        ("interest_bearing_debt", "有利子負債"),
        ("equity", "純資産"),
    ]:
        val = getattr(fd, attr, None)
        if val:
            bs_parts.append(f"{label}: {val:,.0f}万円")
    bs_text = ""
    if bs_parts:
        bs_text = "\n【貸借対照表（単位：万円）】\n" + "\n".join(bs_parts) + "\n"
        # 主要比率
        if getattr(fd, "total_assets", 0):
            equity = getattr(fd, "equity", 0)
            if equity:
                bs_text += f"自己資本比率: {equity/fd.total_assets*100:.1f}%\n"
        if getattr(fd, "current_liabilities", 0):
            ca = getattr(fd, "current_assets", 0)
            if ca:
                bs_text += f"流動比率: {ca/fd.current_liabilities*100:.1f}%\n"

    # 時系列データ（複数年ある場合はトレンド分析コンテキストとして流す）
    historical_text = ""
    trend_metrics = {}  # template にも渡す用
    if historical_data and len(historical_data) > 1:
        # period 昇順で並べる
        sorted_hd = sorted(historical_data, key=lambda x: x.period or "")
        historical_text = "\n【時系列データ（各期の主要数字と前期比伸び率）】\n"

        def _pct(prev, curr):
            return (curr - prev) / abs(prev) * 100 if prev else None

        prev = None
        for h in sorted_hd:
            gm = (h.gross_profit / h.revenue * 100) if h.revenue else 0
            om = (h.operating_profit / h.revenue * 100) if h.revenue else 0
            parts = [f"{h.period}:"]
            # 売上（前期比）
            s = f"売上{h.revenue:,.0f}"
            if prev and prev.revenue:
                g = _pct(prev.revenue, h.revenue)
                if g is not None: s += f"({g:+.1f}%)"
            parts.append(s)
            parts.append(f"粗利率{gm:.1f}%")
            parts.append(f"営業利益{h.operating_profit:+,.0f}({om:+.1f}%)")
            # 現預金（前期比）
            c = f"現預金{h.cash:,.0f}"
            if prev and prev.cash:
                g = _pct(prev.cash, h.cash)
                if g is not None: c += f"({g:+.1f}%)"
            parts.append(c)
            # 売掛金（絶対額＋前期比）
            if h.receivables:
                r = f"売掛{h.receivables:,.0f}"
                if prev and prev.receivables:
                    g = _pct(prev.receivables, h.receivables)
                    if g is not None: r += f"({g:+.1f}%)"
                parts.append(r)
            # 在庫（絶対額＋前期比）
            if h.inventory:
                inv = f"在庫{h.inventory:,.0f}"
                if prev and prev.inventory:
                    g = _pct(prev.inventory, h.inventory)
                    if g is not None: inv += f"({g:+.1f}%)"
                parts.append(inv)
            historical_text += " / ".join(parts) + "\n"
            prev = h

        # 自動検出：売掛金/在庫の伸び率が売上/売上原価を大きく上回る期をプリチェック
        alerts = []
        for i in range(1, len(sorted_hd)):
            p, c = sorted_hd[i-1], sorted_hd[i]
            if p.revenue and c.revenue and p.receivables and c.receivables:
                rev_g = _pct(p.revenue, c.revenue)
                rec_g = _pct(p.receivables, c.receivables)
                if rev_g is not None and rec_g is not None and (rec_g - rev_g) > 20:
                    alerts.append(f"⚠️ {c.period}: 売上{rev_g:+.1f}% vs 売掛金{rec_g:+.1f}%（乖離+{rec_g-rev_g:.1f}pt） → 売上が現金化されていない可能性")
            if p.cost_of_sales and c.cost_of_sales and p.inventory and c.inventory:
                cos_g = _pct(p.cost_of_sales, c.cost_of_sales)
                inv_g = _pct(p.inventory, c.inventory)
                if cos_g is not None and inv_g is not None and (inv_g - cos_g) > 20:
                    alerts.append(f"⚠️ {c.period}: 売上原価{cos_g:+.1f}% vs 棚卸資産{inv_g:+.1f}%（乖離+{inv_g-cos_g:.1f}pt） → 在庫滞留の可能性")
            if c.operating_profit and c.operating_profit > 0 and p.cash and c.cash:
                cash_g = _pct(p.cash, c.cash)
                if cash_g is not None and cash_g < -10:
                    alerts.append(f"⚠️ {c.period}: 営業利益+{c.operating_profit:,.0f}万 だが現預金{cash_g:+.1f}% → 利益がキャッシュ化されていない可能性")

        if alerts:
            historical_text += "\n【⚠️ 自動検出された数字の動きの異常】\n" + "\n".join(alerts) + "\n→ これらは必ず issues に反映せよ（表現は中立・丁寧に）\n"

        historical_text += "\n→ **どの数字がどう動いて何の課題を示しているか** を発見して issues に反映せよ\n"

        # template でグラフ表示するための簡易データ
        trend_metrics["periods"] = [h.period for h in sorted_hd]
        trend_metrics["revenue"] = [h.revenue for h in sorted_hd]
        trend_metrics["gross_margin"] = [round((h.gross_profit / h.revenue * 100) if h.revenue else 0, 1) for h in sorted_hd]
        trend_metrics["operating_profit"] = [h.operating_profit for h in sorted_hd]
        trend_metrics["cash"] = [h.cash for h in sorted_hd]
        trend_metrics["receivables"] = [h.receivables for h in sorted_hd]
        trend_metrics["inventory"] = [h.inventory for h in sorted_hd]
        # PL/BS 積み上げ棒グラフ用（北村先生FB対応）
        trend_metrics["cost_of_sales"] = [h.cost_of_sales or 0 for h in sorted_hd]
        trend_metrics["gross_profit"] = [h.gross_profit or 0 for h in sorted_hd]
        trend_metrics["selling_expenses"] = [h.selling_expenses or 0 for h in sorted_hd]
        trend_metrics["total_assets"] = [h.total_assets or 0 for h in sorted_hd]
        trend_metrics["equity"] = [h.equity or 0 for h in sorted_hd]
        trend_metrics["interest_bearing_debt"] = [h.interest_bearing_debt or 0 for h in sorted_hd]
        trend_metrics["total_liabilities"] = [h.total_liabilities or 0 for h in sorted_hd]
        trend_metrics["current_liabilities"] = [h.current_liabilities or 0 for h in sorted_hd]

        # 前期比成長率（最初の期は null）
        def _growth(arr):
            g = [None]
            for i in range(1, len(arr)):
                prev, curr = arr[i-1], arr[i]
                if prev and prev != 0:
                    g.append(round((curr - prev) / abs(prev) * 100, 1))
                else:
                    g.append(None)
            return g
        trend_metrics["revenue_growth"] = _growth(trend_metrics["revenue"])
        trend_metrics["receivables_growth"] = _growth(trend_metrics["receivables"])
        trend_metrics["inventory_growth"] = _growth(trend_metrics["inventory"])
        # 売上原価の成長率（在庫成長と対比）
        cos_series = [h.cost_of_sales for h in sorted_hd]
        trend_metrics["cost_of_sales_growth"] = _growth(cos_series)
        trend_metrics["cost_of_sales"] = cos_series

        # 月数ベース（社長向け：直感的な指標）。Jinja の sum 互換のため None は 0 に
        trend_metrics["receivables_months"] = [
            round(h.receivables / (h.revenue / 12), 2) if h.revenue and h.receivables else 0
            for h in sorted_hd
        ]
        # 在庫月数：原価があればそれで割る、なければ売上で代替（粗利を含むため参考値）
        inv_months = []
        for h in sorted_hd:
            if h.inventory and h.cost_of_sales:
                inv_months.append(round(h.inventory / (h.cost_of_sales / 12), 2))
            elif h.inventory and h.revenue:
                inv_months.append(round(h.inventory / (h.revenue / 12), 2))
            else:
                inv_months.append(0)
        trend_metrics["inventory_months"] = inv_months

    # 業界ベンチマーク比較（F-03）
    benchmark = compare_to_benchmark(fd, industry)
    benchmark_text = format_benchmark_text(benchmark)

    # バーンレートと運転資本・EBITDA
    burn = compute_burn_rate(fd, breakdown=breakdown)
    wc = compute_working_capital(fd)
    ebitda = compute_ebitda(fd, breakdown)

    cash_lines = ["\n【キャッシュ診断（🚨 この数字を引用すること。AIが独自に計算し直すのは禁止）】"]
    if burn.get("operating_monthly") is not None:
        cash_lines.append(f"月次営業利益: {burn['operating_monthly']:+,.1f}万円/月（会計上の利益）")
    if burn.get("depreciation_monthly"):
        cash_lines.append(f"月次減価償却費: +{burn['depreciation_monthly']:,.1f}万円/月（現金は出ていない・過去投資の費用化）")
    if burn.get("operating_cf_monthly") is not None:
        cash_lines.append(f"★ 月次営業CF（≒EBITDA）: {burn['operating_cf_monthly']:+,.1f}万円/月 = 本業で生まれた現金（営業利益+減価償却）")
    if burn.get("debt_repayment_monthly_est"):
        cash_lines.append(f"推定月次返済: {burn['debt_repayment_monthly_est']:,.1f}万円/月（有利子負債÷10年の仮定）")
    if burn.get("real_burn_monthly") is not None:
        cash_lines.append(f"★ 実質キャッシュフロー（営業CF - 返済）: {burn['real_burn_monthly']:+,.1f}万円/月")
        cash_lines.append(f"  ※ プラスならキャッシュは積み上がる。マイナスなら現金流出。AIはこの値を必ずそのまま引用すること（『営業利益 - 返済』で再計算するのは禁止＝減価償却を無視して誤判定する）")
    if burn.get("runway_months"):
        cash_lines.append(f"資金ショートまで（推定）: 約{burn['runway_months']}ヶ月")
    if burn.get("breakeven_required_yearly"):
        cash_lines.append(f"黒字化に必要な年間改善: +{burn['breakeven_required_yearly']:,.0f}万円")

    if wc:
        cash_lines.append("\n【運転資本指標】")
        if "receivables_days" in wc:
            cash_lines.append(f"売上債権回転期間: {wc['receivables_days']}日")
        if "inventory_days" in wc:
            cash_lines.append(f"在庫回転期間: {wc['inventory_days']}日")
        if "ccc_days" in wc:
            cash_lines.append(f"CCC: {wc['ccc_days']}日（買掛金45日仮定）")
        if "debt_equity_ratio" in wc:
            cash_lines.append(f"負債比率: {wc['debt_equity_ratio']}%（100%以下が健全）")
        if "cash_months_of_sales" in wc:
            cash_lines.append(f"月商キャッシュ倍率: {wc['cash_months_of_sales']}ヶ月分")

    if ebitda.get("ebitda") is not None:
        cash_lines.append("\n【EBITDA（本業の現金生成力）】")
        cash_lines.append(f"減価償却費: {ebitda['depreciation']:,.0f}万円")
        cash_lines.append(f"EBITDA（営業利益+減価償却）: {ebitda['ebitda']:,.0f}万円")
        if ebitda.get("ebitda_margin") is not None:
            cash_lines.append(f"EBITDA マージン: {ebitda['ebitda_margin']}%")
        if ebitda.get("debt_to_ebitda"):
            cash_lines.append(f"有利子負債/EBITDA: {ebitda['debt_to_ebitda']}倍（5倍超で金融機関が警戒）")

    cash_text = "\n".join(cash_lines) + "\n"

    # 会社の状態診断（規模・健全性・禁止打ち手）を Python 事前計算
    state = _diagnose_company_state(fd, ebitda, burn, wc)
    state_text = _format_state_for_prompt(state)

    # 経営フェーズ判定（3年トレンド分析）
    phase_info = _diagnose_business_phase(fd, historical_data, breakdown)
    phase_text = _format_business_phase_for_prompt(phase_info)

    # 余剰キャッシュ（寝てるカネ）算出（業界別倍率）
    idle_info = _compute_idle_cash(fd, burn, wc, industry=industry or "")
    idle_text = _format_idle_cash_for_prompt(idle_info)
    # 投資期判定時は state にメタ情報を持たせる（forbidden に入れる完全禁止ではなく
    # 「観察ノート + ヒアリング必須」のセミ・ガード）
    if phase_info.get("phase") == "investment":
        state["business_phase"] = phase_info.get("phase")
        state["business_phase_label"] = phase_info.get("label")
        # 確認事項として AI に渡す（completely banning is too rigid）
        state.setdefault("required_confirmations", []).append(
            "営業利益悪化が戦略的投資の結果か（投資内容・回収計画の社長ヒアリング必須）"
        )

    prompt = f"""あなたは中小企業の社長に伴走するCFOです。税理士の先生と一緒に働いています。
以下の財務データを分析し、JSON形式のみで回答してください。前置き・説明文・マークダウンは不要。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴【絶対ルール 10箇条】これだけは絶対に守る。他の全てより優先
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **業界比較禁止**: benchmark データの数値は社長向け出力に使わない。
   評価軸は「社内推移」「絶対水準（健全レンジ）」「会社体力（現預金・粗利）対比」のみ。

2. **桁ミス禁止**: 入力データは全て「万円」単位。例：売上86,463=8.6億円。
   「86,463万円」または「8.6億円」と表記。**「8,646万円」は10倍の桁ミスで絶対NG**。
   重要数字は出力後に必ず入力と桁照合せよ。

3. **専門用語は『用語（説明）』形式で統一**:
   ✅「営業利益（本業のもうけ）」「EBITDA（営業利益+減価償却＝本業の現金を生む力）」
   ❌ 用語のみ（「EBITDA」単独）or 説明のみ（「本業の現金を生む力」単独）
   各フィールドで初出時に必ずカッコ説明。社長視点で当然知ってる用語はない。

4. **同一概念は同じ表記で固定**（表記ブレ禁止）:
   1レポート内で「営業利益率」と「営利率」「本業の儲け率」を混在させない。
   「粗利」と「売上総利益」、「売掛回収日数(DSO)」と「売上債権回転期間」も混在NG。

5. **前向き表現**: 「未整備」「できていない」「不足」「悪化」「急減」禁止。
   ✅「改善余地」「精度向上の機会」「見直しタイミング」「最適化余地」「検証の機会」

6. **キャッシュ判定は減価償却込みで**: 営業利益 - 借入返済 で判定するな。
   営業利益 + 減価償却（=営業CF/EBITDA）- 借入返済 が真の実質キャッシュフロー。
   入力データの「★ 月次営業CF」「★ 実質キャッシュフロー」をそのまま引用せよ。

7. **絶対水準内の数字を課題化禁止**: DSO≤45日 / 在庫≤30日 / 自己資本比率≥30% /
   月商キャッシュ≥2ヶ月 / 流動比率≥120% は健全。これらを prioritized_problems
   に書くな。書くなら observation_notes に「念のため確認」で。

8. **ロジック整合（矛盾禁止）**:
   - 1社依存と書いて「その顧客に追加受注」NG → 新規開拓を勧める
   - 在庫過剰と書いて「在庫増やす」NG
   - 現金減少と書いて「設備投資」NG
   - 人件費重と書いて「採用強化」NG（業務委託化等の文脈なら可）

9. **規模と健全性に合わない提案禁止**: 状態診断の `forbidden_proposals` を必ず守る。
   売上1-3億の会社にIPO・大型M&Aを書かない。買収余力不足で他社買収を書かない。

10. **税理士負荷の最小化**: 提案の executor は基本 `referral_partner`（外部紹介）か
    `owner`（社内実行）。`tax_accountant` の場合、月次顧問範囲外なら
    `tax_accountant_upsell=true` + `upsell_message` で「別途お見積もりとなる可能性」明示。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【分析の軸】必ず守る（絶対ルール ①〜⑦の詳細）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔑 **コア：数字の"動き"から課題を特定する**
   個別の数値の高低ではなく、「前期比でどう動いたか」「絶対水準として健全か」「内訳がどう変化したか」「現金がどう動いたか」から課題を発見する。
   数字が動いた → その裏で何が起きているか → 何が課題で、どう手を打つか、の順で考える。

⚠️ **業界比較禁止**（絶対ルール①参照）
   benchmark データは内部参考のみ。社長向け出力（key_insight/summary/strengths/issues/owner_message/detail）に業界平均比較を書かない。

5つの見方：
① **時間軸（前期比・複数年トレンド）** — 伸び率で判断。絶対額に惑わされない
② **構成軸（内訳の変化）** — 合計が同じでも、中身の割合変化で何かが起きている
③ **絶対水準軸** — 一般的な安全水準（売掛≤45日、自己資本比率≥30%、月商キャッシュ≥1.5ヶ月、負債比率≤200% 等）との乖離で評価
④ **キャッシュ軸** — 利益より現金の動きを重視
⑤ **経営体力軸** — 損益分岐点売上、月次固定費、返済後キャッシュ、人件費生産性（一人当たり粗利）— 社長の意思決定に直結する指標を必ず1つ以上 issues か prioritized_problems に反映

よくある"数字の動き"パターン（**例示、これに限定しない**。自分で見つけてほしい）：
- 売上↓ なのに 人件費↑ → 人員再配置が進んでいない
- 売上↑ ペース < 売掛金↑ ペース → 売上が現金化されていない可能性。**ただし判定は「絶対金額」ではなく「回収日数（DSO）」で行う**。DSOが健全水準（≤45日）なら絶対金額増は自然増として扱う
- 売上原価↑ ペース < 在庫↑ ペース → 在庫滞留の可能性
- 粗利率が突然改善 → 会計方針変更・商品構成変化の有無確認
- 広告費が急増しているのに売上伸びない → マーケ投資効率の課題
- 営業利益 ＞ 0 なのに現預金↓ → キャッシュ化されていない
- 有利子負債↑ なのに設備投資なし → 運転資金が回っていない可能性
- 未収入金・仮払金が売上の5%超 → 内訳確認

**🎯 営業利益率悪化を課題化する場合の必須事項**
営業利益率が前期比で1pt以上悪化した場合、prioritized_problems に書くなら以下を満たす：
1. fact に **どの費目が・前期からいくら増えたか** を具体明示（上の【費目別 前期比較】データを使う）
2. 上位3-5費目の前期比増減を fact に書く（例：「人件費 +800万円 / 燃料費 +400万円 / 修繕費 +300万円」）
3. hypothesis は数字根拠ベース（「燃料費+400万円は原油価格上昇 or 走行距離増？」等）
4. solutions は **特定の費目** をターゲットに（人件費なら役員報酬最適化／燃料費なら走行管理 等）
5. 費目データがない場合は observation_notes に「販管費明細の再確認が必要」と書く

つまり **「どの数字が・どう動いて・何の課題を示しているか」** の組を見つけ、issues と actions に反映せよ。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【🚨 健全水準ガイドライン（絶対値の判定基準）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
以下の絶対水準を満たす数字は **「健全」＝prioritized_problems に書かない**。
書くなら strengths（数字根拠付き）か observation_notes（念のため確認レベル）に入れる。

| 指標 | 健全（課題NG） | 要観察（念のため確認） | 課題（要対策） |
|---|---|---|---|
| 売掛回収日数 (DSO) | ≤45日 | 45-60日 | >60日 |
| **🚨 売掛の判定は必ず DSO（日数）で。「売掛金が◯倍に急増」のような絶対金額表現は禁止。売上が伸びれば売掛も伸びるのは自然。日数（DSO）が健全なら課題化しない** | | | |
| 在庫回転日数 | ≤30日 | 30-60日 | >60日 |
| 買掛支払日数 | 30-60日 | 60-90日 | >90日 |
| 月商キャッシュ倍率 | ≥2ヶ月 | 1.5-2ヶ月 | <1.5ヶ月 |
| 自己資本比率 | ≥30% | 20-30% | <20% |
| 負債比率（負債/自己資本） | ≤100% | 100-200% | >200% |
| 有利子負債/EBITDA | ≤3倍 | 3-5倍 | >5倍 |
| 営業利益率 | 業界相当（運輸2-5%、製造3-7%、小売1-3%、サービス5-10%） | 1-2%下回り | 赤字 or 大幅下回り |
| 流動比率 | 120-200% | 100-120% or 200%超 | <100% |

【重要ルール】（絶対ルール⑦の詳細）
- 健全水準内（左列）の数字は **絶対に prioritized_problems に入れない**
- 健全水準内でも「前期比で大きく悪化（-20%超）」なら observation_notes に「念のため確認」で記載
- 「売掛18日」「現金月商3ヶ月」「自己資本50%」を課題化したら明らかに過剰反応。出力禁止

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【strengths 禁止リスト】（1つでも該当したら書くな → issues 行き）
❌ 「流動比率が高い」系（売掛>60日 or 在庫>60日 の時）
❌ 「運転資本◯億の現金化余地」系（滞留の証拠を強みに書くな）
❌ 「支払利息が軽微」系（元本が売上30%超なら NG）
❌ 「自己資本比率プラス」だけ（負債比率 200%超なら issues）
❌ 「形式上／見た目は／数字上は」ヘッジ表現を含むもの
❌ 本業赤字時のコスト構造系（販管費率・人件費率が業界内 等）

✅ strengths OK：粗利率が業界超・売掛/在庫≤45日・(自己資本40%超 & 負債比率≤100%)・月商キャッシュ倍率≥2ヶ月・事業固有（ブランド/商品/設備/顧客/技術）

【書き方ルール】（絶対ルール③④⑤の補足）
- 抽象論（「効率化を」「見直しを」）一切禁止。項目名と金額で語る
- 金額試算は必ず数字：「推定で年間◯◯万円」
- actions には「誰が・いつまでに・一歩目」を必ず入れる
- 表現は中立・丁寧。「粉飾／不正／虚偽」断定語禁止 → 「実態の確認が必要」「可能性」「再確認を推奨」
- 全体10,000文字以内。長文禁止

【業種信頼度】
業種不明／事業構成に2業態以上／粗利率が業界平均から±10%超乖離 → industry_confidence="low"
low の時は hearing_sheet.business_understanding に業態比率・業態別粗利率・主要顧客の質問を必須。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【企業情報】
会社名: {client_name}
業種: {industry if industry else "不明"}
対象期間: {fd.period}
{business_context}
{state_text}
{phase_text}
{idle_text}
【財務データ（単位：万円）】
売上: {fd.revenue:,.0f}（{growth_text}）
売上原価: {fd.cost_of_sales:,.0f} / 粗利: {fd.gross_profit:,.0f}（{gross_margin:.1f}%）
販管費: {fd.selling_expenses:,.0f} / 営業利益: {fd.operating_profit:,.0f}（{operating_margin:.1f}%）
経常利益: {fd.ordinary_profit:,.0f} / 純利益: {fd.net_profit:,.0f}
{bs_text}
{breakdown_text}
{bs_correction_text}
{cost_delta_text}
{historical_text}
{benchmark_text}
{cash_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSON】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "key_insight": "<1行の最重要ポイント。核心を一撃で>",
  "company_state_echo": "<上記【会社の現状診断】の state.health と state.scale をそのまま echo。例『scale=small, health=excess_cash』>",
  "prioritized_problems": [
    {{
      "rank": <1-5の整数。1が最重要>,
      "severity": "<高/中/低>",
      "title": "<課題タイトル20-30字。🚨 断定的なネガティブ表現は避ける（『未整備』『できていない』『不足』『欠如』禁止）。代わりに『〜に改善余地』『〜の精度向上の機会』『〜の見直しタイミング』『〜の最適化余地』のような前向きな表現を使う。事実は fact に書き、title は『次の打ち手につながる前向きな問題提起』にする。例：『売上の77%が1社依存（取引分散の検討余地）』『投資した資産の見える化に改善余地』『販管費の費目分析の精度向上機会』>",
      "fact": "<事実。数字のみで断定可能なこと。例『2025年売上2.73億円のうちいであ㈱が2.10億円(77%)』>",
      "hypothesis": "<仮説。なぜこの状況になっているか・何を示すか。断定せず『可能性』『考えられる』を使う。例『販路が固定化し新規開拓リソースが枯渇している可能性』>",
      "confirmation_needed": ["<税理士が社長にヒアリングして確認すべき事項。例：『新規顧客の獲得チャネル』『営業担当の人数と稼働』>"],
      "detail": "<2-3文の詳細説明。数字を含める。社長向けの易しい言葉で。事実→仮説→影響の順>",
      "solutions": [
        {{
          "title": "<ソリューション名30字以内>",
          "category": "<revenue/cost/finance/tax/regulation のいずれか>",
          "approach_type": "<内製/外注/金融/節税/規程>",
          "executor": "<'tax_accountant' / 'owner' / 'referral_partner'>",
          "tax_accountant_upsell": "<executor=tax_accountant の場合のみ。true=月次顧問の範囲外（追加見積もり対象）、false=範囲内（通常作業）>",
          "upsell_message": "<tax_accountant_upsell=true の場合のみ。例『この打ち手は税理士の月次顧問業務の範囲外となる可能性があります。御社の税理士に相談すると対応してもらえるかもしれませんが、別途お見積もりとなる場合があります』>",
          "referral_partner_type": "<executor=referral_partner の場合のみ。紹介先の種類>",
          "impact_min": <万円。年間効果の最小>,
          "impact_max": <万円。年間効果の最大>,
          "impact_basis": "<効果金額の算式>",
          "timeframe": "<すぐできる/1-3ヶ月/3-6ヶ月/6ヶ月以上>",
          "difficulty": "<低/中/高>",
          "first_step": "<明日からの具体的な一歩>",
          "why": "<1文。なぜこのソリューションが効くか>"
        }}
      ],
      "expected_outcome": "<1-2文。改善後の数字イメージ。例：依存度77%→50%、売上+1,500万円/年で現金流出を補える絵>",
      "related_metrics": ["<この課題と直接連動する数値カードのキー。複数可。次のキーから選ぶ：cash（現預金）/revenue（売上）/gross_profit（粗利）/operating_profit（営業利益）/net_profit（純利益）/ordinary_profit（経常利益）/cost_of_sales（売上原価）/selling_expenses（販管費）/total_assets（総資産）/equity（純資産）/interest_bearing_debt（有利子負債）/equity_ratio（自己資本比率）/current_ratio（流動比率）/receivables_days（売掛サイクル）/inventory_days（在庫サイクル）/payables_days（買掛サイクル）/operating_cf（営業CF）/free_cf（フリーCF）>"]
    }}
  ],
  "owner_message": "<社長向け1-2文。数字が苦手な経営者が30秒で判断できる言葉で。専門用語禁止（幼児語ではなく、経営者語）。**不安を煽る表現禁止**（『危険』『底をつく』『破綻』→ 『気になる』『少し心配な』『余裕が薄くなってきた』に置換）。**事実+所感+問いかけ**の順で。例：『本業は黒字ですが、現金が3年で1.3億→6,600万円に減ってきています。借入の返済が月193万円あるためです。このペースだと余裕が薄くなっていく感じがあります。』>",
  "owner_what_to_do": "<社長向け1-2文。**命令調禁止**（『〜してください』連発NG）。**社長に問いかける形 / 一緒に考える形 / 選択肢を提示する形**で。社長自身に考えさせ、最終判断は社長に委ねる伴走スタンス。例：『主要取引先からの追加受注は、どこまで伸ばせそうですか？同時に他社向けの新規開拓も、月◯件ぐらい狙えると、依存度を下げながら売上も伸ばせる絵が見えてきます。一度社長のお考えを聞かせてください。』>",
  "industry_confidence": "<high/medium/low>",
  "data_limitations": ["<データの限界・推計前提>"],
  "score": <0-100整数>,
  "score_label": "<優良/良好/注意/危険>",
  "summary": "<2-3文の総評。数字を含める>",
  "strengths": ["<3-5件、数字根拠付き。禁止リスト該当ゼロ>"],
  "issues": ["<3-5件。数字の動きから発見した課題を中心に。金額インパクト付き>"],
  "observation_notes": [
    {{
      "metric": "<指標名。例『売掛回収日数』『在庫回転日数』『現預金月商比』>",
      "current_value": "<現在値。例『18日』『3.2ヶ月分』>",
      "judgment": "<判定。例『健全水準内（45日以下）』『要観察（前期比+8日悪化）』>",
      "check_point": "<税理士が念のため社長に確認する1文。例『売掛18日は早いですが、前受金や前払いの会計処理が混ざっていないか確認してください』>"
    }}
  ],
  "growth_opportunities": [
    {{
      "rank": <1-7の整数>,
      "category": "<tax_optimization/employee_return/investment/new_business/franchise/m_and_a/ipo/regulation/cfo_outsource のいずれか。succession（事業承継）は hearing_sheet で社長年齢・後継者を確認するまで提案禁止>",
      "title": "<打ち手タイトル30字以内。専門用語禁止（EBITDA・DSO等を使わず社長語で）>",
      "executor": "<この打ち手の実行主体: 'tax_accountant'（税理士が直接やる）/ 'owner'（社長が社内でやる）/ 'referral_partner'（外部パートナー紹介）。🚨 税理士に手間をかける作業ベース提案は最小限に。データ可視化・運用代行・M&A仲介・不動産・有価証券アドバイス等は referral_partner 推奨>",
      "tax_accountant_upsell": "<executor=tax_accountant の場合のみ。true/false。月次顧問契約の通常範囲を超える追加業務なら true（例：投資回収シナリオ設計、KPI設計、月次経営会議運営、補助金申請代行、財務戦略策定、データ可視化伴走）。通常範囲なら false（規程ひな形提供、申告書作成、年末調整、税務相談）。executor が他なら null>",
      "upsell_message": "<tax_accountant_upsell=true の場合のみ書く。例：『この打ち手は税理士の月次顧問業務の範囲外となる可能性があります。御社の税理士に相談すると対応してもらえるかもしれませんが、別途お見積もりとなる場合があります』。tax_accountant_upsell=false なら空文字>",
      "referral_partner_type": "<executor=referral_partner の場合、紹介先の種類。『CFO代行サービス』『データ可視化BPO（kintone代行/Excel Pro等）』『M&A仲介（トランビ・バトンズ等）』『不動産投資会社』『証券会社・IFA』『社労士法人』『業務SaaS代理店』等。executor が他なら空文字>",
      "fact": "<事実根拠。例『現預金12,000万円、本業の現金を生む力（営業利益+減価償却）は月+800万円ペース』>",
      "rationale": "<なぜこの会社/規模に有効か。1-2文。専門用語は()で補足>",
      "concrete_calculation": "<🚨 必須：具体的な数字計算。投資額・効果・利率を明示。例『従業員30名×30万円=900万円。法人税▲297万円（実効税率33%）。社員1人あたり手取り+24万円』『バス2台×1,200万円=2,400万円。即時償却で当期費用化、法人税▲792万円』『3,000万円を社債3年運用、年利2%で年間+60万円』。曖昧な『年間◯◯-◯◯万円』だけはNG、根拠数式を含めること>",
      "actions": ["<具体アクション40字以内。誰が何をするか明確に>", "<もう1つ>"],
      "impact": "<効果サマリ。concrete_calculation の結論を1-2行で。例『年間+62万円節税（法人税▲32万+所得税▲30万）』>",
      "simple_explanation": "<🚨 必須：社長語で30秒理解できる1-2文。専門用語ゼロ。例『余ってるお金の一部で会社を買うやり方。買収費用の6/10（最大800万円）が国から戻ってくる補助金もあります』>",
      "verify_first": "<実行前に確認すべき既存の対策・前提。例『既に小規模企業共済加入済か確認』。なければ空文字>",
      "differentiation": "<他の似た打ち手と何が違うか、なぜこれが必要か。1文。機械的羅列禁止>",
      "scale_fit_note": "<この会社の規模（{state['scale_label']}）に適している理由を1文>"
    }}
  ],
  "kpi_watch": [{{"name":"<KPI>", "current":"<現在>", "target":"<目標>", "comment":"<1文>"}}],
  "hearing_sheet": {{
    "business_understanding": ["<事業理解 3-5件>"],
    "customer_market": ["<顧客・市場 3-5件。営業体制・新規獲得チャネル・競合認識を必ず1件含める>"],
    "growth_opportunity": ["<成長機会 2-4件>"],
    "debt_and_finance": ["<借入・財務 2-4件>"],
    "regulations_and_compensation": ["<規程・役員報酬 2-4件。**以下を必ず聞く**：①出張規程・旅費規程の有無と日当額、②役員社宅規程の有無、③役員退職金規程の有無、④役員報酬の月額・賞与構成、⑤福利厚生規程の有無、⑥小規模企業共済・経営セーフティ共済・法人保険の加入状況（重複節税提案を防ぐ）>"],
    "owner_and_succession": ["<社長・承継 2-3件必須。**以下を必ず聞く**：①社長の年齢、②後継者候補の有無（家族 / 役員 / 外部）、③事業承継・M&A・親族外承継の検討状況。これが未確認のうちは事業承継・M&A 提案は禁止>"],
    "sales_and_marketing": ["<営業・マーケ 2-3件。**以下を必ず聞く**：①営業担当の人数と稼働、②新規顧客の獲得チャネル、③Webサイト経由の問い合わせ件数、④外部営業代行・広告代理店の利用有無>"]
  }},
  "tax_savings_advice": {{
    "applicable": <true/false。営業利益>0かつ純利益>0なら true。それ以外は false>,
    "headline": "<黒字社向け1文。例『今期は黒字着地。納税前に投資判断を絡めた節税策で〇〇万円圧縮可能』>",
    "ideas": [
      {{
        "title": "<施策名。例：中小企業経営強化税制で機械設備の即時償却>",
        "category": "<設備投資/退職金準備/共済/保険/福利厚生/役員報酬/その他>",
        "estimated_tax_save": <万円。実効税率33%で概算>,
        "investment_required": <万円。施策実行に必要な支出。0なら経費化のみ>,
        "deadline": "<決算前/期中いつでも/翌期初/その他>",
        "why": "<1-2文。今期の黒字幅・キャッシュ余力との整合>",
        "how": "<2-3文。具体的な手順>",
        "concrete_calculation": "<🚨 必須：数式で。例『バス2台×購入価格1,200万円=2,400万円を即時償却。当期費用化で課税所得▲2,400万円、法人税等▲792万円（実効税率33%）』『役員報酬を月10万円減額×12ヶ月=120万円を会社残留、法人税等+39万円増だが社長個人の所得税▲30万円・社保▲17万円で家計的にプラス』。曖昧な金額のみは NG>",
        "simple_explanation": "<🚨 必須：社長語で30秒理解。専門用語ゼロ。例『来期のバス購入費を全額今期の費用にすると、税金が約790万円減ります』>",
        "executor": "<'tax_accountant' / 'owner' / 'referral_partner'>",
        "verify_first": "<🚨実行前に必ず確認すべき既存対策。例『法人保険・経営者保険に加入済か。加入済なら経営セーフティ共済との重複・代替を検討』。重複の可能性がある場合は具体的に書く>",
        "differentiation": "<他の節税策と何が違うか、なぜこの会社にこれが必要か。1文>",
        "warning": "<キャッシュアウト・縛り期間・将来の課税繰延のリスク>"
      }}
    ],
    "strategic_note": "<2-3文。節税は『キャッシュを使う節税』『使わない節税』『繰延』の3軸で整理。投資判断（機械・人材・販路）と紐づけて打つべき>"
  }},
  "quality_check": {{
    "strengths_validation": "<禁止リストに該当しないか自己検証した結果を1文で>",
    "issues_coverage": "<数字の動きから課題を発見できたか・絶対水準軸・キャッシュ軸・経営体力軸（損益分岐点／人件費生産性／返済後CF）が反映できているか>",
    "action_specificity": "<アクションに金額/担当/期限/一歩目が入っているか>",
    "warnings": ["<自己レビューで気になった点>"]
  }}
}}

【件数ルール（state.health で切り替え）】
■ health=excess_cash（余剰キャッシュ過多）の場合：
  - prioritized_problems: **0-2件のみ**。本当の課題がなければ空配列でOK。無理に課題を捻り出すな
  - growth_opportunities: **5-7件**（攻めモード。節税/還元/投資/新規事業/FC加盟/M&A/承継/規程 等）
  - tax_savings_advice.ideas: 3-5件（必須）

■ health=struggling（経営苦境）の場合：
  - prioritized_problems: **3-5件**（rank=1が最重要、現行通り）
  - growth_opportunities: **0-2件**（基本は再建優先、余力ある時のみ）
  - tax_savings_advice.applicable: false でも可

■ health=balanced（普通）の場合：
  - prioritized_problems: **1-3件**
  - growth_opportunities: **2-4件**
  - tax_savings_advice.ideas: 3-5件（黒字時）

共通：
- 各problem の solutions は 3-6件、kpi_watch 3-5件
- **observation_notes は 0-5件**（健全水準内の数字で「念のため確認」レベル。健全な数字を課題化する代わりにここへ）
- revenue_ideas / cost_ideas / actions / cross_sell / integrated_turnaround_plan は廃止（後処理で派生）

【prioritized_problems の重要ルール（のだけ先生FB対応 2026-05-15 + 2026-05-29追加）】
これがレポートの主役。「課題 → ソリューション → 予想効果」の構造で、税理士が社長に提案するときの主軸となる：
- rank=1 は「この会社が今一番取り組むべき課題」を1つだけ
- rank=2,3 は次点
- 各 problem の solutions は「同じ課題に対する複数の打ち手」（内製・外注・節税・規程の組み合わせ）
- 各 solution には category={{revenue/cost/finance/tax/regulation}} を必ず付ける（後処理で売上系/コスト系/財務系に分類するため）
- impact_min/max は具体的な万円。「やる前 → やった後」の差分。impact_basis に算式必須
- expected_outcome は「数字+ストーリー」で社長が腹落ちする表現に
- 課題と打ち手の論理整合は厳守（依存度高い課題に「その顧客から追加受注」は絶対書かない等、既存ルール継続）

【🔥 キャッシュバーン（現金流出）の必須化ルール（Takeru FB 2026-05-29）】
以下のいずれかに該当する会社では、**キャッシュバーン課題を必ず prioritized_problems に含める**こと：
- 営業CF が赤字（推定でも）
- 実質バーン（営業損益 - 借入返済推定）がマイナス
- 現預金が前期比 -10% 以上の減少
- 現預金÷月商 < 1.5ヶ月
- バーンレート × 12 > 現預金（=1年で枯渇ペース）

この場合、現預金推移・月次バーン額・資金余力月数 を detail に明示し、solutions には：
- リスケ・借換等の財務系
- 売掛回収サイクル短縮
- 在庫圧縮
- 経費削減
- 資本性ローン / ファクタリング
等を含めること。**「黒字なのに現金が減る」状況は社長の最大関心事なので、絶対に隠してはいけない。**

【社長向け表現の伴走型ルール】（絶対ルール③④⑤⑧の補足）
owner_message と owner_what_to_do は、決算書を読めない社長に1秒で伝わる「伴走型」表現にすること。
※ 専門用語の扱い: 絶対ルール③④に従い「用語（説明）」形式で書く（古いルール「専門用語禁止＝置換」はもう使わない）

■ 数字は「〇〇万円増えた」「〇〇％下がった」など、増減と方向で伝える

■ 社長が嫌がる表現を絶対に避ける（具体例）
- 不安煽り禁止：「危険です」「底をつきます」「破綻します」「手遅れになる」
   → 代替：「気になる動きです」「余裕が薄くなってきた」「少し心配な水準」
- 命令調連発禁止：「〜してください」を1メッセージに3回以上 NG
   → 代替：「〜という選択肢があります」「〜はいかがでしょうか」「社長のお考えはどうですか」
- 上から目線禁止：「やるべきです」「すべきです」「危ないですよ」
   → 代替：「やってみる価値があります」「検討の余地があります」「一緒に考えませんか」
- 断定で追い詰める禁止：「77%依存は危険」「3年で底をつく」
   → 代替：「77%依存だと一社に何かあったとき影響が大きい」「このペースだと現金の余裕が減っていく」

■ 伴走スタンス
- 問いかけ形（「どこまで伸ばせそうですか？」「お考えを聞かせてください」）
- 社長自身に考えさせる、最終判断は社長に委ねる
- 数字を出して気づきを促すが、結論を押し付けない

■ owner_what_to_do の良い例 / 悪い例
❌ 「来月までに2件以上受注してください。依存度8割以上は危険なので、月10件開拓してください。」
✅ 「主要取引先からの追加受注はどこまで伸ばせそうですか？同時に他社向けの開拓も月◯件ぐらい狙えると、依存度を下げながら売上も伸ばせる絵が見えてきます。社長のお考えを聞かせてください。」

■ ロジック整合の具体例（絶対ルール⑧の詳細）
- 「特定顧客に集中依存（売上50%超を1社）」を指摘 → その顧客への「追加受注」「取引拡大」NG。
  ✅ その顧客以外の「**新規顧客の開拓**」「**取引先の分散**」を勧める
  ✅ 既存顧客への追加受注を触れる場合は「依存度を下げる文脈の中で」のみ
- 「在庫過剰」を指摘 → 「在庫を増やそう」NG
- 「現金が減っている」を指摘 → 「設備投資を進めましょう」NG
- 「人件費が重い」を指摘 → 「採用を強化しましょう」NG（業務委託化等の文脈なら可）
- owner_message で問題提起したら、owner_what_to_do は **その問題を解く方向** で書く

【tax_savings_advice の重要ルール】
- 営業利益<=0 または 純利益<=0 のときは applicable=false にして ideas を空配列にする（赤字社に節税は意味がない）
- applicable=true のときは、今期の純利益・キャッシュ残高を踏まえて「使う節税（投資型）」「使わない節税（経費化）」「繰延型」をバランスよく提示
- **🚨 機械的羅列の絶対禁止**：節税3点セット（旅費規程・社宅規程・経営セーフティ共済）を毎回コピペ提案するのは禁止。会社の状況に合わせて選別する
- estimated_tax_save は実効税率33%で概算
- 各 idea の note に **「他の節税策と何が違うか」「既に類似対策が入っていないか確認すべき点」** を必ず書く
- **以下の打ち手は候補リスト**。会社の状況・hearing_sheet の回答から「実態として未導入の可能性が高いもの」だけを idea に入れる。実態確認なしの機械的提案は禁止：
  ① 旅費規程・出張規程の整備（日当・宿泊費の経費化、社長と従業員の所得税も最適化）
  ② 役員社宅規程（自宅家賃の50%程度を法人経費化）
  ③ 役員退職金規程＋小規模企業共済の組み合わせ
  ④ 中小企業経営セーフティ共済（年間最大240万円、40ヶ月で100%返戻）※ ただし既に法人保険・経営者保険に加入済の場合は重複・代替の有無を verify_first で必ず確認
  ⑤ 中小企業経営強化税制（設備投資の即時償却）
  ⑥ 企業版ふるさと納税（法人税最大9割軽減）
  ⑦ 役員賞与・決算賞与による所得分散

【prioritized_problems.solutions で必ず検討する打ち手】
- 自社の営業体制が薄い／顧客集中度が高い／新規開拓が課題と判定した場合は、以下を category="revenue" の solution として1件以上含める：
  ① **営業代行・インサイドセールス代行の活用**（自社で営業強化が難しい場合の現実解）
  ② **B2Bマーケティング外注**（Web集客・コンテンツマーケ・リスティング広告）
  ③ **業界団体加盟・展示会出展**（同業ネットワーク経由の紹介案件獲得）
  ④ **既存顧客のクロスセル**（ただし依存度高い顧客はNG、分散している場合のみ）

【🚨 JSON出力の絶対ルール】
- 出力は単一のJSONオブジェクトのみ。前置き・後置き・コメント・マークダウン禁止
- 末尾カンマ（,}}や,]）禁止
- 文字列内の改行は \\n でエスケープ（生の改行禁止）
- すべてのキーと文字列値はダブルクォートで囲む
- 数値フィールド（impact_min, impact_max, score 等）は数値リテラルで、引用符で囲まない
- 全フィールド出力後に必ず }} で閉じる。途中で切れない長さに収める

【🎯 提案実行主体ルール】（絶対ルール⑩の詳細）
各 growth_opportunity / solution の `executor` を必須付与：
- **tax_accountant**: 税理士が直接やる（最小限に。例: 役員報酬最適化、節税申告、規程ひな形提供）
- **owner**: 社長 / 社内が実行（例: 採用、賞与決定、KPI入力）
- **referral_partner**: 🌟 推奨。外部パートナーを税理士が紹介して実作業は委託（CFO代行・BPO・M&A仲介等）

【🌟 税理士アップセル判定ルール（重要 - CoPartnerの差別化機能）】
executor=tax_accountant の場合、`tax_accountant_upsell` を以下の基準で判定する：

**true（月次顧問の範囲外＝追加見積もり対象）にすべき業務：**
- 投資回収シナリオの設計・モニタリング
- KPI設計・ダッシュボード構築・運用支援
- 月次経営会議の運営・ファシリテーション
- 補助金申請書の作成代行（事業承継・引継ぎ補助金、ものづくり補助金等）
- 財務戦略策定・3年事業計画作成
- M&A準備サポート（DD立ち会い・価値算定）
- 役員退職金プランニング
- 事業承継スキーム設計
- 販管費の費目別前期比分析レポート作成
- 経営者保険の最適化コンサル

**false（月次顧問の範囲内＝通常業務）にすべき業務：**
- 申告書作成・年末調整
- 通常の税務相談
- 規程ひな形の提供（旅費規程・社宅規程等）
- 単純な節税アドバイス
- 月次試算表の作成

upsell_message には以下のテンプレ表現を使う：
「この打ち手は税理士の月次顧問業務の範囲外となる可能性があります。御社の税理士に相談すると対応してもらえるかもしれませんが、別途お見積もりとなる場合があります」

→ これによりレポート画面で『💼 税理士の追加サービス候補』バッジ表示され、税理士のアップセル機会になる。

referral_partner_type の代表例（できるだけこれを使う）：
- **CFO代行サービス**（月10-30万円。投資管理ダッシュボード・月次経営会議運営・KPI設計）
- **データ可視化BPO**（kintone代行・Excel Pro・freee運用代行。月3-10万）
- **M&A仲介**（トランビ・バトンズ・日本M&Aセンター。成功報酬5-10%）
- **不動産投資会社**（収益不動産仲介・サブリース）
- **証券会社・IFA**（独立系ファイナンシャルアドバイザー）
- **社労士法人**（就業規則・賃金規程整備）
- **業務SaaS代理店**（freee・MoneyForward・楽楽精算等の導入支援）

【🚨 具体性必須ルール（concrete_calculation 必須）】
- 各 growth_opportunity と tax_savings_advice.idea には **concrete_calculation を必ず数式で書く**
- 曖昧な「年間500-1,000万円」だけは NG。**根拠数式（◯人×◯円 / 投資額×税率 / 元本×利率 等）を明示**
- 例:
  ❌ 「決算賞与で社員還元、年間500-1,000万円の節税」
  ✅ 「従業員30名×決算賞与30万円=支給総額900万円。法人税等▲297万円（実効税率33%）。1人あたり手取り+24万円（賞与30万-社保等6万）」

【📚 カッコ説明が必須な専門用語リスト】（絶対ルール③④の用語辞書）
判定基準: 「経理未経験の社長が読んで即座にわかるか？」分からない可能性ありなら必ずカッコ説明。
表記例: ✅「EBITDA（営業利益+減価償却＝本業の現金を生む力）」 ❌「EBITDA」単独・「本業の現金を生む力」単独

■ P/L関連
- 営業利益 → 「営業利益（本業による利益、雑収入・費用や税金は除く）」
- 経常利益 → 「経常利益（本業＋雑収入・費用、税金は除く）」
- 売上総利益 / 粗利 → 「粗利（売上−仕入原価）」
- 売上原価 → 「売上原価（商品やサービス提供のための直接費用）」
- 販管費 → 「販管費（人件費・家賃・広告費等の運営費用）」
- 減価償却費 → 「減価償却費（過去に買った設備・車両等を毎年少しずつ費用化する処理）」
- EBITDA → 「EBITDA（営業利益+減価償却＝本業の現金を生む力）」

■ B/S関連
- 自己資本 / 純資産 → 「自己資本（株主のお金や過去の利益の累積）」
- 有利子負債 → 「有利子負債（金利が発生する借入金）」
- 自己資本比率 → 「自己資本比率（自分のお金で経営している割合）」
- 流動資産 → 「流動資産（1年以内に現金化できる資産）」
- 流動負債 → 「流動負債（1年以内に支払う義務）」
- 流動比率 → 「流動比率（短期の支払い余力）」
- 売掛金 → 「売掛金（売上はしたがまだ入金されていないお金）」

■ キャッシュフロー関連
- キャッシュバーン / バーンレート → 「キャッシュバーン（現金が出ていくペース）」
- ランウェイ → 「ランウェイ（資金がもつ期間）」
- DSO / 売上債権回転日数 → 「DSO（売掛が現金になるまでの日数）」または「売掛回収日数（DSO）」
- CCC → 「CCC（払ってから回収するまでの日数）」
- 月商キャッシュ倍率 → 「月商キャッシュ倍率（現預金が月商の何ヶ月分あるか）」

■ 投資・評価指標
- ROI → 「ROI（投資に対する戻り）」
- IRR → 「IRR（投資効率）」
- NPV → 「NPV（投資の現在価値）」
- ROA / ROE → 「ROA（総資産利益率）」「ROE（自己資本利益率）」

■ サービス・パートナー名
- BPO → 「BPO（業務委託サービス）」
- CFO代行 → 「CFO代行（外部の財務責任者サービス）」
- IFA → 「IFA（独立系のお金の相談役）」
- M&A → 「M&A（会社の買収・合併）」
- DD → 「DD（買収前の調査）」

■ 制度・税制
- 即時償却 → 「即時償却（買った年に全額を経費化できる優遇税制）」
- 経営強化税制 → 「中小企業経営強化税制（設備投資の即時償却が使える制度）」
- 経営セーフティ共済 → 「経営セーフティ共済（取引先倒産時の無担保融資制度。掛金も全額経費）」
- 小規模企業共済 → 「小規模企業共済（経営者の退職金積立制度）」

■ 適用ルール
- **各フィールドごとに「初出扱い」**。同フィールド内では2回目以降省略可
- 特に owner_message / owner_what_to_do / prioritized_problems.fact / observation_notes.judgment では必須
- 「（説明）」は12-25字程度に簡潔に
- 省略しがちな日常用語（営業利益・販管費・減価償却費・粗利・経常利益）も必ず初出で説明
- 表記ブレ禁止の具体例: 「営業利益率」と「営利率」混在NG / 「粗利」と「売上総利益」混在NG /
  「売掛回収日数(DSO)」と「売上債権回転期間」混在NG / 「EBITDA」と「本業の現金生成力」単独混在NG

【📐 単位ルール】（絶対ルール②の補足）
- 入力データはすべて「万円」単位
- 出力は「万円」基準。重要数字（fact / detail / 金額）は万円表記必須
- 例外: owner_message 等の日常会話的な丸めは「約7億円」可。ただし主要数字は万円厳守
- 桁ミス検証: 出力した数字が入力と桁照合できるか必ず確認"""

    text = _call_llm(prompt, provider)
    result = _parse_json_response(text)

    # 業界ベンチマーク・キャッシュ診断・運転資本・EBITDA・トレンドをレスポンスに同梱
    result["benchmark"] = benchmark
    result["burn_rate"] = burn
    result["working_capital"] = wc
    result["ebitda"] = ebitda
    result["trend_metrics"] = trend_metrics
    result["company_state"] = state  # 規模・健全性・禁止提案リスト（UI/PDF/バリデーション用）

    # 各指標の前期比を計算（テンプレート表示用）
    prev_period_data = None
    if historical_data:
        sorted_hd_for_prev = sorted(historical_data, key=lambda x: x.period or "")
        for h in sorted_hd_for_prev:
            if h.id == fd.id:
                break
            prev_period_data = h
    growth_rates = {}
    if prev_period_data:
        if prev_period_data.revenue:
            growth_rates["revenue"] = round((fd.revenue - prev_period_data.revenue) / abs(prev_period_data.revenue) * 100, 1)
        if prev_period_data.gross_profit:
            growth_rates["gross_profit"] = round((fd.gross_profit - prev_period_data.gross_profit) / abs(prev_period_data.gross_profit) * 100, 1)
        if prev_period_data.operating_profit and prev_period_data.operating_profit != 0:
            growth_rates["operating_profit"] = round((fd.operating_profit - prev_period_data.operating_profit) / abs(prev_period_data.operating_profit) * 100, 1)
        if prev_period_data.net_profit and prev_period_data.net_profit != 0:
            growth_rates["net_profit"] = round((fd.net_profit - prev_period_data.net_profit) / abs(prev_period_data.net_profit) * 100, 1)
        if prev_period_data.total_assets:
            growth_rates["total_assets"] = round((fd.total_assets - prev_period_data.total_assets) / abs(prev_period_data.total_assets) * 100, 1) if fd.total_assets else None
        if prev_period_data.equity:
            growth_rates["equity"] = round((fd.equity - prev_period_data.equity) / abs(prev_period_data.equity) * 100, 1) if fd.equity else None
        if prev_period_data.interest_bearing_debt:
            growth_rates["interest_bearing_debt"] = round((fd.interest_bearing_debt - prev_period_data.interest_bearing_debt) / abs(prev_period_data.interest_bearing_debt) * 100, 1) if fd.interest_bearing_debt else None
        if prev_period_data.cash:
            growth_rates["cash"] = round((fd.cash - prev_period_data.cash) / abs(prev_period_data.cash) * 100, 1) if fd.cash else None
    result["growth_rates"] = growth_rates

    # 健全性スコアを Python 計算で上書き（揺らぎ防止・ロジック固定）
    # historical_data も渡して「数字の動きの異常」もペナルティに反映
    health = compute_health_score(fd, benchmark=benchmark, ebitda=ebitda,
                                   historical_data=historical_data)
    result["score"] = health["score"]
    result["score_label"] = health["score_label"]
    result["score_color"] = health["score_color"]
    result["score_breakdown"] = health["breakdown"]
    result["score_penalties"] = health.get("penalties", [])

    # 補助金・助成金マッチング
    result["subsidies"] = match_subsidies(
        fd, breakdown=breakdown, result=result,
        business_details=business_details,
        industry=industry,
        limit=8
    )

    # 提携パートナー（保険・銀行・リース）マッチング
    result["finance_partners"] = match_finance_partners(
        fd, ebitda=ebitda, working_capital=wc
    )

    # 他社比較した強み（業界ベンチマークから抽出）
    result["competitive_strengths"] = extract_competitive_strengths(benchmark, fd)

    # アフィリエイト商品マッチング（referral_code でリンク識別、税理士の除外設定反映）
    result = attach_affiliates_to_result(
        result, fd, industry=industry, referral_code=referral_code,
        excluded_categories=excluded_categories or []
    )

    # 旧フィールド互換維持（hearing_questions → hearing_sheet.business_understanding）
    if "hearing_sheet" in result and "hearing_questions" not in result:
        sheet = result.get("hearing_sheet") or {}
        flat = []
        for k in ("business_understanding", "customer_market", "growth_opportunity", "debt_and_finance"):
            flat.extend(sheet.get(k, []) or [])
        result["hearing_questions"] = flat

    # 後処理：ロジック整合バリデーション（プロンプト任せにしない）
    _validate_logic_consistency(result)

    # 後処理：健全水準の数字が課題化されてないか検出（過剰反応の自動格下げ）
    _demote_healthy_numbers_from_problems(result, fd, wc)

    # 後処理：派生フィールド生成（旧 AI 出力フィールドの互換を維持）
    # AI には prioritized_problems[].solutions[] に category={revenue/cost/finance/tax/regulation} を
    # 付けて出させるだけ。revenue_ideas / cost_ideas / actions は Python 側で派生する。
    _derive_legacy_fields_from_solutions(result)

    return result


def _demote_healthy_numbers_from_problems(result: dict, fd, wc: dict) -> None:
    """健全水準の数字（売掛≤45日、現金≥2ヶ月、自己資本≥30%等）が
    prioritized_problems に出ていたら observation_notes に降格させる。
    AIの過剰反応を機械的に抑制。
    """
    if not isinstance(result, dict):
        return
    problems = result.get("prioritized_problems") or []
    if not problems:
        return

    wc = wc or {}
    receivables_days = wc.get("receivables_days") or 999
    inventory_days = wc.get("inventory_days") or 999
    cash_months = wc.get("cash_months_of_sales") or 0
    equity = getattr(fd, "equity", 0) or 0
    total_assets = getattr(fd, "total_assets", 0) or 1
    equity_ratio = (equity / total_assets * 100) if total_assets else 0

    healthy_signals = []
    if receivables_days <= 45:
        healthy_signals.append(("売掛", "回収", f"{receivables_days:.0f}日"))
    if inventory_days <= 30:
        healthy_signals.append(("在庫", "回転", f"{inventory_days:.0f}日"))
    if cash_months >= 2:
        healthy_signals.append(("現金", "月商", f"{cash_months:.1f}ヶ月分"))
    if equity_ratio >= 30:
        healthy_signals.append(("自己資本", "比率", f"{equity_ratio:.0f}%"))

    if not healthy_signals:
        return

    demoted = []
    kept = []
    notes = result.setdefault("observation_notes", []) or []
    if not isinstance(notes, list):
        notes = []

    for p in problems:
        # 判定対象は title のみ（fact/detail の他指標言及で誤降格しないため）
        title = p.get("title") or ""
        is_over_reaction = False

        # 売掛は DSO で判定（絶対金額の倍表現が出ても DSO が健全なら降格）
        receivables_keywords = ["売掛", "売上債権", "受取手形"]
        if receivables_days <= 45 and any(k in title for k in receivables_keywords):
            is_over_reaction = True
            notes.append({
                "metric": "売掛回収日数 (DSO)",
                "current_value": f"{receivables_days:.0f}日",
                "judgment": "健全水準内（≤45日。自動格下げ）",
                "check_point": f"AIが課題化したが、DSO {receivables_days:.0f}日は健全。売上拡大に伴う絶対金額増加は自然増として扱うべき。元タイトル: {p.get('title','')[:60]}",
            })
            demoted.append(p.get("title", ""))
        else:
            # 他の指標は title が「その指標を主題」にしてる場合のみ降格
            # 例：「自己資本比率が低い」→ 降格対象、「有利子負債41,184万円」→ 対象外
            other_main_themes = []
            if cash_months >= 2:
                other_main_themes.append((["現預金が少", "資金繰り", "キャッシュ不足"], "現預金", f"{cash_months:.1f}ヶ月分"))
            if inventory_days <= 30:
                other_main_themes.append((["在庫過剰", "在庫滞留", "在庫が増"], "在庫回転", f"{inventory_days:.0f}日"))
            if equity_ratio >= 30:
                other_main_themes.append((["自己資本比率が低", "債務超過", "自己資本不足"], "自己資本比率", f"{equity_ratio:.0f}%"))

            for keywords, metric, value in other_main_themes:
                if any(k in title for k in keywords):
                    is_over_reaction = True
                    notes.append({
                        "metric": metric,
                        "current_value": value,
                        "judgment": "健全水準内（自動格下げ）",
                        "check_point": f"AIが課題化したが、{value} は健全水準のため observation_notes に降格。元タイトル: {title[:60]}",
                    })
                    demoted.append(title)
                    break
        if not is_over_reaction:
            kept.append(p)

    if demoted:
        result["prioritized_problems"] = kept
        result["observation_notes"] = notes
        qc = result.setdefault("quality_check", {})
        warnings = qc.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        warnings.append(f"[健全水準の過剰反応を自動格下げ: {len(demoted)}件] " + " / ".join(demoted[:3]))
        qc["warnings"] = warnings


# ---------------------------------------------------------------------------
# ロジック整合バリデーション（後処理）
# ---------------------------------------------------------------------------
# プロンプトの「矛盾禁止ルール」だけでは AI の暴走を完全には防げないため、
# 機械的にキーワードベースで矛盾を検出し quality_check.warnings に積む。
# UI/PDF 側で警告ピル表示・税理士の目視確認を促す運用を想定。
_CONTRADICTION_RULES = [
    {
        "name": "顧客集中×追加受注",
        "issue_keywords": ["依存", "集中", "1社", "一社", "50%", "60%", "70%", "80%", "特定顧客", "主要取引先"],
        "solution_keywords": ["追加受注", "取引拡大", "既存顧客から増", "主要顧客へ", "主要取引先から"],
    },
    {
        "name": "在庫過剰×仕入増",
        "issue_keywords": ["在庫過剰", "在庫滞留", "在庫増", "在庫が積み上"],
        "solution_keywords": ["在庫を増", "仕入れを増", "発注量を増", "買い増し"],
    },
    {
        "name": "現金減少×大型投資",
        "issue_keywords": ["現金が減", "現預金減", "キャッシュ減", "資金繰り", "バーン", "ショート"],
        "solution_keywords": ["設備投資", "大型投資", "新規出店", "新工場"],
    },
    {
        "name": "人件費過大×採用増",
        "issue_keywords": ["人件費が重", "人件費高", "人件費率", "人件費過大"],
        "solution_keywords": ["採用強化", "増員", "正社員化", "人員拡大"],
    },
]


def _validate_logic_consistency(result: dict) -> None:
    """prioritized_problems / owner_what_to_do の論理矛盾を機械検出。
    検出した警告は result['quality_check']['warnings'] に追記する（破壊しない）。
    """
    if not isinstance(result, dict):
        return
    warnings_list = []

    def _hits(text: str, keywords: list) -> list:
        text = (text or "").lower()
        return [k for k in keywords if k.lower() in text]

    for problem in (result.get("prioritized_problems") or []):
        title = problem.get("title", "")
        detail = problem.get("detail", "")
        fact = problem.get("fact", "")
        issue_text = f"{title} {detail} {fact}"

        solutions = problem.get("solutions") or []
        sol_text = " ".join(
            f"{s.get('title','')} {s.get('first_step','')} {s.get('why','')}"
            for s in solutions if isinstance(s, dict)
        )

        for rule in _CONTRADICTION_RULES:
            issue_hits = _hits(issue_text, rule["issue_keywords"])
            sol_hits = _hits(sol_text, rule["solution_keywords"])
            if issue_hits and sol_hits:
                warnings_list.append(
                    f"[矛盾検出: {rule['name']}] rank={problem.get('rank')} "
                    f"title='{title[:30]}' issue keywords={issue_hits} ⇔ solution keywords={sol_hits}"
                )

    # owner_what_to_do と key_insight/summary の矛盾もチェック
    owner_what = result.get("owner_what_to_do", "") or ""
    insight = result.get("key_insight", "") or ""
    summary = result.get("summary", "") or ""
    insight_text = f"{insight} {summary}"
    for rule in _CONTRADICTION_RULES:
        issue_hits = _hits(insight_text, rule["issue_keywords"])
        sol_hits = _hits(owner_what, rule["solution_keywords"])
        if issue_hits and sol_hits:
            warnings_list.append(
                f"[矛盾検出: {rule['name']}] owner_what_to_do ⇔ key_insight/summary "
                f"keywords={issue_hits} ⇔ {sol_hits}"
            )

    # 規模不整合の検出（forbidden_proposals に含まれる提案が growth_opportunities/solutions に
    # 出てないかチェック）
    state = result.get("company_state") or {}
    forbidden = state.get("forbidden_proposals") or []
    forbidden_keywords = []
    for fp in forbidden:
        # "IPO（時期尚早）" → "IPO", "他社買収（買収余力不足）" → "他社買収"
        base = fp.split("（")[0].strip()
        if base:
            forbidden_keywords.append(base)

    if forbidden_keywords:
        # growth_opportunities をチェック
        for go in (result.get("growth_opportunities") or []):
            go_text = f"{go.get('title','')} {go.get('rationale','')} " + \
                      " ".join(go.get('actions') or [])
            for kw in forbidden_keywords:
                if kw.lower() in go_text.lower():
                    warnings_list.append(
                        f"[規模不整合: {kw}] growth_opportunities に出している。"
                        f"forbidden_proposals={forbidden} に該当。要削除"
                    )
        # prioritized_problems.solutions もチェック
        for p in (result.get("prioritized_problems") or []):
            for s in (p.get("solutions") or []):
                sol_text = f"{s.get('title','')} {s.get('why','')} {s.get('first_step','')}"
                for kw in forbidden_keywords:
                    if kw.lower() in sol_text.lower():
                        warnings_list.append(
                            f"[規模不整合: {kw}] prioritized_problems[].solutions に出している。要削除"
                        )

    if warnings_list:
        qc = result.setdefault("quality_check", {})
        existing = qc.get("warnings") or []
        if not isinstance(existing, list):
            existing = [str(existing)]
        qc["warnings"] = existing + warnings_list


def _derive_legacy_fields_from_solutions(result: dict) -> None:
    """prioritized_problems[].solutions[] から revenue_ideas / cost_ideas / actions を派生。
    AI 出力を一元化（重複・矛盾源を断つ）するための後処理。
    既存 UI/PDF が依存しているフィールドのみ最小限再構築。
    """
    if not isinstance(result, dict):
        return
    problems = result.get("prioritized_problems") or []

    revenue_ideas = []
    cost_ideas = []
    actions = []

    for p in problems:
        rank = p.get("rank")
        issue_title = p.get("title", "")
        for s in (p.get("solutions") or []):
            if not isinstance(s, dict):
                continue
            cat = (s.get("category") or "").lower()
            title = s.get("title", "")
            why = s.get("why", "")
            first_step = s.get("first_step", "")
            impact_min = s.get("impact_min")
            impact_max = s.get("impact_max")
            impact_basis = s.get("impact_basis", "")
            timeframe = s.get("timeframe", "")
            difficulty = s.get("difficulty", "")

            if cat == "revenue":
                revenue_ideas.append({
                    "title": title,
                    "timeframe": timeframe,
                    "impact_min": impact_min,
                    "impact_max": impact_max,
                    "why": why,
                    "how": first_step,
                    "_from_problem_rank": rank,
                })
            elif cat == "cost":
                cost_ideas.append({
                    "title": title,
                    "savings_max": impact_max,
                    "why": why,
                    "how": first_step,
                    "_from_problem_rank": rank,
                })

            # 全 solution を action としても束ねる（priority は rank 由来）
            priority = "高" if rank == 1 else ("中" if rank == 2 else "低")
            urgency = "急務" if rank == 1 else ("重要" if rank == 2 else "推奨")
            impact_text = ""
            if impact_min is not None and impact_max is not None:
                impact_text = f"年{impact_min:,.0f}〜{impact_max:,.0f}万円"
            elif impact_max is not None:
                impact_text = f"年{impact_max:,.0f}万円"
            actions.append({
                "title": title[:20] if title else "",
                "detail": (f"[課題{rank}] {issue_title} に対する打ち手。" + (why or ""))[:140],
                "impact": impact_text,
                "impact_basis": impact_basis,
                "who": "",
                "deadline": timeframe,
                "first_step": first_step,
                "urgency": urgency,
                "priority": priority,
                "_from_problem_rank": rank,
            })

    # 既存 AI 生成 fields がある場合は上書きしない（プロンプト過渡期の互換）
    if not result.get("revenue_ideas"):
        result["revenue_ideas"] = revenue_ideas
    if not result.get("cost_ideas"):
        result["cost_ideas"] = cost_ideas
    if not result.get("actions"):
        result["actions"] = actions

    # integrated_turnaround_plan も solutions から派生（UIで表示中のため）
    if not result.get("integrated_turnaround_plan"):
        expense_reduction = []
        revenue_boost = []
        finance_options = []
        for p in problems:
            for s in (p.get("solutions") or []):
                if not isinstance(s, dict):
                    continue
                cat = (s.get("category") or "").lower()
                impact_max = s.get("impact_max")
                if cat == "cost" and impact_max is not None:
                    expense_reduction.append({
                        "item": s.get("title", ""),
                        "current_yearly": None,
                        "target_yearly": None,
                        "delta_yearly": -abs(impact_max),
                        "difficulty": s.get("difficulty", ""),
                        "note": s.get("first_step", ""),
                    })
                elif cat == "revenue" and impact_max is not None:
                    revenue_boost.append({
                        "item": s.get("title", ""),
                        "delta_yearly": abs(impact_max),
                        "difficulty": s.get("difficulty", ""),
                        "note": s.get("first_step", ""),
                    })
                elif cat == "finance":
                    finance_options.append({
                        "item": s.get("title", ""),
                        "amount": impact_max,
                        "difficulty": s.get("difficulty", ""),
                        "note": s.get("first_step", ""),
                    })
        # ストーリーはトップ課題のexpected_outcomeを流用
        story = ""
        if problems:
            top = problems[0]
            story = top.get("expected_outcome", "") or top.get("detail", "")
        result["integrated_turnaround_plan"] = {
            "story": story,
            "expense_reduction": expense_reduction,
            "revenue_boost": revenue_boost,
            "finance_options": finance_options,
        }


# ---------------------------------------------------------------------------
# 複数年比較分析
# ---------------------------------------------------------------------------
def analyze_multi_year(financials_list: list, client_name: str, industry: str, provider: str = None) -> dict:
    """複数年の財務データを LLM で比較分析"""
    years_text = ""
    for fd in financials_list:
        gm = (fd.gross_profit / fd.revenue * 100) if fd.revenue else 0
        om = (fd.operating_profit / fd.revenue * 100) if fd.revenue else 0
        years_text += f"""
■ {fd.period}
  売上高: {fd.revenue:,.0f}万円 / 売上総利益: {fd.gross_profit:,.0f}万円（粗利率{gm:.1f}%）
  営業利益: {fd.operating_profit:,.0f}万円（{om:.1f}%）/ 経常利益: {fd.ordinary_profit:,.0f}万円 / 純利益: {fd.net_profit:,.0f}万円
"""

    prompt = f"""あなたは税理士のサポートをする財務アナリストです。
以下の複数年財務データを比較分析し、必ずJSON形式のみで回答してください。前置きや説明文、コードブロックは不要です。

【企業情報】
会社名: {client_name}
業種: {industry if industry else "不明"}

【複数年財務データ（古い順）】
{years_text}

以下のJSON形式で回答してください：
{{
  "trend_summary": "<トレンドの全体評価を3-4文で。具体的な数字を使って変化を説明>",
  "trend_label": <"成長" または "安定" または "横ばい" または "悪化">,
  "revenue_trend": "<売上推移の評価>",
  "profit_trend": "<利益推移の評価>",
  "key_findings": ["<重要な発見1>", "<重要な発見2>", "<重要な発見3>"],
  "risks": ["<リスク1>", "<リスク2>"],
  "actions": [
    {{"title": "<アクション名>", "detail": "<具体的な内容>", "priority": "高"}},
    {{"title": "<アクション名>", "detail": "<具体的な内容>", "priority": "中"}},
    {{"title": "<アクション名>", "detail": "<具体的な内容>", "priority": "低"}}
  ]
}}"""

    text = _call_llm(prompt, provider)
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def current_provider() -> str:
    """現在有効なプロバイダ名を返す（画面表示用）"""
    return AI_PROVIDER
