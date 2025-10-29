"""
Meal code related API routes
"""
import logging
from fastapi import APIRouter, HTTPException
from ..models import CreateMealCodeRequest, MealCodeInfo
from ..database import db_query, db_execute
from ..utils import generate_meal_code, parse_meal_code, log_api_call, log_error

logger = logging.getLogger("meal")
router = APIRouter(prefix="/api/meal-code", tags=["meal-code"])

# Global storage for meal code types (in production, use database)
meal_code_types = {}

@router.post("/create")
def create_meal_code(request: CreateMealCodeRequest):
    """创建meal code"""
    try:
        log_api_call("/meal-code/create", "POST")
        
        # 解析meal_time获取日期部分
        meal_date = request.meal_time.split(' ')[0]  # 获取日期部分 YYYY-MM-DD
        
        # 检查是否已存在相同餐次的meal code
        existing_meal_code = db_query("""
            SELECT meal_code FROM family_meal_code 
            WHERE family_id=%s AND meal_date=%s AND meal_type=%s 
            LIMIT 1
        """, (request.family_id, meal_date, request.meal_type))
        
        if existing_meal_code:
            # 如果已存在，返回现有的meal code
            meal_code = existing_meal_code[0]["meal_code"]
        else:
            # 生成新的16位meal code
            meal_code = generate_meal_code(
                request.family_id, 
                request.participant_count, 
                meal_date, 
                request.meal_type
            )
            
            # 存储到family_meal_code表，使用INSERT IGNORE防止重复
            db_execute("""
                INSERT IGNORE INTO family_meal_code (meal_code, family_id, user_id, meal_date, meal_type)
                VALUES (%s, %s, %s, %s, %s)
            """, (meal_code, request.family_id, request.user_id, meal_date, request.meal_type))
        
        # 存储meal_type到内存映射中
        global meal_code_types
        meal_code_types[meal_code] = request.meal_type
        
        return {
            "ok": True,
            "meal_code": meal_code,
            "family_id": request.family_id,
            "participant_count": request.participant_count,
            "meal_time": request.meal_time,
            "meal_type": request.meal_type
        }
    except Exception as e:
        log_error(e, f"create_meal_code for family {request.family_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/{meal_code}")
def parse_meal_code_endpoint(meal_code: str):
    """解析meal code"""
    try:
        log_api_call(f"/meal-code/{meal_code}", "GET")
        
        if len(meal_code) != 16:
            raise HTTPException(400, "Invalid meal code format")
        
        # 解析meal code
        parsed_data = parse_meal_code(meal_code)
        
        # 从内存中获取meal_type
        global meal_code_types
        meal_type = meal_code_types.get(meal_code, "dinner")
        
        # 检查家庭是否存在
        family = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (parsed_data["family_id"],))
        if not family:
            raise HTTPException(404, "Family not found")
        
        return {
            "family_id": parsed_data["family_id"],
            "family_name": family[0]["family_name"],
            "participant_count": parsed_data["participant_count"],
            "meal_date": parsed_data["meal_date"],
            "meal_type": meal_type
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"parse_meal_code for code {meal_code}")
        raise HTTPException(500, "Internal server error")

@router.get("/validate/{meal_code}")
def validate_meal_code(meal_code: str):
    """验证meal code是否存在"""
    try:
        log_api_call(f"/meal-code/validate/{meal_code}", "GET")
        
        # 检查meal code是否存在于family_meal_code表中
        meal_code_info = db_query("""
            SELECT fmc.meal_code, fmc.family_id, fmc.meal_date, fmc.meal_type, f.family_name
            FROM family_meal_code fmc
            JOIN families f ON fmc.family_id = f.family_id
            WHERE fmc.meal_code = %s
        """, (meal_code,))
        
        if not meal_code_info:
            raise HTTPException(404, "Meal code not found")
        
        info = meal_code_info[0]
        return {
            "valid": True,
            "meal_code": info["meal_code"],
            "family_id": info["family_id"],
            "family_name": info["family_name"],
            "meal_date": str(info["meal_date"]),
            "meal_type": info["meal_type"]
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"validate_meal_code for code {meal_code}")
        raise HTTPException(500, "Internal server error")

@router.get("/user/{user_id}")
def get_user_meal_codes(user_id: str):
    """获取用户作为holder的家庭中的meal codes"""
    try:
        log_api_call(f"/meal-code/user/{user_id}", "GET", user_id)
        
        # 获取用户作为holder的家庭中的meal codes（只显示今日及今日以后的）
        meal_codes = db_query("""
            SELECT DISTINCT fmc.meal_code, fmc.family_id, fmc.meal_date, fmc.meal_type, f.family_name
            FROM family_meal_code fmc
            JOIN families f ON fmc.family_id = f.family_id
            JOIN family_memberships fm ON f.family_id = fm.family_id
            WHERE fm.user_id = %s AND fm.role = 'holder' 
            AND fmc.meal_date >= CURDATE()
            ORDER BY fmc.meal_date ASC, fmc.meal_type ASC
        """, (user_id,))
        
        return meal_codes
    except Exception as e:
        log_error(e, f"get_user_meal_codes for user {user_id}")
        raise HTTPException(500, "Internal server error")

