# routes/wheel.py
"""
Wheel routes (shared state in DB)
- User add ≤ 2 dishes per (family_id, meal_date, meal_type) session.
- Ballots = number of dishes user added (≤ 2).
- User cannot vote their own dishes.
- Toggle vote (vote/unvote) as long as votes_used < ballots.
- State returns candidates with total votes + voters, and user summary.
"""
import logging
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Body
from ..database import db_query, db_execute
from ..utils import log_api_call, log_error

logger = logging.getLogger("meal")
router = APIRouter(tags=["wheel"])

# ---------- helpers ----------

SESSION_WHERE = "family_id=%s AND meal_date=%s AND meal_type=%s"


def _user_ballots(family_id: str, meal_date: str, meal_type: str, user_id: str) -> int:
    """
    ballots = số dish user đã đề cử trong phiên (chỉ tính những cái chưa bị xóa).
    Giữ đúng giới hạn ≤ 2 ở tầng business.
    """
    rows = db_query(
        f"""SELECT COUNT(*) AS cnt
            FROM wheel_candidates
            WHERE {SESSION_WHERE} AND proposer_user_id=%s AND deleted_at IS NULL""",
        (family_id, meal_date, meal_type, user_id),
    )
    return int(rows[0]["cnt"]) if rows else 0


def _user_voted_ids(family_id: str, meal_date: str, meal_type: str, user_id: str) -> List[int]:
    rows = db_query(
        f"""SELECT v.candidate_id
            FROM wheel_votes v
            JOIN wheel_candidates c ON c.id = v.candidate_id
            WHERE c.{SESSION_WHERE}
              AND v.voter_user_id = %s
              AND c.deleted_at IS NULL""",
        (family_id, meal_date, meal_type, user_id),
    )
    return [int(r["candidate_id"]) for r in rows]


def _load_candidates(family_id: str, meal_date: str, meal_type: str) -> List[Dict[str, Any]]:
    """
    Lấy all candidates đang active + tổng votes + voters.
    (Dùng bảng thật để có voters; có thể dùng view để nhẹ hơn nếu cần)
    """
    cands = db_query(
        f"""SELECT c.id,
                   c.name,
                   c.proposer_user_id,
                   c.proposer_name,
                   (SELECT COUNT(*) FROM wheel_votes v WHERE v.candidate_id = c.id) AS votes
            FROM wheel_candidates c
            WHERE {SESSION_WHERE} AND c.deleted_at IS NULL
            ORDER BY votes DESC, c.id ASC""",
        (family_id, meal_date, meal_type),
    )
    if not cands:
        return []

    ids = [int(c["id"]) for c in cands]
    voters_map: Dict[int, List[str]] = {cid: [] for cid in ids}
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        rows = db_query(
            f"SELECT candidate_id, voter_user_id FROM wheel_votes WHERE candidate_id IN ({placeholders})",
            tuple(ids),
        )
        for r in rows:
            voters_map[int(r["candidate_id"])].append(r["voter_user_id"])

    result = []
    for c in cands:
        result.append(
            {
                "id": str(c["id"]),
                "name": c["name"],
                "votes": int(c["votes"] or 0),
                "proposer": c["proposer_user_id"],
                "proposer_name": c["proposer_name"],
                "voters": voters_map.get(int(c["id"]), []),
            }
        )
    return result


# ---------- endpoints ----------

@router.get("/state")
def get_state(
    family_id: str = Query(...),
    meal_date: str = Query(...),
    meal_type: str = Query(...),
    user_id: Optional[str] = Query(None),
):
    """
    Trả về:
    {
      "candidates": [{id,name,votes,proposer,proposer_name,voters:[...]}],
      "user_summary": {ballots, votes_used, voted_candidate_ids:[...]}  // nếu user_id có truyền
    }
    """
    try:
        log_api_call("/wheel/state", "GET", user_id)
        candidates = _load_candidates(family_id, meal_date, meal_type)
        resp: Dict[str, Any] = {"candidates": candidates}
        if user_id:
            ballots = _user_ballots(family_id, meal_date, meal_type, user_id)
            voted_ids = _user_voted_ids(family_id, meal_date, meal_type, user_id)
            resp["user_summary"] = {
                "ballots": ballots,
                "votes_used": len(voted_ids),
                "voted_candidate_ids": [str(i) for i in voted_ids],
            }
        return resp
    except Exception as e:
        log_error(e, "wheel.get_state")
        raise HTTPException(500, "Internal server error")


