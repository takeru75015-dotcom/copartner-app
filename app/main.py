import json
import hashlib
import os
import threading
from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional, List
from pathlib import Path
from dotenv import load_dotenv
from http.cookies import SimpleCookie
from urllib.parse import quote

load_dotenv(Path(__file__).resolve().parent / ".env")  # 起動cwdに依存せず app/.env を確実に読む

from .database import init_db, get_db, User, Client, FinancialData, Analysis, ReferralService, ReferenceBook
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

# ───────────────────────────────────────────────────────────
# AI分析の二重起動ガード（連打による多重API課金を防止）
# 同一 fd_id の分析が走っている間は、後続の run リクエストを弾く。
# プロセス内メモリ（単一 uvicorn プロセス前提・プロトタイプ用途）。
# ───────────────────────────────────────────────────────────
_analysis_inflight: set[int] = set()
_analysis_inflight_lock = threading.Lock()


def _try_acquire_analysis(fd_id: int) -> bool:
    """fd_id の分析を開始してよければ True（ロック取得）。既に走っていれば False。"""
    with _analysis_inflight_lock:
        if fd_id in _analysis_inflight:
            return False
        _analysis_inflight.add(fd_id)
        return True


def _release_analysis(fd_id: int) -> None:
    with _analysis_inflight_lock:
        _analysis_inflight.discard(fd_id)


def _is_analysis_inflight(fd_id: int) -> bool:
    with _analysis_inflight_lock:
        return fd_id in _analysis_inflight


app = FastAPI(title="CoPartner — 税理士のためのAI財務分析")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _jinja_from_json(s):
    """Jinja2 用フィルタ: JSON文字列 → Python オブジェクト（list/dict）"""
    if s is None:
        return []
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return []


templates.env.filters["from_json"] = _jinja_from_json


def _md5_short(s) -> str:
    """質問テキスト等の文字列から安定した短いハッシュを生成（ヒアリング回答キーで使用）"""
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()[:10]


templates.env.filters["md5_short"] = _md5_short


def _resolve_effective_fd(fd, all_fds_full, fallback_pool=None):
    """月次fd を effective_fd（分析・表示用）に解決する共通関数。
    analyze_page / pdf_view 両方で使う。

    優先順位（Takeru判断 2026-06-07）:
      1. 同FYの年次データ（annual / annual_aggregated）
      2. 期中スナップショット（partial_aggregated）
      3. デフォルト: fd そのまま

    引数:
      fd: 選択された FinancialData（URL/保存用、触らない）
      all_fds_full: 集計対象のFinancialData（as-ofスライス済み）
      fallback_pool: annual探索の最終fallback先（全件参照したい時に渡す）。
                     decree_at スライスで annual がはみ出る場合に効く。
                     None なら all_fds_full と同じ扱い。

    戻り値: (effective_fd, all_fds_for_client, aggregation_meta, fd_substitution_meta)
    """
    effective_fd = fd
    fd_sub_meta = None
    aggregation_meta = {"monthly_count": 0, "annual_count": 0, "aggregated_count": 0,
                        "partial_count": 0, "fiscal_years_with_monthly": [], "discrepancies_by_fy": {}}
    # ★ 例外時の安全なフォールバック値（UnboundLocalError 防止）
    effective_fds = list(all_fds_full) if all_fds_full else []
    try:
        from .services.monthly_aggregator import build_effective_historical_data
        effective_fds, aggregation_meta = build_effective_historical_data(all_fds_full)
        if getattr(fd, "period_type", "") != "monthly":
            return effective_fd, list(effective_fds), aggregation_meta, None

        _fy_raw = getattr(fd, "fiscal_year", "") or ""
        _fy_prefix = _fy_raw.split("（")[0]
        # 1) annual
        _annual_match = next(
            (f for f in effective_fds
             if getattr(f, "period_type", "") == "annual"
             and _fy_prefix and _fy_prefix in (getattr(f, "fiscal_year", "") or getattr(f, "period", ""))),
            None,
        )
        if _annual_match is None:
            _fb = fallback_pool if fallback_pool is not None else all_fds_full
            _annual_match = next(
                (f for f in _fb
                 if getattr(f, "period_type", "") in ("annual", "")
                 and _fy_prefix and _fy_prefix in (getattr(f, "fiscal_year", "") or getattr(f, "period", ""))),
                None,
            )
            if _annual_match is not None and _annual_match not in effective_fds:
                # 同FYの partial/annual_aggregated を除去（重複防止）
                effective_fds = [
                    f for f in effective_fds
                    if not (
                        getattr(f, "period_type", "") in ("partial_aggregated", "annual_aggregated")
                        and (getattr(f, "fiscal_year", "") or "") == _fy_raw
                    )
                ]
                effective_fds = list(effective_fds) + [_annual_match]
        # 2) annual_aggregated
        _agg_match = next(
            (f for f in effective_fds
             if getattr(f, "period_type", "") == "annual_aggregated"
             and (getattr(f, "fiscal_year", "") or "") == _fy_raw),
            None,
        )
        # 3) partial_aggregated
        _partial_match = next(
            (f for f in effective_fds
             if getattr(f, "period_type", "") == "partial_aggregated"
             and (getattr(f, "fiscal_year", "") or "") == _fy_raw),
            None,
        )
        _picked = _annual_match or _agg_match or _partial_match
        if _picked is not None and _picked is not fd:
            effective_fd = _picked
            fd_sub_meta = {
                "original_fd_id": fd.id,
                "original_period": fd.period,
                "effective_period_type": getattr(effective_fd, "period_type", ""),
                "effective_period": getattr(effective_fd, "period", ""),
                "fiscal_year": _fy_raw,
                "n_months": getattr(effective_fd, "_partial_n_months", None),
            }
    except Exception as _e:
        print(f"[_resolve_effective_fd] skip: {_e}", flush=True)
    return effective_fd, list(effective_fds), aggregation_meta, fd_sub_meta

@app.on_event("startup")
def startup():
    init_db()
    # 既存DBに新カラム（selected_books / is_admin 等）を自動適用。
    # ORMが全User SELECTで新カラムを参照するため、uvicorn起動だけでも no such column を防ぐ。
    try:
        from .migrate import migrate
        migrate()
    except Exception as e:
        print(f"[startup] migrate skip: {e}", flush=True)

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
    # 管理者権限は登録では付与しない（運営が `python -m app.grant_admin <username>` で明示付与）
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
               business_details: str = Form(""), website_url: str = Form(""),
               db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = Client(user_id=user.id, name=name, industry=industry, note=note,
                business_details=business_details, website_url=website_url.strip())
    db.add(cl); db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/clients/{client_id}/update")
def update_client(client_id: int, request: Request,
                  industry: str = Form(""), business_details: str = Form(""),
                  note: str = Form(""), website_url: str = Form(""),
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
    cl.website_url = (website_url or "").strip()
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=302)


