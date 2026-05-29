import json
import hashlib
from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional, List
from pathlib import Path
from dotenv import load_dotenv
from http.cookies import SimpleCookie
from urllib.parse import quote

load_dotenv()

from .database import init_db, get_db, User, Client, FinancialData, Analysis
from .auth import hash_password, verify_password, create_session_token, decode_session_token
from .claude_client import (
    analyze_financials,
    extract_financials_from_pdf_text,
    extract_financials_from_pdf_binary,
    extract_financials_from_excel,
    extract_business_context_from_pdf,
    analyze_multi_year,
    AI_PROVIDER,
)

BASE_DIR = Path(__file__).parent

app = FastAPI(title="CoPartner — 税理士のためのAI財務分析")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _md5_short(s) -> str:
    """質問テキスト等の文字列から安定した短いハッシュを生成（ヒアリング回答キーで使用）"""
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()[:10]


templates.env.filters["md5_short"] = _md5_short

@app.on_event("startup")
def startup():
    init_db()

# --- ヘルパー ---
def get_session_from_request(request: Request):
    return request.cookies.get("session")

def get_current_user(request: Request, db: Session = Depends(get_db)):
    session = get_session_from_request(request)
    if not session:
        return None
    user_id = decode_session_token(session)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()

# --- 認証 ---
@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "ユーザー名またはパスワードが違います"})
    token = create_session_token(user.id)
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", token, httponly=True, max_age=86400*7)
    return resp

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": ""})

@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...),
             display_name: str = Form(""), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "このユーザー名は既に使われています"})
    user = User(username=username, password_hash=hash_password(password), display_name=display_name)
    db.add(user); db.commit()
    token = create_session_token(user.id)
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie("session", token, httponly=True, max_age=86400*7)
    return resp

# --- ダッシュボード ---
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    clients = db.query(Client).filter(Client.user_id == user.id).order_by(Client.created_at.desc()).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "clients": clients})

