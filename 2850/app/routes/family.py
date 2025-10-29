"""
Family-related API routes
"""
import json
import logging
import re
from typing import Optional
from pydantic import BaseModel
from datetime import date, datetime
from fastapi import APIRouter, HTTPException
from ..models import (
    CreateFamilyRequest, InviteMemberRequest, DeleteFamilyRequest,
    RemoveMemberRequest, FamilyInfo, CreateMealCodeRequest, MealCodeInfo,
    FamilyMealTimes
)
from ..database import db_query, db_execute
from ..utils import (
    validate_family_id, generate_family_id, generate_meal_code, parse_meal_code,
    log_api_call, log_error
)

logger = logging.getLogger("meal")
router = APIRouter(tags=["family"])

# ---------- helpers for meal-times ----------
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
DEFAULT_MEAL_TIMES = {"breakfast": "08:00", "lunch": "11:00", "dinner": "17:30"}

def _norm_hhmm(s: str, fallback: str) -> str:
    s = (s or "").strip()
    if _TIME_RE.match(s):
        return s
    # chấp nhận "HH:MM:SS"
    if re.match(r"^\d{2}:\d{2}:\d{2}$", s):
        return s[:5]
    return fallback

def _row_to_times(row: dict) -> dict:
    """
    Convert one row from family_meal_settings to {breakfast,lunch,dinner} 'HH:MM'
    (cột TIME -> 'HH:MM:SS' nên cắt 5 ký tự đầu)
    """
    if not row:
        return dict(DEFAULT_MEAL_TIMES)

    def _cell(v, fb):
        if v is None:
            return fb
        s = str(v)
        if re.match(r"^\d{2}:\d{2}:\d{2}$", s):
            return s[:5]
        return _norm_hhmm(s, fb)

    return {
        "breakfast": _cell(row.get("breakfast_start"), DEFAULT_MEAL_TIMES["breakfast"]),
        "lunch":     _cell(row.get("lunch_start"),     DEFAULT_MEAL_TIMES["lunch"]),
        "dinner":    _cell(row.get("dinner_start"),    DEFAULT_MEAL_TIMES["dinner"]),
    }

# ===================== Membership & family CRUD (giữ nguyên) =====================

# Global storage for meal code types (in production, use database)
meal_code_types = {}

class JoinFamilyRequest(BaseModel):
    family_id: str
    user_id: str
    role: Optional[str] = "member"
    display_name: Optional[str] = None

