# services1/preferences.py
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple, List

# Lưu ý:
# - Module này giả định bạn có pool aiomysql và truyền vào các hàm bên dưới.
# - Nếu bạn đang dùng mysqlclient / mysql-connector thuần sync, bạn có thể viết
#   wrapper sync tương tự (chỉ khác await và cách lấy cursor).

# =========================
# ===== Normalizers =======
# =========================

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
    # map các biến thể hay gặp
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


def _normalize_role_like(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Chuẩn hoá dict preference/role:
      - key casing: isChef -> is_chef, beforeTask -> before_task, afterTask -> after_task
      - chuẩn hoá tasks: list[str] với các giá trị 'pre_work' | 'after_work' | 'cooking'
      - nếu có before/after_task -> đảm bảo tasks chứa 'pre_work'/'after_work'
    """
    if not isinstance(obj, dict):
        return {}

    r = dict(obj)

    # alias keys
    if "isChef" in r and "is_chef" not in r:
        r["is_chef"] = r.pop("isChef")
    if "beforeTask" in r and "before_task" not in r:
        r["before_task"] = r.pop("beforeTask")
    if "afterTask" in r and "after_task" not in r:
        r["after_task"] = r.pop("afterTask")
    if "Tasks" in r and "tasks" not in r and isinstance(r["Tasks"], list):
        r["tasks"] = r.pop("Tasks")

    if r.get("pre_work") is True and not r.get("before_task"):
        r["before_task"] = "pre_work"
    if r.get("after_work") is True and not r.get("after_task"):
        r["after_task"] = "after_work"

    # bool
    if "is_chef" in r:
        r["is_chef"] = _to_bool(r["is_chef"])

    # tasks
    tasks: List[str] = []
    if isinstance(r.get("tasks"), list):
        tasks = [_norm_task_name(t) for t in r["tasks"] if _norm_task_name(t)]

    if r.get("pre_work") is True:
        tasks.append("pre_work")

    if r.get("after_work") is True:
        tasks.append("after_work")
        r["tasks"] = list({t for t in tasks if t})

    # before/after task chuẩn hoá
    if "before_task" in r and r["before_task"]:
        r["before_task"] = _norm_task_name(r["before_task"])
        if r["before_task"] and r["before_task"] != "none" and "pre_work" not in r["tasks"]:
            r["tasks"] = r["tasks"] + ["pre_work"]

    if "after_task" in r and r["after_task"]:
        r["after_task"] = _norm_task_name(r["after_task"])
        if r["after_task"] and r["after_task"] != "none" and "after_work" not in r["tasks"]:
            r["tasks"] = r["tasks"] + ["after_work"]

        r.pop("pre_work", None)
        r.pop("after_work", None)
    return r

def _merge_pref(default: Optional[Dict[str, Any]],
                override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Hợp nhất 2 dict preference đã được normalize:
      - override > default
      - tasks: hợp (set union), không trùng lặp
    """
    d = _normalize_role_like(default or {})
    o = _normalize_role_like(override or {})

    merged: Dict[str, Any] = {}

    # các field chính
    for key in ["is_chef", "before_task", "after_task", "age_group"]:
        merged[key] = o.get(key, d.get(key))

    # tasks union
    dt = d.get("tasks") or []
    ot = o.get("tasks") or []
    tasks = list({t for t in dt + ot if t})
    merged["tasks"] = tasks

    # extra: shallow merge (override lấn)
    extra = {}
    if isinstance(d.get("extra"), dict):
        extra.update(d["extra"])
    if isinstance(o.get("extra"), dict):
        extra.update(o["extra"])
    if extra:
        merged["extra"] = extra

    return merged


# =========================
# ===== DB Helpers ========
# =========================

async def _fetchone_dict(cur) -> Optional[Dict[str, Any]]:
    row = await cur.fetchone()
    if row is None:
        return None
    # Với aiomysql DictCursor thì row đã là dict. Nếu không dùng DictCursor,
    # bạn cần map thủ công.
    return row


# ================================
# ===== Public Service API  ======
# ================================

# ---------- Default (user) preference ----------

async def get_user_default_pref(pool, user_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Lấy default preference của 1 user.
    Return: (preference_dict | None, updated_at | None)
    """
    sql = """
        SELECT preference, updated_at
        FROM user_cooking_prefs
        WHERE user_id = %s
        LIMIT 1
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (user_id,))
            row = await _fetchone_dict(cur)
            if not row:
                return None, None
            pref_raw = row["preference"]
            # pref_raw có thể là str JSON hoặc dict (tuỳ driver)
            if isinstance(pref_raw, str):
                try:
                    pref = json.loads(pref_raw)
                except Exception:
                    pref = {}
            else:
                pref = pref_raw or {}
            return _normalize_role_like(pref), str(row.get("updated_at")) if row.get("updated_at") else None


async def upsert_user_default_pref(pool, user_id: str, preference: Dict[str, Any]) -> str:
    """
    Tạo/cập nhật default preference cho user.
    Trả về updated_at (ISO string nếu có).
    """
    pref_norm = _normalize_role_like(preference or {})
    pref_json = json.dumps(pref_norm, ensure_ascii=False)
    sql = """
        INSERT INTO user_cooking_prefs (user_id, preference)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE
          preference = VALUES(preference),
          updated_at = CURRENT_TIMESTAMP
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (user_id, pref_json))
        await conn.commit()

    # Lấy lại updated_at
    _, updated_at = await get_user_default_pref(pool, user_id)
    return updated_at or ""


# ---------- Per-family override preference ----------

async def get_family_user_pref(pool, family_id: str, user_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Lấy override preference cho (family_id, user_id).
    Return: (preference_dict | None, updated_at | None)
    """
    sql = """
        SELECT preference, updated_at
        FROM family_user_cooking_prefs
        WHERE family_id = %s AND user_id = %s
        LIMIT 1
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (family_id, user_id))
            row = await _fetchone_dict(cur)
            if not row:
                return None, None
            pref_raw = row["preference"]
            if isinstance(pref_raw, str):
                try:
                    pref = json.loads(pref_raw)
                except Exception:
                    pref = {}
            else:
                pref = pref_raw or {}
            return _normalize_role_like(pref), str(row.get("updated_at")) if row.get("updated_at") else None


async def upsert_family_user_pref(pool, family_id: str, user_id: str, preference: Dict[str, Any]) -> str:
    """
    Tạo/cập nhật override preference cho (family_id, user_id).
    Trả về updated_at.
    """
    pref_norm = _normalize_role_like(preference or {})
    pref_json = json.dumps(pref_norm, ensure_ascii=False)
    sql = """
        INSERT INTO family_user_cooking_prefs (family_id, user_id, preference)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
          preference = VALUES(preference),
          updated_at = CURRENT_TIMESTAMP
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (family_id, user_id, pref_json))
        await conn.commit()

    _, updated_at = await get_family_user_pref(pool, family_id, user_id)
    return updated_at or ""


