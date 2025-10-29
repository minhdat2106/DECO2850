"""
Utility functions for the Meal Planner application
"""
import datetime
import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from datetime import timedelta
from urllib.parse import quote_plus  # NEW: for building YouTube fallback URLs

def _valid_http_url(x: str) -> bool:
    """Accept http/https URL strings."""
    if not isinstance(x, str):
        return False
    x = x.strip().lower()
    return x.startswith("http://") or x.startswith("https://")

def _youtube_fallback(name: str) -> str:
    """Build a YouTube search URL from dish name (https)."""
    return f"https://www.youtube.com/results?search_query={quote_plus(name)}" if name else ""

logger = logging.getLogger("meal")

# ==================== Time Parsing Utilities ====================

def parse_dinnertime_str(s: str) -> Tuple[datetime.datetime, str]:
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

def meal_window(dt: datetime.datetime) -> Tuple[str, str]:
    """给定一个 datetime，返回 [start, end] 的字符串（含端点），用于 SQL BETWEEN。
       规则：±1 小时属于同一顿饭。"""
    start = (dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    end = (dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return start, end

def get_meal_time_by_type(meal_type: str) -> str:
    """根据餐次类型获取默认时间"""
    times = {
        'breakfast': '08:00',
        'lunch': '12:00',
        'dinner': '18:00'
    }
    return times.get(meal_type, '18:00')

# ==================== Data Processing Utilities ====================

def _as_list(x):
    """Convert single item to list, handle None"""
    return x if isinstance(x, list) else ([] if x is None else [x])

def coerce_to_lan_schema(plan: dict, dinner_time: str, headcount: int, family_info: dict = None) -> dict:
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

    # dishes 归一 + 保证有 video 链接（接收 video_url 或 videoUrl, 缺则按菜名生成 YouTube 搜索）
    fixed_dishes = []
    for d in _as_list(dishes_in):
        if not isinstance(d, dict):
            continue

        # ingredients
        ingredients = []
        for ing in _as_list(d.get("ingredients") or []):
            if isinstance(ing, dict):
                ingredients.append({
                    "name": ing.get("name") or "",
                    "amount": ing.get("amount") or ""
                })
            elif isinstance(ing, str):
                ingredients.append({"name": ing, "amount": ""})

        # steps
        steps = []
        for step in _as_list(d.get("steps") or []):
            if isinstance(step, str):
                steps.append({"description": step, "time": ""})
            elif isinstance(step, dict):
                steps.append({
                    "description": step.get("description") or step.get("step") or "",
                    "time": step.get("time") or ""
                })

        # name + video link (validated + fallback to YouTube)
        name_val = (d.get("name") or "").strip()
        video_val = (d.get("video_url") or d.get("videoUrl") or "").strip()
        if not _valid_http_url(video_val):
            video_val = _youtube_fallback(name_val)

        fixed_dishes.append({
            "name": name_val,
            "category": d.get("category") or "",
            "ingredients": ingredients,
            "steps": steps,
            "image_url": d.get("image_url") or "",
            "video_url": video_val,  # canonical
            "videoUrl": video_val,  # mirror to support FE variants
            "reason": d.get("reason") or "",
            "base_dish": d.get("base_dish") or "",
            "source": d.get("source") or "",
            "similarity_note": d.get("similarity_note") or "",
        })

    # 组装最终结构
    result = {
        "meta": {
            "Time": time_for_meta,
            "headcount": headcount,
            "roles": fixed_roles,
            "family_id": family_info.get("family_id") if family_info else "",
            "family_name": family_info.get("family_name") if family_info else ""
        },
        "dishes": fixed_dishes
    }
    result = ensure_tutorial_links(result)
    return result

def ensure_tutorial_links(plan_obj: dict) -> dict:
    """
    Bảo hiểm lần cuối: đảm bảo mọi dish có video_url/videoUrl hợp lệ.
    Dùng YouTube search nếu thiếu hoặc URL không hợp lệ.
    """
    if not isinstance(plan_obj, dict):
        return plan_obj or {}

    dishes = plan_obj.get("dishes") or []
    fixed = []
    for d in dishes:
        if not isinstance(d, dict):
            fixed.append(d)
            continue
        name = (d.get("name") or "").strip()
        v = (d.get("video_url") or d.get("videoUrl") or "").strip()
        if not _valid_http_url(v):
            v = _youtube_fallback(name)
        d["video_url"] = v
        d["videoUrl"] = v
        fixed.append(d)
    plan_obj["dishes"] = fixed
    return plan_obj

# ==================== Validation Utilities ====================

def validate_user_id(user_id: str) -> bool:
    """验证用户ID格式"""
    if not user_id or len(user_id) < 3:
        return False
    # 允许字母、数字、下划线
    return bool(re.match(r'^[A-Za-z0-9_]+$', user_id))

def validate_family_id(family_id: str) -> bool:
    """验证家庭ID格式"""
    if not family_id or len(family_id) < 3:
        return False
    # 允许字母、数字、下划线
    return bool(re.match(r'^[A-Za-z0-9_]+$', family_id))

def validate_meal_code(meal_code: str) -> bool:
    """验证meal code格式"""
    return len(meal_code) == 16 and meal_code.isalnum()

# ==================== String Utilities ====================

def generate_family_id() -> str:
    """生成8位随机家庭ID"""
    import random
    import string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def generate_meal_code(family_id: str, participant_count: int, meal_date: str, meal_type: str) -> str:
    """生成16位meal code"""
    # 前8位：family_id（不足8位用0填充）
    family_part = family_id[:8].ljust(8, '0')

    # 后8位：6位日期（YYMMDD）+ 2位参与者数量
    date_clean = meal_date.replace('-', '')
    date_short = date_clean[2:]  # 取后6位 YYMMDD
    participant_str = f"{participant_count:02d}"
    meal_info = date_short + participant_str  # 6位日期 + 2位参与者 = 8位

    return family_part + meal_info

def parse_meal_code(meal_code: str) -> Dict[str, Any]:
    """解析16位meal code"""
    if len(meal_code) != 16:
        raise ValueError("Invalid meal code format")

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
        raise ValueError("Invalid date in meal code")

    return {
        "family_id": family_id,
        "participant_count": participant_count,
        "meal_date": formatted_date
    }

# ==================== HTML Generation Utilities ====================

def render_plan_html(plan_obj: dict) -> str:
    """Render a plan object to a self-contained HTML string (shows per-dish reasons)."""
    meta = plan_obj.get("meta", {}) or {}
    dishes = plan_obj.get("dishes", []) or []

    def _get(d: dict, key: str, default: str = "") -> str:
        v = d.get(key)
        return v if isinstance(v, str) else (default or "")

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        "<title>Meal Plan</title>",
        "<style>",
        "body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI','Roboto',Arial,sans-serif; margin: 20px; color:#111827; }",
        ".header { background: #f3f4f6; padding: 16px 18px; border-radius: 10px; margin-bottom: 20px; }",
        ".header h1 { margin:0 0 8px; font-size: 22px; }",
        ".meta { display:flex; gap:18px; flex-wrap:wrap; color:#374151; }",
        ".badge { display:inline-block; font-size:12px; padding:4px 8px; border-radius:999px; background:#e5e7eb; color:#374151; }",

        ".dish { border: 1px solid #e5e7eb; margin: 14px 0; padding: 16px; border-radius: 10px; background:#fff; }",
        ".dish h3 { margin:0 0 8px; font-size:18px; }",
        ".dish-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }",
        ".category { font-size:12px; padding:3px 8px; border-radius:999px; background:#eef2ff; color:#3730a3; }",

        ".reason { margin-top:4px; font-size:13px; color:#2563eb; background:#eff6ff; padding:6px 10px; border-radius:8px; display:inline-block; }",
        ".source { font-size:12px; color:#6b7280; }",

        ".section { margin-top:10px; }",
        ".ingredients { background: #f9fafb; padding: 10px; border-radius: 8px; }",
        ".ingredients h4, .steps h4 { margin:0 0 6px; font-size:14px; color:#374151; }",
        ".ingredients ul { margin:0; padding-left:18px; }",
        ".steps { background: #ecfeff; padding: 10px; border-radius: 8px; }",
        ".steps ol { margin:0; padding-left:18px; }",

        ".footer { margin-top:24px; color:#6b7280; font-size:12px; }",
        "</style>",
        "</head><body>"
    ]

    # Header
    family_name = _get(meta, "family_name", "Unknown Family")
    html_parts.append("<div class='header'>")
    html_parts.append(f"<h1>Meal Plan — {family_name}</h1>")
    html_parts.append("<div class='meta'>")
    html_parts.append(f"<div><span class='badge'>Time</span> &nbsp;{_get(meta, 'Time', 'N/A')}</div>")
    html_parts.append(f"<div><span class='badge'>Headcount</span> &nbsp;{meta.get('headcount', 0)}</div>")
    if meta.get("family_id"):
        html_parts.append(f"<div><span class='badge'>Family ID</span> &nbsp;{meta.get('family_id')}</div>")
    html_parts.append("</div>")
    html_parts.append("</div>")

    # Dishes
    for dish in dishes:
        name = _get(dish, "name", "Unknown Dish")
        category = _get(dish, "category", "N/A")
        reason = _get(dish, "reason", "").strip()
        source = _get(dish, "source", "").strip()
        base_dish = _get(dish, "base_dish", "").strip()

        html_parts.append("<div class='dish'>")

        # Title + category
        html_parts.append("<div class='dish-head'>")
        html_parts.append(f"<h3>{name}</h3>")
        html_parts.append(f"<span class='category'>{category or 'Dish'}</span>")
        if source:
            html_parts.append(f"<span class='source'>• {source}</span>")
        html_parts.append("</div>")

        # Reason (winner/variant explanation)
        if reason or base_dish:
            # Prefer the explicitly provided reason; otherwise build a compact fallback
            if not reason and base_dish:
                reason = f"Similar to {base_dish} — same style/ingredients"
            html_parts.append(f"<div class='reason'>💡 {reason}</div>")

        # Ingredients
        ings = dish.get("ingredients") or []
        if ings:
            html_parts.append("<div class='section ingredients'>")
            html_parts.append("<h4>Ingredients</h4><ul>")
            for ing in ings:
                if isinstance(ing, dict):
                    iname = _get(ing, "name")
                    iamnt = _get(ing, "amount")
                    html_parts.append(f"<li>{iname}{(' — ' + iamnt) if iamnt else ''}</li>")
                else:
                    html_parts.append(f"<li>{ing}</li>")
            html_parts.append("</ul></div>")

        # Steps
        steps = dish.get("steps") or []
        if steps:
            html_parts.append("<div class='section steps'>")
            html_parts.append("<h4>Steps</h4><ol>")
            for st in steps:
                if isinstance(st, dict):
                    html_parts.append(f"<li>{_get(st, 'description')}</li>")
                else:
                    html_parts.append(f"<li>{st}</li>")
            html_parts.append("</ol></div>")

        # Optional links — accept both keys and fallback by name
        vurl = (dish.get("video_url") or dish.get("videoUrl") or "").strip()
        if not vurl and name:
            vurl = f"https://www.youtube.com/results?search_query={quote_plus(name)}"
        iurl = _get(dish, "image_url")

        if vurl or iurl:
            html_parts.append("<div class='section' style='font-size:13px;'>")
            if vurl:
                html_parts.append(f"<div>🎬 <a href='{vurl}' target='_blank' rel='noopener'>Video</a></div>")
            if iurl:
                html_parts.append(f"<div>🖼️ <a href='{iurl}' target='_blank' rel='noopener'>Image</a></div>")
            html_parts.append("</div>")

        html_parts.append("</div>")  # .dish

    html_parts.append("<div class='footer'>Generated by Meal Planner</div>")
    html_parts.append("</body></html>")
    return "\n".join(html_parts)

# ==================== Logging Utilities ====================

def log_api_call(endpoint: str, method: str, user_id: str = None, **kwargs):
    """记录API调用日志"""
    logger.info(f"API Call: {method} {endpoint} - User: {user_id} - {kwargs}")

def log_error(error: Exception, context: str = ""):
    """记录错误日志"""
    logger.error(f"Error in {context}: {str(error)}", exc_info=True)