@router.post("/join")
def join_family(request: JoinFamilyRequest):
    try:
        log_api_call("/family/join", "POST", request.user_id)
        if not validate_family_id(request.family_id):
            raise HTTPException(400, "Invalid family ID format")

        fam = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s",
                       (request.family_id,))
        if not fam:
            raise HTTPException(404, "Family not found")

        user = db_query("SELECT user_id, user_name FROM users WHERE user_id=%s",
                        (request.user_id,))
        if not user:
            raise HTTPException(404, "User not found")

        existing = db_query("""
            SELECT 1 FROM family_memberships
            WHERE family_id=%s AND user_id=%s
            LIMIT 1
        """, (request.family_id, request.user_id))
        if existing:
            raise HTTPException(400, "You are already a member of this family")

        display_name = request.display_name or user[0]["user_name"] or request.user_id
        role = request.role or "member"

        db_execute("""
            INSERT INTO family_memberships (family_id, user_id, role, display_name)
            VALUES (%s, %s, %s, %s)
        """, (request.family_id, request.user_id, role, display_name))

        return {
            "ok": True,
            "message": f"Joined family '{fam[0]['family_name']}' successfully",
            "family_id": request.family_id,
            "family_name": fam[0]["family_name"],
            "role": role,
            "display_name": display_name
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"join_family for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/create")
def create_family(request: CreateFamilyRequest):
    try:
        log_api_call("/family/create", "POST", request.family_id)
        family_id = request.family_id or generate_family_id()

        if not validate_family_id(family_id):
            raise HTTPException(400, "Invalid family ID format")

        existing_family = db_query("SELECT family_id FROM families WHERE family_id=%s",
                                   (family_id,))
        if existing_family:
            raise HTTPException(400, "Family ID already exists")

        user_families = db_query("""
            SELECT f.family_name FROM families f
            JOIN family_memberships fm ON f.family_id = fm.family_id
            WHERE fm.user_id = %s AND f.family_name = %s
        """, (request.user_id, request.family_name))
        if user_families:
            raise HTTPException(400, "You already have a family with this name")

        user_exists = db_query("SELECT user_id FROM users WHERE user_id=%s",
                               (request.user_id,))
        if not user_exists:
            raise HTTPException(400, f"User {request.user_id} does not exist")

        db_execute("""
            INSERT INTO families (family_id, family_name, user_id)
            VALUES (%s, %s, %s)
        """, (family_id, request.family_name, request.user_id))

        db_execute("""
            INSERT INTO family_memberships (family_id, user_id, role, display_name)
            VALUES (%s, %s, %s, %s)
        """, (family_id, request.user_id, "holder", request.family_name))

        # seed mặc định cho bảng settings (nếu chưa có)
        db_execute("""
            INSERT IGNORE INTO family_meal_settings (family_id)
            VALUES (%s)
        """, (family_id,))

        return {
            "ok": True,
            "family_id": family_id,
            "family_name": request.family_name,
            "message": "Family created successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"create_family for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/invite")
def invite_member(request: InviteMemberRequest):
    try:
        log_api_call("/family/invite", "POST", request.invited_user_id)
        user = db_query("SELECT user_id, user_name FROM users WHERE user_id=%s",
                        (request.invited_user_id,))
        if not user:
            raise HTTPException(404, "User not found")

        family = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s",
                          (request.family_id,))
        if not family:
            raise HTTPException(404, "Family not found")

        existing_member = db_query("""
            SELECT user_id FROM family_memberships
            WHERE family_id=%s AND user_id=%s
        """, (request.family_id, request.invited_user_id))
        if existing_member:
            raise HTTPException(400, "User is already a member of this family")

        db_execute("""
            INSERT INTO family_memberships (family_id, user_id, role, display_name)
            VALUES (%s, %s, %s, %s)
        """, (request.family_id, request.invited_user_id, "member", user[0]["user_name"]))

        try:
            from datetime import datetime
            title = f"Family Invitation - {family[0]['family_name']}"
            content = (f"You have been invited to join the family "
                       f"'{family[0]['family_name']}' (ID: {request.family_id}). Welcome!")
            action_url = "manage_family.html"
            db_execute("""
                INSERT INTO messages (user_id, type, title, content, action_url, read_status, created_at)
                VALUES (%s, %s, %s, %s, %s, false, %s)
            """, (request.invited_user_id, "family_invitation", title, content, action_url, datetime.now()))
            logger.info("Invitation message sent to user %s for family %s",
                        request.invited_user_id, request.family_id)
        except Exception as e:
            logger.warning("Failed to send invitation message: %s", e)

        return {"ok": True, "message": f"User {user[0]['user_name']} has been invited to the family"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"invite_member for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.delete("/delete")
def delete_family(request: DeleteFamilyRequest):
    try:
        log_api_call("/family/delete", "DELETE", request.family_id)
        family = db_query("SELECT family_id FROM families WHERE family_id=%s",
                          (request.family_id,))
        if not family:
            raise HTTPException(404, "Family not found")

        db_execute("DELETE FROM family_memberships WHERE family_id=%s", (request.family_id,))
        db_execute("DELETE FROM families WHERE family_id=%s", (request.family_id,))
        return {"ok": True, "message": "Family deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"delete_family for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.delete("/remove-member")
def remove_member(request: RemoveMemberRequest):
    try:
        log_api_call("/family/remove-member", "DELETE", request.user_id)
        membership = db_query("""
            SELECT user_id FROM family_memberships
            WHERE family_id=%s AND user_id=%s
        """, (request.family_id, request.user_id))
        if not membership:
            raise HTTPException(404, "User is not a member of this family")

        db_execute("""
            DELETE FROM family_memberships
            WHERE family_id=%s AND user_id=%s
        """, (request.family_id, request.user_id))
        return {"ok": True, "message": "Member removed successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"remove_member for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/{family_id}/members")
def get_family_members(family_id: str):
    try:
        log_api_call(f"/family/{family_id}/members", "GET")
        members = db_query("""
            SELECT fm.user_id, fm.role, fm.display_name, u.user_name
            FROM family_memberships fm
            JOIN users u ON fm.user_id = u.user_id
            WHERE fm.family_id = %s
            ORDER BY fm.role, fm.display_name
        """, (family_id,))
        return members
    except Exception as e:
        log_error(e, f"get_family_members for family {family_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/user/{user_id}/new-members-count")
def get_new_members_count(user_id: str):
    try:
        log_api_call(f"/family/user/{user_id}/new-members-count", "GET")
        families = db_query("""
            SELECT family_id FROM family_memberships
            WHERE user_id = %s
        """, (user_id,))
        if not families:
            return {"new_members_count": 0}

        family_ids = [f["family_id"] for f in families]
        placeholders = ",".join(["%s"] * len(family_ids))
        query = f"""
            SELECT COUNT(*) as count
            FROM family_memberships
            WHERE family_id IN ({placeholders})
              AND user_id != %s
              AND created_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
        """
        params = family_ids + [user_id]
        result = db_query(query, params)
        count = result[0]["count"] if result else 0
        return {"new_members_count": count}
    except Exception as e:
        log_error(e, f"get_new_members_count for user {user_id}")
        raise HTTPException(500, "Internal server error")

# ===================== Meal-time settings =====================

@router.get("/{family_id}/meal-times")
def get_family_meal_times(family_id: str):
    """Return {breakfast,lunch,dinner} 'HH:MM' for a family."""
    try:
        log_api_call(f"/family/{family_id}/meal-times", "GET")
        fam = db_query("SELECT family_id FROM families WHERE family_id=%s", (family_id,))
        if not fam:
            raise HTTPException(404, "Family not found")

        rows = db_query("""
            SELECT breakfast_start, lunch_start, dinner_start
            FROM family_meal_settings
            WHERE family_id=%s
            LIMIT 1
        """, (family_id,))
        times = _row_to_times(rows[0] if rows else None)

        # đảm bảo có record để lần sau update không lỗi
        db_execute("INSERT IGNORE INTO family_meal_settings (family_id) VALUES (%s)", (family_id,))
        return times
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_family_meal_times for family {family_id}")
        raise HTTPException(500, "Internal server error")

@router.put("/{family_id}/meal-times")
def update_family_meal_times(family_id: str, payload: FamilyMealTimes):
    """Upsert vào family_meal_settings (TIME, không timezone)."""
    try:
        log_api_call(f"/family/{family_id}/meal-times", "PUT")
        fam = db_query("SELECT family_id FROM families WHERE family_id=%s", (family_id,))
        if not fam:
            raise HTTPException(404, "Family not found")

        bf = _norm_hhmm(payload.breakfast, DEFAULT_MEAL_TIMES["breakfast"])
        lu = _norm_hhmm(payload.lunch,     DEFAULT_MEAL_TIMES["lunch"])
        di = _norm_hhmm(payload.dinner,    DEFAULT_MEAL_TIMES["dinner"])

        db_execute("""
            INSERT INTO family_meal_settings (family_id, breakfast_start, lunch_start, dinner_start)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              breakfast_start=VALUES(breakfast_start),
              lunch_start=VALUES(lunch_start),
              dinner_start=VALUES(dinner_start)
        """, (family_id, bf, lu, di))

        return {"ok": True, "meal_times": {"breakfast": bf, "lunch": lu, "dinner": di}}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"update_family_meal_times for family {family_id}")
        raise HTTPException(500, "Internal server error")

# ---- Compat aliases for old frontends ----

@router.get("/{family_id}/settings")
def get_family_settings_compat(family_id: str):
    """Return { meal_times: { ... } } for older clients."""
    return {"meal_times": get_family_meal_times(family_id)}

@router.get("/{family_id}/settings/meal-times")
def get_family_meal_times_compat(family_id: str):
    return get_family_meal_times(family_id)

@router.post("/{family_id}/settings/meal-times")
def update_family_meal_times_compat(family_id: str, body: dict):
    """
    Body có thể là:
      { "breakfast": "HH:MM", "lunch": "HH:MM", "dinner": "HH:MM" }
    hoặc { "meal_times": { ... } }
    """
    mt = body.get("meal_times") if isinstance(body, dict) and "meal_times" in body else (body or {})
    try:
        payload = FamilyMealTimes(
            breakfast=str(mt.get("breakfast", "")),
            lunch=str(mt.get("lunch", "")),
            dinner=str(mt.get("dinner", ""))
        )
    except Exception:
        raise HTTPException(400, "Invalid body format")
    return update_family_meal_times(family_id, payload)

# -------- Active meal session (holder sets, others follow) --------

def _is_holder(family_id: str, user_id: str) -> bool:
    row = db_query("""
        SELECT 1
        FROM family_memberships
        WHERE family_id=%s AND user_id=%s AND role='holder'
        LIMIT 1
    """, (family_id, user_id))
    return bool(row)

@router.get("/{family_id}/active-meal")
def get_active_meal(family_id: str):
    try:
        log_api_call(f"/family/{family_id}/active-meal", "GET")
        fam = db_query("""
            SELECT active_meal_date, active_meal_type, active_updated_by, active_updated_at
            FROM families
            WHERE family_id=%s
            LIMIT 1
        """, (family_id,))
        if not fam:
            raise HTTPException(404, "Family not found")

        r = fam[0]
        d = r.get("active_meal_date")
        # ép về chuỗi YYYY-MM-DD (hoặc None)
        if isinstance(d, (date, datetime)):
            d = d.strftime("%Y-%m-%d")

        return {
            "family_id": family_id,
            "meal_date": d or None,
            "meal_type": (r.get("active_meal_type") or None),
            "updated_by": r.get("active_updated_by"),
            "updated_at": r.get("active_updated_at"),
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_active_meal for family {family_id}")
        raise HTTPException(500, "Internal server error")

class SetActiveMealBody(BaseModel):
    user_id: str
    meal_date: str  # 'YYYY-MM-DD'
    meal_type: str  # 'breakfast'|'lunch'|'dinner'

@router.post("/{family_id}/active-meal")
def set_active_meal(family_id: str, body: SetActiveMealBody):
    """
    Chỉ holder mới được đặt phiên ăn.
    Ghi thẳng vào families.active_meal_date / active_meal_type
    """
    try:
        log_api_call(f"/family/{family_id}/active-meal", "POST", body.user_id)

        # check family
        fam = db_query("SELECT family_id FROM families WHERE family_id=%s", (family_id,))
        if not fam:
            raise HTTPException(404, "Family not found")

        # check holder
        if not _is_holder(family_id, body.user_id):
            raise HTTPException(403, "Only family holder can set active meal")

        meal_type = (body.meal_type or "").strip().lower()
        if meal_type not in ("breakfast", "lunch", "dinner"):
            raise HTTPException(400, "Invalid meal_type")

        # Lưu
        db_execute("""
            UPDATE families
            SET active_meal_date=%s,
                active_meal_type=%s,
                active_updated_by=%s
            WHERE family_id=%s
        """, (body.meal_date, meal_type, body.user_id, family_id))

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"set_active_meal for family {family_id}")
        raise HTTPException(500, "Internal server error")


