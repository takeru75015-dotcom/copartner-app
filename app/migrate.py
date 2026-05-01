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
    ]:
        _add_column(cursor, "financial_data", col, col_def)

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