# --- クライアント管理 ---
@app.post("/clients/add")
def add_client(request: Request, name: str = Form(...), industry: str = Form(""), note: str = Form(""),
               business_details: str = Form(""),
               db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = Client(user_id=user.id, name=name, industry=industry, note=note,
                business_details=business_details)
    db.add(cl); db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/clients/{client_id}/update")
def update_client(client_id: int, request: Request,
                  industry: str = Form(""), business_details: str = Form(""),
                  note: str = Form(""),
                  db: Session = Depends(get_db)):
    """事業構成や業種を更新する（ヒアリング結果を反映する用）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    if industry:
        cl.industry = industry
    cl.business_details = business_details
    cl.note = note
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@app.get("/financials/{fd_id}/preview", response_class=HTMLResponse)
def preview_financial(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """抽出結果プレビュー（ユーザー確認用）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    from .services.integrity import check_integrity
    warnings = check_integrity(fd)
    try:
        bd = json.loads(fd.breakdown_json or "{}")
    except Exception:
        bd = {}
    return templates.TemplateResponse("preview.html", {
        "request": request, "user": user, "client": cl, "fd": fd,
        "warnings": warnings, "breakdown": bd,
    })


@app.get("/financials/{fd_id}/edit", response_class=HTMLResponse)
def edit_financial(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """財務データの編集フォーム"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("edit_financial.html", {
        "request": request, "user": user, "client": cl, "fd": fd,
    })


@app.post("/financials/{fd_id}/edit")
def update_financial(fd_id: int, request: Request,
                      period: str = Form(""),
                      revenue: float = Form(0), cost_of_sales: float = Form(0),
                      gross_profit: float = Form(0), selling_expenses: float = Form(0),
                      operating_profit: float = Form(0), ordinary_profit: float = Form(0),
                      net_profit: float = Form(0),
                      prev_revenue: float = Form(0), prev_operating_profit: float = Form(0),
                      total_assets: float = Form(0), current_assets: float = Form(0),
                      cash: float = Form(0), receivables: float = Form(0), inventory: float = Form(0),
                      total_liabilities: float = Form(0), current_liabilities: float = Form(0),
                      interest_bearing_debt: float = Form(0), equity: float = Form(0),
                      employees: int = Form(0),
                      db: Session = Depends(get_db)):
    """財務データを更新"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    # 数字を更新
    for k, v in [
        ("period", period or fd.period), ("revenue", revenue), ("cost_of_sales", cost_of_sales),
        ("gross_profit", gross_profit), ("selling_expenses", selling_expenses),
        ("operating_profit", operating_profit), ("ordinary_profit", ordinary_profit),
        ("net_profit", net_profit), ("prev_revenue", prev_revenue),
        ("prev_operating_profit", prev_operating_profit),
        ("total_assets", total_assets), ("current_assets", current_assets),
        ("cash", cash), ("receivables", receivables), ("inventory", inventory),
        ("total_liabilities", total_liabilities), ("current_liabilities", current_liabilities),
        ("interest_bearing_debt", interest_bearing_debt), ("equity", equity),
        ("employees", employees),
    ]:
        setattr(fd, k, v)

    # 既存の Analysis を削除（再分析させる）
    db.query(Analysis).filter(Analysis.financial_data_id == fd_id).delete()
    db.commit()
    return RedirectResponse(f"/financials/{fd_id}/preview", status_code=302)


@app.post("/clients/{client_id}/recalc-from-breakdown")
def recalc_from_breakdown(client_id: int, request: Request, db: Session = Depends(get_db)):
    """breakdown_json の内訳から在庫・売掛・現金・有利子負債を再計算"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    from .services.data_recalc import apply_recalc
    fds = db.query(FinancialData).filter(FinancialData.client_id == client_id).all()
    updated = 0
    log = []
    for fd in fds:
        result = apply_recalc(fd)
        if result.get("changed"):
            updated += 1
            log.append(f"[{fd.period}] " + " / ".join(result.get("details") or []))
    if updated:
        db.commit()
    msg = f"{updated}/{len(fds)}件を内訳から再計算"
    if log:
        msg += " | " + " ；".join(log[:3])
    return RedirectResponse(f"/clients/{client_id}?msg={msg}", status_code=302)


@app.post("/clients/{client_id}/reset-financials")
def reset_client_financials(client_id: int, request: Request, db: Session = Depends(get_db)):
    """このクライアントの全期データを一括削除（汚いデータをリセットして再アップロード用）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    fds = db.query(FinancialData).filter(FinancialData.client_id == client_id).all()
    cnt = len(fds)
    for fd in fds:
        db.delete(fd)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}?msg={cnt}件の財務データを削除しました", status_code=302)


@app.post("/financials/{fd_id}/delete")
def delete_financial(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """単一の財務データレコードを削除（重複・空データの掃除用）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    client_id = fd.client_id
    db.delete(fd)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@app.post("/clients/{client_id}/cleanup-duplicates")
def cleanup_duplicates(client_id: int, request: Request, db: Session = Depends(get_db)):
    """期表記を正規化して、同じ期のデータをマージ（より充実したレコードに統合）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    import re

    def _normalize_period(p: str) -> str:
        """期表記を正規化。'2020年6月期', '2020年（7期）', '2020年7月～2021年6月' 等を 'YYYY年M月期' に統一"""
        if not p:
            return ""
        # まず年月を抽出
        m = re.search(r"(\d{4})年\s*(\d{1,2})月", p)
        if m:
            return f"{m.group(1)}年{int(m.group(2))}月期"
        # "2020年（7期）" のパターン
        m = re.search(r"(\d{4})年", p)
        if m:
            return f"{m.group(1)}年期"
        return p.strip()

    fds = db.query(FinancialData).filter(FinancialData.client_id == client_id).all()

    # 期ごとにグループ化
    groups = {}
    for fd in fds:
        norm = _normalize_period(fd.period)
        groups.setdefault(norm, []).append(fd)

    merged_count = 0
    deleted_count = 0
    for norm, group in groups.items():
        if len(group) == 1:
            # 1件だけなら period を正規化して終わり
            if group[0].period != norm:
                group[0].period = norm
            continue
        # 複数件 → 「最も充実したレコード」を keeper に。他をマージして削除
        # スコア = ゼロでないフィールドの数
        def _score(f):
            return sum(1 for attr in ("revenue","cost_of_sales","gross_profit","selling_expenses",
                                       "operating_profit","ordinary_profit","net_profit",
                                       "total_assets","cash","receivables","inventory",
                                       "total_liabilities","equity") if getattr(f, attr, 0))
        keeper = max(group, key=_score)
        keeper.period = norm
        for other in group:
            if other.id == keeper.id:
                continue
            # keeper のゼロフィールドに other の値を埋める
            for attr in ("revenue","cost_of_sales","gross_profit","selling_expenses",
                         "operating_profit","ordinary_profit","net_profit",
                         "prev_revenue","prev_operating_profit",
                         "total_assets","current_assets","cash","receivables","inventory",
                         "total_liabilities","current_liabilities","interest_bearing_debt","equity",
                         "employees"):
                if not getattr(keeper, attr, 0) and getattr(other, attr, 0):
                    setattr(keeper, attr, getattr(other, attr))
            # breakdown_json もマージ（keeper が空なら other のを採用）
            if (not keeper.breakdown_json or keeper.breakdown_json == "{}") and other.breakdown_json:
                keeper.breakdown_json = other.breakdown_json
            db.delete(other)
            deleted_count += 1
        merged_count += 1
    db.commit()
    return RedirectResponse(f"/clients/{client_id}?msg=正規化＆マージ完了：{merged_count}期グループ、{deleted_count}レコード削除", status_code=302)


@app.post("/clients/{client_id}/upload-ledger")
async def upload_ledger(client_id: int, request: Request,
                         file: UploadFile = File(...),
                         doc_type: str = Form("ledger"),
                         db: Session = Depends(get_db)):
    """元帳・固定資産台帳・補助元帳 PDF/Excel を読み取り business_details に要約追記
    doc_type: ledger / fixed_assets / aux_ledger
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    type_labels = {
        "ledger": "総勘定元帳",
        "fixed_assets": "固定資産台帳",
        "aux_ledger": "補助元帳",
    }
    label = type_labels.get(doc_type, "元帳")

    try:
        content = await file.read()
        if AI_PROVIDER != "claude":
            return RedirectResponse(
                f"/clients/{client_id}?err=資料の自動読取は Claude 切替時のみ対応しています",
                status_code=302,
            )
        # extract_business_context_from_pdf を doc_type に応じたコンテキストで呼び出す
        from .claude_client import extract_ledger_summary_from_pdf
        extracted = extract_ledger_summary_from_pdf(content, file.filename, doc_type=doc_type)
        stamp = f"\n\n--- {label}（{file.filename}）から自動抽出 ---\n"
        cl.business_details = (cl.business_details or "") + stamp + extracted
        db.commit()
    except Exception as e:
        return RedirectResponse(
            f"/clients/{client_id}?err={label}読取失敗: {str(e)[:100]}",
            status_code=302,
        )
    return RedirectResponse(f"/clients/{client_id}?ok={label}を読み取りました", status_code=302)


@app.post("/clients/{client_id}/upload-context")
async def upload_business_context(client_id: int, request: Request,
                                   file: UploadFile = File(...),
                                   db: Session = Depends(get_db)):
    """会社概要・事業計画・KPI進捗の補足資料 PDF を読み取り business_details に追記"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    try:
        content = await file.read()
        if AI_PROVIDER != "claude":
            return RedirectResponse(
                f"/clients/{client_id}?err=資料の自動読取は Claude 切替時のみ対応しています",
                status_code=302,
            )
        extracted = extract_business_context_from_pdf(content, file.filename)
        # 既存の business_details に追記
        stamp = f"\n\n--- {file.filename} から自動抽出 ---\n"
        cl.business_details = (cl.business_details or "") + stamp + extracted
        db.commit()
    except Exception as e:
        return RedirectResponse(
            f"/clients/{client_id}?err=資料読取失敗: {str(e)[:100]}",
            status_code=302,
        )
    return RedirectResponse(f"/clients/{client_id}", status_code=302)

def _check_data_quality(financials: list) -> list:
    """登録された財務データの品質をチェックし、警告メッセージのリストを返す"""
    issues = []
    if not financials:
        return issues

    # 1. 期数異常（月次・年次混在の疑い）
    if len(financials) > 12:
        issues.append({
            "severity": "high",
            "title": f"⚠️ {len(financials)}期のデータが登録されています",
            "message": "月次データと年次決算が混在している可能性が高いです。普通の中小企業なら 5-10 期程度になるはずです。",
            "action": "🗑️【全期リセット】で全部削除した後、**年次決算（PL推移表・残高試算表など）のみ**を再アップロードしてください。月次推移ファイルは「12ヶ月→1期」に自動集約されます。",
        })

    # 2. 部分データ（データ不足の期がある）
    thin_count = sum(1 for f in financials if not f.revenue or (not f.cost_of_sales and not f.selling_expenses))
    if thin_count > 0 and thin_count < len(financials):
        issues.append({
            "severity": "medium",
            "title": f"⚠️ {thin_count}件のデータ不足レコード",
            "message": "売上・原価・販管費のいずれかが空のレコードがあります。部分的にしか抽出できなかった可能性。",
            "action": "「⚠️ データ不足」バッジ付きの期を🗑️で個別削除するか、🧹【重複期を整理】で他のレコードと自動マージできます。",
        })

    # 3. 売上規模の異常な差（月次 vs 年次混在）
    revenues = [f.revenue for f in financials if f.revenue and f.revenue > 0]
    if len(revenues) >= 2:
        ratio = max(revenues) / min(revenues)
        if ratio > 10:
            issues.append({
                "severity": "high",
                "title": "⚠️ 売上規模に大きな差があります",
                "message": f"最小 {min(revenues):,.0f}万円 vs 最大 {max(revenues):,.0f}万円（{ratio:.0f}倍差）。月次の単月データと年次決算が混在している疑い。",
                "action": "月次推移ファイルは個別に分かれている場合があります。**通期決算データ（PL推移表・決算書PDF）を優先**してください。",
            })

    # 4. 売上原価0で粗利率が異常に高い
    high_gm = sum(1 for f in financials if f.revenue and f.gross_profit and (f.gross_profit / f.revenue) > 0.95 and not f.cost_of_sales)
    if high_gm > 0:
        issues.append({
            "severity": "medium",
            "title": f"⚠️ {high_gm}件のレコードで売上原価が抽出できていません",
            "message": "粗利率が異常に高い（95%超）のに売上原価が0。サービス業なら正常ですが、卸売・製造業なら抽出失敗の可能性。",
            "action": "該当期を削除してから、原価明細を含む決算書を再アップロードしてください。",
        })

    # 5. B/Sデータが全くない
    has_bs = any(f.total_assets or f.cash or f.equity for f in financials)
    if not has_bs:
        issues.append({
            "severity": "low",
            "title": "💡 貸借対照表（B/S）データなし",
            "message": "PLデータのみで、B/S（資産・負債・純資産）が抽出されていません。キャッシュ診断・運転資本分析が制限されます。",
            "action": "残高試算表 or 決算書PDFを追加アップロードすると、より深い分析ができます。",
        })

    return issues


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    financials = db.query(FinancialData).filter(FinancialData.client_id == client_id).order_by(FinancialData.created_at.desc()).all()
    quality_issues = _check_data_quality(financials)
    msg = request.query_params.get("msg", "")
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("client.html", {
        "request": request, "user": user, "client": cl,
        "financials": financials, "quality_issues": quality_issues, "msg": msg, "error": error,
    })

@app.post("/clients/{client_id}/delete")
def delete_client(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if cl:
        db.delete(cl); db.commit()
    return RedirectResponse("/dashboard", status_code=302)

# --- 財務データ手入力 ---
@app.post("/clients/{client_id}/financials/add")
def add_financial(client_id: int, request: Request,
                  period: str = Form(...),
                  revenue: float = Form(0), cost_of_sales: float = Form(0),
                  gross_profit: float = Form(0), selling_expenses: float = Form(0),
                  operating_profit: float = Form(0), ordinary_profit: float = Form(0),
                  net_profit: float = Form(0), prev_revenue: float = Form(0),
                  db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    fd = FinancialData(
        client_id=client_id, period=period,
        revenue=revenue, cost_of_sales=cost_of_sales, gross_profit=gross_profit,
        selling_expenses=selling_expenses, operating_profit=operating_profit,
        ordinary_profit=ordinary_profit, net_profit=net_profit, prev_revenue=prev_revenue
    )
    db.add(fd); db.commit(); db.refresh(fd)
    return RedirectResponse(f"/financials/{fd.id}/analyze", status_code=302)

# --- PDFアップロード（複数年対応） ---
@app.post("/clients/{client_id}/upload-pdf")
async def upload_pdf(client_id: int, request: Request,
                     files: List[UploadFile] = File(...),
                     db: Session = Depends(get_db)):
    import pdfplumber, io

    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    saved_ids = []
    errors = []

    def _normalize_period(p: str) -> str:
        import re
        if not p: return ""
        m = re.search(r"(\d{4})年\s*(\d{1,2})月", p)
        if m: return f"{m.group(1)}年{int(m.group(2))}月期"
        m = re.search(r"(\d{4})年", p)
        if m: return f"{m.group(1)}年期"
        return p.strip()

    _ATTRS = ("revenue","cost_of_sales","gross_profit","selling_expenses",
              "operating_profit","ordinary_profit","net_profit",
              "prev_revenue","prev_operating_profit",
              "total_assets","current_assets","cash","receivables","inventory",
              "total_liabilities","current_liabilities","interest_bearing_debt","equity",
              "employees")

    def _save_period(data: dict, fallback_period: str):
        """1期分のデータを保存。同じ正規化period が既にあればマージ（不足を補完）"""
        period = data.get("period") or fallback_period
        period = _normalize_period(period) or period

        # 既存レコード探索（同じ client_id, 正規化 period）
        existing = None
        for f in db.query(FinancialData).filter(FinancialData.client_id == client_id).all():
            if _normalize_period(f.period) == period:
                existing = f
                break

        if existing:
            # マージ：existing が0のフィールドを data で埋める
            existing.period = period
            for attr in _ATTRS:
                cur_val = getattr(existing, attr, 0) or 0
                new_val = data.get(attr, 0) or 0
                if not cur_val and new_val:
                    setattr(existing, attr, new_val)
            # breakdown も賢くマージ
            try:
                cur_bd = json.loads(existing.breakdown_json or "{}")
            except Exception:
                cur_bd = {}
            new_bd = data.get("breakdown") or {}
            # 各サブセクションは「既存にない/空なら新しいで埋める」
            for k, v in new_bd.items():
                if k.startswith("__"): continue
                if not cur_bd.get(k):
                    cur_bd[k] = v
                elif isinstance(cur_bd[k], dict) and isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk.startswith("__"): continue
                        if not cur_bd[k].get(kk):
                            cur_bd[k][kk] = vv
            existing.breakdown_json = json.dumps(cur_bd, ensure_ascii=False)
            db.commit(); db.refresh(existing)
            return existing.id

        # 新規作成
        fd = FinancialData(
            client_id=client_id,
            period=period,
            revenue=data.get("revenue", 0) or 0,
            cost_of_sales=data.get("cost_of_sales", 0) or 0,
            gross_profit=data.get("gross_profit", 0) or 0,
            selling_expenses=data.get("selling_expenses", 0) or 0,
            operating_profit=data.get("operating_profit", 0) or 0,
            ordinary_profit=data.get("ordinary_profit", 0) or 0,
            net_profit=data.get("net_profit", 0) or 0,
            prev_revenue=data.get("prev_revenue", 0) or 0,
            prev_operating_profit=data.get("prev_operating_profit", 0) or 0,
            total_assets=data.get("total_assets", 0) or 0,
            current_assets=data.get("current_assets", 0) or 0,
            cash=data.get("cash", 0) or 0,
            receivables=data.get("receivables", 0) or 0,
            inventory=data.get("inventory", 0) or 0,
            total_liabilities=data.get("total_liabilities", 0) or 0,
            current_liabilities=data.get("current_liabilities", 0) or 0,
            interest_bearing_debt=data.get("interest_bearing_debt", 0) or 0,
            equity=data.get("equity", 0) or 0,
            employees=data.get("employees", 0) or 0,
            breakdown_json=json.dumps(data.get("breakdown", {}), ensure_ascii=False),
        )
        db.add(fd); db.commit(); db.refresh(fd)
        return fd.id

    for file in files:
        try:
            content = await file.read()
            filename_lower = (file.filename or "").lower()

            # Excel 分岐（.xlsx, .xlsm, .xls）
            if filename_lower.endswith((".xlsx", ".xlsm", ".xls")):
                try:
                    result = extract_financials_from_excel(content, file.filename)
                except Exception as e:
                    errors.append(f"{file.filename}: Excel 読取失敗: {str(e)[:120]}")
                    continue

                # 予算・事業計画は実績DBから除外
                if result.get("should_save") is False or result.get("data_type") == "budget":
                    errors.append(f"{file.filename}: 予算/事業計画ファイルのため実績データには登録しません（{result.get('source_type','-')}）")
                    continue

                periods_data = result.get("periods") or []
                if not periods_data:
                    errors.append(f"{file.filename}: 財務データを抽出できませんでした")
                    continue
                for p in periods_data:
                    fid = _save_period(p, fallback_period=file.filename)
                    saved_ids.append(fid)
                continue

            # PDF 分岐（既存）
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            if not text.strip():
                # テキスト抽出失敗 → スキャンPDFとしてClaude Visionにフォールバック
                if AI_PROVIDER == "claude":
                    try:
                        data = extract_financials_from_pdf_binary(content, file.filename)
                    except Exception as ve:
                        ve_str = str(ve)
                        if '429' in ve_str or 'rate_limit' in ve_str:
                            errors.append(f"{file.filename}: ⏱ AIのレート制限に達しました。数分待ってから再アップロードしてください")
                        elif 'overloaded' in ve_str.lower():
                            errors.append(f"{file.filename}: 🔥 AIサーバが混雑中です。数分待ってから再アップロードしてください")
                        else:
                            errors.append(f"{file.filename}: 画像PDF読取失敗: {ve_str[:100]}")
                        continue
                else:
                    errors.append(f"{file.filename}: テキスト抽出失敗。画像PDFはClaude切替時のみ対応（AI_PROVIDER=claude）")
                    continue
            else:
                data = extract_financials_from_pdf_text(text, file.filename)

            fid = _save_period(data, fallback_period=file.filename)
            saved_ids.append(fid)

        except Exception as e:
            errors.append(f"{file.filename}: {str(e)}")

    if len(saved_ids) >= 1:
        # 抽出結果プレビュー画面へ（最初の期）。複数期はクライアント画面から個別確認可能
        return RedirectResponse(f"/financials/{saved_ids[0]}/preview", status_code=302)
    else:
        return templates.TemplateResponse("client.html", {
            "request": request, "user": user, "client": cl,
            "financials": db.query(FinancialData).filter(FinancialData.client_id == client_id).all(),
            "upload_errors": errors
        })

# --- 複数年比較分析 ---
@app.get("/clients/{client_id}/compare", response_class=HTMLResponse)
def compare_page(client_id: int, request: Request, ids: str = "", errors: str = "",
                 db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    fd_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    # IDOR対策: client_id でも絞り込む。他人の財務データIDを ?ids= で混ぜられても弾く
    financials = (
        db.query(FinancialData)
        .filter(FinancialData.client_id == client_id, FinancialData.id.in_(fd_ids))
        .order_by(FinancialData.period)
        .all()
    )

    if len(financials) < 2:
        from urllib.parse import quote
        err = "比較するには、下の「登録済みの期データ」から2つ以上の期を選択して「全期間を比較分析」を押してください。"
        return RedirectResponse(f"/clients/{client_id}?error={quote(err)}", status_code=302)

    # 複数年分析（キャッシュなし・毎回実行）
    try:
        result = analyze_multi_year(financials, cl.name, cl.industry)
    except Exception as e:
        result = None
        errors = str(e)

    error_list = [e for e in errors.split("|") if e] if errors else []

    return templates.TemplateResponse("comparison.html", {
        "request": request, "user": user, "client": cl,
        "financials": financials, "result": result,
        "errors": error_list
    })

# --- 単年AI分析 ---
@app.get("/financials/{fd_id}/analyze", response_class=HTMLResponse)
def analyze_page(fd_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    # ヒアリング回答（dict 化してテンプレに渡す）
    try:
        cl.hearing_answers_dict = json.loads(cl.hearing_answers or "{}")
    except Exception:
        cl.hearing_answers_dict = {}

    # 内訳データ parse（template で使用）
    try:
        breakdown = json.loads(fd.breakdown_json or "{}")
    except Exception:
        breakdown = {}

    # キャッシュ・運転資本指標・EBITDAを template に渡す（キャッシュ診断タブで使用）
    from .services.cash_analysis import compute_burn_rate, compute_working_capital, compute_ebitda, compute_cf_buckets
    cash_burn = compute_burn_rate(fd)
    cash_wc = compute_working_capital(fd)
    cash_ebitda = compute_ebitda(fd, breakdown)
    # CF 4バケツ（営業/投資/財務/フリー）
    _all_fds_for_cf = db.query(FinancialData).join(Client).filter(Client.id == fd.client_id).all()
    cash_cf = compute_cf_buckets(fd, breakdown=breakdown, historical_data=_all_fds_for_cf)

    existing = db.query(Analysis).filter(Analysis.financial_data_id == fd_id).order_by(Analysis.created_at.desc()).first()
    if existing:
        result = json.loads(existing.result_json)
        # キャッシュされた古い分析データに新フィールドが無い場合、historical_data から補完
        tm = result.get("trend_metrics") or {}
        if tm.get("periods"):
            # 各期に対応する FinancialData を期で引き直す
            period_to_fd = {h.period: h for h in _all_fds_for_cf}
            sorted_periods = tm["periods"]
            for key in ["cost_of_sales", "gross_profit", "selling_expenses", "total_assets",
                        "equity", "interest_bearing_debt", "total_liabilities", "current_liabilities"]:
                if key not in tm or not tm.get(key):
                    tm[key] = [(getattr(period_to_fd.get(p), key, 0) or 0) if period_to_fd.get(p) else 0 for p in sorted_periods]
            result["trend_metrics"] = tm
        try:
            dismissed_sols = json.loads(existing.dismissed_solutions or "[]")
        except Exception:
            dismissed_sols = []
        return templates.TemplateResponse("analysis.html", {
            "request": request, "user": user, "client": cl, "fd": fd, "result": result,
            "breakdown": breakdown,
            "cash_burn": cash_burn, "cash_wc": cash_wc, "cash_ebitda": cash_ebitda, "cash_cf": cash_cf,
            "analysis_id": existing.id, "cached": True,
            "dismissed_solutions": dismissed_sols,
        })

    # 同じクライアントの"現在見ている期"までのデータを取得（未来データは入れない）
    _all_fds = db.query(FinancialData).filter(
        FinancialData.client_id == fd.client_id
    ).all()
    # period でソートして、現在のfdの位置を特定。それ以前（自身含む）のみ渡す
    _sorted = sorted(_all_fds, key=lambda x: x.period or "")
    _idx = next((i for i, x in enumerate(_sorted) if x.id == fd.id), None)
    if _idx is None:
        # 見つからなければ安全側：現在のfdだけ
        all_fds_for_client = [fd]
    else:
        all_fds_for_client = _sorted[: _idx + 1]

    try:
        # 保存されたヒアリング回答を business_details に追記（AI に文脈として渡す）
        base_bd = getattr(cl, "business_details", "") or ""
        hearing_text = ""
        try:
            ans_dict = json.loads(getattr(cl, "hearing_answers", "{}") or "{}")
            if ans_dict:
                lines = ["", "【社長からのヒアリング回答】"]
                for q_hash, ans in ans_dict.items():
                    lines.append(f"- {ans}")
                hearing_text = "\n".join(lines)
        except Exception:
            pass
        merged_bd = (base_bd + hearing_text).strip()

        # 税理士の除外カテゴリ設定を読み込み
        try:
            user_excluded = json.loads(getattr(user, "excluded_categories", "[]") or "[]")
        except Exception:
            user_excluded = []
        result = analyze_financials(
            fd, cl.name, cl.industry,
            business_details=merged_bd,
            historical_data=all_fds_for_client if len(all_fds_for_client) > 1 else None,
            referral_code=getattr(user, "referral_code", "") or f"tax_{user.id:03d}",
            excluded_categories=user_excluded,
        )
        analysis = Analysis(financial_data_id=fd_id, result_json=json.dumps(result, ensure_ascii=False))
        db.add(analysis); db.commit(); db.refresh(analysis)
        return templates.TemplateResponse("analysis.html", {
            "request": request, "user": user, "client": cl, "fd": fd, "result": result,
            "breakdown": breakdown,
            "cash_burn": cash_burn, "cash_wc": cash_wc, "cash_ebitda": cash_ebitda, "cash_cf": cash_cf,
            "analysis_id": analysis.id, "cached": False
        })
    except Exception as e:
        err_str = str(e)
        if '429' in err_str or 'rate_limit' in err_str:
            friendly_err = "⏱ AIのレート制限に達しました（複数のリトライ後も解消せず）。\n5〜10分待ってから「再分析」ボタンを押してください。\n\n※ 大きな決算書や連続実行で発生しやすい問題です。"
        elif 'overloaded' in err_str.lower():
            friendly_err = "🔥 AIサーバが混雑中です。5分ほど待ってから再分析してください。"
        else:
            friendly_err = err_str
        return templates.TemplateResponse("analysis.html", {
            "request": request, "user": user, "client": cl, "fd": fd, "result": None,
            "breakdown": breakdown,
            "cash_burn": cash_burn, "cash_wc": cash_wc, "cash_ebitda": cash_ebitda, "cash_cf": cash_cf,
            "error": friendly_err, "cached": False, "analysis_id": None
        })

async def _save_hearing_form(client_id: int, request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, None, None, RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return None, None, None, RedirectResponse("/dashboard", status_code=302)
    form = await request.form()
    answers = {}
    for key, val in form.items():
        if key.startswith("ans_") and str(val).strip():
            answers[key[4:]] = str(val).strip()
    cl.hearing_answers = json.dumps(answers, ensure_ascii=False)
    db.commit()
    return user, cl, form.get("fd_id", ""), None


@app.post("/clients/{client_id}/save-hearing")
async def save_hearing_answers(client_id: int, request: Request, db: Session = Depends(get_db)):
    """ヒアリング質問への回答を保存（次回分析で business_details に追記される）"""
    user, cl, fd_id, redir = await _save_hearing_form(client_id, request, db)
    if redir:
        return redir
    if fd_id:
        return RedirectResponse(f"/financials/{fd_id}/analyze#panel-hearing", status_code=302)
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@app.post("/clients/{client_id}/save-hearing-and-reanalyze")
async def save_hearing_and_reanalyze(client_id: int, request: Request, db: Session = Depends(get_db)):
    """ヒアリング保存 → 既存分析を削除 → 再分析画面へリダイレクト"""
    user, cl, fd_id, redir = await _save_hearing_form(client_id, request, db)
    if redir:
        return redir
    if fd_id:
        # ★ 所有者検証: fd_id が この user の client に属することを確認（IDOR対策）
        fd = db.query(FinancialData).filter(
            FinancialData.id == int(fd_id),
            FinancialData.client_id == cl.id,
        ).first()
        if not fd:
            return RedirectResponse(f"/clients/{client_id}", status_code=302)
        db.query(Analysis).filter(Analysis.financial_data_id == fd.id).delete()
        db.commit()
        return RedirectResponse(f"/financials/{fd.id}/analyze", status_code=302)
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@app.post("/financials/{fd_id}/dismiss-solution")
async def dismiss_solution(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """提案を削除/復活する（トグル動作）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    analysis = db.query(Analysis).filter(
        Analysis.financial_data_id == fd_id
    ).order_by(Analysis.created_at.desc()).first()
    if not analysis:
        return RedirectResponse(f"/financials/{fd_id}/analyze", status_code=302)

    form = await request.form()
    sol_id = form.get("sol_id", "").strip()
    if not sol_id:
        return RedirectResponse(f"/financials/{fd_id}/analyze#panel-advice", status_code=302)

    try:
        dismissed = json.loads(analysis.dismissed_solutions or "[]")
    except Exception:
        dismissed = []

    # トグル：あれば削除、なければ追加
    if sol_id in dismissed:
        dismissed.remove(sol_id)
    else:
        dismissed.append(sol_id)

    analysis.dismissed_solutions = json.dumps(dismissed)
    # PDF再生成を促すためキャッシュ削除
    try:
        result = json.loads(analysis.result_json)
        if "owner_pdf_content" in result:
            del result["owner_pdf_content"]
            analysis.result_json = json.dumps(result, ensure_ascii=False)
    except Exception:
        pass
    db.commit()
    return RedirectResponse(f"/financials/{fd_id}/analyze#panel-advice", status_code=302)


@app.get("/financials/{fd_id}/pdf-view", response_class=HTMLResponse)
def pdf_view(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """PDF用の社長プレゼン版ビュー（Playwright経由で読み込まれる）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id).first()

    existing = db.query(Analysis).filter(
        Analysis.financial_data_id == fd_id
    ).order_by(Analysis.created_at.desc()).first()
    if not existing:
        return RedirectResponse(f"/financials/{fd_id}/analyze", status_code=302)

    result = json.loads(existing.result_json)

    # 削除済みソリューションを除外したフィルタ済みresultを作る
    try:
        dismissed = set(json.loads(existing.dismissed_solutions or "[]"))
    except Exception:
        dismissed = set()
    filtered_problems = []
    for p in result.get("prioritized_problems", []) or []:
        kept_sols = []
        for idx, sol in enumerate(p.get("solutions") or []):
            sol_id = f"{p.get('rank')}_{idx}"
            if sol_id not in dismissed:
                kept_sols.append(sol)
        # 採用されたソリューションが0個なら課題ごと除外（任意）
        if kept_sols or not p.get("solutions"):
            p_copy = dict(p)
            p_copy["solutions"] = kept_sols
            filtered_problems.append(p_copy)
    filtered_result = dict(result)
    filtered_result["prioritized_problems"] = filtered_problems

    pdf_content = filtered_result.get("owner_pdf_content")

    # 削除を変更してキャッシュ無効化されていれば再生成
    if not pdf_content:
        try:
            from .claude_client import generate_owner_pdf_content
            pdf_content = generate_owner_pdf_content(filtered_result, fd, cl.name, fd.period)
            # 元のresultに保存（dismissed_solutionsに紐づいた状態のキャッシュ）
            result["owner_pdf_content"] = pdf_content
            existing.result_json = json.dumps(result, ensure_ascii=False)
            db.commit()
        except Exception as e:
            print(f"[pdf_view] narrative generation failed: {e}")
            pdf_content = {
                "cover_subtitle": "経営分析レポート",
                "narrative_intro": result.get("owner_message", "") or result.get("summary", ""),
                "narrative_situation": result.get("summary", ""),
                "narrative_issues": "",
                "narrative_proposal": result.get("owner_what_to_do", ""),
                "narrative_outlook": "",
                "narrative_strengths": "",
                "key_numbers": [],
                "next_actions": [],
                "closing": "",
            }

    # cash_burn/wc/ebitda/cf もテンプレに必要
    from .services.cash_analysis import compute_burn_rate, compute_working_capital, compute_ebitda, compute_cf_buckets
    try:
        breakdown = json.loads(fd.breakdown_json or "{}")
    except Exception:
        breakdown = {}
    cash_burn = compute_burn_rate(fd)
    cash_wc = compute_working_capital(fd)
    cash_ebitda = compute_ebitda(fd, breakdown)
    _all_fds_for_cf = db.query(FinancialData).join(Client).filter(Client.id == fd.client_id).all()
    cash_cf = compute_cf_buckets(fd, breakdown=breakdown, historical_data=_all_fds_for_cf)

    return templates.TemplateResponse("pdf_view.html", {
        "request": request,
        "client": cl,
        "fd": fd,
        "pdf_content": pdf_content,
        "result": filtered_result,  # フィルタ済み（削除済み除外）
        "breakdown": breakdown,
        "cash_burn": cash_burn,
        "cash_wc": cash_wc,
        "cash_ebitda": cash_ebitda,
        "cash_cf": cash_cf,
        "user": user,
    })


@app.get("/financials/{fd_id}/pdf")
def export_pdf(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """分析画面をPDF化してダウンロード"""
    from fastapi.responses import Response
    from .services.pdf_export import generate_pdf_for_fd

    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # 所有者検証
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id).first()

    # 自分のセッション cookie を取得
    session_value = request.cookies.get("session", "")
    if not session_value:
        return RedirectResponse(f"/financials/{fd_id}/analyze?err=session", status_code=302)

    # PDF生成（Playwright）
    pdf_bytes = generate_pdf_for_fd(fd_id, session_value, base_url="http://127.0.0.1:8000")
    if not pdf_bytes:
        return RedirectResponse(f"/financials/{fd_id}/analyze?err=pdf", status_code=302)

    filename = f"{cl.name}_{fd.period}_分析レポート.pdf".replace("/", "_")
    # RFC 5987: filename* で UTF-8 URL エンコード版を提供
    filename_encoded = quote(filename, safe="")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=report.pdf; filename*=UTF-8''{filename_encoded}"},
    )


@app.api_route("/financials/{fd_id}/reanalyze", methods=["GET", "POST"])
def reanalyze(fd_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # ★ 所有者検証: FinancialData → Client.user_id で join し、user所有か確認（IDOR対策）
    fd = (
        db.query(FinancialData)
        .join(Client, Client.id == FinancialData.client_id)
        .filter(FinancialData.id == fd_id, Client.user_id == user.id)
        .first()
    )
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    db.query(Analysis).filter(Analysis.financial_data_id == fd.id).delete()
    db.commit()
    return RedirectResponse(f"/financials/{fd.id}/analyze", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    try:
        excluded = json.loads(user.excluded_categories or "[]")
    except Exception:
        excluded = []
    try:
        own_partners = json.loads(user.own_partners or "{}")
    except Exception:
        own_partners = {}
    # 利用可能なカテゴリリスト（affiliates.jsonから抽出）
    aff_path = BASE_DIR / "data" / "affiliates.json"
    categories = []
    try:
        with open(aff_path, encoding="utf-8") as f:
            data = json.load(f)
        seen = set()
        for a in data.get("affiliates", []):
            c = a.get("category")
            if c and c not in seen:
                categories.append(c)
                seen.add(c)
    except Exception:
        pass
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "excluded": excluded, "own_partners": own_partners,
        "categories": categories,
    })


@app.post("/settings/save")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    # 除外カテゴリ（チェックボックスで複数選択）
    excluded = form.getlist("excluded[]") if hasattr(form, "getlist") else []
    if not excluded:
        # multi-value fields fallback
        excluded = [v for k, v in form.multi_items() if k == "excluded[]"] if hasattr(form, "multi_items") else []
    user.excluded_categories = json.dumps(excluded, ensure_ascii=False)
    # 自前提携先（簡易：カテゴリ別に name|email|note を 1行ずつ）
    own_partners_raw = form.get("own_partners_raw", "")
    own = {}
    for line in (own_partners_raw or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            cat = parts[0]
            name = parts[1]
            email = parts[2] if len(parts) > 2 else ""
            note = parts[3] if len(parts) > 3 else ""
            own.setdefault(cat, []).append({"name": name, "email": email, "note": note})
    user.own_partners = json.dumps(own, ensure_ascii=False)
    db.commit()
    return RedirectResponse("/settings?ok=1", status_code=302)


@app.get("/partner-referral", response_class=HTMLResponse)
def partner_referral(request: Request, type: str = "", title: str = "", client: int = 0, db: Session = Depends(get_db)):
    """提携パートナー紹介（税理士が自前 or CoPartner 提携先から選択）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # 自前の提携先（カテゴリで絞り込み）
    try:
        own = json.loads(getattr(user, "own_partners", "{}") or "{}")
    except Exception:
        own = {}
    own_partner_list = own.get(type, []) if type else []

    # CoPartner 提携先候補（同カテゴリの複数候補）
    copartner_candidates = []
    try:
        aff_path = BASE_DIR / "data" / "affiliates.json"
        with open(aff_path, encoding="utf-8") as f:
            data = json.load(f)
        ref = getattr(user, "referral_code", "") or f"tax_{user.id:03d}"
        for a in data.get("affiliates", []):
            if a.get("category") == type and a.get("active", True):
                copartner_candidates.append({
                    "name": a["name"],
                    "vendor": a.get("vendor", ""),
                    "category": a["category"],
                    "description": a.get("description", ""),
                    "match_score": int(a.get("trust_score", 3)) * 20,
                    "url": (a.get("url_base") or "").replace("{ref}", ref),
                })
    except Exception:
        pass

    return templates.TemplateResponse("partner_referral.html", {
        "request": request,
        "user": user,
        "partner_type": type,
        "title_text": title,
        "client_id": client,
        "own_partner_list": own_partner_list,
        "copartner_candidates": copartner_candidates,
    })


@app.get("/subsidy-referral", response_class=HTMLResponse)
def subsidy_referral(request: Request, subsidy: str = "", client: int = 0, db: Session = Depends(get_db)):
    """補助金申請代行への送客（partner-referral の補助金代行カテゴリにリダイレクト）"""
    return RedirectResponse(
        f"/partner-referral?type={quote('補助金代行')}&title={quote(subsidy or '補助金申請代行')}&client={client}",
        status_code=302,
    )
