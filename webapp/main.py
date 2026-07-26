"""FastAPI application: login, the upload page, job pages, and wiring.

Run locally:
    .venv/bin/python -m uvicorn webapp.main:app --reload --port 8000
"""
import asyncio
import contextlib
import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, routes_jobs, uploads, x_login
from .jobs import cleanup, queue, store

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    store.init()
    for warning in config.startup_warnings():
        print(f"[config] WARNING: {warning}", flush=True)
    if config.EXECUTION_MODE != "inline":
        queue.start()
    cleanup.start_scheduler()
    # Free hosts wipe the disk on restart, so sign in to X now (in the
    # background) rather than making the first report wait for it.
    x_login.warm_up_async()
    print(f"[app] ready — mode={config.EXECUTION_MODE} "
          f"jobs={config.MAX_CONCURRENT_JOBS} workers={config.CAPTURE_WORKERS} "
          f"x-auto-login={'on' if x_login.credentials_configured() else 'off'}",
          flush=True)
    yield
    queue.shutdown()


app = FastAPI(title="Report Automation", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.effective_session_secret(),
    session_cookie="ra_session",
    max_age=config.SESSION_HOURS * 3600,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(routes_jobs.router)


# --------------------------------------------------------------------------- #
# Redirect signed-out page requests to /login instead of showing a JSON 403
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=exc.headers)
    if exc.status_code == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return templates.TemplateResponse(
        request, "error.html",
        {"code": exc.status_code, "detail": exc.detail,
         "user": auth.current_user(request)},
        status_code=exc.status_code)


# --------------------------------------------------------------------------- #
# Auth pages
# --------------------------------------------------------------------------- #
def _safe_next(raw: str) -> str:
    """Only allow same-site relative redirects (no open redirect)."""
    raw = (raw or "/").strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if auth.current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"csrf": auth.csrf_token(request), "next": _safe_next(next), "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request,
                       username: str = Form(""),
                       password: str = Form(""),
                       csrf_token: str = Form(""),
                       next: str = Form("/")):
    ip = auth.client_ip(request)
    target = _safe_next(next)

    def fail(message, status=400):
        return templates.TemplateResponse(
            request, "login.html",
            {"csrf": auth.csrf_token(request), "next": target, "error": message},
            status_code=status)

    try:
        auth.verify_csrf(request, csrf_token)
    except HTTPException as e:
        return fail(e.detail)

    if auth.login_blocked(ip):
        return fail(f"Too many failed attempts. Try again in "
                    f"{config.LOGIN_WINDOW_MINUTES} minutes.", 429)

    user = auth.verify_credentials(username, password)
    if not user:
        auth.note_login_failure(ip)
        left = max(0, config.LOGIN_MAX_ATTEMPTS - store.recent_login_failures(ip))
        extra = f" {left} attempt(s) left." if left <= 2 else ""
        return fail("Incorrect username or password." + extra, 401)

    auth.note_login_success(ip)
    auth.login_session(request, user)
    return RedirectResponse(target, status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    with contextlib.suppress(HTTPException):
        auth.verify_csrf(request, csrf_token)
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# App pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(auth.require_user)):
    jobs = [routes_jobs.public_job(j) for j in store.list_for(user, limit=12)]
    return templates.TemplateResponse(
        request, "index.html",
        {"user": user, "csrf": auth.csrf_token(request), "jobs": jobs,
         "max_links": config.MAX_LINKS, "max_mb": config.MAX_UPLOAD_MB,
         "accept": ",".join(uploads.ALLOWED_SUFFIXES),
         "x_login_ok": config.X_STATE_FILE.exists()})


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(job_id: str, request: Request,
                   user: str = Depends(auth.require_user)):
    job = store.get(job_id)
    if not job or job["owner"] != user:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        request, "job.html",
        {"user": user, "csrf": auth.csrf_token(request),
         "job": routes_jobs.public_job(job)})


def _session_context(request: Request, user: str, flash=None) -> dict:
    info = x_login.session_state()
    modified = info.get("modified")
    info["modified_text"] = (
        datetime.datetime.fromtimestamp(modified).strftime("%d %b %Y, %H:%M")
        if modified else None)
    last = info.get("last_attempt") or {}
    info["last_attempt_text"] = (
        datetime.datetime.fromtimestamp(last["at"]).strftime("%d %b %Y, %H:%M")
        if last.get("at") else None)
    return {"user": user, "csrf": auth.csrf_token(request), "info": info,
            "sessions_dir": str(config.SESSIONS_DIR), "flash": flash}


@app.get("/admin/session-status", response_class=HTMLResponse)
async def session_status(request: Request, user: str = Depends(auth.require_user)):
    """Is a usable X login available, and can the server renew it itself?"""
    return templates.TemplateResponse(
        request, "session_status.html", _session_context(request, user))


@app.post("/admin/x-login", response_class=HTMLResponse)
async def admin_x_login(request: Request, csrf_token: str = Form(""),
                        user: str = Depends(auth.require_user)):
    """Sign in to X now. Credentials come from the environment, never the form."""
    auth.verify_csrf(request, csrf_token)
    ok, message = await asyncio.to_thread(x_login.force_login)
    return templates.TemplateResponse(
        request, "session_status.html",
        _session_context(request, user,
                         flash={"ok": ok, "message": message}))


# Two names for the same check: /healthz is the conventional one, /health is
# what compose's healthcheck and any external uptime monitor use. Neither
# requires a login and neither does any work.
@app.get("/healthz")
@app.get("/health")
async def healthz():
    return {"ok": True, "mode": config.EXECUTION_MODE}
