from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.auth import (
    COOKIE_NAME, SESSION_TTL_DAYS,
    authenticate, start_session, end_session, get_current_user,
    verify_password, hash_password,
)
from db.state_store import update_user_password

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = authenticate(email, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password."}, status_code=401
        )
    token = start_session(user["id"])
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME, token,
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * SESSION_TTL_DAYS,
    )
    return resp


@router.get("/logout")
async def logout(request: Request):
    end_session(request.cookies.get(COOKIE_NAME))
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(request, "profile.html", {
        "page": "profile",
        "user_email": user["email"] if user else "",
        "error": None,
        "success": None,
    })


@router.post("/profile/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = get_current_user(request)
    ctx = {"page": "profile", "user_email": user["email"] if user else "", "error": None, "success": None}

    if not user or not verify_password(current_password, user["password_hash"]):
        ctx["error"] = "Current password is incorrect."
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)
    if len(new_password) < 8:
        ctx["error"] = "New password must be at least 8 characters."
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)
    if new_password != confirm_password:
        ctx["error"] = "New password and confirmation do not match."
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)

    update_user_password(user["email"], hash_password(new_password))
    ctx["success"] = "Password updated successfully."
    return templates.TemplateResponse(request, "profile.html", ctx)
