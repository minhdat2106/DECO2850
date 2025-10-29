"""
User-related API routes
"""
import json
import logging
from fastapi import APIRouter, HTTPException
from ..models import UserRegister, UserLogin, UserUpdateRequest, UserInfo
from ..database import db_query, db_execute
from ..utils import validate_user_id, log_api_call, log_error

logger = logging.getLogger("meal")
router = APIRouter(prefix="/api/user", tags=["user"])

# --- TĨNH TRƯỚC ---

@router.post("/register")
def register_user(request: UserRegister):
    """注册新用户"""
    try:
        log_api_call("/user/register", "POST", request.user_id)

        # 验证用户ID格式
        if not validate_user_id(request.user_id):
            raise HTTPException(400, "Invalid user ID format")

        # 检查用户是否已存在
        existing_user = db_query("SELECT user_id FROM users WHERE user_id=%s", (request.user_id,))
        if existing_user:
            raise HTTPException(400, "User ID already exists")

        # 创建新用户
        db_execute("""
            INSERT INTO users (user_id, user_name, user_pass) 
            VALUES (%s, %s, %s)
        """, (request.user_id, request.user_name, request.user_pass))

        return {"ok": True, "message": "User registered successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"register_user for user {request.user_id}")
        raise HTTPException(500, "Internal server error")


@router.post("/login")
def login_user(request: UserLogin):
    """用户登录"""
    try:
        log_api_call("/user/login", "POST", request.user_id)

        # 验证用户凭据
        user = db_query("""
            SELECT user_id, user_name, user_pass FROM users 
            WHERE user_id=%s AND user_pass=%s
        """, (request.user_id, request.user_pass))

        if not user:
            raise HTTPException(401, "Invalid credentials")

        user_data = user[0]
        return {
            "ok": True,
            "user_id": user_data["user_id"],
            "user_name": user_data["user_name"],
            "message": "Login successful"
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"login_user for user {request.user_id}")
        raise HTTPException(500, "Internal server error")


@router.post("/update")
def update_user_info(request: UserUpdateRequest):
    """更新用户信息"""
    try:
        log_api_call("/user/update", "POST", request.user_id)

        # 检查用户是否存在
        user = db_query("SELECT user_id FROM users WHERE user_id=%s", (request.user_id,))
        if not user:
            raise HTTPException(404, "User not found")

        # 更新用户信息
        db_execute("UPDATE users SET user_name=%s WHERE user_id=%s", (request.user_name, request.user_id))

        return {"ok": True, "message": "User information updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"update_user_info for user {request.user_id}")
        raise HTTPException(500, "Internal server error")


# --- NHÁNH CON CỦA /{user_id} (tĩnh theo mẫu) ---

@router.get("/{user_id}/families")
def get_user_families(user_id: str):
    """获取用户加入的家庭"""
    try:
        log_api_call(f"/user/{user_id}/families", "GET", user_id)

        # 获取用户加入的所有家庭
        families = db_query("""
            SELECT f.family_id, f.family_name, fm.role, fm.display_name, fm.is_primary_today
            FROM families f
            JOIN family_memberships fm ON f.family_id = fm.family_id
            WHERE fm.user_id = %s
            ORDER BY f.family_id ASC
        """, (user_id,))

        return families
    except Exception as e:
        log_error(e, f"get_user_families for user {user_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/{user_id}/owned-families")
def get_user_owned_families(user_id: str):
    """获取用户拥有的家庭"""
    try:
        log_api_call(f"/user/{user_id}/owned-families", "GET", user_id)

        # 获取用户拥有的家庭
        families = db_query("""
            SELECT f.family_id, f.family_name, fm.role, fm.display_name
            FROM families f
            JOIN family_memberships fm ON f.family_id = fm.family_id
            WHERE f.user_id = %s AND fm.role = 'holder'
            ORDER BY f.family_id ASC
        """, (user_id,))

        return families
    except Exception as e:
        log_error(e, f"get_user_owned_families for user {user_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/{user_id}/exists")
def check_user_exists(user_id: str):
    """检查用户是否存在"""
    try:
        log_api_call(f"/user/{user_id}/exists", "GET", user_id)

        user = db_query("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
        return {"exists": len(user) > 0}
    except Exception as e:
        log_error(e, f"check_user_exists for user {user_id}")
        raise HTTPException(500, "Internal server error")


# --- CUỐI CÙNG MỚI LÀ /{user_id} (động, “bắt-mọi”) ---

@router.get("/{user_id}")
def get_user_info(user_id: str):
    """获取用户信息"""
    try:
        log_api_call(f"/user/{user_id}", "GET", user_id)

        user = db_query("SELECT user_id, user_name FROM users WHERE user_id=%s", (user_id,))
        if not user:
            raise HTTPException(404, "User not found")

        user_data = user[0]

        # 从family_memberships表获取用户加入的家庭
        joined_families = db_query("""
            SELECT f.family_id, f.family_name, fm.role, fm.display_name
            FROM families f
            JOIN family_memberships fm ON f.family_id = fm.family_id
            WHERE fm.user_id = %s
            ORDER BY f.family_id ASC
        """, (user_id,))

        return {
            "user_id": user_data["user_id"],
            "user_name": user_data["user_name"],
            "joined_families": joined_families
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_user_info for user {user_id}")
        raise HTTPException(500, "Internal server error")

