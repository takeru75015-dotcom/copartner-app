#!/bin/bash
# CoPartner local dev server starter
# 使い方: copartner/ 配下で `bash start.sh`
# 環境: Windows (Git Bash) / Linux / WSL 両対応

set -e

# スクリプトのあるディレクトリに移動（呼び出し位置に依存しない）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# venv を activate（Windows / Linux 両対応）
if [ -f ".venv/Scripts/activate" ]; then
    # Windows (Git Bash)
    source .venv/Scripts/activate
elif [ -f ".venv/bin/activate" ]; then
    # Linux / macOS / WSL
    source .venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "warning: venv not found. system python を使用します"
fi

# DB マイグレーション（既存 DB に新カラム追加。新規 DB はスキップ）
# これを忘れると /login で "no such column" エラーで死ぬ
echo "→ Running migrations..."
python -m app.migrate || echo "warning: migration step failed (continuing anyway)"

# uvicorn 起動（README.md と一致：127.0.0.1:8000）
exec python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
