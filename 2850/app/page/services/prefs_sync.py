# services/prefs_sync.py
from typing import Any, Dict, List, Optional
import json
from database import db_query, db_execute

_TRUE = {"true", "1", "y", "yes", "on"}

def _to_bool(v: Any) -> Optional[bool]:
    if v is None: return None
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return bool(v)
    if isinstance(v, str): return v.strip().lower() in _TRUE
    return bool(v)

def _norm_task_name(x: Optional[str]) -> Optional[str]:
    if not x: return None
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

def normalize_pref(p0: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    p = dict(p0 or {})
    if "isChef" in p and "is_chef" not in p: p["is_chef"] = p.pop("isChef")
    if "beforeTask" in p and "before_task" not in p: p["before_task"] = p.pop("beforeTask")
    if "afterTask" in p and "after_task" not in p: p["after_task"] = p.pop("afterTask")
    if "Tasks" in p and "tasks" not in p and isinstance(p["Tasks"], list): p["tasks"] = p.pop("Tasks")

    if p.get("pre_work") is True and not p.get("before_task"): p["before_task"] = "pre_work"
    if p.get("after_work") is True and not p.get("after_task"): p["after_task"] = "after_work"

    if "is_chef" in p: p["is_chef"] = _to_bool(p["is_chef"])

    tasks: List[str] = []
    if isinstance(p.get("tasks"), list):
        tasks = [_norm_task_name(t) for t in p["tasks"] if _norm_task_name(t)]
    if p.get("pre_work") is True: tasks.append("pre_work")
    if p.get("after_work") is True: tasks.append("after_work")
    tasks = list({t for t in tasks if t})

    bt = _norm_task_name(p.get("before_task"))
    at = _norm_task_name(p.get("after_task"))
    if bt and bt != "none" and "pre_work" not in tasks: tasks.append("pre_work")
    if at and at != "none" and "after_work" not in tasks: tasks.append("after_work")

    return {
        "is_chef": p.get("is_chef", False),
        "before_task": bt or "none",
        "after_task": at or "none",
        "tasks": tasks
    }

def upsert_family_user_pref(family_id: str, user_id: str, preference: Dict[str, Any]) -> int:
    pref = normalize_pref(preference)
    sql = """
      INSERT INTO cooking_preferences (family_id, user_id, is_chef, before_task, after_task, tasks)
      VALUES (%s,%s,%s,%s,%s,%s)
      ON DUPLICATE KEY UPDATE
        is_chef=VALUES(is_chef),
        before_task=VALUES(before_task),
        after_task=VALUES(after_task),
        tasks=VALUES(tasks),
        updated_at=CURRENT_TIMESTAMP
    """
    return db_execute(sql, (
        family_id, user_id,
        1 if pref.get("is_chef") else 0,
        pref.get("before_task"), pref.get("after_task"),
        json.dumps(pref.get("tasks") or [], ensure_ascii=False)
    ))

def get_family_user_pref(family_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    rows = db_query(
        """SELECT is_chef, before_task, after_task, tasks, updated_at
           FROM cooking_preferences
           WHERE family_id=%s AND user_id=%s
           LIMIT 1""",
        (family_id, user_id)
    )
    if not rows: return None
    r = rows[0]
    tasks = r.get("tasks")
    if isinstance(tasks, str):
        try: tasks = json.loads(tasks)
        except Exception: tasks = []
    return normalize_pref({
        "is_chef": bool(r.get("is_chef")),
        "before_task": r.get("before_task"),
        "after_task": r.get("after_task"),
        "tasks": tasks or []
    })

def list_family_prefs(family_id: str) -> List[Dict[str, Any]]:
    rows = db_query(
        """
        SELECT
          m.user_id,
          m.role,
          m.display_name,
          u.user_name,
          cp.is_chef, cp.before_task, cp.after_task, cp.tasks, cp.updated_at
        FROM family_memberships m
        JOIN users u ON u.user_id = m.user_id
        LEFT JOIN cooking_preferences cp
          ON cp.family_id = m.family_id AND cp.user_id = m.user_id
        WHERE m.family_id = %s
        ORDER BY m.display_name, m.user_id
        """,
        (family_id,)
    )
    out = []
    for r in rows:
        tasks = r.get("tasks")
        if isinstance(tasks, str):
            try: tasks = json.loads(tasks)
            except Exception: tasks = []
        pref = normalize_pref({
            "is_chef": r.get("is_chef"),
            "before_task": r.get("before_task"),
            "after_task": r.get("after_task"),
            "tasks": tasks or []
        }) if (r.get("is_chef") is not None) or tasks else None
        out.append({
            "user_id": r["user_id"],
            "display_name": r.get("display_name") or r.get("user_name") or r["user_id"],
            "role": r.get("role") or "member",
            "preference": pref,
            "updated_at": r.get("updated_at")
        })
    return out