@router.post("/nominate")
def nominate(payload: Dict[str, Any] = Body(...)):
    """
    Thêm 1 dish.
    body: {family_id, meal_date, meal_type, user_id, dish}  // hoặc dishes:[...] -> lấy phần tử đầu
    Ràng buộc:
    - Mỗi user tối đa 2 dish/phiên.
    - Không trùng (family_id, meal_date, meal_type, name) đang active (do unique index).
    """
    try:
        family_id = payload.get("family_id")
        meal_date = payload.get("meal_date")
        meal_type = payload.get("meal_type")
        user_id = payload.get("user_id")
        dish = (payload.get("dish") or "").strip()
        if not dish:
            dishes = payload.get("dishes") or []
            dish = (dishes[0] or "").strip() if dishes else ""

        if not (family_id and meal_date and meal_type and user_id and dish):
            raise HTTPException(400, "Missing fields")

        log_api_call("/wheel/nominate", "POST", user_id)

        # ballots hiện tại (giới hạn theo NGƯỜI DÙNG)
        cnt = _user_ballots(family_id, meal_date, meal_type, user_id)
        if cnt >= 2:
            raise HTTPException(400, "You can add at most 2 dishes for this time")

        # lấy proposer_name
        u = db_query("SELECT user_name FROM users WHERE user_id=%s", (user_id,))
        proposer_name = u[0]["user_name"] if u else user_id

        # kiểm tra trùng tên đang active trong cùng phiên
        exists = db_query(
            """SELECT id FROM wheel_candidates
               WHERE family_id=%s AND meal_date=%s AND meal_type=%s
                 AND name=%s AND deleted_at IS NULL""",
            (family_id, meal_date, meal_type, dish),
        )
        if exists:
            raise HTTPException(409, "This dish already exists in the current list")

        new_id = db_execute(
            """INSERT INTO wheel_candidates
               (family_id, meal_date, meal_type, name, proposer_user_id, proposer_name)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (family_id, meal_date, meal_type, dish, user_id, proposer_name),
        )
        return {"ok": True, "candidate_id": str(new_id)}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "wheel.nominate")
        raise HTTPException(500, "Internal server error")


@router.post("/vote")
def vote_toggle(payload: Dict[str, Any] = Body(...)):
    """
    Toggle vote.
    body: {family_id, meal_date, meal_type, user_id, candidate_id}
    - Không vote món của chính mình.
    - Nếu đã vote ứng viên đó -> unvote.
    - Nếu chưa và đã dùng hết ballots -> 400.
    """
    try:
        family_id = payload.get("family_id")
        meal_date = payload.get("meal_date")
        meal_type = payload.get("meal_type")
        user_id = payload.get("user_id")
        candidate_id = payload.get("candidate_id")

        if not (family_id and meal_date and meal_type and user_id and candidate_id):
            raise HTTPException(400, "Missing fields")

        log_api_call("/wheel/vote", "POST", user_id)

        # xác thực candidate thuộc phiên, chưa bị delete
        cand = db_query(
            f"""SELECT id, proposer_user_id
                FROM wheel_candidates
                WHERE id=%s AND {SESSION_WHERE} AND deleted_at IS NULL""",
            (candidate_id, family_id, meal_date, meal_type),
        )
        if not cand:
            raise HTTPException(404, "Candidate not found")
        cand = cand[0]

        if cand["proposer_user_id"] == user_id:
            raise HTTPException(400, "You cannot vote your own dish")

        ballots = _user_ballots(family_id, meal_date, meal_type, user_id)
        voted_ids = _user_voted_ids(family_id, meal_date, meal_type, user_id)
        already = int(candidate_id) in voted_ids

        if already:
            db_execute(
                "DELETE FROM wheel_votes WHERE candidate_id=%s AND voter_user_id=%s",
                (candidate_id, user_id),
            )
            return {"ok": True, "action": "unvote"}
        else:
            if len(voted_ids) >= ballots:
                raise HTTPException(400, "No ballots left")
            db_execute(
                "INSERT INTO wheel_votes (candidate_id, voter_user_id) VALUES (%s,%s)",
                (candidate_id, user_id),
            )
            return {"ok": True, "action": "vote"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "wheel.vote_toggle")
        raise HTTPException(500, "Internal server error")


@router.put("/candidate/{candidate_id}")
def edit_candidate(candidate_id: int, payload: Dict[str, Any] = Body(...)):
    """Đổi tên dish (chỉ proposer). body: {user_id, name}"""
    try:
        user_id = payload.get("user_id")
        name = (payload.get("name") or "").strip()
        if not (user_id and name):
            raise HTTPException(400, "Missing fields")

        log_api_call(f"/wheel/candidate/{candidate_id}", "PUT", user_id)

        row = db_query(
            "SELECT proposer_user_id, family_id, meal_date, meal_type FROM wheel_candidates WHERE id=%s AND deleted_at IS NULL",
            (candidate_id,),
        )
        if not row:
            raise HTTPException(404, "Candidate not found")
        if row[0]["proposer_user_id"] != user_id:
            raise HTTPException(403, "Only proposer can edit this dish")

        # tránh trùng tên trong cùng phiên
        dup = db_query(
            """SELECT id FROM wheel_candidates
               WHERE family_id=%s AND meal_date=%s AND meal_type=%s
                 AND name=%s AND deleted_at IS NULL AND id<>%s""",
            (row[0]["family_id"], row[0]["meal_date"], row[0]["meal_type"], name, candidate_id),
        )
        if dup:
            raise HTTPException(409, "This dish name already exists in this list")

        db_execute("UPDATE wheel_candidates SET name=%s WHERE id=%s", (name, candidate_id))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "wheel.edit_candidate")
        raise HTTPException(500, "Internal server error")


@router.delete("/candidate/{candidate_id}")
def delete_candidate(candidate_id: int, user_id: str = Query(...)):
    """
    Xoá dish (chỉ proposer). Hiện dùng hard delete để đơn giản (votes xóa theo FK CASCADE).
    """
    try:
        log_api_call(f"/wheel/candidate/{candidate_id}", "DELETE", user_id)

        row = db_query("SELECT proposer_user_id FROM wheel_candidates WHERE id=%s", (candidate_id,))
        if not row:
            raise HTTPException(404, "Candidate not found")
        if row[0]["proposer_user_id"] != user_id:
            raise HTTPException(403, "Only proposer can delete this dish")

        db_execute("DELETE FROM wheel_candidates WHERE id=%s", (candidate_id,))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "wheel.delete_candidate")
        raise HTTPException(500, "Internal server error")

@router.post("/pick")
def save_pick(payload: Dict[str, Any] = Body(...)):
    """
    Lưu kết quả quay wheel để /plan/generate đọc đúng winner.
    body: {family_id, meal_date, meal_type, winner_name, picked_by}
    """
    try:
        family_id = (payload.get("family_id") or "").strip()
        meal_date = (payload.get("meal_date") or "").strip()
        meal_type = (payload.get("meal_type") or "").strip()
        winner_name = (payload.get("winner_name") or "").strip()
        picked_by = (payload.get("picked_by") or "").strip()

        if not (family_id and meal_date and meal_type and winner_name and picked_by):
            raise HTTPException(400, "Missing fields")

        log_api_call("/wheel/pick", "POST", picked_by)

        try:
            db_execute(
                """
                INSERT INTO wheel_picks (family_id, meal_date, meal_type, winner_name, picked_by)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (family_id, meal_date, meal_type, winner_name, picked_by),
            )
        except Exception as ie:
            # Nếu bảng chưa tồn tại -> ghi log rồi trả ok để không chặn luồng
            msg = str(ie)
            if "1146" in msg or "doesn't exist" in msg or "does not exist" in msg:
                logger.warning("wheel_picks table missing; skipping persist. err=%s", msg)
            else:
                raise

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "wheel.save_pick")
        raise HTTPException(500, "Internal server error")

@router.get("/picks/latest")
def get_latest_pick(
    family_id: str = Query(...),
    meal_date: str = Query(...),
    meal_type: str = Query(...)
):
    """
    Lấy winner gần nhất (cùng family_id, meal_date, meal_type)
    """
    try:
        rows = db_query(
            """SELECT winner_name, picked_by, created_at
               FROM wheel_picks
               WHERE family_id=%s AND meal_date=%s AND meal_type=%s
               ORDER BY created_at DESC
               LIMIT 1""",
            (family_id, meal_date, meal_type)
        )
        if not rows:
            return {"winner_name": None}
        r = rows[0]
        return {
            "winner_name": r.get("winner_name"),
            "picked_by": r.get("picked_by"),
            "created_at": str(r.get("created_at"))
        }
    except Exception as e:
        log_error(e, "wheel.get_latest_pick")
        raise HTTPException(500, "Internal server error")


