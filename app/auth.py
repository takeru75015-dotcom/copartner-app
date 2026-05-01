from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
import os

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
serializer = URLSafeTimedSerializer(SECRET_KEY)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})

def decode_session_token(token: str, max_age: int = 86400 * 7):
    try:
        data = serializer.loads(token, max_age=max_age)
        return data.get("user_id")
    except Exception:
        return None