# ---------- Merge ----------

async def get_merged_pref(
    pool,
    user_id: str,
    family_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Lấy preference hợp nhất cho user:
      - Nếu có family_id: lấy override + default và merge (override > default)
      - Nếu không có family_id: trả về default
    Return structure:
      {
        "user_id": "...",
        "family_id": "... | None",
        "merged": {...},
        "default": {...} | None,
        "override": {...} | None,
        "updated_at_default": "2025-01-01 10:00:00" | None,
        "updated_at_override": "2025-01-01 10:00:00" | None
      }
    """
    default_pref, updated_default = await get_user_default_pref(pool, user_id)
    override_pref, updated_override = (None, None)

    if family_id:
        override_pref, updated_override = await get_family_user_pref(pool, family_id, user_id)
        merged = _merge_pref(default_pref, override_pref)
    else:
        merged = _normalize_role_like(default_pref or {})

    return {
        "user_id": user_id,
        "family_id": family_id,
        "merged": merged,
        "default": default_pref,
        "override": override_pref,
        "updated_at_default": updated_default,
        "updated_at_override": updated_override,
    }


# ================================
# ===== Optional: Deletes  =======
# ================================

async def delete_user_default_pref(pool, user_id: str) -> int:
    """
    Xoá default pref của user. Trả về số row affected.
    """
    sql = "DELETE FROM user_cooking_prefs WHERE user_id = %s"
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            affected = await cur.execute(sql, (user_id,))
        await conn.commit()
    return affected or 0


async def delete_family_user_pref(pool, family_id: str, user_id: str) -> int:
    """
    Xoá override pref (family_id, user_id). Trả về số row affected.
    """
    sql = "DELETE FROM family_user_cooking_prefs WHERE family_id = %s AND user_id = %s"
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            affected = await cur.execute(sql, (family_id, user_id))
        await conn.commit()
    return affected or 0