@app.post("/clients/{client_id}/extract-website")
def extract_website_for_client(client_id: int, request: Request,
                                website_url: str = Form(""),
                                db: Session = Depends(get_db)):
    """指定URLからWebサイト情報を取得+AI抽出してClientに保存。
    UI上で「自動取得」ボタンクリック → このルートが叩かれる。
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    # URLがフォーム経由で更新されていればまず保存
    url_to_use = (website_url or "").strip() or (cl.website_url or "")
    if not url_to_use:
        return RedirectResponse(f"/clients/{client_id}?web_err=URL未入力", status_code=302)
    cl.website_url = url_to_use

    from .services.web_extract import extract_from_website
    try:
        result = extract_from_website(url_to_use, company_name=cl.name)
    except Exception as e:
        return RedirectResponse(f"/clients/{client_id}?web_err={str(e)[:60]}", status_code=302)

    cl.web_extracted_json = json.dumps(result, ensure_ascii=False)
    from datetime import datetime as _dt
    cl.web_extracted_at = _dt.utcnow()

    # business_details にプリ入力（既存内容は保持し、AI取得分を追記）
    ex = (result or {}).get("extracted") or {}
    if ex and not ex.get("error"):
        added_lines = []
        if ex.get("business_summary"):
            added_lines.append(f"【事業概要】{ex['business_summary']}")
        if ex.get("main_services"):
            added_lines.append(f"【主要サービス】{' / '.join(ex['main_services'])}")
        if ex.get("target_customers"):
            added_lines.append(f"【主要顧客】{ex['target_customers']}")
        if ex.get("strengths"):
            added_lines.append(f"【強み】{' / '.join(ex['strengths'])}")
        if ex.get("regions"):
            added_lines.append(f"【対応エリア】{' / '.join(ex['regions'])}")
        added_text = "\n".join(added_lines)
        # 既存 business_details の末尾に追加（重複防止のためマーカーで判定）
        existing = cl.business_details or ""
        marker = "──Web自動取得──"
        if marker in existing:
            # 既存 Web 取得分を削除して上書き
            existing = existing.split(marker)[0].rstrip()
        if added_text:
            sep = "\n\n" if existing else ""
            cl.business_details = f"{existing}{sep}{marker}\n{added_text}"

    db.commit()
    return RedirectResponse(f"/clients/{client_id}?web_ok=1", status_code=302)


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


@app.post("/clients/{client_id}/bulk-delete-financials")
def bulk_delete_financials(client_id: int, request: Request,
                            fd_ids: str = Form(""),
                            db: Session = Depends(get_db)):
    """指定された複数のfd_idを一括削除（チェックボックス選択用）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)
    ids = [int(i) for i in (fd_ids or "").split(",") if i.strip().isdigit()]
    if not ids:
        return RedirectResponse(f"/clients/{client_id}?err=削除対象が選択されていません", status_code=302)
    # IDOR対策: client_id でも絞る
    fds = db.query(FinancialData).filter(
        FinancialData.id.in_(ids),
        FinancialData.client_id == client_id
    ).all()
    cnt = len(fds)
    # Analysis（外部キー）も削除
    fd_id_list = [f.id for f in fds]
    if fd_id_list:
        db.query(Analysis).filter(Analysis.financial_data_id.in_(fd_id_list)).delete(synchronize_session=False)
    for fd in fds:
        db.delete(fd)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}?msg={cnt}件のデータを削除しました", status_code=302)


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
                                   files: list[UploadFile] = File(...),
                                   db: Session = Depends(get_db)):
    """会社概要・事業計画・KPI進捗の補足資料を読み取り business_details に追記。
    複数ファイル同時アップロード対応。順番に AI で抽出して追記。
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    cl = db.query(Client).filter(Client.id == client_id, Client.user_id == user.id).first()
    if not cl:
        return RedirectResponse("/dashboard", status_code=302)

    if AI_PROVIDER != "claude":
        return RedirectResponse(
            f"/clients/{client_id}?err=資料の自動読取は Claude 切替時のみ対応しています",
            status_code=302,
        )

    success_count = 0
    error_msgs = []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            content = await f.read()
            if not content:
                error_msgs.append(f"{f.filename}: 空ファイル")
                continue
            extracted = extract_business_context_from_pdf(content, f.filename)
            stamp = f"\n\n--- {f.filename} から自動抽出 ---\n"
            cl.business_details = (cl.business_details or "") + stamp + extracted
            success_count += 1
        except Exception as e:
            error_msgs.append(f"{f.filename}: {str(e)[:60]}")

    if success_count > 0:
        db.commit()

    if error_msgs and success_count == 0:
        return RedirectResponse(
            f"/clients/{client_id}?err=" + quote("資料読取失敗: " + " / ".join(error_msgs)[:200]),
            status_code=302,
        )
    if error_msgs:
        return RedirectResponse(
            f"/clients/{client_id}?ok=" + quote(f"{success_count}件読取成功 / 失敗: " + " / ".join(error_msgs)[:150]),
            status_code=302,
        )
    return RedirectResponse(
        f"/clients/{client_id}?ok=" + quote(f"{success_count}件のファイルから事業情報を追記しました"),
        status_code=302,
    )

def _check_data_quality(financials: list) -> list:
    """登録された財務データの品質をチェックし、警告メッセージのリストを返す
    🌟 月次データは品質チェック対象外（年次データのみで判定）
    """
    issues = []
    if not financials:
        return issues

    # 月次データは除外して年次データのみでチェック
    annual_fds = [f for f in financials if (getattr(f, "period_type", "") or "") != "monthly"]
    if not annual_fds:
        return issues

    # 1. 期数異常（年次が多すぎる場合のみ。月次は集約されるので除外）
    if len(annual_fds) > 12:
        issues.append({
            "severity": "high",
            "title": f"⚠️ {len(annual_fds)}期分の年次データが登録されています",
            "message": "年次データが多すぎる可能性。普通の中小企業なら 5-10 期程度になるはずです。",
            "action": "重複期がないか確認し、🗑️で個別削除するか、🧹【重複期を整理】で自動マージしてください。",
        })

    # 以降のチェックも annual_fds で行う
    financials = annual_fds

    # 2. 部分データ（データ不足の期がある）
    thin_count = sum(1 for f in financials if not f.revenue or (not f.cost_of_sales and not f.selling_expenses))
    if thin_count > 0 and thin_count < len(financials):
        issues.append({
            "severity": "medium",
            "title": f"⚠️ {thin_count}件のデータ不足レコード",
            "message": "売上・原価・販管費のいずれかが空のレコードがあります。部分的にしか抽出できなかった可能性。",
            "action": "「⚠️ データ不足」バッジ付きの期を🗑️で個別削除するか、🧹【重複期を整理】で他のレコードと自動マージできます。",
        })

    # 3. 売上規模の異常な差（月次 vs 年次混在 / 単位ミス）
    revenues = [f.revenue for f in financials if f.revenue and f.revenue > 0]
    if len(revenues) >= 2:
        ratio = max(revenues) / min(revenues)
        if ratio > 10:
            # 単位ミスの可能性が高いケース（100倍・10000倍）を判別
            unit_mismatch_likely = False
            for r in revenues:
                for r2 in revenues:
                    if r > 0 and r2 > 0:
                        rr = r / r2
                        if 80 <= rr <= 120 or 900 <= rr <= 1100 or 9000 <= rr <= 11000:
                            unit_mismatch_likely = True
                            break
                if unit_mismatch_likely:
                    break
            if unit_mismatch_likely:
                issues.append({
                    "severity": "high",
                    "title": "🚨 単位の桁違いを検出（円⇔万円⇔百万円の取り違え疑い）",
                    "message": f"最小 {min(revenues):,.0f} vs 最大 {max(revenues):,.0f}（{ratio:.0f}倍差）。100倍/1万倍に近い差があり、円ベースで保存されたレコードと万円ベースが混在している疑い。",
                    "action": "桁ミスのレコードを🗑️で削除→PDF再アップロード推奨。または各レコードを開いて手動補正。",
                })
            else:
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
    # 並び順: 年次（新→旧）→ 月次（新→旧）。年次優先で表示。
    _all = db.query(FinancialData).filter(FinancialData.client_id == client_id).all()
    import re as _re_fd
    def _sort_key(f):
        pt = f.period_type or ""
        is_monthly = 1 if pt == "monthly" else 0  # 月次は後（1）、年次は前（0）
        p = f.period or ""
        y = _re_fd.search(r"(\d{4})", p)
        m = _re_fd.search(r"年\s*(\d{1,2})\s*月", p) or _re_fd.search(r"[/\-](\d{1,2})", p)
        # 降順にしたいので負号
        return (is_monthly, -(int(y.group(1)) if y else 0), -(int(m.group(1)) if m else 0))
    financials = sorted(_all, key=_sort_key)
    # 最新の年次データ（分析推奨）
    latest_annual = next((f for f in financials if (f.period_type or "") != "monthly"), None)
    quality_issues = _check_data_quality(financials)
    msg = request.query_params.get("msg", "")
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("client.html", {
        "request": request, "user": user, "client": cl,
        "financials": financials, "latest_annual": latest_annual,
        "quality_issues": quality_issues, "msg": msg, "error": error,
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

        # 期間文字列を西暦正規化 + 月次/年次を自動判定
        from .services.period_classifier import classify_period, normalize_to_seireki
        period = normalize_to_seireki(period or "")
        ptype, fy_label = classify_period(period)
        fy_label = normalize_to_seireki(fy_label)

        # 新規作成
        fd = FinancialData(
            client_id=client_id,
            period=period,
            period_type=ptype,
            fiscal_year=fy_label,
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

            # 🆕 月次推移表PDF 自動判定 → 月次抽出モード
            from .services.monthly_extractor import is_monthly_trend_file, extract_monthly_from_pdf, _guess_fiscal_year_from_filename
            if is_monthly_trend_file(file.filename) and AI_PROVIDER == "claude":
                try:
                    fy_label = _guess_fiscal_year_from_filename(file.filename)
                    mres = extract_monthly_from_pdf(content, file.filename, fy_label)
                    months = mres.get("months") or []
                    if not months:
                        errors.append(f"{file.filename}: 月次データを抽出できませんでした（{mres.get('error') or '原因不明'}）")
                        continue
                    fy = mres.get("fiscal_year") or fy_label or ""
                    m_saved = 0
                    m_skipped = 0
                    for m in months:
                        ml = m.get("month_label", "")
                        rev = m.get("revenue") or 0
                        if not ml or rev == 0:
                            continue
                        # 既存重複チェック
                        existing_m = db.query(FinancialData).filter(
                            FinancialData.client_id == client_id,
                            FinancialData.period == ml
                        ).first()
                        if existing_m:
                            m_skipped += 1
                            continue
                        mfd = FinancialData(
                            client_id=client_id,
                            period=ml, period_type="monthly", fiscal_year=fy,
                            revenue=rev,
                            cost_of_sales=m.get("cost_of_sales") or 0,
                            gross_profit=m.get("gross_profit") or 0,
                            selling_expenses=m.get("selling_expenses") or 0,
                            operating_profit=m.get("operating_profit") or 0,
                        )
                        db.add(mfd); m_saved += 1
                    db.commit()
                    print(f"[月次抽出] {file.filename}: 保存{m_saved}件 / 既存skip{m_skipped}件 / fy={fy}", flush=True)
                    if m_saved == 0 and m_skipped > 0:
                        errors.append(f"{file.filename}: 月次{len(months)}件すべて既存と重複（skipped）")
                    continue  # 月次抽出完了したので次のファイルへ
                except Exception as me:
                    print(f"[月次抽出] 失敗 {file.filename}: {me}", flush=True)
                    errors.append(f"{file.filename}: 月次抽出失敗: {str(me)[:100]} → 通常のPDF抽出にフォールバック")
                    # フォールバックして通常PDF抽出を試みる

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
def analyze_page(fd_id: int, request: Request, db: Session = Depends(get_db), run: int = 0):
    # run=0: 通常アクセス。キャッシュがあれば結果、無ければ「生成中」画面を返す（AIは走らせない）
    # run=1: ローディング画面のJSからの非同期キック。実際にAIを呼んで分析を生成・保存する
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
    cash_burn = compute_burn_rate(fd, breakdown=breakdown)
    cash_wc = compute_working_capital(fd)
    cash_ebitda = compute_ebitda(fd, breakdown)
    # CF 4バケツ（営業/投資/財務/フリー）
    _all_fds_for_cf = db.query(FinancialData).join(Client).filter(Client.id == fd.client_id).all()
    cash_cf = compute_cf_buckets(fd, breakdown=breakdown, historical_data=_all_fds_for_cf)

    # 費目グループ集計（販管費・売上原価のグループ別 + 前期比）
    expense_groups = {}
    try:
        from .services.expense_grouping import aggregate_expenses, compute_group_delta
        expense_groups["selling_expenses"] = aggregate_expenses(breakdown.get("selling_expenses_detail") or {})
        expense_groups["cost_of_sales"] = aggregate_expenses(breakdown.get("cost_of_sales_detail") or {})
        # 前期比
        if _all_fds_for_cf and len(_all_fds_for_cf) >= 2:
            sorted_hd = sorted(_all_fds_for_cf, key=lambda x: x.period or "")
            prev_fd_x = None
            for h in sorted_hd:
                if h.id == fd.id:
                    break
                prev_fd_x = h
            if prev_fd_x:
                try:
                    prev_bd_x = json.loads(getattr(prev_fd_x, "breakdown_json", "{}") or "{}")
                    expense_groups["selling_expenses_delta"] = compute_group_delta(
                        prev_bd_x.get("selling_expenses_detail") or {},
                        breakdown.get("selling_expenses_detail") or {},
                    )
                    expense_groups["cost_of_sales_delta"] = compute_group_delta(
                        prev_bd_x.get("cost_of_sales_detail") or {},
                        breakdown.get("cost_of_sales_detail") or {},
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # 月次集計＋月次グラフ用データを先に計算（キャッシュパスでも使うため）
    _sorted = sorted(_all_fds_for_cf, key=lambda x: x.period or "")
    _idx = next((i for i, x in enumerate(_sorted) if x.id == fd.id), None)
    if _idx is None:
        all_fds_for_client = [fd]
    else:
        all_fds_for_client = _sorted[: _idx + 1]

    aggregation_meta = {"monthly_count": 0, "annual_count": 0, "aggregated_count": 0,
                        "fiscal_years_with_monthly": [], "discrepancies_by_fy": {}}
    monthly_chart_data = None
    # ★ fd（DBレコード・URL/保存用）と effective_fd（分析・表示用）を分離
    #   - fd は触らない（fd.id は DB の本物のID、URL・Analysis保存・dismiss・PDF出力に使う）
    #   - effective_fd は分析・画面表示用の集計値（AggregatedFinancial / PartialYearSnapshot / annual の何れか）
    #   - 両方が同じものを指す場合もある（fd が annual の時）
    effective_fd = fd  # デフォルト: fd そのまま使う
    fd_substitution_meta = None
    try:
        from .services.monthly_aggregator import build_effective_historical_data
        effective_fds, aggregation_meta = build_effective_historical_data(all_fds_for_client)
        # ★ aggregated_count/partial_count が0でも、月次fdをクリックした場合は
        # 同FYの annual を探して effective_fd に採用する（「年次あれば年次優先」ルール）
        _need_substitution = (
            getattr(fd, "period_type", "") == "monthly"
            or aggregation_meta.get("aggregated_count", 0) > 0
            or aggregation_meta.get("partial_count", 0) > 0
        )
        if _need_substitution:
            all_fds_for_client = effective_fds
            # ★ fdが月次レコードの場合、effective_fds から該当する集計値を effective_fd として取り出す
            #   優先順位（Takeru判断 2026-06-07）: 年次があれば年次 / なければ月次集計 / 期中なら期中スナップ
            if getattr(fd, "period_type", "") == "monthly":
                _fy_raw = getattr(fd, "fiscal_year", "") or ""
                _fy_prefix = _fy_raw.split("（")[0]  # 「2026年2月期（第51期）」→「2026年2月期」
                # 1) annual を最優先で探す
                # ★ effective_fds は fd の手前までしかスライスされてないため、annual が
                #    fd より後ろ（決算月ベース）にある場合は含まれない。
                #    → 全件 (_all_fds_for_cf) から annual を探す
                _annual_match = next(
                    (f for f in effective_fds
                     if getattr(f, "period_type", "") == "annual"
                     and _fy_prefix and _fy_prefix in (getattr(f, "fiscal_year", "") or getattr(f, "period", ""))),
                    None,
                )
                if _annual_match is None:
                    # 履歴スライスで除外されている可能性 → 全件から探す
                    _annual_match = next(
                        (f for f in _all_fds_for_cf
                         if getattr(f, "period_type", "") in ("annual", "")
                         and _fy_prefix
                         and _fy_prefix in (getattr(f, "fiscal_year", "") or getattr(f, "period", ""))),
                        None,
                    )
                    # ★ 全件から拾った annual を all_fds_for_client（=effective_fds）に追加。
                    # こうしないと historical_data に effective_fd 自身が含まれず、
                    # trend metrics や前期検出が「分析対象の期」を見失う。
                    # 同FYの partial_aggregated/annual_aggregated は除去（annualが優先・重複防止）
                    if _annual_match is not None and _annual_match not in all_fds_for_client:
                        all_fds_for_client = [
                            f for f in all_fds_for_client
                            if not (
                                getattr(f, "period_type", "") in ("partial_aggregated", "annual_aggregated")
                                and (getattr(f, "fiscal_year", "") or "") == _fy_raw
                            )
                        ]
                        all_fds_for_client = list(all_fds_for_client) + [_annual_match]
                # 2) annual_aggregated（月次12ヶ月集計）
                _agg_match = next(
                    (f for f in effective_fds
                     if getattr(f, "period_type", "") == "annual_aggregated"
                     and (getattr(f, "fiscal_year", "") or "") == _fy_raw),
                    None,
                )
                # 3) partial_aggregated（期中n月集計）
                _partial_match = next(
                    (f for f in effective_fds
                     if getattr(f, "period_type", "") == "partial_aggregated"
                     and (getattr(f, "fiscal_year", "") or "") == _fy_raw),
                    None,
                )
                _picked = _annual_match or _agg_match or _partial_match
                if _picked is not None and _picked is not fd:
                    effective_fd = _picked
                    fd_substitution_meta = {
                        "original_fd_id": fd.id,
                        "original_period": fd.period,
                        "effective_period_type": getattr(effective_fd, "period_type", ""),
                        "effective_period": getattr(effective_fd, "period", ""),
                        "fiscal_year": _fy_raw,
                        "n_months": getattr(effective_fd, "_partial_n_months", None),
                    }
                    print(f"[effective_fd採用] {cl.name} 月次({fd.period}) → {fd_substitution_meta['effective_period_type']}({fd_substitution_meta['effective_period']})", flush=True)
                    # ★ effective_fd 用に breakdown / cash系 / expense_groups を再計算
                    try:
                        breakdown = json.loads(effective_fd.breakdown_json or "{}")
                    except Exception:
                        breakdown = {}
                    cash_burn = compute_burn_rate(effective_fd, breakdown=breakdown)
                    cash_wc = compute_working_capital(effective_fd)
                    cash_ebitda = compute_ebitda(effective_fd, breakdown)
                    # cash_cf 用の history は all_fds_for_client（effective_fds 入り）を使う。
                    # ただし、effective_fd 自身が含まれるなら除いて渡す（前期検出を狂わせないため）。
                    _hist_for_cf = [h for h in all_fds_for_client if h is not effective_fd]
                    cash_cf = compute_cf_buckets(effective_fd, breakdown=breakdown, historical_data=_hist_for_cf)
                    try:
                        from .services.expense_grouping import aggregate_expenses
                        expense_groups["selling_expenses"] = aggregate_expenses(breakdown.get("selling_expenses_detail") or {})
                        expense_groups["cost_of_sales"] = aggregate_expenses(breakdown.get("cost_of_sales_detail") or {})
                        # ★ 月次fd時に計算済みの古い delta を除去（年次/期中とのスケール不一致を防ぐ）
                        # effective_fd 用の delta は別途必要なら再計算
                        for _del_key in ("selling_expenses_delta", "cost_of_sales_delta"):
                            expense_groups.pop(_del_key, None)
                    except Exception as _e:
                        print(f"[effective_fd 再計算 expense_groups] skip: {_e}", flush=True)

        monthly_fds_for_chart = [f for f in _sorted if getattr(f, "period_type", "") == "monthly"]
        if len(monthly_fds_for_chart) >= 3:
            import re as _re
            def _key(f):
                p = f.period or ""
                y = _re.search(r"(\d{4})", p)
                m = _re.search(r"年\s*(\d{1,2})\s*月", p) or _re.search(r"[/\-](\d{1,2})", p)
                return (int(y.group(1)) if y else 0, int(m.group(1)) if m else 0)
            monthly_fds_for_chart.sort(key=_key)

            # ★ 年度別グルーピング（売上・営利の年度間比較用）
            from collections import defaultdict
            groups = defaultdict(dict)  # fy -> {month_num: {revenue, op}}
            for f in monthly_fds_for_chart:
                fy = getattr(f, "fiscal_year", "") or ""
                mo = _re.search(r"年\s*(\d{1,2})\s*月", f.period or "")
                if not mo:
                    continue
                m = int(mo.group(1))
                groups[fy][m] = {
                    "revenue": round(f.revenue or 0, 1),
                    "op": round(f.operating_profit or 0, 1),
                }

            # 決算月を fiscal_year ラベルから推定（例: "2025年2月期" → 2）
            close_month = 12
            for fy in groups.keys():
                mm = _re.search(r"(\d{1,2})\s*月期", fy or "")
                if mm:
                    close_month = int(mm.group(1))
                    break

            # X軸ラベル：決算月+1 から12ヶ月（例：決算月2なら 3,4,...,2）
            x_months = []
            for i in range(12):
                m_val = ((close_month + i) % 12) + 1
                x_months.append(f"{m_val}月")

            # 各年度のシリーズデータ
            revenue_series = []
            op_series = []
            for fy in sorted(groups.keys()):
                rev_arr = []
                op_arr = []
                for i in range(12):
                    m_val = ((close_month + i) % 12) + 1
                    data = groups[fy].get(m_val) or {}
                    rev_arr.append(data.get("revenue"))
                    op_arr.append(data.get("op"))
                revenue_series.append({"label": fy, "data": rev_arr})
                op_series.append({"label": fy, "data": op_arr})

            monthly_chart_data = {
                # 新形式: 年度別重ね折れ線用
                "x_months": x_months,
                "revenue_series": revenue_series,
                "op_series": op_series,
                # 旧形式: 後方互換（PDF用）
                "labels": [f.period for f in monthly_fds_for_chart],
                "revenue": [round(f.revenue or 0, 1) for f in monthly_fds_for_chart],
                "operating_profit": [round(f.operating_profit or 0, 1) for f in monthly_fds_for_chart],
                "cash": [round(f.cash or 0, 1) for f in monthly_fds_for_chart],
                "count": len(monthly_fds_for_chart),
                "fy_count": len(groups),
            }
    except Exception as _e:
        print(f"[月次集計] skip: {_e}", flush=True)

    existing = db.query(Analysis).filter(Analysis.financial_data_id == fd_id).order_by(Analysis.created_at.desc()).first()
    # ★ キャッシュ整合性チェック: effective_fd が置き換わってる場合、
    # 古いキャッシュ（月次fd時点）と新しい表示数字（年次/期中集計）が食い違うため、
    # キャッシュの fd_substitution が現在と一致しないなら無効化して再分析させる
    if existing and fd_substitution_meta:
        try:
            _cached = json.loads(existing.result_json)
            _cached_sub = _cached.get("fd_substitution") or {}
            _curr_eff_type = fd_substitution_meta.get("effective_period_type")
            _curr_eff_period = fd_substitution_meta.get("effective_period")
            _cache_eff_type = _cached_sub.get("effective_period_type")
            _cache_eff_period = _cached_sub.get("effective_period")
            if (_cache_eff_type != _curr_eff_type) or (_cache_eff_period != _curr_eff_period):
                print(f"[キャッシュ無効化] effective_fd 変更検知: cache={_cache_eff_type}/{_cache_eff_period} → current={_curr_eff_type}/{_curr_eff_period}", flush=True)
                # この古い行は今後も描画されない（effective_fd 不一致）。
                # DBに残すと /analyze/status が誤って done を返し、結果ページ→再無効化→
                # ローディングのリダイレクトループになる。削除してDB状態を正す
                # （analyze_page と status が同じ状態を見て一致する）。
                try:
                    db.delete(existing); db.commit()
                except Exception as _de:
                    db.rollback()
                    print(f"[キャッシュ無効化] 古い行の削除に失敗（無視して続行）: {_de}", flush=True)
                existing = None  # bypass
        except Exception as _e:
            print(f"[キャッシュ整合性チェック] skip: {_e}", flush=True)
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
            "request": request, "user": user, "client": cl, "fd": fd, "effective_fd": effective_fd, "result": result, "fd_substitution_meta": fd_substitution_meta,
            "breakdown": breakdown,
            "cash_burn": cash_burn, "cash_wc": cash_wc, "cash_ebitda": cash_ebitda, "cash_cf": cash_cf,
            "expense_groups": expense_groups,
            "aggregation_meta": aggregation_meta,
            "monthly_chart_data": monthly_chart_data,
            "analysis_id": existing.id, "cached": True,
            "dismissed_solutions": dismissed_sols,
        })

    # ── キャッシュ無し ──────────────────────────────────────────
    # run=0（通常アクセス／ボタン押下）: AIは走らせず、まず「生成中」画面を即返す。
    #   画面のJSが run=1 でこのエンドポイントを非同期に叩いて生成を開始し、
    #   /analyze/status をポーリングして完了したら結果ページへ遷移する。
    if not run:
        return templates.TemplateResponse("analyzing.html", {
            "request": request, "user": user, "client": cl, "fd": fd,
            "inflight": _is_analysis_inflight(fd_id),
        })

    # run=1（非同期キック）: 二重起動ガード。既に同じ期の分析が走っていれば
    #   新規にAIを呼ばず running を返す（連打による多重API課金を防止）。
    if not _try_acquire_analysis(fd_id):
        return JSONResponse({"status": "running"})

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
        # Web自動取得した事業情報も business_context に注入
        web_text = ""
        try:
            web_json = json.loads(getattr(cl, "web_extracted_json", "") or "{}")
            if web_json:
                from .services.web_extract import format_for_business_context
                web_text = format_for_business_context(web_json)
        except Exception:
            pass
        merged_bd = (base_bd + hearing_text + web_text).strip()

        # 税理士の除外カテゴリ設定を読み込み
        try:
            user_excluded = json.loads(getattr(user, "excluded_categories", "[]") or "[]")
        except Exception:
            user_excluded = []
        # 📚 税理士が選択した参照書籍を読み込み
        reference_books = []
        try:
            sel_ids = [int(x) for x in json.loads(getattr(user, "selected_books", "[]") or "[]")]
        except Exception:
            sel_ids = []
        if sel_ids:
            books = db.query(ReferenceBook).filter(
                ReferenceBook.id.in_(sel_ids), ReferenceBook.is_active == 1
            ).all()
            reference_books = [
                {"title": b.title, "author": b.author, "content": b.processed_content}
                for b in books
            ]
        # ★ analyze_financials には effective_fd を渡す（分析用）
        # ★ Analysis 保存は fd_id（DB上の本物のID、URLパラメータ）を使う → DB整合性
        # ★ raw_monthly_data = 期中スナップショットの YoY 検索用に生月次データを別途渡す
        #   （historical_data は effective_fds 置換で生月次が消えてるため）
        _raw_monthly = [f for f in _all_fds_for_cf if getattr(f, "period_type", "") == "monthly"]
        result = analyze_financials(
            effective_fd, cl.name, cl.industry,
            business_details=merged_bd,
            historical_data=all_fds_for_client if len(all_fds_for_client) > 1 else None,
            referral_code=getattr(user, "referral_code", "") or f"tax_{user.id:03d}",
            excluded_categories=user_excluded,
            reference_books=reference_books,
            raw_monthly_data=_raw_monthly if _raw_monthly else None,
        )
        # fd差し替えメタを result にも同梱（UI表示用）
        if fd_substitution_meta:
            result["fd_substitution"] = fd_substitution_meta
        analysis = Analysis(financial_data_id=fd_id, result_json=json.dumps(result, ensure_ascii=False))
        db.add(analysis); db.commit(); db.refresh(analysis)
        # run=1（非同期キック）からの呼び出し。結果はキャッシュに保存済みなので
        # JSONで完了を返す。画面側は /analyze（run=0）へ遷移して結果を表示する。
        return JSONResponse({"status": "done", "analysis_id": analysis.id})
    except Exception as e:
        err_str = str(e)
        if '429' in err_str or 'rate_limit' in err_str:
            friendly_err = "⏱ AIのレート制限に達しました（複数のリトライ後も解消せず）。\n5〜10分待ってから「再分析」ボタンを押してください。\n\n※ 大きな決算書や連続実行で発生しやすい問題です。"
        elif 'overloaded' in err_str.lower():
            friendly_err = "🔥 AIサーバが混雑中です。5分ほど待ってから再分析してください。"
        else:
            friendly_err = err_str
        return JSONResponse({"status": "error", "error": friendly_err}, status_code=500)
    finally:
        # 成功・失敗どちらでも二重起動ガードを解放
        _release_analysis(fd_id)


@app.get("/financials/{fd_id}/analyze/status")
def analyze_status(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """ローディング画面のポーリング用。分析がキャッシュ済みか／生成中かを返す。"""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"status": "error", "error": "unauthenticated"}, status_code=401)
    # 所有権チェック（fd → client → user）
    fd = db.query(FinancialData).filter(FinancialData.id == fd_id).first()
    if not fd:
        return JSONResponse({"status": "error", "error": "not_found"}, status_code=404)
    cl = db.query(Client).filter(Client.id == fd.client_id, Client.user_id == user.id).first()
    if not cl:
        return JSONResponse({"status": "error", "error": "forbidden"}, status_code=403)
    existing = db.query(Analysis).filter(Analysis.financial_data_id == fd_id).order_by(Analysis.created_at.desc()).first()
    if existing:
        return JSONResponse({"status": "done", "analysis_id": existing.id})
    if _is_analysis_inflight(fd_id):
        return JSONResponse({"status": "running"})
    return JSONResponse({"status": "idle"})


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


@app.get("/financials/{fd_id}/tax-proposals")
def tax_proposals_api(fd_id: int, request: Request, db: Session = Depends(get_db), need: str = "tax"):
    """確定系の打ち手シミュレーターの初期データ（提案一覧＋ニーズ別並び替え）を返す。"""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return JSONResponse({"error": "not_found"}, status_code=404)
    from .services import proposal_engine as pe
    # ページ本体と同じ effective_fd（月次→年換算）を使う。生のfdだと月次で桁ズレする
    all_fds = db.query(FinancialData).join(Client).filter(
        Client.id == fd.client_id, Client.user_id == user.id
    ).all()
    eff_fd, _, _, _ = _resolve_effective_fd(fd, all_fds)
    ctx = pe.build_context(eff_fd)
    data = pe.list_proposals(ctx)
    data["proposals"] = pe.rank(ctx, need_key=need)
    data["current_need"] = need
    return data


@app.post("/financials/{fd_id}/tax-simulate")
async def tax_simulate_api(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """選択された打ち手の累積影響を計算して返す（税は合算後に1回だけ再計算）。"""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    selections = body.get("selections") or []
    from .services import proposal_engine as pe
    # ページ本体と同じ effective_fd（月次→年換算）を使う。生のfdだと月次で桁ズレする
    all_fds = db.query(FinancialData).join(Client).filter(
        Client.id == fd.client_id, Client.user_id == user.id
    ).all()
    eff_fd, _, _, _ = _resolve_effective_fd(fd, all_fds)
    ctx = pe.build_context(eff_fd)
    return pe.simulate(ctx, selections)


@app.post("/financials/{fd_id}/tax-impact")
async def tax_impact_api(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """1提案の単独effectを指定額で正確に計算（カードのプレビュー用）。"""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    from .services import proposal_engine as pe
    all_fds = db.query(FinancialData).join(Client).filter(
        Client.id == fd.client_id, Client.user_id == user.id
    ).all()
    eff_fd, _, _, _ = _resolve_effective_fd(fd, all_fds)
    ctx = pe.build_context(eff_fd)
    return pe.impact_at(ctx, body.get("id"), body.get("amount"))


@app.post("/financials/{fd_id}/tax-project")
async def tax_project_api(fd_id: int, request: Request, db: Session = Depends(get_db)):
    """複数年のキャッシュ推移（概算）。body: {selections, years, growth, loan_repay}。"""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    from .services import proposal_engine as pe
    all_fds = db.query(FinancialData).join(Client).filter(
        Client.id == fd.client_id, Client.user_id == user.id
    ).all()
    eff_fd, _, _, _ = _resolve_effective_fd(fd, all_fds)
    ctx = pe.build_context(eff_fd)
    return pe.project_years(
        ctx, body.get("selections") or [],
        years=body.get("years", 3), growth=body.get("growth", 0.0),
        loan_repay=body.get("loan_repay", 0.0),
    )


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

    # ★ analyze_page と同様に effective_fd に解決する（月次fdクリック時の整合性）
    # ★ fd の手前までスライス（as-of）して analyze_page と同じ範囲で解決する
    # こうしないと後から月次が追加された場合、PDFは新しい snapshot を使うが
    # analyze_page のキャッシュは古いものを使う食い違いが起きる
    _all_fds_full = db.query(FinancialData).filter(FinancialData.client_id == cl.id).all()
    _sorted_pdf = sorted(_all_fds_full, key=lambda x: x.period or "")
    _idx_pdf = next((i for i, x in enumerate(_sorted_pdf) if x.id == fd.id), None)
    _all_fds_for_resolve = _sorted_pdf[: _idx_pdf + 1] if _idx_pdf is not None else [fd]
    # fallback_pool=_all_fds_full: as-ofスライスで annual が除外される場合、全件から拾えるように
    # こうしないと PDF が partial/月次 を effective_fd に採用するが、analyze_page は annual を採用する食い違いが起きる
    effective_fd, _effective_fds, _agg_meta, fd_substitution_meta = _resolve_effective_fd(
        fd, _all_fds_for_resolve, fallback_pool=_all_fds_full
    )

    existing = db.query(Analysis).filter(
        Analysis.financial_data_id == fd_id
    ).order_by(Analysis.created_at.desc()).first()
    if not existing:
        return RedirectResponse(f"/financials/{fd_id}/analyze", status_code=302)

    # ★ キャッシュ整合性チェック: effective_fd が現在と一致しないなら analyze_page にリダイレクトして再生成
    # （analyze_page と同じロジック・古い narrative と新しい display 値の食い違いを防ぐ）
    if fd_substitution_meta:
        try:
            _cached = json.loads(existing.result_json)
            _cached_sub = _cached.get("fd_substitution") or {}
            _curr_eff_type = fd_substitution_meta.get("effective_period_type")
            _curr_eff_period = fd_substitution_meta.get("effective_period")
            _cache_eff_type = _cached_sub.get("effective_period_type")
            _cache_eff_period = _cached_sub.get("effective_period")
            if (_cache_eff_type != _curr_eff_type) or (_cache_eff_period != _curr_eff_period):
                print(f"[pdf_view キャッシュ無効化] cache={_cache_eff_type}/{_cache_eff_period} → current={_curr_eff_type}/{_curr_eff_period}", flush=True)
                return RedirectResponse(f"/financials/{fd_id}/analyze", status_code=302)
        except Exception as _e:
            print(f"[pdf_view キャッシュ整合性チェック] skip: {_e}", flush=True)

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

    # 掴みページ用：構成比（取引先依存など）の信号は推移グラフで表せないため、
    # 課題文から「最大主体名」と「集中度%」を抽出して構成比バー用データを作る。
    import re as _re_conc
    _conc_kw = ("依存", "集中", "シェア", "構成比")
    for _p in filtered_result["prioritized_problems"][:3]:
        _txt = (_p.get("title") or "") + " " + (_p.get("fact") or "")
        if any(k in _txt for k in _conc_kw):
            # 社名の直後の括弧内%（例「いであ㈱が…（77.0%）」）を最優先で取る。
            # こうすると「利益率0.2%…1社77%」のように非依存の%が先頭にあっても誤らない。
            _pct_str = None
            _label = "最大の取引先"
            _paired = _re_conc.search(r"([^\s、。（）()]{1,14}(?:㈱|株式会社))[^（(]{0,16}[（(]\s*(\d+(?:\.\d+)?)\s*[%％]", _txt)
            if _paired:
                _label = _paired.group(1)
                _pct_str = _paired.group(2)
            else:
                # フォールバック：文中最初の% ＋ 取れれば社名
                _m = _re_conc.search(r"(\d+(?:\.\d+)?)\s*[%％]", _txt)
                _pct_str = _m.group(1) if _m else None
                _nm = _re_conc.search(r"([^\s、。（）()]{1,14}(?:㈱|株式会社))", _txt)
                if _nm:
                    _label = _nm.group(1)
            if _pct_str:
                try:
                    _pct = float(_pct_str)
                except Exception:
                    _pct = None
                if _pct is not None and 0 < _pct <= 100:
                    _p["_concentration"] = {"pct": round(_pct, 1), "label": _label}

    pdf_content = filtered_result.get("owner_pdf_content")

    # 削除を変更してキャッシュ無効化されていれば再生成
    if not pdf_content:
        try:
            from .claude_client import generate_owner_pdf_content
            pdf_content = generate_owner_pdf_content(filtered_result, effective_fd, cl.name, effective_fd.period)
            # 元のresultに保存（dismissed_solutionsに紐づいた状態のキャッシュ）
            result["owner_pdf_content"] = pdf_content
            existing.result_json = json.dumps(result, ensure_ascii=False)
            db.commit()
        except Exception as e:
            print(f"[pdf_view] narrative generation failed: {e}")
            # 新構造のキー（section1-6）に合わせたフォールバック。
            # AI生成失敗時でも既存の分析結果から最低限の内容を出す。
            owner_msg = result.get("owner_message", "") or result.get("summary", "")
            strengths_src = result.get("strengths") or []
            issues_src = (filtered_result.get("prioritized_problems") or result.get("prioritized_problems") or [])

            pdf_content = {
                "cover_subtitle": (result.get("key_insight") or "経営分析レポート")[:60],
                "section1_performance": {
                    "headline": result.get("key_insight", "") or owner_msg[:80],
                    "summary_text": result.get("summary", "") or owner_msg,
                    "trend_comment": "",
                },
                "section2_strengths": {
                    "headline": "御社の強み" if strengths_src else "",
                    "strengths_list": [
                        {"title": s if isinstance(s, str) else str(s)[:40], "evidence": "", "implication": ""}
                        for s in strengths_src[:5]
                    ],
                },
                "section3_issues": {
                    "headline": "取り組むべき課題",
                    "issues": [
                        {
                            "rank": p.get("rank", i + 1),
                            "title": p.get("title", ""),
                            "fact": p.get("detail", ""),
                            "causal": "",
                            "implication": p.get("expected_outcome", ""),
                        }
                        for i, p in enumerate(issues_src[:3])
                    ],
                },
                "section4_proposals_per_issue": [
                    {
                        "issue_rank": p.get("rank", i + 1),
                        "issue_title": p.get("title", ""),
                        "intro": "",
                        "solutions": [
                            {
                                "title": s.get("title", ""),
                                "expected_effect": (
                                    f"+{s.get('impact_min', 0):,.0f}〜{s.get('impact_max', 0):,.0f}万円"
                                    if s.get("impact_min") is not None and s.get("impact_max") is not None
                                    else "—"
                                ),
                                "cost": "—",
                                "timeframe": s.get("timeframe", "—"),
                                "first_step": s.get("first_step", ""),
                                "why": s.get("why", ""),
                            }
                            for s in (p.get("solutions") or [])
                        ],
                    }
                    for i, p in enumerate(issues_src[:3])
                ],
                "section5_tax_proposals": {"applicable": False, "proposals": []},
                "section6_conclusion": {
                    "headline": "",
                    "positive_summary": "",
                    "next_steps_summary": result.get("owner_what_to_do", ""),
                    "closing_message": "本レポートはAI生成が失敗したため、既存分析結果からの簡易版です。「再分析」後に再度PDF出力を試してください。",
                },
            }

    # cash_burn/wc/ebitda/cf もテンプレに必要
    from .services.cash_analysis import compute_burn_rate, compute_working_capital, compute_ebitda, compute_cf_buckets
    try:
        breakdown = json.loads(fd.breakdown_json or "{}")
    except Exception:
        breakdown = {}
    cash_burn = compute_burn_rate(fd, breakdown=breakdown)
    cash_wc = compute_working_capital(fd)
    cash_ebitda = compute_ebitda(fd, breakdown)
    _all_fds_for_cf = db.query(FinancialData).join(Client).filter(Client.id == fd.client_id).all()
    cash_cf = compute_cf_buckets(fd, breakdown=breakdown, historical_data=_all_fds_for_cf)

    # ★ effective_fd が置換された場合は、breakdown / cash 系を effective_fd ベースで再計算
    # （analyze_page と同じ整合性ロジック）
    if fd_substitution_meta:
        try:
            breakdown = json.loads(effective_fd.breakdown_json or "{}")
        except Exception:
            breakdown = {}
        cash_burn = compute_burn_rate(effective_fd, breakdown=breakdown)
        cash_wc = compute_working_capital(effective_fd)
        cash_ebitda = compute_ebitda(effective_fd, breakdown)
        # cash_cf 用の history は _effective_fds から effective_fd を除いて使う
        # （生月次データを渡すと前期検出が h.id==fd.id で走査ミスする）
        _hist_for_cf = [h for h in (_effective_fds or []) if h is not effective_fd]
        cash_cf = compute_cf_buckets(effective_fd, breakdown=breakdown, historical_data=_hist_for_cf)

    # 月次データ集計（pdf_view 用。analyze と同じロジック）
    monthly_chart_data = None
    try:
        monthly_fds = [f for f in _all_fds_for_cf if (getattr(f, "period_type", "") == "monthly")]
        if len(monthly_fds) >= 3:
            import re as _re
            def _key(f):
                p = f.period or ""
                y = _re.search(r"(\d{4})", p); mo = _re.search(r"年\s*(\d{1,2})\s*月", p) or _re.search(r"[/\-](\d{1,2})", p)
                return (int(y.group(1)) if y else 0, int(mo.group(1)) if mo else 0)
            monthly_fds.sort(key=_key)
            monthly_chart_data = {
                "labels": [f.period for f in monthly_fds],
                "revenue": [round(f.revenue or 0, 1) for f in monthly_fds],
                "operating_profit": [round(f.operating_profit or 0, 1) for f in monthly_fds],
                "count": len(monthly_fds),
            }
    except Exception:
        pass

    # 費目グループ集計（既存分析に無い場合の動的計算 - 過去 result への遡及適用）
    if not filtered_result.get("expense_groups"):
        try:
            from .services.expense_grouping import aggregate_expenses, compute_group_delta
            eg = {
                "selling_expenses": aggregate_expenses(breakdown.get("selling_expenses_detail") or {}),
                "cost_of_sales": aggregate_expenses(breakdown.get("cost_of_sales_detail") or {}),
            }
            # 前期比較
            if _all_fds_for_cf and len(_all_fds_for_cf) >= 2:
                sorted_hd = sorted(_all_fds_for_cf, key=lambda x: x.period or "")
                prev_fd_x = None
                for h in sorted_hd:
                    if h.id == fd.id:
                        break
                    prev_fd_x = h
                if prev_fd_x:
                    try:
                        prev_bd_x = json.loads(getattr(prev_fd_x, "breakdown_json", "{}") or "{}")
                        eg["selling_expenses_delta"] = compute_group_delta(
                            prev_bd_x.get("selling_expenses_detail") or {},
                            breakdown.get("selling_expenses_detail") or {},
                        )
                    except Exception:
                        pass
            filtered_result["expense_groups"] = eg
        except Exception:
            pass

    return templates.TemplateResponse("pdf_view.html", {
        "request": request,
        "client": cl,
        "fd": fd,
        "effective_fd": effective_fd,
        "fd_substitution_meta": fd_substitution_meta,
        "pdf_content": pdf_content,
        "result": filtered_result,  # フィルタ済み（削除済み除外）
        "breakdown": breakdown,
        "cash_burn": cash_burn,
        "cash_wc": cash_wc,
        "cash_ebitda": cash_ebitda,
        "cash_cf": cash_cf,
        "monthly_chart_data": monthly_chart_data,
        "user": user,
    })


def _find_solution_by_id(result: dict, sol_id: str):
    """sol_id ('rank_idx' 形式) から solution dict を取り出す"""
    if not sol_id or "_" not in sol_id:
        return None
    try:
        rank_s, idx_s = sol_id.split("_", 1)
        target_rank = int(rank_s)
        idx = int(idx_s)
    except Exception:
        return None
    for p in (result.get("prioritized_problems") or []):
        if p.get("rank") == target_rank:
            sols = p.get("solutions") or []
            if 0 <= idx < len(sols):
                return sols[idx]
    return None


@app.get("/financials/{fd_id}/referral-kit/{sol_id}", response_class=HTMLResponse)
def referral_kit_page(fd_id: int, sol_id: str, request: Request,
                      mode: str = "memo",  # 'memo' / 'email' / 'pdf'
                      db: Session = Depends(get_db)):
    """採択された solution に対する紹介キット（口頭メモ/メール/PDF）を返す"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id).first()

    analysis = db.query(Analysis).filter(
        Analysis.financial_data_id == fd_id
    ).order_by(Analysis.created_at.desc()).first()
    if not analysis:
        return HTMLResponse("分析結果がまだありません", status_code=404)

    result = json.loads(analysis.result_json)
    sol = _find_solution_by_id(result, sol_id)
    if not sol:
        return HTMLResponse(f"指定された提案が見つかりません (sol_id={sol_id})", status_code=404)

    # 税理士事務所名は user.display_name を流用（暫定）
    tax_office_name = getattr(user, "display_name", "") or user.username
    tax_ref_code = getattr(user, "referral_code", "") or f"tax_{user.id:03d}"
    base_url = str(request.base_url).rstrip("/")

    from .services.referral_kit import build_referral_kit
    kit = build_referral_kit(
        solution=sol,
        client_name=cl.name,
        tax_office_name=tax_office_name,
        fd_id=fd_id,
        client_id=cl.id,
        base_url=base_url,
        tax_referral_code=tax_ref_code,
    )

    # mode=pdf なら紹介PDF（A4縦1ページ）を返す
    if mode == "pdf":
        return templates.TemplateResponse("referral_pdf.html", {
            "request": request,
            "kit": kit,
            "client": cl,
            "user": user,
        })

    # memo / email は JSON フラグメントで返す（フロントでモーダル表示）
    return JSONResponse({
        "ok": True,
        "sol_title": sol.get("title", ""),
        "kit": kit,
    })


