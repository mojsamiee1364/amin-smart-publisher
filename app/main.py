from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import jdatetime
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

BASE = Path(__file__).resolve().parent.parent
STORAGE = BASE / "storage"
UPLOADS = STORAGE / "uploads"
DB_PATH = STORAGE / "publisher.sqlite3"
UPLOADS.mkdir(parents=True, exist_ok=True)
TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Tehran"))
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024

app = FastAPI(title="Amin Smart Publisher", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "development-only-change-me"),
    same_site="lax",
    https_only=os.getenv("COOKIE_HTTPS_ONLY", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=BASE / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "app" / "templates")
scheduler = BackgroundScheduler(timezone=TZ)


def now_local() -> datetime:
    return datetime.now(TZ)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            image_path TEXT,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            recurrence TEXT NOT NULL DEFAULT 'none',
            series_id TEXT,
            approved_at TEXT,
            published_at TEXT,
            rubika_message_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_posts_due ON posts(status, scheduled_at)")


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, token: str) -> None:
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "درخواست معتبر نیست؛ صفحه را تازه‌سازی کنید.")


def require_login(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    return None


def render(request: Request, template: str, **context):
    context.update({"request": request, "csrf": csrf_token(request)})
    return templates.TemplateResponse(template, context)


def jalali_to_iso(date_text: str, time_text: str) -> str:
    try:
        y, m, d = [int(x) for x in date_text.replace("-", "/").split("/")]
        hh, mm = [int(x) for x in time_text.split(":")]
        g = jdatetime.date(y, m, d).togregorian()
        dt = datetime(g.year, g.month, g.day, hh, mm, tzinfo=TZ)
        if dt <= now_local():
            raise ValueError("زمان انتشار باید در آینده باشد.")
        return dt.isoformat()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("تاریخ یا ساعت شمسی معتبر نیست.") from exc


def iso_to_jalali(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value).astimezone(TZ)
    jd = jdatetime.date.fromgregorian(date=dt.date())
    return f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d} {dt:%H:%M}"


def rubika_call(method: str, payload: dict):
    token = os.getenv("RUBIKA_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("توکن ربات روبیکا در تنظیمات سرور ثبت نشده است.")
    url = f"https://botapi.rubika.ir/v3/{token}/{method}"
    response = requests.post(url, json=payload, timeout=35)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "OK":
        raise RuntimeError(data.get("status_det") or str(data))
    return data.get("data") or {}


def upload_to_rubika(path: Path) -> str:
    # Official Bot API flow: requestSendFile -> upload_url -> multipart upload -> file_id.
    requested = rubika_call("requestSendFile", {"type": "Image"})
    upload_url = requested.get("upload_url")
    if not upload_url:
        raise RuntimeError("روبیکا آدرس آپلود تصویر را برنگرداند.")
    with path.open("rb") as fh:
        resp = requests.post(upload_url, files={"file": (path.name, fh, "image/" + path.suffix.lower().lstrip("."))}, timeout=90)
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != "OK":
        raise RuntimeError(result.get("status_det") or "آپلود تصویر ناموفق بود.")
    file_id = (result.get("data") or {}).get("file_id")
    if not file_id:
        raise RuntimeError("شناسه فایل از پاسخ روبیکا دریافت نشد.")
    return file_id


def send_post(post_id: int) -> None:
    with db() as con:
        row = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row["status"] != "approved":
            return
        channel_id = os.getenv("RUBIKA_CHANNEL_ID", "").strip()
        if not channel_id:
            con.execute("UPDATE posts SET status='failed', last_error=? WHERE id=?", ("شناسه کانال در تنظیمات سرور ثبت نشده است.", post_id))
            return
        # Mark as sending before network I/O to avoid duplicate sends on concurrent scheduler ticks.
        con.execute("UPDATE posts SET status='sending', last_error=NULL WHERE id=? AND status='approved'", (post_id,))
        post = dict(row)
    try:
        if post["image_path"]:
            file_id = upload_to_rubika(Path(post["image_path"]))
            result = rubika_call("sendFile", {"chat_id": channel_id, "file_id": file_id, "text": post["body"] or ""})
        else:
            result = rubika_call("sendMessage", {"chat_id": channel_id, "text": post["body"]})
        message_id = str(result.get("message_id", ""))
        with db() as con:
            con.execute("UPDATE posts SET status='published', published_at=?, rubika_message_id=?, last_error=NULL WHERE id=? AND status='sending'",
                        (now_local().isoformat(), message_id, post_id))
            if post["recurrence"] in ("daily", "weekly"):
                next_dt = datetime.fromisoformat(post["scheduled_at"]).astimezone(TZ) + timedelta(days=1 if post["recurrence"] == "daily" else 7)
                con.execute("INSERT INTO posts(title,body,image_path,scheduled_at,status,recurrence,series_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (post["title"], post["body"], post["image_path"], next_dt.isoformat(), "draft", post["recurrence"], post["series_id"] or str(uuid.uuid4()), now_local().isoformat()))
    except Exception as exc:
        with db() as con:
            con.execute("UPDATE posts SET status='failed', last_error=? WHERE id=? AND status='sending'", (str(exc)[:1000], post_id))


def scheduler_tick():
    # Only manually approved posts are eligible. Unapproved overdue posts remain drafts.
    now = now_local().isoformat()
    with db() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM posts WHERE status='approved' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 10", (now,)).fetchall()]
    for post_id in ids:
        send_post(post_id)


@app.on_event("startup")
def startup():
    init_db()
    # A crash during network send leaves an ambiguous result. Never auto-retry it,
    # because that could publish duplicates; require a human to inspect and retry.
    with db() as con:
        con.execute("UPDATE posts SET status='failed', last_error=? WHERE status='sending'",
                    ("سرور هنگام ارسال متوقف شده بود؛ نتیجه ارسال نامشخص است. ابتدا کانال را بررسی کنید و سپس در صورت نیاز دوباره تأیید کنید.",))
    scheduler.add_job(scheduler_tick, "interval", seconds=20, id="publisher_tick", max_instances=1, coalesce=True)
    scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, password: str = Form(...), csrf: str = Form(...)):
    check_csrf(request, csrf)
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected or not secrets.compare_digest(password, expected):
        return render(request, "login.html", error="رمز عبور نادرست است.")
    request.session.clear()
    request.session["logged_in"] = True
    request.session["csrf"] = secrets.token_urlsafe(24)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redir = require_login(request)
    if redir: return redir
    with db() as con:
        rows = con.execute("SELECT * FROM posts ORDER BY scheduled_at DESC LIMIT 100").fetchall()
        counts = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall()}
    posts = []
    for r in rows:
        p = dict(r)
        p["jalali_time"] = iso_to_jalali(p["scheduled_at"])
        p["overdue"] = p["status"] == "draft" and datetime.fromisoformat(p["scheduled_at"]) <= now_local()
        posts.append(p)
    return render(request, "dashboard.html", posts=posts, counts=counts)


