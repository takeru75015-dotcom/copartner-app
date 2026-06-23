# CoPartner

税理士事務所を経営企画室化する AI プロダクト。
決算書・月次データから、社長が腹落ちする提案を AI が自動生成する。

> 「税理士に "会計士モード" を装着する AI」

---

## 機能

### 4タブの分析画面
- 📋 **財務概要** — KPIカード、業界比較、月数推移、4軸スコア（収益性・安全性・効率性・成長性）
- 🔥 **キャッシュ診断** — バーンレート、EBITDA、運転資本、CCC、月商キャッシュ倍率
- 💡 **ビジネスアドバイス** — 統合再生プラン、売上向上アイデア4つ、コスト最適化3つ、補助金マッチング、ヒアリングシート
- 🧮 **施策効果シミュレーター** — 打ち手（節税5/売上拡大4/コスト削減2）にチェック→金額調整で、売上/粗利/販管費/営業利益/税/現金/純資産・複数年キャッシュをその場で再計算。確定/仮定の2本表示、社長個人の手取りレンジ、想定効果レンジ（要検証）、計算根拠の表示
  - エンジン: `app/services/proposal_engine.py` ／ マスタ: `app/data/tax_proposals.json` ／ API: `/financials/{id}/tax-proposals|tax-simulate|tax-project|tax-impact`
  - ⚠️ 税計算は**概算**（繰越欠損・均等割・消費税は未対応）。本番で社長に出す前に税理士レビュー必須

### データ取り込み
- PDF（テキスト・スキャン画像両対応 / Claude Vision）
- Excel（月次推移・年次決算・残高試算表）
- ファイル名から **予算/月次/年次** を自動判別
- 同期マージ・期表記正規化・整合性チェック

### スコアリング
- Python 固定計算（揺らぎなし）
- 数字の動きの異常を自動検知（売掛膨張・在庫膨張・粗利乖離 等）

---

## クイックスタート（開発者向け・最短）

```bash
git clone https://github.com/takeru75015-dotcom/copartner-app.git
cd copartner-app
git checkout feature/kitamura-fb-2026-05-01   # ★最新コードはこのブランチ（main は古い）
cp app/.env.example app/.env                  # 鍵を埋める（ANTHROPIC_API_KEY / SECRET_KEY は別途共有）
pip install -r app/requirements.txt
bash start.sh                                 # migration→uvicorn 127.0.0.1:8000
```
- ログイン: ブラウザで `http://127.0.0.1:8000` → 新規登録 or テスト `test / test`
- DBは空から始まる（クライアントデータはコミットされていない）。新規クライアント登録から試す
- ⚠️ **`venv/` は Linux 用**（WSL残骸）。Windows は `.venv/` か システムPython を使う（`start.sh` が自動判定）
- ⚠️ コード変更時は **`/code_qa`（Codex二重レビュー）必須**（プロジェクト規約）。税計算など財務ロジックは特に

---

## セットアップ（詳細）

### 1. リポジトリをクローン
```bash
git clone https://github.com/takeru75015-dotcom/copartner-app.git
cd copartner-app
git checkout feature/kitamura-fb-2026-05-01
```

### 2. 仮想環境作成 & 依存インストール
```bash
python -m venv .venv
# Windows
./.venv/Scripts/activate
# Linux/Mac
source .venv/bin/activate

pip install -r app/requirements.txt
```

### 3. 環境変数設定
```bash
cp app/.env.example app/.env
# .env を編集して API キー設定
# - ANTHROPIC_API_KEY（必須）
# - GEMINI_API_KEY（オプション）
```

### 4. サーバ起動
```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

http://127.0.0.1:8000 でアクセス。

---

## 開発

### ディレクトリ構成
```
app/
├ main.py              FastAPI ルート
├ database.py          SQLAlchemy モデル
├ auth.py              認証
├ claude_client.py     LLM 抽象化（Claude / Gemini）
├ migrate.py           DB migration
├ services/
│  ├ proposal_engine.py 施策効果シミュレーターの計算（節税/売上/コスト・税は合算後1回再計算）
│  ├ benchmark.py      業界ベンチマーク比較
│  ├ cash_analysis.py  バーンレート・運転資本・EBITDA
│  ├ score.py          健全性スコア（4軸 × 25点）
│  ├ expense_grouping.py 科目集約（人件費=役員報酬+給与+賞与+社保 等）
│  ├ affiliate_match.py 紹介サービスのマッチング
│  ├ subsidy.py        補助金マッチング
│  ├ integrity.py      整合性チェック
│  └ data_recalc.py    内訳から再計算
├ data/
│  ├ industries.csv    業種別ベンチマーク
│  ├ subsidies.json    補助金マスタ
│  ├ affiliates.json   紹介サービス（アフィリ）マスタ
│  └ tax_proposals.json 施策効果シミュの打ち手マスタ
├ templates/           Jinja2 テンプレート（analysis.html にシミュレーター）
└ static/              CSS
```

### 主要技術
- Backend: Python 3.11+ / FastAPI
- Frontend: Jinja2 / HTMX / Tailwind / Chart.js
- DB: SQLite（開発） → PostgreSQL（本番予定）
- AI: Claude API（Anthropic SDK） / Gemini API（フォールバック）

### コミット・レビュールール
- 最新の作業ブランチ: **`feature/kitamura-fb-2026-05-01`**（`main` は遅れている）
- `main` への直接コミット禁止。feature ブランチで開発し PR 経由でマージ
- **コード変更（.py/.ts/.html/.css）は `/code_qa`（Claude self-review＋Codex 二重レビュー）を通してからコミット**（特に財務・税計算）
- 秘密情報（API キー）は **絶対にコミットしない**（`.env` は `.gitignore` 済み）。鍵は GitHub 外の安全な経路で共有

---

## 関連リポジトリ
- 戦略・ヒアリング・リサーチ ドキュメント: [copartner-docs](https://github.com/takeru75015-dotcom/copartner-docs)（Private）

---

## ライセンス
Private（社内利用のみ）