@app.get("/financials/{fd_id}/referral-pdf-download/{sol_id}")
def referral_pdf_download(fd_id: int, sol_id: str, request: Request,
                          db: Session = Depends(get_db)):
    """紹介PDF（手渡し用）を Playwright で PDF 化してダウンロード"""
    from fastapi.responses import Response
    from .services.pdf_export import generate_pdf_for_fd

    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    fd = db.query(FinancialData).join(Client).filter(
        FinancialData.id == fd_id, Client.user_id == user.id
    ).first()
    if not fd:
        return RedirectResponse("/dashboard", status_code=302)
    cl = db.query(Client).filter(Client.id == fd.client_id).first()

    session_value = request.cookies.get("session", "")
    if not session_value:
        return RedirectResponse("/login", status_code=302)

    base_url = str(request.base_url).rstrip("/")

    # 紹介PDF用の専用エンドポイントを開かせる（既存 generate_pdf_for_fd は固定URL前提のため、
    # 簡易的に Playwright を別経路で叩く実装にする）
    import subprocess, tempfile, os
    from pathlib import Path

    SCRIPT_DIR = Path(__file__).parent.parent
    node_script = SCRIPT_DIR / "pdf_export.js"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name
    try:
        env = os.environ.copy()
        env["COPARTNER_BASE"] = base_url
        env["COPARTNER_FD_ID"] = str(fd_id)
        env["COPARTNER_SESSION"] = session_value
        env["COPARTNER_OUTPUT"] = pdf_path
        env["COPARTNER_TARGET_PATH"] = f"/financials/{fd_id}/referral-kit/{sol_id}?mode=pdf"
        try:
            result = subprocess.run(
                ["node", str(node_script)],
                cwd=str(SCRIPT_DIR), env=env,
                capture_output=True, text=False, timeout=180,
            )
        except Exception as e:
            return RedirectResponse(f"/financials/{fd_id}/analyze?err=referral_pdf:{str(e)[:40]}", status_code=302)
        if result.returncode != 0 or not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return RedirectResponse(f"/financials/{fd_id}/analyze?err=referral_pdf_failed", status_code=302)
        with open(pdf_path, "rb") as f:
            data = f.read()
    finally:
        try: os.unlink(pdf_path)
        except Exception: pass

    filename = f"{cl.name}_紹介資料_{sol_id}.pdf".replace("/", "_")
    filename_enc = quote(filename, safe="")
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=referral.pdf; filename*=UTF-8''{filename_enc}"},
    )


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

    # PDF生成（Playwright）— base_url は受信リクエストから動的に決める
    # （本番デプロイ・Docker・プロキシ経由でも動くように）
    base_url = str(request.base_url).rstrip("/")
    try:
        pdf_bytes = generate_pdf_for_fd(fd_id, session_value, base_url=base_url)
    except Exception as e:
        # subprocess の異常終了等は generate_pdf_for_fd 内で握り潰すが、
        # 想定外の例外（PermissionError 等）でも 500 にせず redirect で返す
        print(f"[export_pdf] failed for fd={fd_id}: {e}")
        return RedirectResponse(f"/financials/{fd_id}/analyze?err=pdf", status_code=302)
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
    # 📚 書籍ナレッジDB：利用可能な書籍と、この税理士が選択中のID
    all_books = db.query(ReferenceBook).filter(ReferenceBook.is_active == 1).order_by(ReferenceBook.id.desc()).all()
    try:
        selected_books = [str(x) for x in json.loads(user.selected_books or "[]")]
    except Exception:
        selected_books = []
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "excluded": excluded, "own_partners": own_partners,
        "categories": categories,
        "all_books": all_books, "selected_books": selected_books,
        "is_admin": _is_admin(user),
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
    # 📚 参照する書籍の選択（チェックボックスで複数選択）
    sel_books = form.getlist("books[]") if hasattr(form, "getlist") else []
    if not sel_books:
        sel_books = [v for k, v in form.multi_items() if k == "books[]"] if hasattr(form, "multi_items") else []
    user.selected_books = json.dumps([str(b) for b in sel_books], ensure_ascii=False)
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