@app.get("/new", response_class=HTMLResponse)
def new_post_page(request: Request):
    redir = require_login(request)
    if redir: return redir
    return render(request, "new.html", error=None)


@app.post("/new")
async def create_post(request: Request, title: str = Form(...), body: str = Form(""), jalali_date: str = Form(...), publish_time: str = Form(...), recurrence: str = Form("none"), image: UploadFile | None = File(None), csrf: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    check_csrf(request, csrf)
    if recurrence not in {"none", "daily", "weekly"}:
        raise HTTPException(400, "الگوی تکرار نامعتبر است.")
    if not body.strip() and not image:
        return render(request, "new.html", error="متن یا تصویر باید وارد شود.")
    try:
        scheduled = jalali_to_iso(jalali_date, publish_time)
    except ValueError as exc:
        return render(request, "new.html", error=str(exc))
    image_path = None
    if image and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            return render(request, "new.html", error="فقط تصویر JPG، PNG یا WEBP مجاز است.")
        raw = await image.read()
        if len(raw) > MAX_UPLOAD:
            return render(request, "new.html", error=f"حجم تصویر نباید بیشتر از {MAX_UPLOAD // (1024*1024)} مگابایت باشد.")
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(raw))
            im.verify()
        except Exception:
            return render(request, "new.html", error="فایل تصویر معتبر نیست.")
        target = UPLOADS / f"{uuid.uuid4().hex}{suffix}"
        target.write_bytes(raw)
        image_path = str(target)
    with db() as con:
        con.execute("INSERT INTO posts(title,body,image_path,scheduled_at,status,recurrence,series_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (title.strip()[:160], body.strip(), image_path, scheduled, "draft", recurrence, str(uuid.uuid4()) if recurrence != "none" else None, now_local().isoformat()))
    return RedirectResponse("/", status_code=303)


@app.post("/posts/{post_id}/approve")
def approve_post(post_id: int, request: Request, csrf: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    check_csrf(request, csrf)
    with db() as con:
        row = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row["status"] not in {"draft", "failed"}:
            raise HTTPException(409, "این پیام در وضعیت قابل تأیید نیست.")
        con.execute("UPDATE posts SET status='approved', approved_at=?, last_error=NULL WHERE id=?", (now_local().isoformat(), post_id))
    return RedirectResponse("/", status_code=303)


@app.post("/posts/{post_id}/reject")
def reject_post(post_id: int, request: Request, csrf: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    check_csrf(request, csrf)
    with db() as con:
        con.execute("UPDATE posts SET status='rejected' WHERE id=? AND status IN ('draft','failed')", (post_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/posts/{post_id}/delete")
def delete_post(post_id: int, request: Request, csrf: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    check_csrf(request, csrf)
    with db() as con:
        row = con.execute("SELECT image_path,status FROM posts WHERE id=?", (post_id,)).fetchone()
        if row and row["status"] not in {"published", "sending"}:
            con.execute("DELETE FROM posts WHERE id=?", (post_id,))
            if row["image_path"]:
                try: Path(row["image_path"]).unlink(missing_ok=True)
                except OSError: pass
    return RedirectResponse("/", status_code=303)


@app.post("/test-connection")
def test_connection(request: Request, csrf: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    check_csrf(request, csrf)
    try:
        info = rubika_call("getMe", {})
        return render(request, "connection.html", ok=True, detail=str(info)[:700])
    except Exception as exc:
        return render(request, "connection.html", ok=False, detail=str(exc)[:700])


@app.get("/connection", response_class=HTMLResponse)
def connection_page(request: Request):
    redir = require_login(request)
    if redir: return redir
    return render(request, "connection.html", ok=None, detail=None)


@app.get("/health")
def health():
    return {"status": "ok", "service": "amin-smart-publisher"}
