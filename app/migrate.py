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
    # users.selected_books（参照する書籍IDのJSON配列）
    _add_column(cursor, "users", "selected_books", "TEXT DEFAULT '[]'")
    # users.is_admin（運営管理者フラグ。登録では設定不可）
    _add_column(cursor, "users", "is_admin", "INTEGER DEFAULT 0")
    # 管理者の初期ブートストラップ: 「誰も管理者がいない場合のみ」最初に登録されたアカウント（最小ID）に付与。
    # ※ユーザー名ベースの付与はしない（/register は任意ユーザー名を許すため乗っ取り可能）。
    # ※既にDBに管理者がいる場合は何もしない（冪等・既存権限を尊重）。
    try:
        already = cursor.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]
        if not already:
            cursor.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
            if cursor.rowcount:
                print(f"  + bootstrapped is_admin on the first-registered account (lowest id)")
    except sqlite3.OperationalError as e:
        print(f"  ! is_admin bootstrap skip: {e}")
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

    # 既存 financial_data の period を西暦に正規化 + period_type / fiscal_year 遡及判定
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from app.services.period_classifier import classify_period, normalize_to_seireki

        cursor.execute("SELECT id, period, period_type, fiscal_year FROM financial_data")
        rows = cursor.fetchall()
        normalized = 0
        reclassified = 0
        for rid, period, pt, fy in rows:
            # 西暦正規化
            new_period = normalize_to_seireki(period or "")
            need_update = False
            if new_period != period:
                period = new_period
                need_update = True
                normalized += 1
            # period_type / fiscal_year を再判定
            if not pt or pt == "":
                ptype, fyl = classify_period(period or "")
                if ptype:
                    cursor.execute(
                        "UPDATE financial_data SET period = ?, period_type = ?, fiscal_year = ? WHERE id = ?",
                        (period, ptype, normalize_to_seireki(fyl), rid)
                    )
                    reclassified += 1
                    need_update = False  # 上の UPDATE で更新済
            if need_update:
                cursor.execute(
                    "UPDATE financial_data SET period = ?, fiscal_year = ? WHERE id = ?",
                    (period, normalize_to_seireki(fy or ""), rid)
                )
        if normalized > 0:
            print(f"  + normalized {normalized} period strings to 西暦")
        if reclassified > 0:
            print(f"  + reclassified {reclassified} financial_data rows (period_type/fiscal_year)")
    except Exception as e:
        print(f"  ! period normalize/reclassify skip: {e}")

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

    # reference_books テーブル新規作成（書籍ナレッジDB）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reference_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            publisher TEXT DEFAULT '',
            processed_content TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            license_status TEXT DEFAULT 'none',
            is_active INTEGER DEFAULT 1,
            uploaded_by_user_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("  = reference_books table ready")

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
