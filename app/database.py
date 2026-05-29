from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

DATABASE_URL = "sqlite:///./kpi_saas.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, default="")
    referral_code = Column(String, default="", index=True)  # アフィ紹介ID（税理士ごとに発行）
    excluded_categories = Column(Text, default="[]")  # 除外したいアフィカテゴリのJSON配列（例：["法人保険","補助金代行"]）
    own_partners = Column(Text, default="{}")  # 自前で持ってる提携先 {カテゴリ: [{name, email, note}, ...]}
    created_at = Column(DateTime, default=datetime.utcnow)
    clients = relationship("Client", back_populates="owner", cascade="all, delete-orphan")

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    industry = Column(String, default="")
    note = Column(Text, default="")
    # 事業構成詳細（社長からのヒアリング情報）
    business_details = Column(Text, default="")
    hearing_answers = Column(Text, default="{}")  # 質問ハッシュ → 回答 のJSON
    created_at = Column(DateTime, default=datetime.utcnow)
    owner = relationship("User", back_populates="clients")
    financials = relationship("FinancialData", back_populates="client", cascade="all, delete-orphan")

class FinancialData(Base):
    __tablename__ = "financial_data"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    period = Column(String, nullable=False)          # 例: "2024年3月期"
    revenue = Column(Float, default=0)               # 売上高
    cost_of_sales = Column(Float, default=0)         # 売上原価
    gross_profit = Column(Float, default=0)          # 売上総利益
    selling_expenses = Column(Float, default=0)      # 販管費
    operating_profit = Column(Float, default=0)      # 営業利益
    ordinary_profit = Column(Float, default=0)       # 経常利益
    net_profit = Column(Float, default=0)            # 当期純利益
    prev_revenue = Column(Float, default=0)          # 前期売上高（前期比用）
    prev_operating_profit = Column(Float, default=0) # 前期営業利益（前期比用）
    # --- 貸借対照表（B/S）---
    total_assets = Column(Float, default=0)          # 総資産
    current_assets = Column(Float, default=0)        # 流動資産
    cash = Column(Float, default=0)                  # 現預金
    receivables = Column(Float, default=0)           # 売掛金・受取手形
    inventory = Column(Float, default=0)             # 棚卸資産
    total_liabilities = Column(Float, default=0)     # 負債合計
    current_liabilities = Column(Float, default=0)   # 流動負債
    interest_bearing_debt = Column(Float, default=0) # 有利子負債（長短合計）
    equity = Column(Float, default=0)                # 純資産
    # --- 追加情報 ---
    employees = Column(Integer, default=0)           # 従業員数（1人あたり売上等計算用）
    # 内訳データ（販管費内訳・売上内訳・売上原価内訳など、柔軟にJSONで保持）
    breakdown_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
    client = relationship("Client", back_populates="financials")
    analyses = relationship("Analysis", back_populates="financial_data", cascade="all, delete-orphan")

class Analysis(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True, index=True)
    financial_data_id = Column(Integer, ForeignKey("financial_data.id"), nullable=False)
    result_json = Column(Text, nullable=False)
    dismissed_solutions = Column(Text, default="[]")  # 削除した提案ID（"{rank}_{sol_idx}"）のJSON配列
    created_at = Column(DateTime, default=datetime.utcnow)
    financial_data = relationship("FinancialData", back_populates="analyses")

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