# ============================================================
# 🌟 紹介可能サービス管理（ReferralService CRUD）
# ============================================================
@app.get("/admin/referral-services", response_class=HTMLResponse)
def referral_services_list(request: Request, db: Session = Depends(get_db),
                            q: str = "", category: str = ""):
    """紹介可能サービス 一覧"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    query = db.query(ReferralService).order_by(
        ReferralService.is_active.desc(),
        ReferralService.sort_order.asc(),
        ReferralService.id.asc(),
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            (ReferralService.name.like(like))
            | (ReferralService.provider.like(like))
            | (ReferralService.description_short.like(like))
        )
    if category:
        query = query.filter(ReferralService.category == category)
    services = query.all()
    # カテゴリ一覧（フィルタ用）
    cat_rows = db.query(ReferralService.category).distinct().all()
    categories = sorted({r[0] for r in cat_rows if r[0]})
    return templates.TemplateResponse("admin_referral_services_list.html", {
        "request": request, "user": user,
        "services": services, "categories": categories,
        "q": q, "selected_category": category,
    })


@app.get("/admin/referral-services/new", response_class=HTMLResponse)
def referral_services_new(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("admin_referral_service_edit.html", {
        "request": request, "user": user, "service": None,
    })


@app.get("/admin/referral-services/{svc_id}/edit", response_class=HTMLResponse)
def referral_services_edit(svc_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    svc = db.query(ReferralService).filter(ReferralService.id == svc_id).first()
    if not svc:
        return RedirectResponse("/admin/referral-services", status_code=302)
    return templates.TemplateResponse("admin_referral_service_edit.html", {
        "request": request, "user": user, "service": svc,
    })


def _extract_text_from_upload(filename: str, raw: bytes) -> str:
    """アップロードファイルからテキスト抽出（.pdf=PyMuPDF / それ以外=UTF-8デコード）"""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import pdfplumber, io  # 既存の依存（requirements.txt）を流用
            parts = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    parts.append(page.extract_text() or "")
            return "\n".join(parts)
        except Exception as e:
            print(f"[reference_books] PDF抽出失敗: {e}", flush=True)
            return ""
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


# アップロード上限（バイト）。環境変数 COPARTNER_BOOK_MAX_BYTES で上書き可（既定20MB）
BOOK_MAX_BYTES = int(os.environ.get("COPARTNER_BOOK_MAX_BYTES", str(20 * 1024 * 1024)))


def _is_admin(user) -> bool:
    """運営管理者か。判定は非クレーム可能な is_admin フラグのみ。
    付与は運営がサーバ上で `python -m app.grant_admin <username>` を実行して明示的に行う
    （ユーザー名ベース・自動昇格は乗っ取り/誤昇格の余地があるため採用しない）。"""
    return user is not None and getattr(user, "is_admin", 0) == 1


@app.get("/admin/reference-books", response_class=HTMLResponse)
def reference_books_list(request: Request, db: Session = Depends(get_db)):
    """書籍ナレッジDB 一覧・アップロード（運営のみ）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings", status_code=302)
    books = db.query(ReferenceBook).order_by(
        ReferenceBook.is_active.desc(), ReferenceBook.id.desc()
    ).all()
    return templates.TemplateResponse("admin_reference_books.html", {
        "request": request, "user": user, "books": books,
    })


