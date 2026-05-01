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
- 🎚️ **シミュレーター** — スライダーで「もしこうしたら」を即時計算

### データ取り込み
- PDF（テキスト・スキャン画像両対応 / Claude Vision）
- Excel（月次推移・年次決算・残高試算表）
- ファイル名から **予算/月次/年次** を自動判別
- 同期マージ・期表記正規化・整合性チェック

### スコアリング
- Python 固定計算（揺らぎなし）
- 数字の動きの異常を自動検知（売掛膨張・在庫膨張・粗利乖離 等）

---

## セットアップ

### 1. リポジトリをクローン
```bash
git clone https://github.com/takeru75015-dotcom/copartner-app.git
cd copartner-app
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
│  ├ benchmark.py      業界ベンチマーク比較
│  ├ cash_analysis.py  バーンレート・運転資本・EBITDA
│  ├ score.py          健全性スコア（4軸 × 25点）
│  ├ subsidy.py        補助金マッチング
│  ├ integrity.py      整合性チェック
│  └ data_recalc.py    内訳から再計算
├ data/
│  ├ industries.csv    業種別ベンチマーク（21業種）
│  └ subsidies.json    補助金マスタ（10件）
├ prompts/             プロンプト（外出し予定）
├ templates/           Jinja2 テンプレート
└ static/              CSS
```

### 主要技術
- Backend: Python 3.11+ / FastAPI
- Frontend: Jinja2 / HTMX / Tailwind / Chart.js
- DB: SQLite（開発） → PostgreSQL（本番予定）
- AI: Claude API（Anthropic SDK） / Gemini API（フォールバック）

### コミットルール
- main ブランチへの直接コミット禁止
- feature/xxx ブランチで開発
- Pull Request 経由でマージ

---

## 関連リポジトリ
- 戦略・ヒアリング・リサーチ ドキュメント: [copartner-docs](https://github.com/takeru75015-dotcom/copartner-docs)（Private）

---

## ライセンス
Private（社内利用のみ）
