# routes/preferences.py
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel
import json

from database import db_query, db_execute
from utils import log_api_call, log_error

router = APIRouter(prefix="/api/preferences", tags=["preferences"])

# -------------------------
# Helpers: normalize fields
# -------------------------

_TRUE_SET = {"true", "1", "y", "yes", "on"}

def _to_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in _TRUE_SET
    return bool(v)

def _norm_task_name(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    k = str(x).strip().lower().replace(" ", "_")
    mapping = {
        "prework": "pre_work",
        "before": "pre_work",
        "before_work": "pre_work",
        "cleanup": "after_work",
        "after": "after_work",
        "after_work": "after_work",
        "cook": "cooking",
    }
    return mapping.get(k, k)

def _normalize_pref(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Chuẩn hoá cấu trúc preference để lưu/đọc ổn định."""
    if not isinstance(obj, dict):
        return {}

    r = dict(obj)

    # alias keys -> snake_case quen thuộc
    if "isChef" in r and "is_chef" not in r:
        r["is_chef"] = r.pop("isChef")
    if "beforeTask" in r and "before_task" not in r:
        r["before_task"] = r.pop("beforeTask")
    if "afterTask" in r and "after_task" not in r:
        r["after_task"] = r.pop("afterTask")
    if "Tasks" in r and "tasks" not in r and isinstance(r["Tasks"], list):
        r["tasks"] = r.pop("Tasks")

    # bool
    if "is_chef" in r:
        r["is_chef"] = _to_bool(r["is_chef"])

    # tasks list
    tasks: List[str] = []
    if isinstance(r.get("tasks"), list):
        tasks = [_norm_task_name(t) for t in r["tasks"] if _norm_task_name(t)]
    # derive from flags
    if _to_bool(r.get("pre_work")):
        tasks.append("pre_work")
    if _to_bool(r.get("after_work")):
        tasks.append("after_work")
    tasks = list({t for t in tasks if t})
    if tasks:
        r["tasks"] = tasks

    # normalize before/after_task + đảm bảo tasks chứa các role tương ứng
    if r.get("before_task"):
        r["before_task"] = _norm_task_name(r["before_task"])
        if r["before_task"] and r["before_task"] != "none" and "pre_work" not in r.get("tasks", []):
            r["tasks"] = r.get("tasks", []) + ["pre_work"]
    if r.get("after_task"):
        r["after_task"] = _norm_task_name(r["after_task"])
        if r["after_task"] and r["after_task"] != "none" and "after_work" not in r.get("tasks", []):
            r["tasks"] = r.get("tasks", []) + ["after_work"]

    # remove flag aliases (đã quy về tasks)
    r.pop("pre_work", None)
    r.pop("after_work", None)
    return r


# -------------------------
# Models
# -------------------------

class SavePrefIn(BaseModel):
    family_id: str
    user_id: str
    preference: Dict[str, Any]
    # Tùy chọn – nếu muốn “đánh dấu ngày” cho bản lưu này
    effective_date: Optional[str] = None  # 'YYYY-MM-DD'
    meal_date: Optional[str] = None       # không bắt buộc, chỉ để tham chiếu
    meal_type: Optional[str] = None       # không bắt buộc
    display_name: Optional[str] = None    # ghi lại tên hiển thị nếu có

# -------------------------
# Routes
# -------------------------

@router.post("/save")
def save_family_pref(req: SavePrefIn):
    """
    Lưu (upsert) cooking preference lâu dài cho (family_id, user_id).
    Lưu JSON chuẩn hoá vào bảng `cooking_preferences`.

    Kỳ vọng schema (đã đề xuất ở phần SQL):
      cooking_preferences(
        id BIGINT PK AI,
        family_id VARCHAR(64),
        user_id   VARCHAR(64),
        display_name VARCHAR(128) NULL,
        pref_json JSON NOT NULL,
        effective_date DATE NULL,
        meal_date DATE NULL,
        meal_type VARCHAR(16) NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_family_user_date (family_id, user_id, effective_date)
      )
    """
    try:
        log_api_call("/preferences/save", "POST", req.user_id, family_id=req.family_id)

        if not (req.family_id and req.user_id):
            raise HTTPException(400, "Missing family_id or user_id")

        pref_norm = _normalize_pref(req.preference or {})
        pref_json = json.dumps(pref_norm, ensure_ascii=False)

        # Nếu không truyền display_name -> lấy từ membership
        display_name = req.display_name
        if not display_name:
            row = db_query(
                "SELECT display_name FROM family_memberships WHERE family_id=%s AND user_id=%s",
                (req.family_id, req.user_id),
            )
            if row:
                display_name = row[0].get("display_name")

        # Cố gắng upsert (nếu có UNIQUE( family_id, user_id, effective_date ))
        # Nếu DB chưa có UNIQUE, câu lệnh vẫn là "insert hoặc duplicate update"
        try:
            db_execute(
                """
                INSERT INTO cooking_preferences
                    (family_id, user_id, display_name, pref_json, effective_date, meal_date, meal_type)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    display_name = VALUES(display_name),
                    pref_json    = VALUES(pref_json),
                    meal_date    = VALUES(meal_date),
                    meal_type    = VALUES(meal_type),
                    updated_at   = CURRENT_TIMESTAMP
                """,
                (
                    req.family_id,
                    req.user_id,
                    display_name,
                    pref_json,
                    req.effective_date,
                    req.meal_date,
                    req.meal_type,
                ),
            )
        except Exception as ie:
            # Nếu bảng chưa tồn tại -> trả lỗi dễ hiểu
            msg = str(ie)
            if "1146" in msg or "doesn't exist" in msg or "does not exist" in msg:
                raise HTTPException(
                    500,
                    "Table 'cooking_preferences' is missing. Please run the SQL migration to create it.",
                )
            raise

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "save_family_pref")
        raise HTTPException(500, "Internal server error")


@router.get("/family/{family_id}")
def list_family_preferences(family_id: str = Path(..., description="Family ID")):
    """
    Liệt kê **toàn bộ thành viên** của family + preference đã lưu (nếu có).
    Trả về mảng:
      [{ user_id, user_name, display_name, role, preference, updated_at }]
    """
    try:
        log_api_call(f"/preferences/family/{family_id}", "GET")

        # Lấy danh sách thành viên
        members = db_query(
            """
            SELECT fm.user_id, fm.display_name, fm.role, u.user_name
            FROM family_memberships fm
            JOIN users u ON u.user_id = fm.user_id
            WHERE fm.family_id = %s
            ORDER BY fm.id ASC
            """,
            (family_id,),
        ) or []

        if not members:
            return []

        # Ghép với bản lưu pref mới nhất (per user)
        # Dùng subquery lấy bản updated_at lớn nhất cho từng (family_id,user_id)
        try:
            rows = db_query(
                """
                SELECT cp.family_id, cp.user_id, cp.pref_json, cp.updated_at
                FROM cooking_preferences cp
                JOIN (
                    SELECT family_id, user_id, MAX(updated_at) AS mx
                    FROM cooking_preferences
                    WHERE family_id = %s
                    GROUP BY family_id, user_id
                ) t ON t.family_id = cp.family_id
                   AND t.user_id   = cp.user_id
                   AND t.mx        = cp.updated_at
                """,
                (family_id,),
            )
        except Exception as ie:
            # Bảng chưa có → coi như chưa ai lưu pref
            msg = str(ie)
            if "1146" in msg or "doesn't exist" in msg or "does not exist" in msg:
                rows = []
            else:
                raise

        pref_map: Dict[str, Dict[str, Any]] = {}
        for r in (rows or []):
            try:
                pref_map[str(r["user_id"])] = {
                    "preference": json.loads(r["pref_json"]) if r.get("pref_json") else {},
                    "updated_at": str(r.get("updated_at")) if r.get("updated_at") else None,
                }
            except Exception:
                pref_map[str(r["user_id"])] = {
                    "preference": {},
                    "updated_at": str(r.get("updated_at")) if r.get("updated_at") else None,
                }

        # Trộn: **mỗi member đều có mặt**, kể cả khi chưa lưu pref
        result = []
        for m in members:
            u = str(m["user_id"])
            p = pref_map.get(u, {"preference": None, "updated_at": None})
            result.append(
                {
                    "user_id": u,
                    "user_name": m.get("user_name"),
                    "display_name": m.get("display_name"),
                    "role": m.get("role"),
                    "preference": p["preference"],
                    "updated_at": p["updated_at"],
                }
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "list_family_preferences")
        raise HTTPException(500, "Internal server error")


@router.get("/merged")
def merged_pref(
    family_id: str = Query(...),
    user_id: str = Query(...),
):
    """
    Trả về preference **mới nhất** đã lưu cho (family_id, user_id).
    Nếu chưa có -> preference = {}.
    """
    try:
        log_api_call("/preferences/merged", "GET", user_id, family_id=family_id)

        # lấy bản mới nhất
        try:
            rows = db_query(
                """
                SELECT pref_json, updated_at
                FROM cooking_preferences
                WHERE family_id=%s AND user_id=%s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (family_id, user_id),
            )
        except Exception as ie:
            msg = str(ie)
            if "1146" in msg or "doesn't exist" in msg or "does not exist" in msg:
                rows = []
            else:
                raise

        pref: Dict[str, Any] = {}
        updated = None
        if rows:
            try:
                pref = json.loads(rows[0]["pref_json"]) if rows[0].get("pref_json") else {}
            except Exception:
                pref = {}
            updated = str(rows[0].get("updated_at")) if rows[0].get("updated_at") else None

        # trả về cấu trúc đã normalize để FE dùng trực tiếp
        return {
            "user_id": user_id,
            "family_id": family_id,
            "preference": _normalize_pref(pref),
            "updated_at": updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "merged_pref")
        raise HTTPException(500, "Internal server error")