@app.post("/admin/reference-books/save")
async def reference_books_save(
    request: Request,
    title: str = Form(...),
    author: str = Form(""),
    publisher: str = Form(""),
    license_status: str = Form("none"),
    pasted_content: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """書籍を新規登録（ファイルアップロード or テキスト貼り付け）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings", status_code=302)
    content = (pasted_content or "").strip()
    if file is not None and getattr(file, "filename", ""):
        # 全読み込み前に上限チェック（チャンク読みで上限を超えたら即中断＝メモリ保護）
        raw = b""
        too_large = False
        while True:
            chunk = await file.read(1024 * 1024)  # 1MBずつ
            if not chunk:
                break
            raw += chunk
            if len(raw) > BOOK_MAX_BYTES:
                too_large = True
                break
        await file.close()
        if too_large:
            return RedirectResponse("/admin/reference-books?err=size", status_code=302)
        extracted = _extract_text_from_upload(file.filename, raw)
        if extracted:
            content = (content + "\n" + extracted).strip() if content else extracted.strip()
    # 本文が空（抽出失敗・貼付なし）なら保存しない：選んでも無言で効かない書籍を防ぐ
    if not content.strip():
        return RedirectResponse("/admin/reference-books?err=empty", status_code=302)
    book = ReferenceBook(
        title=title.strip(),
        author=(author or "").strip(),
        publisher=(publisher or "").strip(),
        processed_content=content,
        license_status=license_status if license_status in ("none", "licensed") else "none",
        is_active=1,
        uploaded_by_user_id=user.id,
    )
    db.add(book); db.commit()
    return RedirectResponse("/admin/reference-books?ok=1", status_code=302)


@app.post("/admin/reference-books/{book_id}/delete")
def reference_books_delete(book_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings", status_code=302)
    book = db.query(ReferenceBook).filter(ReferenceBook.id == book_id).first()
    if book:
        db.delete(book); db.commit()
    return RedirectResponse("/admin/reference-books?ok=1", status_code=302)


@app.post("/admin/reference-books/{book_id}/toggle")
def reference_books_toggle(book_id: int, request: Request, db: Session = Depends(get_db)):
    """書籍ナレッジの有効/無効を切り替える（admin）。無効化したものは分析プロンプトに注入されない。"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings", status_code=302)
    book = db.query(ReferenceBook).filter(ReferenceBook.id == book_id).first()
    if book:
        book.is_active = 0 if book.is_active else 1
        db.commit()
    return RedirectResponse("/admin/reference-books?ok=1", status_code=302)


