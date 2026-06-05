"""管理者権限を明示的に付与/剥奪する運用コマンド（運営がサーバ上で実行）。

判定は User.is_admin フラグのみ。Web登録では付与されないため、運営がこのコマンドで
明示的に付与する（自動昇格・ユーザー名ベース付与は誤昇格/乗っ取りの余地があるため不採用）。

使い方:
  python -m app.grant_admin --list                 # 管理者一覧
  python -m app.grant_admin <username>             # 付与
  python -m app.grant_admin <username> --revoke    # 剥奪
"""
import sys
from .database import SessionLocal, User


def main(argv):
    db = SessionLocal()
    try:
        args = [a for a in argv]
        if not args or args[0] == "--list":
            admins = db.query(User).filter(User.is_admin == 1).all()
            print("admins:", ", ".join(u.username for u in admins) or "(none)")
            return 0
        username = args[0]
        revoke = "--revoke" in args[1:]
        u = db.query(User).filter(User.username == username).first()
        if not u:
            print(f"user not found: {username}")
            return 1
        u.is_admin = 0 if revoke else 1
        db.commit()
        print(f"{'revoked admin from' if revoke else 'granted admin to'}: {username}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
