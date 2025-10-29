"""
Submission-related API routes (meal_code-optional)
"""
import json
import logging
from fastapi import APIRouter, HTTPException, Query
from ..models import InfoCollectIn
from ..database import db_query, db_execute
from ..utils import log_api_call, log_error

logger = logging.getLogger("meal")
router = APIRouter(prefix="/api/submissions", tags=["submission"])

@router.post("/submit")
def submit_info(request: InfoCollectIn):
    """提交用户信息（không còn dùng meal_code）"""
    try:
        log_api_call("/submissions/submit", "POST", request.user_id)

        if not (request.family_id and request.user_id and request.meal_date and request.meal_type):
            raise HTTPException(400, "Missing required fields")

        existed = db_query(
            """
            SELECT id FROM info_submissions
            WHERE family_id=%s AND user_id=%s AND meal_date=%s AND meal_type=%s
            ORDER BY id DESC LIMIT 1
            """,
            (request.family_id, request.user_id, request.meal_date, request.meal_type),
        )

        # Không còn dùng meal_code -> để None (hoặc '' nếu cột NOT NULL)
        meal_code_value = ""

        if existed:
            db_execute(
                """
                UPDATE info_submissions
                SET role=%s,
                    display_name=%s,
                    age=%s,
                    meal_date=%s,
                    meal_type=%s,
                    preferences=%s,
                    drinks=%s,
                    remark=%s,
                    meal_code=%s,
                    participant_count=%s
                WHERE id=%s
                """,
                (
                    request.role,
                    request.display_name,
                    request.age,
                    request.meal_date,
                    request.meal_type,
                    json.dumps(request.preferences, ensure_ascii=False),
                    request.drinks,
                    request.remark,
                    meal_code_value,
                    request.participant_count or 1,
                    existed[0]["id"],
                ),
            )
        else:
            db_execute(
                """
                INSERT INTO info_submissions
                    (family_id,user_id,role,display_name,age,meal_date,meal_type,preferences,drinks,remark,meal_code,participant_count)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    request.family_id,
                    request.user_id,
                    request.role,
                    request.display_name,
                    request.age,
                    request.meal_date,
                    request.meal_type,
                    json.dumps(request.preferences, ensure_ascii=False),
                    request.drinks,
                    request.remark,
                    meal_code_value,
                    request.participant_count or 1,
                ),
            )

        return {"ok": True, "message": "Information submitted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"submit_info for user {request.user_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/submit-by-meal-code")
def submit_info_by_meal_code(request: InfoCollectIn, meal_code: str):
    """ĐÃ NGỪNG: submit bằng meal code không còn được hỗ trợ"""
    raise HTTPException(410, "Meal code feature has been retired. Please select a family and submit normally.")


@router.delete("/{submission_id}")
def delete_submission(
    submission_id: int,
    user_id: str = Query(..., description="User ID for verification"),
):
    """Xoá submission (chỉ xoá được của chính mình)."""
    try:
        log_api_call(f"/submissions/{submission_id}", "DELETE", user_id)

        submission = db_query(
            """
            SELECT id FROM info_submissions
            WHERE id = %s AND user_id = %s
            """,
            (submission_id, user_id),
        )
        if not submission:
            raise HTTPException(404, "Submission not found or not owned by user")

        db_execute("DELETE FROM info_submissions WHERE id = %s", (submission_id,))
        return {"ok": True, "message": "Submission deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"delete_submission {submission_id} for user {user_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/my")
def list_my_submissions(
    family_id: str = Query(..., description="Family ID or 'all' for all families"),
    user_id: str = Query(..., description="User ID"),
    limit: int = Query(100, description="Maximum number of submissions to return"),
):
    """Lấy danh sách submission của người dùng."""
    try:
        log_api_call("/submissions/my", "GET", user_id)

        if family_id == "all":
            rows = db_query(
                """
                SELECT s.id, s.family_id, s.role, s.display_name, s.age,
                       s.meal_date, s.meal_type, s.preferences, s.drinks,
                       s.remark, s.created_at, s.meal_code, f.family_name
                FROM info_submissions s
                JOIN families f ON s.family_id = f.family_id
                WHERE s.user_id = %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
        else:
            rows = db_query(
                """
                SELECT s.id, s.family_id, s.role, s.display_name, s.age,
                       s.meal_date, s.meal_type, s.preferences, s.drinks,
                       s.remark, s.created_at, s.meal_code, f.family_name
                FROM info_submissions s
                JOIN families f ON s.family_id = f.family_id
                WHERE s.user_id = %s AND s.family_id = %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (user_id, family_id, limit),
            )

        for row in rows:
            if row.get("preferences"):
                try:
                    row["preferences"] = json.loads(row["preferences"])
                except Exception:
                    row["preferences"] = {}
        return rows
    except Exception as e:
        log_error(e, f"list_my_submissions for user {user_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/family/{family_id}")
def list_family_submissions(family_id: str):
    """Lấy toàn bộ submission của 1 family (mọi ngày/bữa)."""
    try:
        log_api_call(f"/submissions/family/{family_id}", "GET")

        rows = db_query(
            """
            SELECT s.id, s.user_id, s.role, s.display_name, s.age,
                   s.meal_date, s.meal_type, s.preferences, s.drinks,
                   s.remark, s.created_at, u.user_name
            FROM info_submissions s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.family_id = %s
            ORDER BY s.created_at DESC
            """,
            (family_id,),
        )

        for row in rows:
            if row.get("preferences"):
                try:
                    row["preferences"] = json.loads(row["preferences"])
                except Exception:
                    row["preferences"] = {}
        return rows
    except Exception as e:
        log_error(e, f"list_family_submissions for family {family_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/family/{family_id}/at")
def list_family_submissions_at(family_id: str, meal_date: str):
    """Lấy submission của 1 ngày cho family (không lọc meal_type để front tự nhóm)."""
    try:
        log_api_call(f"/submissions/family/{family_id}/at", "GET")

        rows = db_query(
            """
            SELECT id, user_id, role, display_name, age,
                   meal_date, meal_type, preferences, drinks,
                   remark, created_at, participant_count
            FROM info_submissions
            WHERE family_id = %s AND meal_date = %s
            ORDER BY meal_type, id ASC
            """,
            (family_id, meal_date),
        )

        logger.info(f"Found {len(rows)} submissions for family {family_id} on {meal_date}")

        for row in rows:
            if row.get("preferences"):
                try:
                    row["preferences"] = json.loads(row["preferences"])
                except Exception:
                    row["preferences"] = {}
        return rows
    except Exception as e:
        log_error(e, f"list_family_submissions_at for family {family_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/family/{family_id}/meals")
def list_family_meals(family_id: str, date: str = None):
    """Thống kê submission của family."""
    try:
        log_api_call(f"/submissions/family/{family_id}/meals", "GET")

        if not date:
            from datetime import date as _date
            date = _date.today().isoformat()

        rows = db_query(
            """
            SELECT COUNT(*) as submission_count
            FROM info_submissions
            WHERE family_id = %s
            """,
            (family_id,),
        )
        return rows
    except Exception as e:
        log_error(e, f"list_family_meals for family {family_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/{submission_id}")
def get_submission_details(submission_id: int):
    """Chi tiết 1 submission."""
    try:
        log_api_call(f"/submissions/{submission_id}", "GET")

        submission = db_query(
            """
            SELECT s.id, s.family_id, s.user_id, s.role, s.display_name, s.age,
                   s.meal_date, s.meal_type, s.preferences, s.drinks, s.remark, s.created_at,
                   f.family_name, u.user_name
            FROM info_submissions s
            JOIN families f ON s.family_id = f.family_id
            JOIN users u ON s.user_id = u.user_id
            WHERE s.id = %s
            """,
            (submission_id,),
        )
        if not submission:
            raise HTTPException(404, "Submission not found")

        row = submission[0]
        if row.get("preferences"):
            try:
                row["preferences"] = json.loads(row["preferences"])
            except Exception:
                row["preferences"] = {}
        return row
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_submission_details {submission_id}")
        raise HTTPException(500, "Internal server error")