def _parse_json_list(s: str) -> str:
    """フォーム入力の改行/カンマ区切り → JSON配列文字列"""
    if not s:
        return "[]"
    s = s.strip()
    if s.startswith("["):
        # JSONそのまま
        try:
            json.loads(s)
            return s
        except Exception:
            pass
    # 改行/カンマ区切り
    items = [x.strip() for x in s.replace("\r", "").replace("、", ",").replace("\n", ",").split(",") if x.strip()]
    return json.dumps(items, ensure_ascii=False)


@app.post("/admin/referral-services/save")
def referral_services_save(
    request: Request, db: Session = Depends(get_db),
    svc_id: str = Form(""),
    name: str = Form(...),
    provider: str = Form(""),
    category: str = Form(""),
    target_issue_tags: str = Form(""),
    target_industries: str = Form(""),
    target_revenue_min: str = Form(""),
    target_revenue_max: str = Form(""),
    description_short: str = Form(""),
    description_long: str = Form(""),
    service_features: str = Form(""),
    pricing: str = Form(""),
    url: str = Form(""),
    referral_url_template: str = Form(""),
    commission_type: str = Form(""),
    commission_value: str = Form("0"),
    commission_note: str = Form(""),
    logo_url: str = Form(""),
    notes: str = Form(""),
    is_active: str = Form("on"),
    sort_order: str = Form("100"),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # target_size JSON
    ts = {}
    try:
        if target_revenue_min:
            ts["min_revenue"] = float(target_revenue_min)
        if target_revenue_max:
            ts["max_revenue"] = float(target_revenue_max)
    except ValueError:
        pass

    payload = {
        "name": name.strip(),
        "provider": provider.strip(),
        "category": category.strip(),
        "target_issue_tags": _parse_json_list(target_issue_tags),
        "target_industries": _parse_json_list(target_industries) or '["全業種"]',
        "target_size": json.dumps(ts, ensure_ascii=False),
        "description_short": description_short.strip(),
        "description_long": description_long.strip(),
        "service_features": _parse_json_list(service_features),
        "pricing": pricing.strip(),
        "url": url.strip(),
        "referral_url_template": referral_url_template.strip(),
        "commission_type": commission_type.strip(),
        "commission_value": float(commission_value or 0) if (commission_value or "").strip() else 0,
        "commission_note": commission_note.strip(),
        "logo_url": logo_url.strip(),
        "notes": notes.strip(),
        "is_active": 1 if is_active in ("on", "1", "true") else 0,
        "sort_order": int(sort_order or 100),
    }

    if svc_id and svc_id.isdigit():
        svc = db.query(ReferralService).filter(ReferralService.id == int(svc_id)).first()
        if svc:
            for k, v in payload.items():
                setattr(svc, k, v)
            db.commit()
            return RedirectResponse(f"/admin/referral-services?saved={svc.id}", status_code=302)
    # 新規
    payload["created_by_user_id"] = user.id
    svc = ReferralService(**payload)
    db.add(svc); db.commit(); db.refresh(svc)
    return RedirectResponse(f"/admin/referral-services?saved={svc.id}", status_code=302)


@app.post("/admin/referral-services/{svc_id}/delete")
def referral_services_delete(svc_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    svc = db.query(ReferralService).filter(ReferralService.id == svc_id).first()
    if svc:
        db.delete(svc); db.commit()
    return RedirectResponse("/admin/referral-services?deleted=1", status_code=302)


@app.post("/admin/referral-services/{svc_id}/toggle")
def referral_services_toggle(svc_id: int, request: Request, db: Session = Depends(get_db)):
    """有効/無効切替"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    svc = db.query(ReferralService).filter(ReferralService.id == svc_id).first()
    if svc:
        svc.is_active = 0 if svc.is_active else 1
        db.commit()
    return RedirectResponse("/admin/referral-services", status_code=302)


@app.post("/admin/referral-services/seed")
def referral_services_seed(request: Request, db: Session = Depends(get_db)):
    """シードデータを投入（既に名前が存在するものはスキップ）"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    seeds = [
        {
            "name": "支払い.com",
            "provider": "UPSIDER株式会社",
            "category": "資金繰り改善",
            "target_issue_tags": ["売掛買掛サイクル", "資金繰り", "支払期日延長", "運転資金", "キャッシュフロー改善"],
            "target_industries": ["全業種"],
            "target_size": {"min_revenue": 3000},
            "description_short": "請求書を最大60日後払いにできる支払いサービス。買掛サイトを延ばして資金繰りを改善。",
            "description_long": "UPSIDERの法人カード決済を経由して、取引先への振込を最大60日後払いにできる。買掛金の支払期日を延ばすことで、キャッシュフローを改善。個人保証なし。",
            "service_features": ["最大60日後払い", "手数料4%/月", "個人保証なし", "Visa法人カード経由", "審査スピード重視"],
            "pricing": "手数料 4%（取引額）",
            "url": "https://shi-harai.com",
            "commission_type": "percentage", "commission_value": 10.0,
            "commission_note": "成約後手数料の10%（要確認）",
            "sort_order": 10,
        },
        {
            "name": "M&Aクラウド",
            "provider": "株式会社M&Aクラウド",
            "category": "M&A仲介",
            "target_issue_tags": ["事業承継", "M&A", "売却検討", "事業拡大"],
            "target_industries": ["全業種"],
            "target_size": {"min_revenue": 10000},
            "description_short": "中小企業向けM&Aプラットフォーム。買い手・売り手のマッチングから成約まで支援。",
            "service_features": ["1万社超の買い手", "成功報酬制", "事業承継対応", "DD支援"],
            "pricing": "成功報酬 取引額の5%程度",
            "url": "https://macloud.jp",
            "commission_type": "percentage", "commission_value": 5.0,
            "commission_note": "成約時手数料の5%（要交渉）",
            "sort_order": 20,
        },
        {
            "name": "freee会計",
            "provider": "freee株式会社",
            "category": "会計SaaS",
            "target_issue_tags": ["業務効率化", "DX", "経理人手不足", "クラウド化"],
            "target_industries": ["全業種"],
            "target_size": {"max_revenue": 100000},
            "description_short": "中小企業向けクラウド会計ソフト。請求書発行・経費精算・申告まで一気通貫。",
            "service_features": ["月額2,680円〜", "銀行連携", "請求書自動取込", "電子帳簿保存法対応"],
            "pricing": "月額 2,680〜5,980円",
            "url": "https://www.freee.co.jp",
            "commission_type": "fixed", "commission_value": 30000,
            "commission_note": "成約時 30,000円（参考）",
            "sort_order": 30,
        },
        {
            "name": "ビズリーチ・サクシード",
            "provider": "株式会社ビズリーチ",
            "category": "M&A仲介",
            "target_issue_tags": ["事業承継", "M&A", "後継者不在"],
            "target_industries": ["全業種"],
            "description_short": "後継者不在の中小企業向け事業承継M&Aプラットフォーム。買い手探索を効率化。",
            "service_features": ["事業承継特化", "完全成功報酬", "オンライン交渉対応"],
            "pricing": "完全成功報酬",
            "url": "https://br-succeed.jp",
            "commission_type": "percentage", "commission_value": 3.0,
            "commission_note": "成約時 取引額の3%（要交渉）",
            "sort_order": 25,
        },
        {
            "name": "MFクラウド債権請求",
            "provider": "株式会社マネーフォワード",
            "category": "資金繰り改善",
            "target_issue_tags": ["売掛回収", "請求業務効率化", "資金繰り"],
            "target_industries": ["全業種"],
            "description_short": "請求書発行から入金消込まで自動化。売掛金の見える化と回収サイクル短縮。",
            "service_features": ["請求書自動発行", "入金消込自動", "売掛残高ダッシュボード"],
            "pricing": "月額 3,980円〜",
            "url": "https://biz.moneyforward.com/invoice",
            "commission_type": "fixed", "commission_value": 20000,
            "commission_note": "成約時 20,000円（参考）",
            "sort_order": 40,
        },
        {
            "name": "ビジとり",
            "provider": "（要確認）",
            "category": "旅費規程・節税",
            "target_issue_tags": [
                "旅費規程整備", "出張手当", "日当", "日帰り出張",
                "役員報酬最適化", "節税", "社会保険料削減", "手取りアップ"
            ],
            "target_industries": ["全業種"],
            "target_size": {"min_revenue": 5000},
            "description_short": "LINE×GPS連動の出張管理システム（特許出願中）。日帰り出張も含めた出張記録を自動生成。役員報酬480万円なら手取り最大99万円アップの実績。",
            "description_long": "多くの経営者が日帰り出張の経費化（日当）を見逃しているチャンスを最大化するシステム。LINEからワンタップ・GPS連動で「税務署が納得せざるを得ない」移動管理表を自動生成（特許出願中）。社労士による規定作成と改ざん不能エビデンスで税務調査リスクを排除。旅費日当は法人側「全額損金」、個人側「非課税」かつ社保算定対象外で、最も効率的な節税が可能。",
            "service_features": [
                "LINE×GPSで出張記録を自動生成（特許出願中）",
                "社労士による旅費規程作成",
                "改ざん不能エビデンスで税務調査対応",
                "日帰り出張も対象（商談/会議/営業訪問/現場/研修/交流会）",
                "法人全額損金×個人非課税×社保算定対象外の3重節税",
                "役員報酬480万円→年99万円手取りUP の実例"
            ],
            "pricing": "要問い合わせ",
            "url": "",
            "commission_type": "",
            "commission_value": 0,
            "commission_note": "紹介条件・料金は要確認。問い合わせ先を登録時に追記",
            "notes": "🎯 マッチ条件: 役員報酬250万円以上 / 出張機会のある経営者・営業活動の多い会社 / 旅費規程が未整備 or 形骸化している会社",
            "sort_order": 15,
        },
    ]
    added = 0
    for s in seeds:
        if db.query(ReferralService).filter(ReferralService.name == s["name"]).first():
            continue
        rs = ReferralService(
            name=s["name"], provider=s.get("provider", ""), category=s.get("category", ""),
            target_issue_tags=json.dumps(s.get("target_issue_tags", []), ensure_ascii=False),
            target_industries=json.dumps(s.get("target_industries", ["全業種"]), ensure_ascii=False),
            target_size=json.dumps(s.get("target_size", {}), ensure_ascii=False),
            description_short=s.get("description_short", ""),
            description_long=s.get("description_long", ""),
            service_features=json.dumps(s.get("service_features", []), ensure_ascii=False),
            pricing=s.get("pricing", ""), url=s.get("url", ""),
            referral_url_template=s.get("referral_url_template", ""),
            commission_type=s.get("commission_type", ""),
            commission_value=s.get("commission_value", 0),
            commission_note=s.get("commission_note", ""),
            sort_order=s.get("sort_order", 100),
            created_by_user_id=user.id,
        )
        db.add(rs); added += 1
    db.commit()
    return RedirectResponse(f"/admin/referral-services?seeded={added}", status_code=302)
