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
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,  # 新スキーマ（統合プラン・売上アイデア等）で8k超えるため拡大。Sonnet 4.6 以降推奨
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    else:  # gemini
        model = _get_gemini()
        response = model.generate_content(prompt)
        return response.text.strip()


def _parse_json_response(text: str) -> dict:
    """両プロバイダ共通の JSON 抽出。途中切れでも可能な限り救済"""
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 途中で切れた時の復旧: 最後の閉じ括弧以降を削除して再試行
        # { の深さを数えて完全な部分までで切る
        salvaged = _salvage_truncated_json(text)
        if salvaged:
            try:
                return json.loads(salvaged)
            except json.JSONDecodeError:
                pass
        # それでもダメなら元エラーを投げる
        raise


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
    message = client.messages.create(
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
    message = client.messages.create(
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
    message = client.messages.create(
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

        if len(parts) > 1:
            breakdown_text = "\n".join(parts) + "\n"

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
    burn = compute_burn_rate(fd)
    wc = compute_working_capital(fd)
    ebitda = compute_ebitda(fd, breakdown)

    cash_lines = ["\n【キャッシュ診断】"]
    if burn.get("operating_monthly") is not None:
        cash_lines.append(f"月次営業損益: {burn['operating_monthly']:+,.1f}万円/月")
    if burn.get("debt_repayment_monthly_est"):
        cash_lines.append(f"推定月次返済: {burn['debt_repayment_monthly_est']:,.1f}万円/月（有利子負債÷10年の仮定）")
    if burn.get("real_burn_monthly") is not None:
        cash_lines.append(f"実質バーン: {burn['real_burn_monthly']:+,.1f}万円/月")
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

    prompt = f"""あなたは中小企業の社長に伴走するCFOです。税理士の先生と一緒に働いています。
以下の財務データを分析し、JSON形式のみで回答してください。前置き・説明文・マークダウンは不要。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【分析の軸】必ず守る
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔑 **コア：数字の"動き"から課題を特定する**
   個別の数値の高低ではなく、「前期比でどう動いたか」「業界平均と比べてどうか」「内訳がどう変化したか」から課題を発見する。
   数字が動いた → その裏で何が起きているか → 何が課題で、どう手を打つか、の順で考える。

4つの見方：
① **時間軸（前期比・複数年トレンド）** — 伸び率で判断。絶対額に惑わされない
② **構成軸（内訳の変化）** — 合計が同じでも、中身の割合変化で何かが起きている
③ **業界軸（ベンチマーク乖離）** — ±10%超の乖離は必ず言及
④ **キャッシュ軸** — 利益より現金の動きを重視

よくある"数字の動き"パターン（**例示、これに限定しない**。自分で見つけてほしい）：
- 売上↓ なのに 人件費↑ → 人員再配置が進んでいない
- 売上↑ ペース < 売掛金↑ ペース → 売上が現金化されていない可能性
- 売上原価↑ ペース < 在庫↑ ペース → 在庫滞留の可能性
- 粗利率が突然改善 → 会計方針変更・商品構成変化の有無確認
- 広告費が急増しているのに売上伸びない → マーケ投資効率の課題
- 営業利益 ＞ 0 なのに現預金↓ → キャッシュ化されていない
- 有利子負債↑ なのに設備投資なし → 運転資金が回っていない可能性
- 未収入金・仮払金が売上の5%超 → 内訳確認

つまり **「どの数字が・どう動いて・何の課題を示しているか」** の組を見つけ、issues と actions に反映せよ。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【strengths 禁止リスト】（1つでも該当したら書くな → issues 行き）
❌ 「流動比率が高い」系（売掛>60日 or 在庫>60日 の時）
❌ 「運転資本◯億の現金化余地」系（滞留の証拠を強みに書くな）
❌ 「支払利息が軽微」系（元本が売上30%超なら NG）
❌ 「自己資本比率プラス」だけ（負債比率 200%超なら issues）
❌ 「形式上／見た目は／数字上は」ヘッジ表現を含むもの
❌ 本業赤字時のコスト構造系（販管費率・人件費率が業界内 等）

✅ strengths OK：粗利率が業界超・売掛/在庫≤45日・(自己資本40%超 & 負債比率≤100%)・月商キャッシュ倍率≥2ヶ月・事業固有（ブランド/商品/設備/顧客/技術）

【書き方ルール】
- 抽象論（「効率化を」「見直しを」）一切禁止。項目名と金額で語る
- 金額試算は必ず数字：「推定で年間◯◯万円」
- actions には「誰が・いつまでに・一歩目」を必ず入れる
- 社長向けフィールド（key_insight, summary, strengths, issues, revenue_ideas, cost_ideas, hearing_sheet）は**中学生が読んで分かる言葉**で
- プロ向けフィールド（actions, integrated_turnaround_plan, kpi_watch, cross_sell）は専門用語OK、ただし必ず()で補足
- 専門用語の例：「粗利率（売上から仕入れを引いた利益の割合）」「負債比率（借金が自己資金の何倍か）」「EBITDA（本業で生み出す現金相当の利益）」
- 表現は中立・丁寧。「粉飾／不正／虚偽」などの断定語は禁止。「実態の確認が必要」「可能性」「再確認を推奨」を使う
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
【財務データ（単位：万円）】
売上: {fd.revenue:,.0f}（{growth_text}）
売上原価: {fd.cost_of_sales:,.0f} / 粗利: {fd.gross_profit:,.0f}（{gross_margin:.1f}%）
販管費: {fd.selling_expenses:,.0f} / 営業利益: {fd.operating_profit:,.0f}（{operating_margin:.1f}%）
経常利益: {fd.ordinary_profit:,.0f} / 純利益: {fd.net_profit:,.0f}
{bs_text}
{breakdown_text}
{historical_text}
{benchmark_text}
{cash_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSON】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "key_insight": "<1行の最重要ポイント。核心を一撃で>",
  "prioritized_problems": [
    {{
      "rank": <1-5の整数。1が最重要>,
      "severity": "<高/中/低>",
      "title": "<課題タイトル20-30字。例：売上の77%が1社依存（取引停止リスク）>",
      "detail": "<2-3文の詳細説明。数字を必ず含める。社長向けの易しい言葉で>",
      "solutions": [
        {{
          "title": "<ソリューション名30字以内>",
          "approach_type": "<内製/外注/金融/節税/規程>",
          "affiliate_categories": ["<該当するアフィカテゴリ。例：営業代行/Webマーケティング/業界ネットワーク/銀行借換・条件交渉/補助金代行/法人保険/法人カード/ふるさと納税/節税・退職金準備/福利厚生/会計SaaS/労務SaaS/業務SaaS/AI-OCR/節税・設備投資/法務オンライン相談/BPO・業務委託>"],
          "impact_min": <万円。年間効果の最小>,
          "impact_max": <万円。年間効果の最大>,
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
  "owner_message": "<社長向け1-2文。中学生でも分かる日常語のみ。専門用語禁止。**不安を煽る表現禁止**（『危険』『底をつく』『破綻』→ 『気になる』『少し心配な』『余裕が薄くなってきた』に置換）。**事実+所感+問いかけ**の順で。例：『本業は黒字ですが、現金が3年で1.3億→6,600万円に減ってきています。借入の返済が月193万円あるためです。このペースだと余裕が薄くなっていく感じがあります。』>",
  "owner_what_to_do": "<社長向け1-2文。**命令調禁止**（『〜してください』連発NG）。**社長に問いかける形 / 一緒に考える形 / 選択肢を提示する形**で。社長自身に考えさせ、最終判断は社長に委ねる伴走スタンス。例：『主要取引先からの追加受注は、どこまで伸ばせそうですか？同時に他社向けの新規開拓も、月◯件ぐらい狙えると、依存度を下げながら売上も伸ばせる絵が見えてきます。一度社長のお考えを聞かせてください。』>",
  "industry_confidence": "<high/medium/low>",
  "data_limitations": ["<データの限界・推計前提>"],
  "score": <0-100整数>,
  "score_label": "<優良/良好/注意/危険>",
  "summary": "<2-3文の総評。数字を含める>",
  "strengths": ["<3-5件、数字根拠付き。禁止リスト該当ゼロ>"],
  "issues": ["<3-5件。数字の動きから発見した課題を中心に。金額インパクト付き>"],
  "integrated_turnaround_plan": {{
    "story": "<3-5文で支出×収入×財務を繋いだストーリー>",
    "expense_reduction": [{{"item":"<費目>", "current_yearly":<万円>, "target_yearly":<万円>, "delta_yearly":<負数>, "difficulty":"<低/中/高>", "note":"<施策>"}}],
    "revenue_boost": [{{"item":"<項目>", "delta_yearly":<正数万円>, "difficulty":"<低/中/高>", "note":"<施策>"}}],
    "finance_options": [{{"item":"<リスケ/増資/借入/補助金>", "amount":<万円>, "difficulty":"<低/中/高>", "note":"<前提>"}}]
  }},
  "revenue_ideas": [{{"title":"<打ち手>", "timeframe":"<すぐできる/1〜3ヶ月/3〜6ヶ月/6ヶ月以上>", "impact_min":<万円>, "impact_max":<万円>, "why":"<1-2文>", "how":"<2-3文>"}}],
  "cost_ideas": [{{"title":"<打ち手>", "savings_max":<万円>, "why":"<1-2文>", "how":"<2-3文>"}}],
  "actions": [{{"title":"<20字以内>", "detail":"<100字以内>", "impact":"<年◯◯万円>", "impact_basis":"<根拠>", "who":"<担当>", "deadline":"<期限>", "first_step":"<明日の一歩>", "urgency":"<急務/重要/推奨>", "priority":"<高/中/低>"}}],
  "cross_sell": [{{"title":"<アップセル商品>", "reason":"<根拠>", "price_range":"<料金>", "category":"<カテゴリ>", "talk_script":"<社長への切り口1文>"}}],
  "kpi_watch": [{{"name":"<KPI>", "current":"<現在>", "target":"<目標>", "comment":"<1文>"}}],
  "hearing_sheet": {{
    "business_understanding": ["<事業理解 3-5件>"],
    "customer_market": ["<顧客・市場 3-5件。営業体制・新規獲得チャネル・競合認識を必ず1件含める>"],
    "growth_opportunity": ["<成長機会 2-4件>"],
    "debt_and_finance": ["<借入・財務 2-4件>"],
    "regulations_and_compensation": ["<規程・役員報酬 2-4件。**以下を必ず聞く**：①出張規程・旅費規程の有無と日当額、②役員社宅規程の有無、③役員退職金規程の有無、④役員報酬の月額・賞与構成、⑤福利厚生規程の有無>"],
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
        "warning": "<キャッシュアウト・縛り期間・将来の課税繰延のリスク>"
      }}
    ],
    "strategic_note": "<2-3文。節税は『キャッシュを使う節税』『使わない節税』『繰延』の3軸で整理。投資判断（機械・人材・販路）と紐づけて打つべき>"
  }},
  "quality_check": {{
    "strengths_validation": "<禁止リストに該当しないか自己検証した結果を1文で>",
    "issues_coverage": "<数字の動きから課題を発見できたか・業界比較・キャッシュ目線が反映できているか>",
    "action_specificity": "<アクションに金額/担当/期限/一歩目が入っているか>",
    "warnings": ["<自己レビューで気になった点>"]
  }}
}}

件数目安：prioritized_problems 3-5件（rank=1が最重要）、各problem の solutions は 2-4件、revenue_ideas 4件、cost_ideas 3件、actions 3-5件、cross_sell 2-3件、kpi_watch 3-5件、tax_savings_advice.ideas は黒字時のみ3-5件（赤字時は applicable=false で空配列）。

【prioritized_problems の重要ルール（のだけ先生FB対応 2026-05-15 + 2026-05-29追加）】
これがレポートの主役。「課題 → ソリューション → 予想効果」の構造で、税理士が社長に提案するときの主軸となる：
- rank=1 は「この会社が今一番取り組むべき課題」を1つだけ
- rank=2,3 は次点
- 各 problem の solutions は「同じ課題に対する複数の打ち手」（内製・外注・節税・規程の組み合わせ）
- affiliate_categories は existing affiliate JSON の category と一致させる（自動マッチング用）
- impact_min/max は具体的な万円。「やる前 → やった後」の差分
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

【社長向け表現の重要ルール（北村先生FB対応 + Takeru FB 2026-05-15）】
owner_message と owner_what_to_do は、決算書を読めない社長に1秒で伝わる「伴走型」表現にすること：

■ 専門用語禁止
粗利率→「仕入れを引いた利益率」、営業利益→「本業のもうけ」、EBITDA→「本業の現金生成力」、運転資本→「日々の運営に必要なお金」、自己資本比率→「自分のお金で経営してる割合」、流動比率→「短期の支払い余力」、CCC→「現金が戻ってくるサイクル」、ROI→「投資に対する戻り」、KPI→「重要な数字」

■ 数字は「〇〇万円増えた」「〇〇％下がった」など、増減と方向で伝える

■ 🔥【最重要】社長が嫌がる表現を絶対に避ける
- 不安煽り禁止：「危険です」「底をつきます」「破綻します」「手遅れになる」→ NG
   → 代替：「気になる動きです」「余裕が薄くなってきた」「少し心配な水準」
- 命令調連発禁止：「〜してください」を1メッセージに3回以上 → NG
   → 代替：「〜という選択肢があります」「〜はいかがでしょうか」「社長のお考えはどうですか」
- 上から目線禁止：「やるべきです」「すべきです」「危ないですよ」→ NG
   → 代替：「やってみる価値があります」「検討の余地があります」「一緒に考えませんか」
- 断定で追い詰める表現禁止：「77%依存は危険」「3年で底をつく」→ NG
   → 代替：「77%依存だと一社に何かあったとき影響が大きい」「このペースだと現金の余裕が減っていく」

■ 伴走スタンス（重要）
- 社長に問いかける形を使う（「どこまで伸ばせそうですか？」「いけそうですか？」「お考えを聞かせてください」）
- 社長自身に考えさせる、最終判断は社長に委ねる
- 一緒に検討するパートナーの姿勢を出す
- 数字を出して気づきを促すが、結論を押し付けない

■ owner_what_to_do の良い例 / 悪い例
❌ 悪い例：「来月までに2件以上受注してください。依存度を8割以上にするのは危険なので、月10件開拓してください。」
✅ 良い例：「主要取引先からの追加受注はどこまで伸ばせそうですか？同時に他社向けの開拓も月◯件ぐらい狙えると、依存度を下げながら売上も伸ばせる絵が見えてきます。社長のお考えを聞かせてください。」

■ 🚨【ロジック整合の絶対ルール】矛盾した提案を絶対に書かない
- 「特定顧客に集中依存している（売上の50%超を1社が占める）」と指摘した場合：
  ❌ その顧客への「追加受注」「取引拡大」を勧めない（依存度がさらに上がるので矛盾）
  ✅ その顧客以外の「**新規顧客の開拓**」「**取引先の分散**」を勧める
  ✅ 既存顧客への追加受注を触れる場合は「依存度を下げる文脈の中で」のみ（例：他社開拓と並列で）
- 「在庫過剰」と指摘して「在庫を増やそう」と言わない
- 「現金が減っている」と指摘して「設備投資を進めましょう」と言わない
- 「人件費が重い」と指摘して「採用を強化しましょう」と言わない（業務委託化等の文脈が必要）
- owner_message で問題提起したら、owner_what_to_do は **その問題を解く方向**で書く。問題と打ち手の論理を必ず一致させる。

【tax_savings_advice の重要ルール】
- 営業利益<=0 または 純利益<=0 のときは applicable=false にして ideas を空配列にする（赤字社に節税は意味がない）
- applicable=true のときは、今期の純利益・キャッシュ残高を踏まえて「使う節税（投資型）」「使わない節税（経費化）」「繰延型」をバランスよく提示
- 機械的な「保険入れ」連発は禁止。投資判断（設備・人材・販路）と必ず紐づける
- estimated_tax_save は実効税率33%で概算
- **以下の鉄板打ち手を必ず1-2件検討してリストに含める**（該当しなければ理由を warning に書く）：
  ① 旅費規程・出張規程の整備（日当・宿泊費の経費化、社長と従業員の所得税も最適化）
  ② 役員社宅規程（自宅家賃の50%程度を法人経費化）
  ③ 役員退職金規程＋小規模企業共済の組み合わせ
  ④ 中小企業経営セーフティ共済（年間最大240万円、40ヶ月で100%返戻）
  ⑤ 中小企業経営強化税制（設備投資の即時償却）
  ⑥ 企業版ふるさと納税（法人税最大9割軽減）
  ⑦ 役員賞与・決算賞与による所得分散

【revenue_ideas で必ず検討する打ち手】
- 自社の営業体制が薄い／顧客集中度が高い／新規開拓が課題と判定した場合は、以下を1件以上 ideas に含める：
  ① **営業代行・インサイドセールス代行の活用**（自社で営業強化が難しい場合の現実解）
  ② **B2Bマーケティング外注**（Web集客・コンテンツマーケ・リスティング広告）
  ③ **業界団体加盟・展示会出展**（同業ネットワーク経由の紹介案件獲得）
  ④ **既存顧客のクロスセル**（ただし依存度高い顧客はNG、分散している場合のみ）"""

    text = _call_llm(prompt, provider)
    result = _parse_json_response(text)

    # 業界ベンチマーク・キャッシュ診断・運転資本・EBITDA・トレンドをレスポンスに同梱
    result["benchmark"] = benchmark
    result["burn_rate"] = burn
    result["working_capital"] = wc
    result["ebitda"] = ebitda
    result["trend_metrics"] = trend_metrics

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

    return result


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
