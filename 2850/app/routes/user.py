# at top
from typing import Optional
from fastapi import APIRouter, HTTPException, Body, Form
from ..utils import validate_user_id, log_api_call, log_error
from ..models import UserRegister, UserLogin, UserUpdateRequest  # keep if you use them elsewhere
from ..database import db_query, db_execute

router = APIRouter(tags=["user"])

# ---------- Helper to normalize credentials ----------
def _first(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

@router.post("/login")
def login_user(
    # Accept JSON body (arbitrary dict) OR form fields
    payload: Optional[dict] = Body(None),
    userId: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    user_id_f: Optional[str] = Form(None),
    user_pass_f: Optional[str] = Form(None),
):
    """Login — accepts JSON or form; camelCase or snake_case."""
    try:
        # Pull from JSON if present
        if isinstance(payload, dict):
            uid_json  = payload.get("user_id") or payload.get("userId")
            pwd_json  = payload.get("user_pass") or payload.get("password") or payload.get("userPass")
        else:
            uid_json = pwd_json = None

        uid = _first(uid_json, userId, user_id_f)
        pwd = _first(pwd_json, password, user_pass_f)

        if not uid or not pwd:
            raise HTTPException(status_code=400, detail="Missing credentials")

        log_api_call("/user/login", "POST", uid)

        rows = db_query(
            "SELECT user_id, user_name FROM users WHERE user_id=%s AND user_pass=%s LIMIT 1",
            (uid, pwd),
        )
        if not rows:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        u = rows[0]
        return {"ok": True, "user_id": u["user_id"], "user_name": u["user_name"], "message": "Login successful"}

    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"login_user for user {payload!r}")
        # Let your global handler format the 500, but keep message consistent here too:
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/register")
def register_user(
    # Same trick: accept both JSON or form
    payload: Optional[dict] = Body(None),
    userId: Optional[str] = Form(None),
    userName: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    user_id_f: Optional[str] = Form(None),
    user_name_f: Optional[str] = Form(None),
    user_pass_f: Optional[str] = Form(None),
):
    """Register — accepts JSON or form; camelCase or snake_case."""
    try:
        uid_json  = payload.get("user_id") if isinstance(payload, dict) else None
        uid_json  = uid_json or (payload.get("userId") if isinstance(payload, dict) else None)
        uname_json = payload.get("user_name") if isinstance(payload, dict) else None
        uname_json = uname_json or (payload.get("userName") if isinstance(payload, dict) else None)
        pwd_json  = None
        if isinstance(payload, dict):
            pwd_json = payload.get("user_pass") or payload.get("password") or payload.get("userPass")

        uid   = _first(uid_json, userId, user_id_f)
        uname = _first(uname_json, userName, user_name_f)
        pwd   = _first(pwd_json, password, user_pass_f)

        if not uid or not uname or not pwd:
            raise HTTPException(status_code=400, detail="Missing fields")

        log_api_call("/user/register", "POST", uid)

        if not validate_user_id(uid):
            raise HTTPException(status_code=400, detail="Invalid user ID format")

        exists = db_query("SELECT 1 FROM users WHERE user_id=%s", (uid,))
        if exists:
            raise HTTPException(status_code=400, detail="User ID already exists")

        db_execute(
            "INSERT INTO users (user_id, user_name, user_pass) VALUES (%s, %s, %s)",
            (uid, uname, pwd),
        )
        return {"ok": True, "message": "User registered successfully"}

    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"register_user for payload {payload!r}")
        raise HTTPException(status_code=500, detail="Internal server error")
