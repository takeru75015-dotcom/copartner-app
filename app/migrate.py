"""
DB migration: 既存データを保持しつつ新規カラムを追加する。
実行: python -m app.migrate
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "kpi_saas.db"


def migrate():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH} — will be created fresh on server start")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # clients.business_details
    _add_column(cursor, "clients", "business_details", "TEXT DEFAULT ''")
    _add_column(cursor, "clients", "hearing_answers", "TEXT DEFAULT '{}'")
    # clients.website_url + web_extracted_json + web_extracted_at（HP自動取得機能）
    _add_column(cursor, "clients", "website_url", "TEXT DEFAULT ''")
    _add_column(cursor, "clients", "web_extracted_json", "TEXT DEFAULT ''")
    _add_column(cursor, "clients", "web_extracted_at", "DATETIME")
    # analyses.dismissed_solutions
    _add_column(cursor, "analyses", "dismissed_solutions", "TEXT DEFAULT '[]'")

    # users.referral_code（アフィ紹介ID）
    _add_column(cursor, "users", "referral_code", "TEXT DEFAULT ''")
    # users.excluded_categories（除外したいアフィカテゴリ）/ own_partners（自前提携先）
    _add_column(cursor, "users", "excluded_categories", "TEXT DEFAULT '[]'")
    _add_column(cursor, "users", "own_partners", "TEXT DEFAULT '{}'")
    # 既存ユーザーに referral_code を発番（空の場合）
    try:
        import secrets
        cursor.execute("SELECT id FROM users WHERE referral_code IS NULL OR referral_code = ''")
        for (uid,) in cursor.fetchall():
            code = "tax_" + secrets.token_hex(4)  # tax_ + 8桁hex
            cursor.execute("UPDATE users SET referral_code = ? WHERE id = ?", (code, uid))
            print(f"  + assigned referral_code {code} to user {uid}")
    except sqlite3.OperationalError as e:
        print(f"  ! referral_code backfill skip: {e}")

    # financial_data に B/S 等を追加
    for col, col_def in [
        ("prev_operating_profit", "REAL DEFAULT 0"),
        ("total_assets", "REAL DEFAULT 0"),
        ("current_assets", "REAL DEFAULT 0"),
        ("cash", "REAL DEFAULT 0"),
        ("receivables", "REAL DEFAULT 0"),
        ("inventory", "REAL DEFAULT 0"),
        ("total_liabilities", "REAL DEFAULT 0"),
        ("current_liabilities", "REAL DEFAULT 0"),
        ("interest_bearing_debt", "REAL DEFAULT 0"),
        ("equity", "REAL DEFAULT 0"),
        ("employees", "INTEGER DEFAULT 0"),
        ("breakdown_json", "TEXT DEFAULT '{}'"),
        ("period_type", "TEXT DEFAULT ''"),   # 'annual'/'monthly'
        ("fiscal_year", "TEXT DEFAULT ''"),   # 月次データの所属年度
    ]:
        _add_column(cursor, "financial_data", col, col_def)

    # 既存 financial_data の period_type / fiscal_year を遡及判定
    # （月次データ「2024年5月」 vs 年次「2024年5月期」を自動分類）
    try:
        cursor.execute("SELECT id, period, period_type, fiscal_year FROM financial_data WHERE period_type IS NULL OR period_type = ''")
        rows = cursor.fetchall()
        if rows:
            # period_classifier を後でインポート（main.py 経由でなく直接）
            import sys, os as _os
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from app.services.period_classifier import classify_period
            updated = 0
            for rid, period, _pt, _fy in rows:
                ptype, fy = classify_period(period or "")
                if ptype:
                    cursor.execute(
                        "UPDATE financial_data SET period_type = ?, fiscal_year = ? WHERE id = ?",
                        (ptype, fy, rid)
                    )
                    updated += 1
            if updated > 0:
                print(f"  + reclassified {updated} financial_data rows (period_type/fiscal_year)")
    except Exception as e:
        print(f"  ! period reclassify skip: {e}")

    # referral_services テーブル新規作成
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            provider TEXT DEFAULT '',
            category TEXT DEFAULT '',
            target_issue_tags TEXT DEFAULT '[]',
            target_industries TEXT DEFAULT '["全業種"]',
            target_size TEXT DEFAULT '{}',
            description_short TEXT DEFAULT '',
            description_long TEXT DEFAULT '',
            service_features TEXT DEFAULT '[]',
            pricing TEXT DEFAULT '',
            url TEXT DEFAULT '',
            referral_url_template TEXT DEFAULT '',
            commission_type TEXT DEFAULT '',
            commission_value REAL DEFAULT 0,
            commission_note TEXT DEFAULT '',
            logo_url TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 100,
            created_by_user_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_referral_services_category ON referral_services(category)")
    print("  = referral_services table ready")

    conn.commit()
    conn.close()
    print("Migration done.")


def _add_column(cursor, table: str, column: str, col_def: str):
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        print(f"  + {table}.{column} added")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            print(f"  = {table}.{column} already exists")
        else:
            print(f"  ! {table}.{column} skip: {e}")


if __name__ == "__main__":
    migrate()
