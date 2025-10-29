"""
Pydantic models for the Meal Planner application
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

# ==================== User Models ====================

class UserRegister(BaseModel):
    user_id: str
    user_name: str
    user_pass: str

class UserLogin(BaseModel):
    user_id: str
    user_pass: str

class UserUpdateRequest(BaseModel):
    user_id: str
    user_name: str

class UserInfo(BaseModel):
    user_id: str
    user_name: str
    joined_families: List[Dict[str, Any]]

# ==================== Family Models ====================

class FamilyRegister(BaseModel):
    family_id: str
    family_name: str
    family_password: str

class FamilyLogin(BaseModel):
    family_id: str
    family_password: str

class CreateFamilyRequest(BaseModel):
    family_id: Optional[str] = None
    family_name: str
    user_id: str

class JoinFamily(BaseModel):
    family_id: str
    user_id: str
    role: str  # '父亲','母亲','爷爷','奶奶','儿子','女儿','朋友'
    display_name: str

class InviteMemberRequest(BaseModel):
    family_id: str
    invited_user_id: str

class DeleteFamilyRequest(BaseModel):
    family_id: str

class RemoveMemberRequest(BaseModel):
    family_id: str
    user_id: str

class FamilyInfo(BaseModel):
    family_id: str
    family_name: str
    role: str
    display_name: str
    is_primary_today: bool

class FamilyMealTimes(BaseModel):
    # 24h "HH:MM" (không timezone)
    breakfast: str
    lunch: str
    dinner: str

# ==================== Meal Code Models ====================

class CreateMealCodeRequest(BaseModel):
    family_id: str
    user_id: str
    participant_count: int
    meal_time: str  # "YYYY-MM-DD HH:MM"
    meal_type: str  # "breakfast", "lunch", "dinner"

class MealCodeInfo(BaseModel):
    family_id: str
    family_name: str
    participant_count: int
    meal_date: str
    meal_type: str

# ==================== Submission Models ====================

class InfoCollectIn(BaseModel):
    family_id: str
    user_id: str
    role: str
    display_name: str
    age: Optional[int] = None
    meal_date: str  # 对应数据库的meal_date字段 (date类型)
    meal_type: str  # 对应数据库的meal_type字段 (varchar(100))
    preferences: dict  # 对应数据库的preferences字段 (json)
    drinks: Optional[str] = None  # 对应数据库的drinks字段 (text)
    remark: Optional[str] = None  # 对应数据库的remark字段 (text)
    participant_count: Optional[int] = None  # 对应数据库的Participant_Count字段 (int)

class SubmissionInfo(BaseModel):
    id: int
    family_id: str
    user_id: str
    role: str
    display_name: str
    age: Optional[int]
    preferences: dict
    drinks: Optional[str]
    remark: Optional[str]
    created_at: str

# ==================== Plan Generation Models ====================

class GenerateRequest(BaseModel):
    family_id: str
    meal_date: str  # "YYYY-MM-DD"
    meal_type: str  # "breakfast", "lunch", "dinner"
    feedback: Optional[str] = None
    headcount: Optional[int] = None
    submissions: Optional[List[Dict[str, Any]]] = None
    anchors: Optional[List[str]] = None
    hard_lock: Optional[bool] = False

class PlanIngest(BaseModel):
    family_id: str
    meal_date: str  # "YYYY-MM-DD"
    meal_type: str  # "breakfast", "lunch", "dinner"
    headcount: Optional[int] = None
    payload: dict  # 外部API的原始结果(JSON)

class PlanInfo(BaseModel):
    id: int
    plan_code: str
    family_id: str
    meal_type: Optional[str] = None
    meal_date: Optional[str] = None
    source_date: Optional[str] = None
    submission_cnt: int
    plan_json: dict
    plan_html: Optional[str] = None
    created_at: str

# ==================== Legacy Models (for compatibility) ====================

class LegacyUserRegister(BaseModel):
    user_id: str
    user_name: str
    user_pass: str

class LegacyUserLogin(BaseModel):
    user_id: str
    user_pass: str

class LegacyJoinFamily(BaseModel):
    family_id: str
    user_id: str
    role: str
    display_name: str
    age: Optional[int] = None
    dinner_time: str
    preferences: dict
    drinks: Optional[str] = None
    remark: Optional[str] = None

class LegacyInfoCollectIn(BaseModel):
    family_id: str
    user_id: str
    role: str
    display_name: str
    age: Optional[int] = None
    dinner_time: str
    preferences: dict
    drinks: Optional[str] = None
    remark: Optional[str] = None

# ==================== Response Models ====================

class ApiResponse(BaseModel):
    ok: bool
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

class HealthResponse(BaseModel):
    ok: bool
    db: bool
    error: Optional[str] = None
