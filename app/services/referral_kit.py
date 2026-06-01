"""
税理士紹介フロー（パターンA/B/C）の文面生成サービス
- A: 口頭で説明用の要点メモ
- B: 手渡し用1ページPDFの素材
- C: 後日メール送信用の文面

すべて Python テンプレで生成（AI呼び出し不要・即時応答）。
"""
from typing import Dict, Optional
from urllib.parse import quote


def _referral_url(base_url: str, fd_id: int, sol_title: str, partner_type: str,
                  client_id: int, tax_referral_code: str = "") -> str:
    """既存 /partner-referral ルートと整合させた紹介URL"""
    params = (
        f"type={quote(partner_type, safe='')}"
        f"&title={quote(sol_title, safe='')}"
        f"&client={client_id}"
    )
    if tax_referral_code:
        params += f"&ref={quote(tax_referral_code, safe='')}"
    return f"{base_url}/partner-referral?{params}"


def _resolve_partner_name(partner_type: str) -> str:
    """referral_partner_type の文字列から代表的なパートナー名を返す"""
    if not partner_type:
        return "提携先サービス"
    pt = partner_type
    if "CFO代行" in pt:
        return "提携先CFO代行サービス"
    if "BPO" in pt or "可視化" in pt:
        return "提携先のデータ可視化BPOサービス"
    if "M&A" in pt:
        return "提携先のM&A仲介会社"
    if "不動産" in pt:
        return "提携先の不動産投資会社"
    if "証券" in pt or "IFA" in pt:
        return "提携先の証券会社・IFA"
    if "社労士" in pt:
        return "提携先の社労士法人"
    if "SaaS" in pt:
        return "提携先の業務SaaS代理店"
    return pt


def build_referral_kit(solution: Dict, client_name: str, tax_office_name: str,
                       fd_id: int, client_id: int, base_url: str = "",
                       tax_referral_code: str = "") -> Dict:
    """1つの solution から A/B/C 3パターンの素材を生成。
    戻り値:
    {
      "partner_name": "...",
      "referral_url": "...",
      "verbal_memo": {... 口頭メモ素材 ...},
      "pdf_payload": {... 紹介PDFテンプレ用 ...},
      "email_payload": {"subject": "...", "body": "..."},
    }
    """
    title = solution.get("title", "")
    why = solution.get("why", "")
    first_step = solution.get("first_step", "")
    impact_min = solution.get("impact_min")
    impact_max = solution.get("impact_max")
    timeframe = solution.get("timeframe", "")
    partner_type = solution.get("referral_partner_type", "")
    simple_expl = solution.get("simple_explanation", "")

    partner_name = _resolve_partner_name(partner_type)
    referral_url = _referral_url(base_url, fd_id, title, partner_type or title,
                                  client_id, tax_referral_code)

    # 期待効果テキスト
    effect_text = ""
    if impact_min is not None and impact_max is not None:
        effect_text = f"年間+{impact_min:,.0f}〜{impact_max:,.0f}万円"
    elif impact_max is not None:
        effect_text = f"年間+{impact_max:,.0f}万円"

    # ========================================
    # A: 口頭で説明用の要点メモ
    # ========================================
    verbal_points = [
        f"「{title}」をご提案します",
    ]
    if simple_expl:
        verbal_points.append(simple_expl)
    elif why:
        verbal_points.append(f"理由: {why}")
    if effect_text:
        verbal_points.append(f"期待効果: {effect_text}")
    if timeframe:
        verbal_points.append(f"期間: {timeframe}")
    if partner_type:
        verbal_points.append(f"実行: {partner_name}（弊事務所で提携してます）")
    if first_step:
        verbal_points.append(f"初手: {first_step}")
    verbal_points.append("ご興味あれば、後ほど紹介リンクをメールで送ります")

    verbal_memo = {
        "title": f"口頭説明メモ: {title}",
        "points": verbal_points,
        "url_for_share": referral_url,  # その場でQRコード化やコピー用
        "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={quote(referral_url, safe='')}",
    }

    # ========================================
    # B: 手渡し用1ページPDFの素材
    # ========================================
    pdf_payload = {
        "headline": f"{title} のご紹介",
        "subhead": (simple_expl or why or "")[:120],
        "service_summary": (f"{partner_name}のサービスを通じて、御社の経営課題「{title}」"
                            f"に対する打ち手を実行できます。"),
        "expected_effect": effect_text or "詳細はお問い合わせください",
        "timeframe": timeframe or "ご相談",
        "first_step": first_step or "初回相談（無料）からスタート",
        "url": referral_url,
        "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote(referral_url, safe='')}",
        "tax_office_name": tax_office_name,
        "client_name": client_name,
    }

    # ========================================
    # C: 後日メール送信文面
    # ========================================
    body_lines = [
        f"{client_name} 御中",
        "",
        f"先日の経営分析レポートでご相談した「{title}」の件について、",
        f"弊事務所で提携している{partner_name}をご紹介します。",
        "",
    ]
    if simple_expl:
        body_lines += [f"■ サービス概要", simple_expl, ""]
    if effect_text:
        body_lines += [f"■ 期待効果", effect_text, ""]
    if timeframe:
        body_lines += [f"■ 想定期間", timeframe, ""]
    if first_step:
        body_lines += [f"■ 最初の一歩", first_step, ""]
    body_lines += [
        f"■ お申込み・詳細",
        referral_url,
        "",
        "（このURLは弊事務所の紹介リンクです。初回相談は無料です）",
        "",
        "ご不明点があれば、いつでもご連絡ください。",
        "",
        tax_office_name or "（税理士事務所名）",
    ]
    email_payload = {
        "subject": f"【ご紹介】{title} — {partner_name}のご案内",
        "body": "\n".join(body_lines),
    }

    return {
        "partner_name": partner_name,
        "partner_type": partner_type,
        "referral_url": referral_url,
        "verbal_memo": verbal_memo,
        "pdf_payload": pdf_payload,
        "email_payload": email_payload,
    }
