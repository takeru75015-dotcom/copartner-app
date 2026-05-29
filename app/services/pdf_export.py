"""
PDF出力サービス（Playwright経由）
分析画面のHTMLをそのままレンダリングしてPDFに変換
"""
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).parent.parent.parent  # copartner/


def generate_pdf_for_fd(fd_id: int, session_cookie: str, base_url: str = "http://127.0.0.1:8000") -> Optional[bytes]:
    """
    分析画面の PDF を生成して bytes で返す。
    内部で Node.js + Playwright を spawn する。

    Args:
        fd_id: 対象の financial_data ID
        session_cookie: 認証用のセッション cookie 値
        base_url: サーバの base URL

    Returns:
        PDF の bytes、失敗時は None
    """
    # 一時ファイルに PDF 出力させる
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name

    try:
        # Node.js スクリプトを実行
        node_script = SCRIPT_DIR / "pdf_export.js"
        env = os.environ.copy()
        env["COPARTNER_BASE"] = base_url
        env["COPARTNER_FD_ID"] = str(fd_id)
        env["COPARTNER_SESSION"] = session_cookie
        env["COPARTNER_OUTPUT"] = pdf_path

        result = subprocess.run(
            ["node", str(node_script)],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,  # 2分タイムアウト
        )

        if result.returncode != 0:
            print(f"[pdf_export] error: {result.stderr[:500]}")
            return None

        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return None

        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass
