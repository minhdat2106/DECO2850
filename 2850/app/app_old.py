import os, json, re, datetime, urllib.parse, http.client, random, time
from datetime import timedelta
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import mysql.connector
from mysql.connector import pooling
from dotenv import load_dotenv
import openai

# Set environment variables if not already set
os.environ.setdefault('DB_HOST', '127.0.0.1')
os.environ.setdefault('DB_PORT', '3306')
os.environ.setdefault('DB_USER', 'Meal Planner')
os.environ.setdefault('DB_PASSWORD', 'NJTteam')
os.environ.setdefault('DB_NAME', 'meal_planner')
os.environ.setdefault('OPENAI_API_KEY', 'sk-g073X9xcUOeMA0mN953b31B03b4146F18f1eDbFf2eDeCc01')
os.environ.setdefault('OPENAI_BASE_URL', 'https://free.v36.cm/v1/')
os.environ.setdefault('OPENAI_MODEL', 'gpt-4o-mini')
os.environ.setdefault('DEBUG_LLM', 'false')

# 日志--- Debug logging setup ---
import logging, pathlib
from collections import deque

from fastapi.staticfiles import StaticFiles
import pathlib

LOG_DIR = pathlib.Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("meal")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

fh = logging.FileHandler(LOG_DIR / "meal.log", encoding="utf-8")
fh.setFormatter(fmt)
sh = logging.StreamHandler()
sh.setFormatter(fmt)

if not logger.handlers:  # 避免重复添加 handler
    logger.addHandler(fh)
    logger.addHandler(sh)

# 最近 20 次 LLM 调用的内存缓冲
LLM_DEBUG = deque(maxlen=20)

# 开关：是否把原始返回也透传到 500 的 detail（仅本地调试时开）
DEBUG_LLM = os.getenv("DEBUG_LLM", "false").lower() == "true"

# ------------------------- Env & OpenAI -------------------------
load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")
# 兼容自定义网关（可选）
if os.getenv("OPENAI_BASE_URL"):
    openai.base_url = os.getenv("OPENAI_BASE_URL")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ------------------------- DB Pool -------------------------
cnxpool = pooling.MySQLConnectionPool(
    pool_name="meal_pool",
    pool_size=5,
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
    autocommit=True,
)

# 检查并添加drinks列
def ensure_drinks_column():
    try:
        conn = cnxpool.get_connection()
        cursor = conn.cursor()
        # 检查列是否存在
        cursor.execute("SHOW COLUMNS FROM info_submissions LIKE 'drinks'")
        if not cursor.fetchone():
            # 列不存在，添加它
            cursor.execute("ALTER TABLE info_submissions ADD COLUMN drinks TEXT")
            print("Added drinks column to info_submissions table")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error ensuring drinks column: {e}")

# 在应用启动时确保列存在
ensure_drinks_column()

# ------------------------- App & CORS -------------------------
app = FastAPI(title="Meal Planner API (Family Edition)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------- Helpers -------------------------
def db_execute(sql: str, params: Optional[tuple] = None):
    conn = cnxpool.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        last_id = cur.lastrowid
        conn.commit()
        cur.close()
        return last_id
    finally:
        conn.close()

def db_query(sql: str, params: Optional[tuple] = None):
    conn = cnxpool.get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()

def url_exists(url: str, timeout=5) -> bool:
    """轻量 HEAD 检查，失败即返回 False；不抛异常影响主流程。"""
    try:
        parsed = urllib.parse.urlparse(url)
        conn = (
            http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
            if parsed.scheme == "https"
            else http.client.HTTPConnection(parsed.netloc, timeout=timeout)
        )
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        conn.request("HEAD", path)
        res = conn.getresponse()
        return 200 <= res.status < 400
    except Exception:
        return False

# ------------------------- JSON Schema & Prompts -------------------------
PLAN_SCHEMA_EXAMPLE = {
    "meta": {
        "Time": "YYYY-MM-DD,TT:TT",
        "headcount": 3,
        "roles": [
            {"person_role": "爸爸", "is_chef": False, "tasks": ["清洗", "炒菜"]},
            {"person_role": "妈妈", "is_chef": False, "tasks": ["备菜", "摆台"]},
            {"person_role": "儿子", "is_chef": False, "tasks": ["收桌", "洗碗"]},
        ],
    },
    "dishes": [
        {
            "dish1":"",
            "name": "xxx",
            "category": "前菜|热菜|汤",
            "ingredients": [{"name": "鸡胸肉", "amount": "300g"}, {"name": "西兰花", "amount": "200g"}],
            "steps": ["步骤1...", "步骤2..."],
            "image_url": "https://...",
            "video_url": "https://www.youtube.com/watch?v=...",
        },
        {
            "dish2":"",
            "name": "xxx",
            "category": "前菜|热菜|汤",
            "ingredients": [{"name": "牛肉", "amount": "300g"}, {"name": "土豆", "amount": "200g"}],
            "steps": ["步骤1...", "步骤2..."],
            "image_url": "https://...",
            "video_url": "https://www.youtube.com/watch?v=...",

        }
    ],
    "drinks": [
        {"name": "Green Tea", "type": "Hot", "serving": "1 cup per person"},
        {"name": "Orange Juice", "type": "Cold", "serving": "200ml per person"}
    ],
}

PROMPT_SYSTEM = (
    "你是一个家庭晚餐计划助理。严格输出英文版JSON，结构与我提供的 schema 一致，不要输出多余文本。"
)
PROMPT_RULES = "生成规则：必须遵守这条规则：Jason文件内全部用英文输出！必须遵守这条规则：你的一切回答必须用英文！1) 根据当日/同一开饭时间的所有提交与限制，生成满足偏好的菜品组合；数量为 N-1（N 为用餐人数；2<=N-1<=8）。2) 菜品需营养均衡，除非全员素食，否则荤素搭配。包含前菜/热菜/汤（若仅 2 道，则三者中任选与热菜搭配）。3) 每道菜给出食材(含克/毫升)、详细步骤、图片 URL、以及该菜品名称在YouTube搜索到的结果的网站(例如麻婆豆腐在YouTube搜到的结果为：https://www.youtube.com/results?search_query=%E9%BA%BB%E5%A9%86%E8%B1%86%E8%85%90) URL。4) 为每位成员分配任务：Washing, preparing ingredients (cutting/arranging, not allocating to the youngest age group), stir-frying, setting the table, clearing the table, washing dishes.；若有人被设为主厨则分配Chef的角色。5) 凡 is_chef=true 的成员，其 tasks **必须**包含stir-frying(Chef)。6) 若 remark 中提及会晚到/只做饭后工作等，则优先分配收桌/洗碗等饭后任务，务必严格采纳每位成员的remark；所有人的 remark 需逐条采纳。7) **必须返回非空的 meta.roles**；提交报告人数少于 headcount 时可虚拟未提交者身份并完成分工；严禁 roles 为空数组。8) **必须包含饮料信息**：根据用户提交的drinks偏好，在drinks数组中包含所有用户选择的饮料，包括名称、类型(热饮/冷饮)和每人的份量。如果用户没有指定饮料，则提供默认的茶水和果汁选项。"
def try_parse_plan(text: str):
    """尽力把模型输出转成 JSON 对象：剥离```json围栏 → 直接loads → 花括号截取再loads。"""
    if not text:
        return None
    t = text.strip()

    # 1) 剥离 ```json ... ``` 或 ``` ... ```
    m = re.match(r"```(?:json)?\s*([\s\S]*?)\s*```", t, flags=re.IGNORECASE)
    if m:
        t = m.group(1).strip()

    # 2) 直接 json.loads
    try:
        return json.loads(t)
    except Exception:
        pass

    # 3) 花括号包围的最大 JSON 片段
    m2 = re.search(r"\{[\s\S]*\}", t)
    if m2:
        frag = m2.group(0)
        try:
            return json.loads(frag)
        except Exception:
            return None
    return None

def normalize_plan(plan: dict) -> dict:
    """把缺字段/错类型做温和校正，保证前端与存库不中断。"""
    meta = plan.get("meta") or {}
    dishes = plan.get("dishes") or []
    drinks = plan.get("drinks") or []

    # meta 兜底
    date = meta.get("date") or datetime.date.today().isoformat()
    headcount = meta.get("headcount")
    try:
        headcount = int(headcount) if headcount is not None else None
    except Exception:
        headcount = None

    roles = meta.get("roles") or []
    if not isinstance(roles, list):
        roles = []

    def _as_list(x):
        return x if isinstance(x, list) else ([] if x is None else [x])

    # 逐道菜兜底
    fixed_dishes = []
    for d in (dishes if isinstance(dishes, list) else _as_list(dishes)):
        if not isinstance(d, dict):
            continue
        fixed_dishes.append({
            "name": d.get("name") or "Unnamed meal",
            "category": d.get("category") or "Hot meal",
            "nutrition_tags": _as_list(d.get("nutrition_tags")),
            "ingredients": [
                i for i in (d.get("ingredients") or [])
                if isinstance(i, dict) and i.get("name")
            ],
            "steps": [str(s) for s in _as_list(d.get("steps"))],
            "image_url": d.get("image_url") or "",
            "video_url": d.get("video_url") or "",
        })

    # 饮料兜底
    fixed_drinks = []
    for d in _as_list(drinks):
        if not isinstance(d, dict):
            continue
        fixed_drinks.append({
            "name": d.get("name") or "Unknown drink",
            "type": d.get("type") or "Cold",
            "serving": d.get("serving") or "1 serving per person",
        })

    return {
        "meta": {
            "date": date,
            "headcount": headcount,
            "roles": roles,
        },
        "dishes": fixed_dishes,
        "drinks": fixed_drinks,
    }

def render_plan_html(plan: dict) -> str:
    meta = plan.get("meta", {})
    time_text = meta.get("Time") or meta.get("date") or datetime.date.today().isoformat()
    headcount = meta.get("headcount")
    roles = meta.get("roles", [])
    dishes = plan.get("dishes", [])
    drinks = plan.get("drinks", [])

    def esc(x):
        return (str(x or "")).replace("<", "&lt;").replace(">", "&gt;")

    role_items = []
    for r in roles:
        dn = (r.get('display_name') or '').strip()
        paren = f"（{esc(dn)}）" if dn else ""  # ★ 只有有昵称才显示括号
        role_items.append(
            f"<li class='mb-1'><span class='font-semibold'>{esc(r.get('person_role'))}</span>"
            f"{paren} - "
            f"{'Chef' if r.get('is_chef') else 'Member'}：{', '.join(r.get('tasks', []))}</li>"
        )
    role_html = "".join(role_items)

    def ingredients_html(d):
        return "".join(
            [f"<li>{esc(i.get('name') or '')} {esc(i.get('amount') or '')}</li>" for i in d.get("ingredients", [])]
        )

    def steps_html(d):
        return "".join([f"<li>{esc(s)}</li>" for s in d.get("steps", [])])

    def unverified_span(d):
        return ('<span class="text-xs text-amber-600 ml-2">(unverified)</span>' if d.get("video_status") == "unverified" else "")

    dish_html = "".join([
        f"""
        <div class='p-4 rounded-xl border mb-4'>
          <h3 class='text-lg font-bold mb-1'>{esc(d.get('name'))}
            <span class='text-sm text-gray-500'>[{esc(d.get('category'))}]</span>
          </h3>
          <div class='flex flex-col md:flex-row gap-4'>
            <div class='md:w-1/2'>
              <div class='font-semibold'>Ingredients</div>
              <ul class='list-disc ml-6 text-sm'>{ingredients_html(d)}</ul>
              <div class='font-semibold mt-2'>Steps</div>
              <ol class='list-decimal ml-6 text-sm'>{steps_html(d)}</ol>
            </div>
            <div class='md:w-1/2'>
              <img class='w-full h-48 object-cover rounded-lg' src='{esc(d.get('image_url') or '')}' onerror="this.style.display='none'"/>
              <div class='mt-2'>
                <a class='text-blue-600 underline' href='{esc(d.get('video_url') or '')}' target='_blank'>Tutorial Video</a>{unverified_span(d)}
              </div>
            </div>
          </div>
        </div>
        """ for d in dishes
    ])

    # Drinks HTML
    drinks_html = ""
    if drinks:
        drinks_items = []
        for d in drinks:
            drinks_items.append(
                f"<li class='mb-1'><span class='font-semibold'>{esc(d.get('name'))}</span> "
                f"({esc(d.get('type'))}) - {esc(d.get('serving'))}</li>"
            )
        drinks_html = f"""
        <div class='p-4 rounded-xl bg-blue-50 mb-4'>
          <div class='font-semibold mb-2'>🍹 Drinks</div>
          <ul class='list-disc ml-6 text-sm'>{''.join(drinks_items)}</ul>
        </div>
        """

    fam_line = ""
    if meta.get("family_name") or meta.get("family_id"):
        fam_line = f" · Family:{esc(meta.get('family_name') or '')}({esc(meta.get('family_id') or '')})"

    return f"""
    <section class='max-w-3xl mx-auto my-6'>
      <h2 class='text-2xl font-bold mb-2'>Meal Plan</h2>
      <div class='text-sm text-gray-600 mb-4'>Serving time：{esc(time_text)} · Headcount：{esc(str(headcount or ''))}{fam_line}</div>
      <div class='p-4 rounded-xl bg-gray-50 mb-4'>
        <div class='font-semibold mb-1'>Task Assignment</div>
        <ul class='list-disc ml-6 text-sm'>{role_html}</ul>
      </div>
      {drinks_html}
      <div>{dish_html}</div>
    </section>
    """

# ------------------------- Pydantic Schemas -------------------------
# （保留你旧接口的结构，兼容使用）
class Likes(BaseModel):
    ingredients: List[str] = []
    cuisines: List[str] = []
    methods: List[str] = []

class Dislikes(BaseModel):
    tastes: List[str] = []
    ingredients: List[str] = []

class Restrictions(BaseModel):
    allergies: List[str] = []
    religion: List[str] = []
    health: List[str] = []

class Taste(BaseModel):
    salt_level: Optional[str] = None
    spicy_level: Optional[str] = None
    sweet: Optional[bool] = None
    sour: Optional[bool] = None

class Preferences(BaseModel):
    likes: Likes = Likes()
    dislikes: Dislikes = Dislikes()
    restrictions: Restrictions = Restrictions()
    taste: Taste = Taste()
    drinks: List[str] = []
    lifestyle: List[str] = []

class SubmissionIn(BaseModel):
    person_role: str = Field(pattern="^(Father|Mother|Son|Daughter|Grandpa|Grandma|Friend)$")
    is_primary: bool = False
    headcount: Optional[int] = None
    is_chef: bool = False
    age_group: Optional[str] = None
    preferences: Preferences
    dining_date: Optional[str] = None  # 仅旧接口用

# 新增：家庭/用户/信息收集/生成
class FamilyRegister(BaseModel):
    family_id: str
    family_name: str
    family_password: str

class FamilyLogin(BaseModel):
    family_id: str
    family_password: str

class UserRegister(BaseModel):
    user_id: str
    user_name: str
    user_pass: str

class UserLogin(BaseModel):
    user_id: str
    user_pass: str

class JoinFamily(BaseModel):
    family_id: str
    user_id: str
    role: str            # '父亲','母亲','爷爷','奶奶','儿子','女儿','朋友'
    display_name: str

class InfoCollectIn(BaseModel):
    family_id: str
    user_id: str
    role: str
    display_name: str
    age: Optional[int] = None
    preferences: dict       # 自由结构（含"其他"等）
    drinks: Optional[str] = None  # 饮料偏好
    remark: Optional[str] = None

class GenerateRequest(BaseModel):
    family_id: str
    dinner_time: str        # "YYYY-MM-DD HH:00"
    feedback: Optional[str] = None
    headcount: Optional[int] = None   # 新增：可手动覆盖用餐人数


# ------------------------- Health -------------------------
@app.get("/api/health")
def health():
    try:
        db_query("SELECT 1 AS ping")
        return {"ok": True, "db": True}
    except Exception as e:
        return {"ok": False, "db": False, "error": str(e)}

# ------------------------- New API Endpoints for Frontend -------------------------

# 获取用户信息
@app.get("/api/user/{user_id}")
def get_user_info(user_id: str):
    user = db_query("SELECT user_id, user_name, joined_json FROM users WHERE user_id=%s", (user_id,))
    if not user:
        raise HTTPException(404, "User not found")
    
    user_data = user[0]
    joined_families = json.loads(user_data["joined_json"] or "[]")
    
    return {
        "user_id": user_data["user_id"],
        "user_name": user_data["user_name"],
        "joined_families": joined_families
    }

# 更新用户信息
class UserUpdateRequest(BaseModel):
    user_id: str
    user_name: str

@app.post("/api/user/update")
def update_user_info(request: UserUpdateRequest):
    # 检查用户是否存在
    user = db_query("SELECT user_id FROM users WHERE user_id=%s", (request.user_id,))
    if not user:
        raise HTTPException(404, "User not found")
    
    # 更新用户信息
    db_execute("UPDATE users SET user_name=%s WHERE user_id=%s", (request.user_name, request.user_id))
    
    return {"ok": True, "message": "User information updated successfully"}

# 获取用户加入的家庭
@app.get("/api/user/{user_id}/families")
def get_user_families(user_id: str):
    # 获取用户加入的所有家庭
    families = db_query("""
        SELECT f.family_id, f.family_name, fm.role, fm.display_name, fm.is_primary_today
        FROM families f
        JOIN family_memberships fm ON f.family_id = fm.family_id
        WHERE fm.user_id = %s
        ORDER BY f.family_id ASC
    """, (user_id,))
    
    return families

# 获取用户作为holder的家庭
@app.get("/api/user/{user_id}/owned-families")
def get_user_owned_families(user_id: str):
    families = db_query("""
        SELECT f.family_id, f.family_name
        FROM families f
        JOIN family_memberships fm ON f.family_id = fm.family_id
        WHERE fm.user_id = %s AND fm.role = 'holder'
        ORDER BY f.family_id ASC
    """, (user_id,))
    
    return families

# 创建家庭
class CreateFamilyRequest(BaseModel):
    family_id: Optional[str] = None
    family_name: str
    user_id: str
    user_name: str

@app.post("/api/family/create")
def create_family(request: CreateFamilyRequest):
    # 检查用户是否已经创建了同名家庭
    existing_family = db_query("""
        SELECT f.family_id FROM families f
        JOIN family_memberships fm ON f.family_id = fm.family_id
        WHERE fm.user_id = %s AND fm.role = 'holder' AND f.family_name = %s
    """, (request.user_id, request.family_name))
    
    if existing_family:
        raise HTTPException(400, f"You already have a family named '{request.family_name}'. Please choose a different name.")
    
    # 如果未提供family_id，生成一个8位随机ID
    if not request.family_id:
        import string
        family_id = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # 确保ID唯一
        while db_query("SELECT 1 FROM families WHERE family_id=%s", (family_id,)):
            family_id = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    else:
        family_id = request.family_id
        # 检查ID是否已存在
        if db_query("SELECT 1 FROM families WHERE family_id=%s", (family_id,)):
            raise HTTPException(400, "Family ID already exists")
    
    # 创建家庭
    db_execute("""
        INSERT INTO families (family_id, family_name, family_password, user_id) 
        VALUES (%s, %s, %s, %s)
    """, (family_id, request.family_name, "default_password", request.user_id))
    
    # 将创建者添加为holder
    db_execute("""
        INSERT INTO family_memberships (family_id, user_id, role, display_name) 
        VALUES (%s, %s, %s, %s)
    """, (family_id, request.user_id, "holder", request.user_name))
    
    # 更新用户的joined_json
    user = db_query("SELECT joined_json FROM users WHERE user_id=%s", (request.user_id,))
    if user:
        joined_families = json.loads(user[0]["joined_json"] or "[]")
        joined_families.append({"family_id": family_id, "role": "holder"})
        db_execute("UPDATE users SET joined_json=%s WHERE user_id=%s", 
                  (json.dumps(joined_families, ensure_ascii=False), request.user_id))
    
    return {
        "ok": True,
        "family_id": family_id,
        "family_name": request.family_name,
        "message": f"Family '{request.family_name}' created successfully!"
    }

# 邀请用户加入家庭
class InviteUserRequest(BaseModel):
    family_id: str
    invited_user_id: str
    inviter_user_id: str

@app.post("/api/family/invite")
def invite_user_to_family(request: InviteUserRequest):
    # 检查家庭是否存在
    family = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (request.family_id,))
    if not family:
        raise HTTPException(404, "Family not found")
    
    # 检查被邀请用户是否存在
    invited_user = db_query("SELECT user_id, user_name FROM users WHERE user_id=%s", (request.invited_user_id,))
    if not invited_user:
        raise HTTPException(404, "Invited user not found")
    
    # 检查用户是否已经在家庭中
    existing_membership = db_query("""
        SELECT id FROM family_memberships 
        WHERE family_id=%s AND user_id=%s
    """, (request.family_id, request.invited_user_id))
    
    if existing_membership:
        raise HTTPException(400, "User is already a member of this family")
    
    # 添加用户到家庭
    db_execute("""
        INSERT INTO family_memberships (family_id, user_id, role, display_name) 
        VALUES (%s, %s, %s, %s)
    """, (request.family_id, request.invited_user_id, "member", invited_user[0]["user_name"]))
    
    # 更新被邀请用户的joined_json
    user = db_query("SELECT joined_json FROM users WHERE user_id=%s", (request.invited_user_id,))
    if user:
        joined_families = json.loads(user[0]["joined_json"] or "[]")
        joined_families.append({"family_id": request.family_id, "role": "member"})
        db_execute("UPDATE users SET joined_json=%s WHERE user_id=%s", 
                  (json.dumps(joined_families, ensure_ascii=False), request.invited_user_id))
    
    return {
        "ok": True,
        "message": f"Successfully invited {invited_user[0]['user_name']} to join the family",
        "family_name": family[0]["family_name"]
    }

# 创建meal code
class CreateMealCodeRequest(BaseModel):
    family_id: str
    participant_count: int
    meal_time: str  # "YYYY-MM-DD HH:MM"
    meal_type: str  # "breakfast", "lunch", "dinner"

@app.post("/api/meal-code/create")
def create_meal_code(request: CreateMealCodeRequest):
    # 生成16位meal code: 前8位是family_id，后8位是加密信息
    import hashlib
    import base64
    
    # 解析meal_time获取日期部分
    meal_date = request.meal_time.split(' ')[0]  # 获取日期部分 YYYY-MM-DD
    date_clean = meal_date.replace('-', '')  # YYYYMMDD
    
    # 创建后8位的加密信息: 2位参与者数量 + 8位日期 + 3位meal type
    # 但我们需要压缩到8位，所以使用更紧凑的格式
    participant_str = f"{request.participant_count:02d}"
    meal_type_code = request.meal_type[:3]  # bre, lun, din
    
    # 使用日期后6位 + 参与者数量2位 = 8位
    date_short = date_clean[2:]  # 取后6位 YYMMDD
    meal_info = date_short + participant_str  # 6位日期 + 2位参与者 = 8位
    
    # 将meal_type信息编码到family_id中（如果family_id不足8位）
    family_id = request.family_id[:8].ljust(8, '0')
    
    meal_code = family_id + meal_info
    
    # 存储meal_type到数据库或使用其他方式关联
    # 这里我们使用一个简单的映射存储在内存中（实际应用中应该存储到数据库）
    global meal_code_types
    if 'meal_code_types' not in globals():
        meal_code_types = {}
    meal_code_types[meal_code] = request.meal_type
    
    return {
        "ok": True,
        "meal_code": meal_code,
        "family_id": request.family_id,
        "participant_count": request.participant_count,
        "meal_time": request.meal_time,
        "meal_type": request.meal_type
    }

# 解析meal code
@app.get("/api/meal-code/{meal_code}")
def parse_meal_code(meal_code: str):
    if len(meal_code) != 16:
        raise HTTPException(400, "Invalid meal code format")
    
    family_id = meal_code[:8].rstrip('0')  # 移除填充的0
    meal_info = meal_code[8:]
    
    # 解析meal信息: 6位日期 + 2位参与者数量
    date_short = meal_info[:6]  # YYMMDD
    participant_count = int(meal_info[6:8])
    
    # 转换日期格式 (假设是20XX年)
    try:
        full_date = "20" + date_short  # 20YYMMDD
        date_obj = datetime.datetime.strptime(full_date, "%Y%m%d")
        formatted_date = date_obj.strftime("%Y-%m-%d")
    except:
        raise HTTPException(400, "Invalid date in meal code")
    
    # 从内存中获取meal_type（实际应用中应该从数据库获取）
    global meal_code_types
    meal_type = meal_code_types.get(meal_code, "dinner")
    
    # 检查家庭是否存在
    family = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (family_id,))
    if not family:
        raise HTTPException(404, "Family not found")
    
    return {
        "family_id": family_id,
        "family_name": family[0]["family_name"],
        "participant_count": participant_count,
        "meal_date": formatted_date,
        "meal_type": meal_type
    }

# 删除家庭
class DeleteFamilyRequest(BaseModel):
    family_id: str

@app.delete("/api/family/delete")
def delete_family(request: DeleteFamilyRequest):
    # 检查家庭是否存在
    family = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (request.family_id,))
    if not family:
        raise HTTPException(404, "Family not found")
    
    # 删除家庭（级联删除相关记录）
    db_execute("DELETE FROM families WHERE family_id=%s", (request.family_id,))
    
    return {
        "ok": True,
        "message": f"Family '{family[0]['family_name']}' has been deleted successfully"
    }

# 移除家庭成员
class RemoveMemberRequest(BaseModel):
    family_id: str
    user_id: str

@app.post("/api/family/remove-member")
def remove_family_member(request: RemoveMemberRequest):
    # 检查成员是否存在
    member = db_query("""
        SELECT fm.id, fm.role, u.user_name 
        FROM family_memberships fm
        JOIN users u ON fm.user_id = u.user_id
        WHERE fm.family_id=%s AND fm.user_id=%s
    """, (request.family_id, request.user_id))
    
    if not member:
        raise HTTPException(404, "Member not found in this family")
    
    # 移除成员
    db_execute("DELETE FROM family_memberships WHERE family_id=%s AND user_id=%s", 
              (request.family_id, request.user_id))
    
    # 更新用户的joined_json
    user = db_query("SELECT joined_json FROM users WHERE user_id=%s", (request.user_id,))
    if user:
        joined_families = json.loads(user[0]["joined_json"] or "[]")
        joined_families = [f for f in joined_families if f.get("family_id") != request.family_id]
        db_execute("UPDATE users SET joined_json=%s WHERE user_id=%s", 
                  (json.dumps(joined_families, ensure_ascii=False), request.user_id))
    
    return {
        "ok": True,
        "message": f"Member has been removed from the family"
    }

# ------------------------- 家庭 & 用户 -------------------------
@app.post("/api/family/register")
def family_register(b: FamilyRegister):
    if not re.fullmatch(r"[A-Za-z0-9]+", b.family_id or ""):
        raise HTTPException(400, "family_id must be alphanumeric")
    if not re.fullmatch(r"[A-Za-z0-9]+", b.family_password or ""):
        raise HTTPException(400, "family_pass must be alphanumeric")
    if db_query("SELECT 1 FROM families WHERE family_id=%s", (b.family_id,)):
        raise HTTPException(400, "family_id already exist")
    db_execute(
        "INSERT INTO families(family_id,family_name,family_password) VALUES(%s,%s,%s)",
        (b.family_id, b.family_name, b.family_password),
    )
    return {"ok": True}

@app.post("/api/family/login")
def family_login(b: FamilyLogin):
    r = db_query(
        "SELECT family_name FROM families WHERE family_id=%s AND family_password=%s",
        (b.family_id, b.family_password),
    )
    if not r:
        raise HTTPException(401, "User not exist Or Wrong Password")
    return {"ok": True, "family_id": b.family_id, "family_name": r[0]["family_name"]}

@app.post("/api/user/register")
def user_register(b: UserRegister):
    if not re.fullmatch(r"[A-Za-z0-9]+", b.user_id or ""):
        raise HTTPException(400, "user_id must be alphanumeric")
    if not re.fullmatch(r"[A-Za-z0-9]+", b.user_pass or ""):
        raise HTTPException(400, "user_pass must be alphanumeric")
    if db_query("SELECT 1 FROM users WHERE user_id=%s", (b.user_id,)):
        raise HTTPException(400, "user_id already exist")
    db_execute(
        "INSERT INTO users(user_id,user_name,user_pass,joined_json) VALUES(%s,%s,%s,%s)",
        (b.user_id, b.user_name, b.user_pass, json.dumps([], ensure_ascii=False)),
    )
    return {"ok": True}

@app.post("/api/user/login")
def user_login(b: UserLogin):
    r = db_query(
        "SELECT user_name, joined_json FROM users WHERE user_id=%s AND user_pass=%s",
        (b.user_id, b.user_pass),
    )
    if not r:
        raise HTTPException(401, "User not exist Or Wrong Password")
    return {"ok": True, "user_id": b.user_id, "user_name": r[0]["user_name"], "joined": r[0]["joined_json"]}

@app.get("/api/family/members")
def get_family_members(family_id: str):
    rows = db_query(
        """
        SELECT fm.role,fm.user_id,u.user_name,
               CASE WHEN fm.role='Friend' THEN '******' ELSE u.user_pass END AS user_pass,
               fm.display_name, fm.is_primary_today
        FROM family_memberships fm
        JOIN users u ON fm.user_id=u.user_id
        WHERE fm.family_id=%s ORDER BY fm.id ASC
        """,
        (family_id,),
    )
    return rows

class MemberUpdate(BaseModel):
    family_id: str
    user_id: str
    user_name: str
    user_pass: str
    role: str

@app.post("/api/family/members/update")
def update_member(m: MemberUpdate):
    # 朋友的密码不允许修改（按你的规则，如需更严格再做角色校验）
    if m.role == "Friend":
        raise HTTPException(400, "Can not modify the password family members of Friend")
    db_execute("UPDATE users SET user_name=%s, user_pass=%s WHERE user_id=%s", (m.user_name, m.user_pass, m.user_id))
    db_execute(
        "UPDATE family_memberships SET role=%s WHERE family_id=%s AND user_id=%s",
        (m.role, m.family_id, m.user_id),
    )
    return {"ok": True}

class FamilyProfileUpdate(BaseModel):
    family_id: str
    family_name: str
    family_password: str

@app.post("/api/family/profile/update")
def update_family_profile(b: FamilyProfileUpdate):
    db_execute(
        "UPDATE families SET family_name=%s, family_password=%s WHERE family_id=%s",
        (b.family_name, b.family_password, b.family_id),
    )
    return {"ok": True}

@app.post("/api/family/join")
def join_family(b: JoinFamily):
    # 父亲/母亲唯一性
    if b.role in ("Father", "Mother"):
        cnt = db_query(
            "SELECT COUNT(*) c FROM family_memberships WHERE family_id=%s AND role=%s",
            (b.family_id, b.role),
        )[0]["c"]
        if cnt >= 1:
            raise HTTPException(400, f"{b.role}Role already exist")

    ex = db_query(
        "SELECT id FROM family_memberships WHERE family_id=%s AND user_id=%s", (b.family_id, b.user_id)
    )
    if ex:
        db_execute(
            "UPDATE family_memberships SET role=%s,display_name=%s WHERE id=%s",
            (b.role, b.display_name, ex[0]["id"]),
        )
    else:
        db_execute(
            "INSERT INTO family_memberships(family_id,user_id,role,display_name) VALUES(%s,%s,%s,%s)",
            (b.family_id, b.user_id, b.role, b.display_name),
        )

    # 同步 users.joined_json
    u = db_query("SELECT joined_json FROM users WHERE user_id=%s", (b.user_id,))
    arr = json.loads(u[0]["joined_json"] or "[]")
    if not any(x.get("family_id") == b.family_id for x in arr):
        arr.append({"family_id": b.family_id, "role": b.role})
        db_execute("UPDATE users SET joined_json=%s WHERE user_id=%s", (json.dumps(arr, ensure_ascii=False), b.user_id))
    return {"ok": True}

@app.post("/api/family/set_primary_today")
def set_primary_today(family_id: str, user_id: str, is_primary: bool):
    db_execute("UPDATE family_memberships SET is_primary_today=FALSE WHERE family_id=%s", (family_id,))
    if is_primary:
        db_execute(
            "UPDATE family_memberships SET is_primary_today=TRUE WHERE family_id=%s AND user_id=%s",
            (family_id, user_id),
        )
    return {"ok": True}

# ------------------------- 信息收集 & 计划生成 -------------------------
@app.post("/api/info/submit")
def info_submit(b: InfoCollectIn):
    # 覆盖策略：同一家、同用户 → UPDATE；否则 INSERT
    existed = db_query(
        "SELECT id FROM info_submissions WHERE family_id=%s AND user_id=%s ORDER BY id DESC LIMIT 1",
        (b.family_id, b.user_id),
    )

    if existed:
        db_execute(
            "UPDATE info_submissions SET role=%s,display_name=%s,age=%s,preferences=%s,drinks=%s,remark=%s WHERE id=%s",
            (
                b.role,
                b.display_name,
                b.age,
                json.dumps(b.preferences, ensure_ascii=False),
                b.drinks,
                b.remark,
                existed[0]["id"],
            ),
        )
    else:
        db_execute(
            "INSERT INTO info_submissions(family_id,user_id,role,display_name,age,preferences,drinks,remark) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                b.family_id, b.user_id, b.role, b.display_name, b.age,
                json.dumps(b.preferences, ensure_ascii=False),
                b.drinks,
                b.remark,
            ),
        )
    return {"ok": True}
def _llm_generate_plan(payload: dict):
    """
    调用 LLM 并返回 (plan_obj, raw_text)。
    - 强制 JSON 输出（response_format=json_object）
    - 记录 payload 与 raw 到日志/内存缓冲
    - 解析失败时可选把 raw 透传到 500（DEBUG_LLM=true）
    """
    def _call_llm(user_payload, extra_sys=None):
        sys_prompt = PROMPT_SYSTEM if not extra_sys else f"{PROMPT_SYSTEM}\n{extra_sys}"
        
        # 检查是否是regenerate请求（包含original_plan和user_feedback）
        if "original_plan" in user_payload and "user_feedback" in user_payload:
            # 这是regenerate请求，使用特殊的prompt
            sys_prompt = (
                "你是一个家庭晚餐计划助理。用户对之前的计划不满意，提供了反馈。"
                "请根据用户的反馈对原计划进行修改。"
                "严格输出英文版JSON，结构与原计划一致，不要输出多余文本。"
            )
            if extra_sys:
                sys_prompt = f"{sys_prompt}\n{extra_sys}"
            
            # 构建特殊的用户消息
            user_message = f"""
原计划：
{json.dumps(user_payload['original_plan'], ensure_ascii=False, indent=2)}

用户反馈：
{user_payload['user_feedback']}

请根据用户的反馈对原计划进行修改。只针对用户提出的具体反馈进行修改，不要做额外的修改。
保持原有的结构和格式，确保输出合法的JSON。
"""
        else:
            # 正常的生成请求
            user_message = json.dumps(user_payload, ensure_ascii=False)
        
        completion = openai.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            response_format={"type": "json_object"},  # ★ 强制 JSON
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return completion.choices[0].message.content or ""

    logger.info("[LLM] request payload=%s", json.dumps(payload, ensure_ascii=False))

    # 第一次调用
    content = _call_llm(payload)
    LLM_DEBUG.append({"ts": datetime.datetime.now().isoformat(), "payload": payload, "raw": content})
    logger.info("[LLM] raw=%s", content)

    plan_obj = try_parse_plan(content)

    # 解析失败 → 明确再要求一次严格 JSON
    if not plan_obj or "dishes" not in plan_obj:
        content = _call_llm(
            payload,
            extra_sys=(
                "务必严格输出合法 JSON（UTF-8，无注释、无多余文本、无代码块围栏），"
                "根对象必须包含键 'dishes' 与 'meta'，其中 meta.roles **必须为非空数组**，"
                "且为每位成员给出 tasks；is_chef=true 的成员 tasks 中必须含\"炒菜\"。"
                "分工必须用英文！"
            )
        )
        LLM_DEBUG.append({"ts": datetime.datetime.now().isoformat(), "payload": payload, "raw": content})
        logger.info("[LLM][retry] raw=%s", content)
        plan_obj = try_parse_plan(content)

    if not plan_obj or "dishes" not in plan_obj:
        logger.error("[LLM] parse failed. raw=%s", content)
        if DEBUG_LLM:
            # 本地调试时直接把原始返回透出（前端会收到 500）
            raise HTTPException(status_code=500, detail=f"Plan JSON parse failed. raw: {content}")
        raise HTTPException(status_code=500, detail="Plan JSON parse failed")

    # 统一结构做兜底（保证前端不崩）
    plan_obj = normalize_plan(plan_obj)

    # 校验视频链接不可用 → 标 unverified（可选）
    for d in plan_obj.get("dishes", []):
        url = d.get("video_url")
        if url and not url_exists(url):
            d["video_status"] = "unverified"

    return plan_obj, content



@app.post("/api/plan/generate")
def plan_generate(req: GenerateRequest, debug: int = Query(0)):
    # 1) 校验“今日主要负责人”
    row = db_query(
        "SELECT user_id FROM family_memberships WHERE family_id=%s AND is_primary_today=TRUE",
        (req.family_id,)
    )
    if not row:
        raise HTTPException(403, "No main person in charge is set")
    primary_user_id = row[0]["user_id"]   # ✅ 明确负责人ID

    # 2) 聚合同一开饭时间的所有提交（记得规范化时间）
    _, norm = parse_dinnertime_str(req.dinner_time)
    subs = db_query(
        "SELECT * FROM info_submissions WHERE family_id=%s AND dinner_time=%s ORDER BY id ASC",
        (req.family_id, norm),
    )
    if not subs:
        raise HTTPException(400, "At that time,no submission")

    # 3) 家庭名
    fam = db_query("SELECT family_name FROM families WHERE family_id=%s", (req.family_id,))
    family_name = fam[0]["family_name"] if fam else ""

    # 4) 组装 people（这里用 primary_user_id）
    headcount = req.headcount or len(subs)
    roles_lines = []
    for s in subs:
        prefs = s["preferences"]
        if not isinstance(prefs, dict):
            try:
                prefs = json.loads(prefs or "{}")
            except Exception:
                prefs = {}
        disp = (s.get("display_name") or "").strip()
        roles_lines.append({
            "person_role": s["role"],
            "age_group": None,
            "is_chef": bool(prefs.get("is_chef", False)),
            "is_primary": (s["user_id"] == primary_user_id),  # ✅ 这里不再用 primary[0]["user_id"]
            "preferences": prefs,
            "display_name": disp,
            "remark": (str(s.get("remark") or "").strip()),
            "drinks": (str(s.get("drinks") or "").strip()),
        })


    # 5) 目标菜数 target = clamp(N−1, 2..8)
    target = max(2, min(8, int(headcount) - 1 if headcount else len(subs) - 1))

    # 6) 加严规则：恰好 target 道 + 备注优先 + 必须合法 JSON
    rules_hard = (
        PROMPT_RULES
        + f" 5) 必须**恰好**输出 {target} 道菜；"
        + " 6) 如备注中出现具体菜名/强烈愿望，在不与禁忌冲突时必须包含；"
        + " 7) 返回必须是合法 JSON（UTF-8，无围栏、无额外注释/markdown）。"
    )

    # 7) 组织用户内容
    user_content = {
        "date": req.dinner_time[:10],
        "headcount": headcount,
        "people": roles_lines,
        "schema": PLAN_SCHEMA_EXAMPLE,
        "rules": rules_hard,
        "feedback": req.feedback or "",
    }

    # 8) 第一次生成
    try:
        plan_obj_raw, content = _llm_generate_plan(user_content)
    except Exception as e:
        raise HTTPException(500, f"LLM error: {e}")

    # 9) 归一化为 LAN_SCHEMA
    lan_plan = coerce_to_lan_schema(
        plan_obj_raw,
        dinner_time=req.dinner_time,
        headcount=headcount,
        family_info={"family_id": req.family_id, "family_name": family_name},
    )

    # 10) 数量不足 → 加严重试一次
    if len(lan_plan.get("dishes", [])) < target:
        user_content["rules"] += (
            "（上次数量不足；请补齐缺少的菜，使总数恰好达到目标数量；"
            "不要重复已给出的菜名；务必仅返回 JSON 对象。）"
        )
        plan_obj_raw2, content2 = _llm_generate_plan(user_content)
        lan_plan = coerce_to_lan_schema(
            plan_obj_raw2,
            dinner_time=req.dinner_time,
            headcount=headcount,
            family_info={"family_id": req.family_id, "family_name": family_name},
        )
        content = content2

    # 11) 渲染 + 入库（与你现有写法一致）
    html = render_plan_html(lan_plan)
    plan_code = f"{req.family_id}_{int(datetime.datetime.now().timestamp())}"

    db_execute(
        "INSERT INTO plans(plan_code,family_id,dinner_time,submission_cnt,plan_json,plan_html,model_raw) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (plan_code, req.family_id, req.dinner_time, len(subs),
         json.dumps(lan_plan, ensure_ascii=False), html, content),
    )

    # 12) 返回“报告内容本身”（不返 id/plan_code，如你之前的要求）
    return {
        "ok": True,
        "plan_json": lan_plan,
        "plan_html": html,
        "family_id": req.family_id,
        "dinner_time": req.dinner_time,
        "submission_cnt": len(subs),
        **({"model_raw": (content or "")[:4000]} if debug else {}),
    }

@app.get("/api/plans")
def list_plans(family_id: str):
    return db_query(
        "SELECT id,plan_code,created_at,dinner_time FROM plans WHERE family_id=%s ORDER BY id DESC", (family_id,)
    )

@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: int):
    r = db_query("SELECT plan_json,plan_html FROM plans WHERE id=%s", (plan_id,))
    if not r:
        raise HTTPException(404, "plan not exist")
    return {"plan_json": json.loads(r[0]["plan_json"]), "plan_html": r[0]["plan_html"]}

@app.get("/api/cooking-schedule/{plan_id}")
def get_cooking_schedule(plan_id: int):
    """获取做饭顺序安排"""
    try:
        r = db_query("SELECT plan_json FROM plans WHERE id=%s", (plan_id,))
        if not r:
            raise HTTPException(404, "plan not exist")
        
        plan_data = json.loads(r[0]["plan_json"])
        schedule = generate_cooking_schedule(plan_data)
        return schedule
    except Exception as e:
        print(f"Error in cooking schedule: {e}")
        raise HTTPException(500, f"Error generating cooking schedule: {str(e)}")

@app.delete("/api/plan/{plan_id}")
def delete_plan(plan_id: int):
    db_execute("DELETE FROM plans WHERE id=%s", (plan_id,))
    return {"ok": True}


@app.post("/api/plan/{plan_id}/regenerate")
async def regenerate(plan_id: int, request: Request):
    # 兼容多种传参：JSON body {feedback}, 或查询参数 ?feedback=
    feedback_text = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            feedback_text = body.get("feedback")
    except Exception:
        pass
    if not feedback_text:
        feedback_text = request.query_params.get("feedback")
    if not feedback_text:
        raise HTTPException(400, "need feedback")
    
    # 获取原报告信息
    old_plan = db_query("SELECT * FROM plans WHERE id=%s", (plan_id,))
    if not old_plan:
        raise HTTPException(404, "plan not exist")
    
    old_plan_data = old_plan[0]
    family_id = old_plan_data["family_id"]
    dinner_time = old_plan_data["dinner_time"].strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 将feedback存储到feedbacks表
    feedback_id = db_execute(
        "INSERT INTO feedbacks (plan_id, content) VALUES (%s, %s)",
        (plan_id, feedback_text)
    )
    
    # 2. 获取原报告的JSON数据
    original_plan_json = json.loads(old_plan_data["plan_json"])
    
    # 3. 创建新的GPT调用，将原报告和feedback一起发送
    try:
        # 构建发送给GPT的payload，包含原报告和用户反馈
        gpt_payload = {
            "original_plan": original_plan_json,
            "user_feedback": feedback_text,
            "instructions": "请根据用户的反馈对原报告进行修改。只针对用户提出的具体反馈进行修改，不要做额外的修改。保持原有的结构和格式。"
        }
        
        # 调用GPT API
        plan_obj_raw, content = _llm_generate_plan(gpt_payload)
        
        # 4. 获取家庭信息
        fam = db_query("SELECT family_name FROM families WHERE family_id=%s", (family_id,))
        family_name = fam[0]["family_name"] if fam else ""
        
        # 5. 归一化新生成的计划
        headcount = original_plan_json.get("meta", {}).get("headcount", 3)
        lan_plan = coerce_to_lan_schema(
            plan_obj_raw,
            dinner_time=dinner_time,
            headcount=headcount,
            family_info={"family_id": family_id, "family_name": family_name},
        )
        
        # 6. 渲染HTML
        html = render_plan_html(lan_plan)
        plan_code = f"{family_id}_{int(datetime.datetime.now().timestamp())}"
        
        # 7. 创建新的报告记录（不覆盖原报告）
        new_plan_id = db_execute(
            "INSERT INTO plans(plan_code,family_id,dinner_time,submission_cnt,plan_json,plan_html,model_raw) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (plan_code, family_id, dinner_time, old_plan_data["submission_cnt"],
             json.dumps(lan_plan, ensure_ascii=False), html, content),
        )
        
        return {
            "ok": True,
            "message": "New plan generated based on your feedback.",
            "plan_id": new_plan_id,
            "plan_json": lan_plan,
            "plan_html": html,
            "family_id": family_id,
            "dinner_time": dinner_time,
            "feedback_id": feedback_id,
            "original_plan_id": plan_id
        }
        
    except Exception as e:
        logger.error(f"Regenerate error: {e}")
        raise HTTPException(500, f"Regenerate error: {e}")

# ------------------------- 兼容你旧有的最小接口 -------------------------
@app.post("/api/submissions")
def create_submission(sub: SubmissionIn):
    """兼容旧版：按‘当日’收集单人提交（仍可使用）。"""
    dining_date = sub.dining_date or datetime.date.today().isoformat()
    prefs_json = json.dumps(sub.preferences.model_dump(), ensure_ascii=False)
    sql = (
        "INSERT INTO submissions (dining_date, person_role, is_primary, headcount, is_chef, age_group, preferences) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)"
    )
    last_id = db_execute(
        sql,
        (dining_date, sub.person_role, sub.is_primary, sub.headcount, sub.is_chef, sub.age_group or "", prefs_json),
    )
    return {"ok": True, "id": last_id}

class PlanRequest(BaseModel):
    date: Optional[str] = None
    feedback: Optional[str] = None

@app.post("/api/generate_plan")
def generate_plan(req: PlanRequest):
    """兼容旧版：基于 submissions（以日期为维度）生成计划。"""
    date_str = req.date or datetime.date.today().isoformat()
    subs = db_query("SELECT * FROM submissions WHERE dining_date = %s ORDER BY id ASC", (date_str,))
    if not subs:
        raise HTTPException(status_code=400, detail="No submissions for the date")

    headcount = max([s.get("headcount") or 0 for s in subs] + [len(subs)])
    roles_lines = []
    for s in subs:
        prefs = s["preferences"] if isinstance(s["preferences"], dict) else json.loads(s["preferences"])
        roles_lines.append(
            {
                "person_role": s["person_role"],
                "age_group": s.get("age_group"),
                "is_chef": bool(s["is_chef"]),
                "is_primary": bool(s["is_primary"]),
                "preferences": prefs,
            }
        )

    user_content = {
        "date": date_str,
        "headcount": headcount,
        "people": roles_lines,
        "schema": PLAN_SCHEMA_EXAMPLE,
        "rules": PROMPT_RULES,
        "feedback": req.feedback or "",
    }

    try:
        plan_obj, content = _llm_generate_plan(user_content)
    except Exception as e:
        raise HTTPException(500, f"LLM error: {e}")

    html = render_plan_html(plan_obj)
    plan_id = db_execute(
        "INSERT INTO plans (plan_code,family_id,dinner_time,submission_cnt,plan_json,plan_html,model_raw) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (f"legacy_{int(datetime.datetime.now().timestamp())}", "", date_str + " 18:00:00",
         len(subs), json.dumps(plan_obj, ensure_ascii=False), html, content),
    )
    return {"ok": True, "plan_id": plan_id, "plan_json": plan_obj, "plan_html": html}

@app.get("/api/plan/latest")
def get_latest_plan(date: Optional[str] = None):
    date_str = date or datetime.date.today().isoformat()
    rows = db_query("SELECT * FROM plans WHERE DATE(dinner_time)=%s ORDER BY id DESC LIMIT 1", (date_str,))
    if not rows:
        raise HTTPException(status_code=404, detail="No plan for date")
    r = rows[0]
    return {
        "plan_id": r["id"],
        "plan_json": json.loads(r["plan_json"]),
        "plan_html": r["plan_html"],
        "created_at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime.datetime) else str(r["created_at"]),
    }

# ====== NEW: Submissions query APIs ======
@app.get("/api/submissions/my")
def list_my_submissions(family_id: str, user_id: str, limit: int = 20):
    rows = db_query(
        """
        SELECT id,family_id,user_id,role,display_name,age,preferences,remark
        FROM info_submissions
        WHERE family_id=%s AND user_id=%s
        ORDER BY id DESC
        LIMIT %s
        """,
        (family_id, user_id, int(max(1, min(limit, 100))))
    )
    # 解析 preferences JSON
    for r in rows:
        prefs = r.get("preferences")
        if not isinstance(prefs, dict):
            try: r["preferences"] = json.loads(prefs or "{}")
            except Exception: r["preferences"] = {}
        if isinstance(r.get("created_at"), datetime.datetime):
            r["created_at"] = r["created_at"].isoformat()
    return rows

@app.get("/api/submissions/family/meals")
def list_family_meals(family_id: str, date: Optional[str] = None):
    # 按某日聚合餐次（±1小时规则由生成时使用，这里按 dinner_time 精确）
    if not date:
        date = datetime.date.today().isoformat()
    rows = db_query(
        """
        SELECT COUNT(*) cnt
        FROM info_submissions
        WHERE family_id=%s
        """,
        (family_id,)
    )
    return rows

@app.get("/api/submissions/family/at")
def list_family_submissions_at(family_id: str, dinner_time: str):
    # 规范化输入时间，返回该餐次所有成员的提交
    _, norm = parse_dinnertime_str(dinner_time)
    rows = db_query(
        """
        SELECT user_id,role,display_name,age,preferences,remark
        FROM info_submissions
        WHERE family_id=%s
        ORDER BY id ASC
        """,
        (family_id,)
    )
    for r in rows:
        prefs = r.get("preferences")
        if not isinstance(prefs, dict):
            try: r["preferences"] = json.loads(prefs or "{}")
            except Exception: r["preferences"] = {}
        if isinstance(r.get("created_at"), datetime.datetime):
            r["created_at"] = r["created_at"].isoformat()
    return rows

@app.post("/api/plan/{plan_id}/feedback")
def add_feedback(plan_id: int, req: PlanRequest):
    if not req.feedback:
        raise HTTPException(status_code=400, detail="feedback is required")
    db_execute("INSERT INTO feedbacks (plan_id, content) VALUES (%s,%s)", (plan_id, req.feedback))
    return {"ok": True}

####新修改
# === NEW: time parser ===
def parse_dinnertime_str(s: str) -> tuple[datetime.datetime, str]:
    """
    接受 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD,TT:TT' 等，返回 (dt, 'YYYY-MM-DD HH:MM:00')
    """
    s = (s or "").strip()
    s = s.replace("，", ",").replace("T", " ").replace("：", ":")
    # 允许 'YYYY-MM-DD,HH:MM'
    if "," in s and " " not in s:
        s = s.replace(",", " ")
    # 允许无秒
    fmt_candidates = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    dt = None
    for fmt in fmt_candidates:
        try:
            dt = datetime.datetime.strptime(s, fmt)
            break
        except Exception:
            continue
    if dt is None:
        # 兜底：仅日期 → 18:00
        try:
            d = datetime.datetime.strptime(s[:10], "%Y-%m-%d").date()
            dt = datetime.datetime.combine(d, datetime.time(18, 0, 0))
        except Exception:
            dt = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
    norm = dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt, norm

def meal_window(dt: datetime.datetime) -> tuple[str, str]:
    """给定一个 datetime，返回 [start, end] 的字符串（含端点），用于 SQL BETWEEN。
       规则：±1 小时属于同一顿饭。"""
    start = (dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    end   = (dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return start, end

def get_today_primary_user_id(family_id: str) -> Optional[str]:
    r = db_query(
        "SELECT user_id FROM family_memberships WHERE family_id=%s AND is_primary_today=TRUE",
        (family_id,)
    )
    return (r[0]["user_id"] if r else None)

# === NEW: robust list helper ===
def _as_list(x):
    return x if isinstance(x, list) else ([] if x is None else [x])

# === NEW: coerce to your LAN schema ===
def coerce_to_lan_schema(plan: dict, dinner_time: str, headcount: int, family_info: dict | None = None) -> dict:
    """
    把任意结构 plan 变成你要求的 LAN_SCHEMA：
    {
      "meta": {
        "Time": "YYYY-MM-DD,TT:TT",
        "headcount": N,
        "roles": [{"person_role": "...", "is_chef": false, "tasks": [...], "display_name": "..."}, ...],
        "family_id": "...", "family_name": "..."
      },
      "dishes": [{ name, category, ingredients:[{name,amount}], steps:[], image_url, video_url }, ...]
    }
    """
    plan = plan or {}
    meta_in = plan.get("meta") or {}
    dishes_in = plan.get("dishes") or []

    # 时间统一：保存为 "YYYY-MM-DD HH:MM:SS"，并额外提供 "YYYY-MM-DD,TT:TT" 形态给前端
    _, dinner_time_norm = parse_dinnertime_str(dinner_time)
    time_for_meta = dinner_time_norm[:16].replace(" ", ",")  # "YYYY-MM-DD,HH:MM"

    # roles 兼容不同来源键
    roles_in = meta_in.get("roles") or plan.get("people") or []
    fixed_roles = []
    for r in _as_list(roles_in):
        if not isinstance(r, dict):
            continue
        fixed_roles.append({
            "person_role": r.get("person_role") or r.get("role"),
            "is_chef": bool(r.get("is_chef", False)),
            "tasks": _as_list(r.get("tasks") or []),
            "display_name": r.get("display_name") or r.get("name") or "",
            "is_primary": bool(r.get("is_primary", False)),
            "age_group": r.get("age_group"),
        })

    # dishes 归一
    fixed_dishes = []
    for d in _as_list(dishes_in):
        if not isinstance(d, dict):
            continue
        ingredients = []
        for i in _as_list(d.get("ingredients")):
            if isinstance(i, dict) and i.get("name"):
                ingredients.append({"name": i.get("name", ""), "amount": i.get("amount", "")})
            elif isinstance(i, str) and i.strip():
                # 如有纯字符串，尝试“食材 数量”拆分
                parts = i.split()
                ingredients.append({"name": parts[0], "amount": " ".join(parts[1:]) if len(parts) > 1 else ""})

        fixed_dishes.append({
            "name": d.get("name") or d.get("dish") or "Unnamed meal",
            "category": d.get("category") or "Hot meal",
            "ingredients": ingredients,
            "steps": [str(s) for s in _as_list(d.get("steps"))],
            "image_url": d.get("image_url") or "",
            "video_url": d.get("video_url") or "",
        })

    fam_id = (family_info or {}).get("family_id") or ""
    fam_name = (family_info or {}).get("family_name") or ""

    # --- drinks 归一 ---
    drinks_in = plan.get("drinks") or []
    fixed_drinks = []
    for d in _as_list(drinks_in):
        if not isinstance(d, dict):
            continue
        fixed_drinks.append({
            "name": d.get("name") or "Unknown drink",
            "type": d.get("type") or "Cold",
            "serving": d.get("serving") or "1 serving per person",
        })

    lan = {
        "meta": {
            "Time": time_for_meta,              # 形如 "2025-08-20,16:00"
            "headcount": int(headcount) if headcount else len(fixed_roles) or None,
            "roles": fixed_roles,
            "family_id": fam_id,
            "family_name": fam_name,
        },
        "dishes": fixed_dishes,
        "drinks": fixed_drinks,
    }

    # 可选：视频有效性标记
    for d in lan["dishes"]:
        url = d.get("video_url")
        if url and not url_exists(url):
            d["video_status"] = "unverified"
    return lan


class PlanIngest(BaseModel):
    family_id: str
    dinner_time: str              # "YYYY-MM-DD HH:00"
    headcount: Optional[int] = None
    payload: dict                 # 外部API的原始结果(JSON)

@app.post("/api/plan/ingest")
def ingest_external_plan(b: PlanIngest):
    fam = db_query("SELECT family_name FROM families WHERE family_id=%s", (b.family_id,))
    family_name = fam[0]["family_name"] if fam else ""
    subs_cnt = db_query(
        "SELECT COUNT(*) c FROM info_submissions WHERE family_id=%s AND dinner_time=%s",
        (b.family_id, b.dinner_time)
    )[0]["c"]

    lan_plan = coerce_to_lan_schema(
        b.payload,
        dinner_time=b.dinner_time,
        headcount=b.headcount or subs_cnt,
        family_info={"family_id": b.family_id, "family_name": family_name},
    )
    html = render_plan_html(lan_plan)
    plan_code = f"{b.family_id}_{int(datetime.datetime.now().timestamp())}"

    plan_id = db_execute(
        "INSERT INTO plans(plan_code,family_id,dinner_time,submission_cnt,plan_json,plan_html,model_raw) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (plan_code, b.family_id, b.dinner_time, subs_cnt,
         json.dumps(lan_plan, ensure_ascii=False), html, json.dumps(b.payload, ensure_ascii=False)),
    )
    return {
        "ok": True,
        "plan_json": lan_plan,
        "plan_html": html,
        "family_id": b.family_id,
        "dinner_time": b.dinner_time,
        "submission_cnt": subs_cnt,
    }


###新修改，角色名单：
# ==== 角色白名单（NEW） ====
ALLOWED_ROLES = {"Father", "Mother", "Grandpa", "Grandma", "Son", "Daughter", "Friend"}

def _map_role(role: str) -> str:
    r = (role or "").strip()
    return r if r in ALLOWED_ROLES else "Friend"

# === NEW: 做饭顺序生成算法 ===
def generate_cooking_schedule(plan_data: dict) -> dict:
    """根据餐食计划生成做饭顺序安排"""
    dishes = plan_data.get("dishes", [])
    meta = plan_data.get("meta", {})
    dinner_time = meta.get("Time", "18:00")
    
    # 解析用餐时间
    try:
        if "," in dinner_time:
            time_part = dinner_time.split(",")[1]
        else:
            time_part = dinner_time.split(" ")[1] if " " in dinner_time else "18:00"
        
        hour, minute = map(int, time_part.split(":"))
        target_time = datetime.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except:
        target_time = datetime.datetime.now().replace(hour=18, minute=0, second=0, microsecond=0)
    
    # 分析每道菜的制作时间和工具需求
    dish_analysis = []
    for dish in dishes:
        analysis = analyze_dish_cooking(dish)
        dish_analysis.append(analysis)
    
    # 生成时间安排
    timeline = generate_timeline(dish_analysis, target_time)
    
    # 生成工具需求
    tools = generate_tool_requirements(dish_analysis)
    
    return {
        "timeline": timeline,
        "tools": tools,
        "dishes": [{"name": d["name"], "category": d["category"], "cookingTime": d["cooking_time"]} for d in dish_analysis],
        "target_time": target_time.isoformat(),
        "total_cooking_time": max([d["cooking_time"] for d in dish_analysis]) if dish_analysis else 0
    }

def analyze_dish_cooking(dish: dict) -> dict:
    """分析单道菜的制作需求"""
    name = dish.get("name", "Unknown Dish")
    category = dish.get("category", "Main Dish")
    ingredients = dish.get("ingredients", [])
    steps = dish.get("steps", [])
    
    # 根据菜品类型和步骤估算制作时间
    cooking_time = estimate_cooking_time(name, category, ingredients, steps)
    
    # 估算保温时间
    keep_warm_time = estimate_keep_warm_time(name, category, ingredients, steps)
    
    # 分析所需工具
    tools = analyze_required_tools(name, category, ingredients, steps)
    
    # 分析制作方法
    cooking_method = analyze_cooking_method(name, category, steps)
    
    return {
        "name": name,
        "category": category,
        "cooking_time": cooking_time,
        "keep_warm_time": keep_warm_time,
        "tools": tools,
        "cooking_method": cooking_method,
        "ingredients": ingredients,
        "steps": steps
    }

def estimate_cooking_time(name: str, category: str, ingredients: list, steps: list) -> int:
    """估算制作时间（分钟）"""
    base_time = 15  # 基础时间
    
    # 根据菜品类型调整
    if "汤" in category or "Soup" in category:
        base_time = 30
    elif "前菜" in category or "Appetizer" in category:
        base_time = 10
    elif "热菜" in category or "Main" in category:
        base_time = 20
    
    # 根据食材复杂度调整
    meat_ingredients = ["chicken", "beef", "pork", "fish", "鸡", "牛", "猪", "鱼"]
    has_meat = any(any(meat in str(ingredient).lower() for meat in meat_ingredients) for ingredient in ingredients)
    if has_meat:
        base_time += 10
    
    # 根据步骤数量调整
    step_count = len(steps)
    if step_count > 5:
        base_time += 5
    
    return min(base_time, 60)  # 最多60分钟

def estimate_keep_warm_time(name: str, category: str, ingredients: list, steps: list) -> int:
    """估算保温时间（分钟）- 菜品做好后能保持热度的最长时间"""
    # 根据菜品类型估算保温时间
    if "汤" in category or "Soup" in category:
        return 15  # 汤类保温时间较短
    elif "前菜" in category or "Appetizer" in category:
        return 5   # 前菜保温时间最短
    elif "热菜" in category or "Main" in category:
        return 20  # 热菜保温时间较长
    elif "沙拉" in category or "Salad" in category or "Dessert" in category:
        return 0   # 冷菜不需要保温
    else:
        return 10  # 默认保温时间

def analyze_required_tools(name: str, category: str, ingredients: list, steps: list) -> list:
    """分析所需工具 - 根据实际制作方法智能分配"""
    tools = []
    
    # 根据制作方法智能分配工具
    has_stir_fry = any("炒" in step or "stir-fry" in step.lower() for step in steps)
    has_steam = any("蒸" in step or "steam" in step.lower() for step in steps)
    has_boil = any("煮" in step or "boil" in step.lower() for step in steps)
    has_bake = any("烤" in step or "bake" in step.lower() for step in steps)
    has_cut = any("切" in step or "cut" in step.lower() for step in steps)
    has_mix = any("拌" in step or "mix" in step.lower() for step in steps)
    
    # 根据制作方法添加相应工具
    if has_stir_fry:
        tools.extend([
            {"name": "Wok", "icon": "🍳", "quantity": "1 piece"},
            {"name": "Spatula", "icon": "🥄", "quantity": "1 piece"}
        ])
    
    if has_steam:
        tools.append({"name": "Steamer", "icon": "🥘", "quantity": "1 piece"})
    
    if has_boil:
        tools.append({"name": "Soup Pot", "icon": "🍲", "quantity": "1 piece"})
    
    if has_bake:
        tools.append({"name": "Oven", "icon": "🔥", "quantity": "1 piece"})
    
    # 根据是否需要切菜添加工具
    if has_cut or any(ingredient for ingredient in ingredients if any(meat in str(ingredient).lower() for meat in ["chicken", "beef", "pork", "fish", "鸡", "牛", "猪", "鱼", "肉"])):
        tools.extend([
            {"name": "Kitchen Knife", "icon": "🔪", "quantity": "1 piece"},
            {"name": "Cutting Board", "icon": "🪵", "quantity": "1 piece"}
        ])
    
    # 根据食材添加特殊工具
    if any("土豆" in str(ingredient) or "potato" in str(ingredient).lower() for ingredient in ingredients):
        tools.append({"name": "Peeler", "icon": "🔧", "quantity": "1 piece"})
    
    # 根据菜品类型添加工具
    if "汤" in category or "Soup" in category:
        if not has_boil:  # 如果还没有汤锅
            tools.append({"name": "Soup Pot", "icon": "🍲", "quantity": "1 piece"})
    
    if "沙拉" in category or "Salad" in category or "Dessert" in category:
        if not has_cut:  # 如果还没有切菜工具
            tools.extend([
                {"name": "Kitchen Knife", "icon": "🔪", "quantity": "1 piece"},
                {"name": "Cutting Board", "icon": "🪵", "quantity": "1 piece"}
            ])
        if has_mix:
            tools.append({"name": "Mixing Bowl", "icon": "🥣", "quantity": "1 piece"})
    
    # 如果没有检测到任何制作方法，给一些基础工具
    if not tools and len(steps) > 0:
        tools.extend([
            {"name": "Kitchen Knife", "icon": "🔪", "quantity": "1 piece"},
            {"name": "Cutting Board", "icon": "🪵", "quantity": "1 piece"}
        ])
    
    # 去重
    seen = set()
    unique_tools = []
    for tool in tools:
        key = tool["name"]
        if key not in seen:
            seen.add(key)
            unique_tools.append(tool)
    
    return unique_tools

def analyze_cooking_method(name: str, category: str, steps: list) -> str:
    """分析主要制作方法"""
    methods = []
    
    if any("炒" in step or "stir-fry" in step.lower() for step in steps):
        methods.append("Stir-fry")
    if any("蒸" in step or "steam" in step.lower() for step in steps):
        methods.append("Steam")
    if any("煮" in step or "boil" in step.lower() for step in steps):
        methods.append("Boil")
    if any("烤" in step or "bake" in step.lower() for step in steps):
        methods.append("Bake")
    
    return ", ".join(methods) if methods else "Stir-fry"

def generate_timeline(dish_analysis: list, target_time: datetime.datetime) -> list:
    """生成时间安排 - 所有菜品在用餐时间前完成，不使用保温"""
    timeline = []
    
    if not dish_analysis:
        return timeline
    
    # 按制作时间排序（时间长的先开始，这样可以更早开始准备）
    sorted_dishes = sorted(dish_analysis, key=lambda x: x["cooking_time"], reverse=True)
    
    # 计算总制作时间（考虑并行制作）
    max_parallel = min(3, len(sorted_dishes))  # 最多同时制作3道菜
    
    if len(sorted_dishes) <= max_parallel:
        # 如果菜品数量少，可以完全并行制作
        total_cooking_time = max([d["cooking_time"] for d in sorted_dishes])
    else:
        # 如果菜品多，需要分批制作
        first_batch = sorted_dishes[:max_parallel]
        first_batch_time = max([d["cooking_time"] for d in first_batch])
        
        remaining_dishes = sorted_dishes[max_parallel:]
        if remaining_dishes:
            second_batch_time = max([d["cooking_time"] for d in remaining_dishes])
            total_cooking_time = first_batch_time + second_batch_time + 10  # 10分钟间隔
        else:
            total_cooking_time = first_batch_time
    
    # 计算开始时间：让所有菜品在用餐前5-10分钟完成
    # 这样既不会太早（菜凉），也不会太晚（来不及）
    finish_time = target_time - datetime.timedelta(minutes=5)  # 提前5分钟完成
    start_time = finish_time - datetime.timedelta(minutes=total_cooking_time)
    
    # 添加准备阶段（在开始制作前15分钟）
    prep_time = start_time - datetime.timedelta(minutes=15)
    timeline.append({
        "time": prep_time.strftime("%H:%M"),
        "scheduledTime": prep_time.isoformat(),
        "title": "Preparation Phase",
        "description": "Wash ingredients, prepare tools, preheat equipment",
        "tools": ["Kitchen Knife", "Cutting Board", "Vegetable Washing Basin"]
    })
    
    # 为每道菜安排时间 - 确保在finish_time完成
    if len(sorted_dishes) <= max_parallel:
        # 所有菜同时开始制作，在finish_time完成
        # 计算开始时间：finish_time - 最长制作时间
        max_cooking_time = max([d["cooking_time"] for d in sorted_dishes])
        actual_start_time = finish_time - datetime.timedelta(minutes=max_cooking_time)
        
        for dish in sorted_dishes:
            timeline.append({
                "time": actual_start_time.strftime("%H:%M"),
                "scheduledTime": actual_start_time.isoformat(),
                "title": f"Cook {dish['name']}",
                "description": f"{dish['cooking_method']} - {dish['category']}",
                "tools": [tool["name"] for tool in dish["tools"]]
            })
    else:
        # 分批制作 - 确保最后一批在finish_time完成
        first_batch = sorted_dishes[:max_parallel]
        remaining_dishes = sorted_dishes[max_parallel:]
        
        if remaining_dishes:
            # 计算时间安排
            first_batch_time = max([d["cooking_time"] for d in first_batch])
            second_batch_time = max([d["cooking_time"] for d in remaining_dishes])
            
            # 第二批在finish_time完成，所以第二批开始时间 = finish_time - second_batch_time
            second_batch_start = finish_time - datetime.timedelta(minutes=second_batch_time)
            
            # 第一批在第二批开始前完成，所以第一批开始时间 = second_batch_start - first_batch_time - 5分钟间隔
            first_batch_start = second_batch_start - datetime.timedelta(minutes=first_batch_time + 5)
            
            # 第一批
            for dish in first_batch:
                timeline.append({
                    "time": first_batch_start.strftime("%H:%M"),
                    "scheduledTime": first_batch_start.isoformat(),
                    "title": f"Cook {dish['name']}",
                    "description": f"{dish['cooking_method']} - {dish['category']}",
                    "tools": [tool["name"] for tool in dish["tools"]]
                })
            
            # 第二批
            for dish in remaining_dishes:
                timeline.append({
                    "time": second_batch_start.strftime("%H:%M"),
                    "scheduledTime": second_batch_start.isoformat(),
                    "title": f"Cook {dish['name']}",
                    "description": f"{dish['cooking_method']} - {dish['category']}",
                    "tools": [tool["name"] for tool in dish["tools"]]
                })
        else:
            # 只有一批，在finish_time完成
            for dish in first_batch:
                timeline.append({
                    "time": start_time.strftime("%H:%M"),
                    "scheduledTime": start_time.isoformat(),
                    "title": f"Cook {dish['name']}",
                    "description": f"{dish['cooking_method']} - {dish['category']}",
                    "tools": [tool["name"] for tool in dish["tools"]]
                })
    
    # 添加最后装盘时间
    timeline.append({
        "time": target_time.strftime("%H:%M"),
        "scheduledTime": target_time.isoformat(),
        "title": "Plate and Serve",
        "description": "All dishes completed, ready to serve",
        "tools": ["Plates", "Tableware"]
    })
    
    return timeline

def generate_tool_requirements(dish_analysis: list) -> list:
    """生成工具需求汇总 - 考虑实际厨房使用情况"""
    all_tools = []
    for dish in dish_analysis:
        all_tools.extend(dish["tools"])
    
    # 统计工具数量，考虑实际使用情况
    tool_count = {}
    for tool in all_tools:
        name = tool["name"]
        if name in tool_count:
            # 对于共享工具，不增加数量
            continue
        else:
            tool_count[name] = tool
    
    # 根据实际厨房使用情况调整工具数量
    final_tools = []
    for tool in tool_count.values():
        name = tool["name"]
        quantity = tool["quantity"]
        
        # 共享工具：一个厨房通常只需要1-2个
        if name in ["菜刀", "砧板"]:
            final_tools.append({
                "name": "Kitchen Knife" if name == "菜刀" else "Cutting Board",
                "icon": tool["icon"],
                "quantity": "1-2 pieces" if name == "砧板" else "1-2 pieces"
            })
        # 炉具：根据同时制作的菜品数量
        elif name in ["炒锅", "汤锅", "蒸锅"]:
            # 统计需要这种炉具的菜品数量
            need_count = sum(1 for dish in dish_analysis if any(t["name"] == name for t in dish["tools"]))
            tool_name_map = {"炒锅": "Wok", "汤锅": "Soup Pot", "蒸锅": "Steamer"}
            if need_count > 1:
                final_tools.append({
                    "name": tool_name_map.get(name, name),
                    "icon": tool["icon"],
                    "quantity": f"{min(need_count, 2)} pieces"  # 最多2个
                })
            else:
                final_tools.append({
                    "name": tool_name_map.get(name, name),
                    "icon": tool["icon"],
                    "quantity": tool["quantity"]
                })
        # 其他工具：保持原数量
        else:
            final_tools.append(tool)
    
    return final_tools


# ------------------------- Run (tips) -------------------------
# ==================== 新的简化API ====================

class FamilyCreateRequest(BaseModel):
    family_name: str

class FamilyJoinRequest(BaseModel):
    family_code: str

class PreferenceSubmitRequest(BaseModel):
    family_id: str
    role: str
    display_name: str
    age: Optional[int] = None
    preferences: dict
    drinks: Optional[str] = None
    remark: Optional[str] = None
    dinner_time: str

# ==================== 旧简化API (保留兼容) ====================

class SimpleUserInfo(BaseModel):
    room_code: str          # 6位数字房间码
    display_name: str
    role: str
    age: Optional[int] = None
    dinner_time: str        # "YYYY-MM-DD HH:00"
    preferences: dict       # 自由结构（含"其他"等）
    drinks: Optional[str] = None
    remark: Optional[str] = None

class CreateRoomRequest(BaseModel):
    family_name: str
    display_name: str
    role: str
    age: Optional[int] = None
    dinner_time: str
    preferences: dict
    drinks: Optional[str] = None
    remark: Optional[str] = None

@app.post("/api/simple/create-room")
def create_room(request: CreateRoomRequest):
    """创建新房间并提交第一个用户信息"""
    try:
        # 生成6位数字房间码
        room_code = f"{random.randint(100000, 999999)}"
        
        # 检查房间码是否已存在
        while db_query("SELECT family_id FROM families WHERE family_id=%s", (room_code,)):
            room_code = f"{random.randint(100000, 999999)}"
        
        # 创建家庭
        db_execute(
            "INSERT INTO families (family_id, family_name, family_password, created_at) VALUES (%s, %s, %s, NOW())",
            (room_code, request.family_name, "room_password")
        )
        
        # 创建用户
        user_id = f"user_{int(time.time())}_{request.display_name.replace(' ', '_')}"
        db_execute(
            "INSERT INTO users (user_id, user_name, user_pass, created_at) VALUES (%s, %s, %s, NOW())",
            (user_id, request.display_name, "default_password")
        )
        
        # 提交信息
        db_execute(
            "INSERT INTO info_submissions (family_id, user_id, role, display_name, age, preferences, drinks, remark, dinner_time, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (
                room_code,
                user_id,
                request.role,
                request.display_name,
                request.age,
                json.dumps(request.preferences, ensure_ascii=False),
                request.drinks,
                request.remark,
                request.dinner_time
            )
        )
        
        return {
            "success": True,
            "message": "Room created successfully",
            "room_code": room_code,
            "family_id": room_code,
            "user_id": user_id
        }
        
    except Exception as e:
        print(f"Error creating room: {e}")
        raise HTTPException(500, f"Error creating room: {str(e)}")

@app.post("/api/simple/join-room")
def join_room(info: SimpleUserInfo):
    """加入现有房间"""
    try:
        # 检查房间是否存在
        family_exists = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (info.room_code,))
        if not family_exists:
            raise HTTPException(400, "Room code not found")
        
        family_name = family_exists[0]["family_name"]
        
        # 创建用户
        user_id = f"user_{int(time.time())}_{info.display_name.replace(' ', '_')}"
        db_execute(
            "INSERT INTO users (user_id, user_name, user_pass, created_at) VALUES (%s, %s, %s, NOW())",
            (user_id, info.display_name, "default_password")
        )
        
        # 提交信息
        db_execute(
            "INSERT INTO info_submissions (family_id, user_id, role, display_name, age, preferences, drinks, remark, dinner_time, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (
                info.room_code,
                user_id,
                info.role,
                info.display_name,
                info.age,
                json.dumps(info.preferences, ensure_ascii=False),
                info.drinks,
                info.remark,
                info.dinner_time
            )
        )
        
        return {
            "success": True,
            "message": "Joined room successfully",
            "room_code": info.room_code,
            "family_name": family_name,
            "user_id": user_id
        }
        
    except Exception as e:
        print(f"Error joining room: {e}")
        raise HTTPException(500, f"Error joining room: {str(e)}")

@app.post("/api/simple/submit")
def simple_submit_info(info: SimpleUserInfo):
    """简化版信息提交 - 自动创建用户和家庭"""
    try:
        # 使用固定的家庭ID
        family_id = "default_family"
        family_name = "My Family"
        
        # 检查家庭是否存在，不存在则创建
        family_exists = db_query("SELECT family_id FROM families WHERE family_id=%s", (family_id,))
        if not family_exists:
            db_execute(
                "INSERT INTO families (family_id, family_name, family_password, created_at) VALUES (%s, %s, %s, NOW())",
                (family_id, family_name, "default_password")
            )
        
        # 生成用户ID（基于姓名和时间戳）
        import time
        user_id = f"user_{int(time.time())}_{info.display_name.replace(' ', '_')}"
        
        # 检查用户是否存在，不存在则创建
        user_exists = db_query("SELECT user_id FROM users WHERE user_id=%s", (user_id,))
        if not user_exists:
            db_execute(
                "INSERT INTO users (user_id, user_name, user_pass, created_at) VALUES (%s, %s, %s, NOW())",
                (user_id, info.display_name, "default_password")
            )
        
        # 提交信息
        db_execute(
            "INSERT INTO info_submissions (family_id, user_id, role, display_name, age, preferences, drinks, remark, dinner_time, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (
                family_id,
                user_id,
                info.role,
                info.display_name,
                info.age,
                json.dumps(info.preferences, ensure_ascii=False),
                info.drinks,
                info.remark,
                info.dinner_time
            )
        )
        
        return {
            "success": True,
            "message": "Information submitted successfully",
            "family_id": family_id,
            "user_id": user_id
        }
        
    except Exception as e:
        print(f"Error in simple submit: {e}")
        raise HTTPException(500, f"Error submitting information: {str(e)}")

@app.get("/api/simple/plans/{room_code}")
def get_simple_plans(room_code: str):
    """获取简化版的用餐计划列表"""
    try:
        plans = db_query(
            "SELECT id, plan_json, created_at FROM plans WHERE family_id=%s ORDER BY created_at DESC LIMIT 20",
            (room_code,)
        )
        
        result = []
        for plan in plans:
            plan_data = json.loads(plan["plan_json"])
            result.append({
                "id": plan["id"],
                "date": plan_data.get("meta", {}).get("date", ""),
                "headcount": plan_data.get("meta", {}).get("headcount", 0),
                "dishes": plan_data.get("dishes", []),
                "drinks": plan_data.get("drinks", []),
                "created_at": plan["created_at"].isoformat()
            })
        
        return result
        
    except Exception as e:
        print(f"Error getting simple plans: {e}")
        raise HTTPException(500, f"Error getting plans: {str(e)}")

@app.post("/api/simple/generate/{room_code}")
def simple_generate_plan(room_code: str):
    """简化版生成用餐计划 - 基于指定房间的所有信息"""
    try:
        family_id = room_code
        
        # 获取该房间的信息提交（按用餐时间分组）
        submissions = db_query(
            "SELECT * FROM info_submissions WHERE family_id=%s ORDER BY dinner_time DESC, created_at DESC",
            (family_id,)
        )
        
        if not submissions:
            raise HTTPException(400, "No information submitted for today")
        
        # 设置主要负责人（第一个提交的人）
        primary_user_id = submissions[0]["user_id"]
        db_execute(
            "UPDATE families SET primary_user_id=%s WHERE family_id=%s",
            (primary_user_id, family_id)
        )
        
        # 构建LLM输入
        roles_lines = []
        for s in submissions:
            prefs = json.loads(s["preferences"]) if s["preferences"] else {}
            disp = s["display_name"] or s["user_id"]
            roles_lines.append({
                "person_role": s["role"],
                "age_group": None,
                "is_chef": bool(prefs.get("is_chef", False)),
                "is_primary": (s["user_id"] == primary_user_id),
                "preferences": prefs,
                "display_name": disp,
                "remark": (str(s.get("remark") or "").strip()),
                "drinks": (str(s.get("drinks") or "").strip()),
            })
        
        # 调用LLM生成计划
        prompt = f"""Generate a comprehensive meal plan for a family dinner. Here are the family members:

{json.dumps(roles_lines, ensure_ascii=False, indent=2)}

Please return ONLY a valid JSON object with this exact structure:
{{
    "dishes": [
        {{
            "name": "Chicken Stir Fry",
            "category": "Main Dish",
            "ingredients": [
                {{"name": "chicken breast", "amount": "500g", "unit": "grams"}},
                {{"name": "bell peppers", "amount": "2", "unit": "pieces"}},
                {{"name": "soy sauce", "amount": "3", "unit": "tablespoons"}},
                {{"name": "garlic", "amount": "3", "unit": "cloves"}},
                {{"name": "ginger", "amount": "1", "unit": "tablespoon"}},
                {{"name": "vegetable oil", "amount": "2", "unit": "tablespoons"}}
            ],
            "steps": [
                "Cut chicken breast into bite-sized pieces",
                "Slice bell peppers into strips",
                "Mince garlic and ginger",
                "Heat oil in a wok over high heat",
                "Add chicken and stir-fry for 5 minutes",
                "Add vegetables and stir-fry for 3 minutes",
                "Add soy sauce and seasonings, stir-fry for 2 minutes"
            ],
            "cooking_time": 20,
            "serving": "4 people",
            "video_url": "https://www.youtube.com/watch?v=example1",
            "image_url": "https://example.com/chicken-stir-fry.jpg"
        }}
    ],
    "drinks": [
        {{
            "name": "Water",
            "type": "Cold",
            "serving": "1 glass per person"
        }}
    ]
}}

Requirements:
1. Include detailed ingredients with specific amounts and units
2. Provide step-by-step cooking instructions
3. Add realistic cooking times
4. Include YouTube video URLs for cooking tutorials (use real, popular cooking video URLs that are likely to be available)
5. Include image URLs for the dishes
6. Make sure all measurements are practical and realistic
7. Consider the family preferences and dietary restrictions
8. Generate 2-3 dishes for a complete meal"""

        response = openai.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        
        raw_content = response.choices[0].message.content
        print(f"LLM raw response: {raw_content}")
        
        # 尝试清理响应内容
        if raw_content.startswith('```json'):
            raw_content = raw_content[7:]
        if raw_content.endswith('```'):
            raw_content = raw_content[:-3]
        raw_content = raw_content.strip()
        
        raw_plan = json.loads(raw_content)
        normalized_plan = normalize_plan(raw_plan)
        
        # 保存计划
        plan_json = json.dumps(normalized_plan, ensure_ascii=False)
        plan_code = f"plan_{int(time.time())}"
        db_execute(
            "INSERT INTO plans (plan_code, family_id, plan_json, submission_cnt, created_at) VALUES (%s, %s, %s, %s, NOW())",
            (plan_code, family_id, plan_json, len(submissions))
        )
        
        return {
            "success": True,
            "plan": normalized_plan,
            "message": "Meal plan generated successfully"
        }
        
    except Exception as e:
        print(f"Error in simple generate: {e}")
        raise HTTPException(500, f"Error generating meal plan: {str(e)}")


# ==================== 新的简化API端点 ====================

@app.post("/api/simple/family/create")
def create_family(request: FamilyCreateRequest):
    """创建新家庭"""
    try:
        # 生成8位字符串家庭码（字母+数字）
        import string
        family_code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # 检查家庭码是否已存在
        while db_query("SELECT family_id FROM families WHERE family_id=%s", (family_code,)):
            family_code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # 生成复杂的唯一密码：8位随机字符串（字母+数字）
        family_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # 检查密码是否已存在（虽然概率很低）
        while db_query("SELECT family_id FROM families WHERE family_password=%s", (family_password,)):
            family_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        
        # 创建家庭
        db_execute(
            "INSERT INTO families (family_id, family_name, family_password, created_at) VALUES (%s, %s, %s, NOW())",
            (family_code, request.family_name, family_password)
        )
        
        return {
            "success": True,
            "message": "Family created successfully",
            "family_id": family_code,
            "family_name": request.family_name,
            "family_code": family_code
        }
        
    except Exception as e:
        print(f"Error creating family: {e}")
        raise HTTPException(500, f"Error creating family: {str(e)}")

@app.post("/api/simple/family/join")
def join_family(request: FamilyJoinRequest):
    """加入现有家庭"""
    try:
        # 检查家庭是否存在
        family_exists = db_query("SELECT family_id, family_name FROM families WHERE family_id=%s", (request.family_code,))
        if not family_exists:
            raise HTTPException(400, "Family code not found")
        
        family_name = family_exists[0]["family_name"]
        
        return {
            "success": True,
            "message": "Joined family successfully",
            "family_id": request.family_code,
            "family_name": family_name,
            "family_code": request.family_code
        }
        
    except Exception as e:
        print(f"Error joining family: {e}")
        raise HTTPException(500, f"Error joining family: {str(e)}")

@app.post("/api/simple/preference/submit")
def submit_preference(request: PreferenceSubmitRequest):
    """提交个人偏好"""
    try:
        # 生成唯一的提交ID
        submission_id = f"sub_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # 先创建用户记录（简化版，不需要密码）
        db_execute(
            "INSERT INTO users (user_id, user_name, user_pass, created_at) VALUES (%s, %s, %s, NOW())",
            (submission_id, request.display_name, "no_password")
        )
        
        # 提交偏好信息
        db_execute(
            "INSERT INTO info_submissions (family_id, user_id, role, display_name, age, preferences, drinks, remark, dinner_time, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (
                request.family_id,
                submission_id,  # 使用submission_id作为user_id
                request.role,
                request.display_name,
                request.age,
                json.dumps(request.preferences, ensure_ascii=False),
                request.drinks,
                request.remark,
                request.dinner_time
            )
        )
        
        return {
            "success": True,
            "message": "Preferences submitted successfully",
            "submission_id": submission_id
        }
        
    except Exception as e:
        print(f"Error submitting preferences: {e}")
        raise HTTPException(500, f"Error submitting preferences: {str(e)}")

# start：
#   cd server
#   python -m uvicorn app:app --reload --port 8000

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
