"""
Plan generation and management API routes
"""
import json
import logging
import datetime
import re
import urllib.parse

from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Body

from models import GenerateRequest, PlanIngest  # (không cần model mới cho /suggest)
from database import db_query, db_execute
from utils import coerce_to_lan_schema, render_plan_html, log_api_call, log_error, get_meal_time_by_type
from config import OPENAI_CONFIG


# --- OpenAI client (giữ nguyên cách bạn đang dùng) ---
from openai import OpenAI
client = OpenAI(
    api_key=OPENAI_CONFIG["api_key"],
    base_url=OPENAI_CONFIG["base_url"],
)

ENGLISH_SYSTEM_PROMPT = (
    "You are Meal Planner AI. ALWAYS respond in ENGLISH only, regardless of the "
    "user's input language. Write clear, concise cooking content: dish names, "
    "ingredients (with units), and step-by-step instructions in English. "
    "Do not include any non-English words. Return JSON only when asked."
)

logger = logging.getLogger("meal")
router = APIRouter(prefix="/api/plan", tags=["plan"])

def postprocess_with_anchors(plan_obj: dict, anchors: list[str], hard_lock: bool = False) -> dict:
    """
    Bảo đảm các món anchors có mặt và nằm đầu danh sách; nếu hard_lock=True thì
    không sửa tên anchors và không loại bỏ chúng trong các bước tiếp theo.
    """
    anchors = [a for a in (anchors or []) if isinstance(a, str) and a.strip()]
    if not anchors:
        return plan_obj

    plan_obj = plan_obj or {}
    dishes = list(plan_obj.get("dishes") or [])
    name_to_idx = { (d.get("name") or "").casefold(): i for i, d in enumerate(dishes) }

    # a) thêm anchors bị thiếu
    for a in anchors:
        key = a.casefold()
        if key not in name_to_idx:
            dishes.insert(0, {
                "name": a.strip(),
                "category": "Main",
                "ingredients": [],
                "steps": [],
                "image_url": "",
                "video_url": ""
            })
        else:
            # đưa món anchor có sẵn lên đầu (giữ nguyên thông tin hiện có)
            idx = name_to_idx[key]
            dishes.insert(0, dishes.pop(idx))

    # b) nếu hard_lock: gắn cờ để tầng khác không xoá/sửa anchors
    if hard_lock:
        anchor_keys = {a.casefold() for a in anchors}
        for d in dishes:
            if (d.get("name") or "").casefold() in anchor_keys:
                d["_locked"] = True

    plan_obj["dishes"] = dishes
    # ghi vào meta để front-end hiển thị
    meta = plan_obj.get("meta") or {}
    meta["anchors"] = anchors
    plan_obj["meta"] = meta
    return plan_obj

def _norm_participant_count(x: dict) -> int:
    """
    Lấy số người đi kèm từ submission nếu có.
    Hỗ trợ nhiều khoá đặt tên khác nhau để tương thích FE cũ/mới.
    """
    if not isinstance(x, dict):
        return 1
    v = (
        x.get("participant_count")
        or x.get("participants")
        or x.get("Participant_Count")
        or x.get("headcount")
        or 1
    )
    try:
        n = int(v)
        return n if n > 0 else 1
    except Exception:
        return 1

def _inject_generation_reasons(original_dishes: list[dict], lan_plan: dict) -> dict:
    """Mang reason/source/base_dish từ original_dishes vào lan_plan theo name (casefold)."""
    try:
        if not original_dishes or not lan_plan or "dishes" not in lan_plan:
            return lan_plan
        src = {
            (d.get("name") or "").casefold(): {
                "reason": d.get("reason"),
                "source": d.get("source"),
                "base_dish": d.get("base_dish"),
            }
            for d in original_dishes if isinstance(d, dict)
        }
        for d in lan_plan.get("dishes", []):
            key = (d.get("name") or "").casefold()
            if key in src:
                meta = src[key]
                if meta.get("reason") is not None: d["reason"] = meta["reason"]
                if meta.get("source") is not None: d["source"] = meta["source"]
                if meta.get("base_dish") is not None: d["base_dish"] = meta["base_dish"]
        lan_plan.setdefault("meta", {})["generation_reasons"] = [
            {"name": d.get("name",""),
             "source": d.get("source",""),
             "base_dish": d.get("base_dish",""),
             "reason": d.get("reason","")}
            for d in lan_plan.get("dishes", [])
        ]
        return lan_plan
    except Exception:
        return lan_plan

def get_meal_time(meal_type: str) -> str:
    """根据餐次类型获取时间"""
    times = {"breakfast": "08:00", "lunch": "12:00", "dinner": "18:00"}
    return times.get(meal_type.lower(), "18:00")

def _wheel_load_candidates(family_id: str, meal_date: str, meal_type: str):
    """
    Đọc danh sách ứng viên từ wheel_candidates + tổng votes từ wheel_votes.
    Trả về list[{id,name,votes,proposer_user_id,proposer_name}].
    """
    rows = db_query(
        """
        SELECT c.id,
               c.name,
               c.proposer_user_id,
               c.proposer_name,
               (SELECT COUNT(*) FROM wheel_votes v WHERE v.candidate_id = c.id) AS votes
        FROM wheel_candidates c
        WHERE family_id=%s AND meal_date=%s AND meal_type=%s AND c.deleted_at IS NULL
        ORDER BY c.id ASC
        """,
        (family_id, meal_date, meal_type),
    )
    out = []
    for r in rows or []:
        out.append({
            "id": int(r["id"]),
            "name": r["name"],
            "votes": int(r.get("votes") or 0),
            "proposer_user_id": r.get("proposer_user_id"),
            "proposer_name": r.get("proposer_name"),
        })
    return out


def _try_fetch_winner_from_db(family_id: str, meal_date: str, meal_type: str):
    """
    Nếu bạn có bảng wheel_picks (winner thật sau khi quay), lấy winner ở đây.
    Nếu không tồn tại bảng/cột -> trả None (để fallback theo 'max votes').
    """
    try:
        rows = db_query(
            """
            SELECT winner_name
            FROM wheel_picks
            WHERE family_id=%s AND meal_date=%s AND meal_type=%s
            ORDER BY id DESC LIMIT 1
            """,
            (family_id, meal_date, meal_type),
        )
        if rows:
            return (rows[0].get("winner_name") or "").strip() or None
        return None
    except Exception:
        # Bảng có thể chưa tồn tại -> im lặng fallback
        return None


def _wheel_build_context(family_id: str, meal_date: str, meal_type: str):
    """
    Tạo ngữ cảnh:
      - participants: list unique proposer_user_id đã đề cử món (tức là 'tham gia vòng xoay')
      - nominations: map user_id -> các món họ đề cử (≤2) kèm votes (sort desc)
      - winner_dish: ưu tiên lấy từ wheel_picks; nếu không có -> ứng viên votes cao nhất
      - winner_proposer: proposer_user_id của winner (nếu xác định được)
      - wheel_all_names: set tất cả tên món xuất hiện trên wheel
    """
    cands = _wheel_load_candidates(family_id, meal_date, meal_type)
    participants_order = []            # giữ thứ tự gặp lần đầu
    seen = set()
    nominations = {}
    wheel_all = set()

    for c in cands:
        wheel_all.add(c["name"])
        uid = c["proposer_user_id"]
        if uid not in seen:
            seen.add(uid)
            participants_order.append(uid)
        nominations.setdefault(uid, [])
        nominations[uid].append({"dish": c["name"], "votes": c["votes"], "cid": c["id"]})

    # sort mỗi user theo votes giảm dần, rồi theo id tăng dần
    for uid, arr in nominations.items():
        arr.sort(key=lambda x: (-int(x["votes"] or 0), int(x["cid"] or 0)))
        # giữ tối đa 2 đề cử
        nominations[uid] = arr[:2]

    # winner: ưu tiên bảng wheel_picks; nếu không có -> max votes
    winner_name = _try_fetch_winner_from_db(family_id, meal_date, meal_type)
    winner_proposer = None
    if not winner_name and cands:
        best = sorted(cands, key=lambda x: (-x["votes"], x["id"]))[0]
        winner_name = best["name"]
        winner_proposer = best["proposer_user_id"]
    elif winner_name:
        # tìm proposer tương ứng nếu có trong cands
        for c in cands:
            if (c["name"] or "").strip().lower() == winner_name.strip().lower():
                winner_proposer = c["proposer_user_id"]
                break

    return {
        "participants": participants_order,
        "nominations": nominations,
        "winner_dish": winner_name,
        "winner_proposer": winner_proposer,
        "wheel_all_names": wheel_all,
    }


def _pick_member_base_dish(noms: list, forbid: set) -> str | None:
    """
    Chọn món có votes cao hơn làm base. Nếu base nằm trong 'forbid' thì thử món còn lại.
    """
    if not noms:
        return None
    for item in noms:  # đã sort votes giảm dần ở _wheel_build_context
        d = (item.get("dish") or "").strip()
        if d and d not in forbid:
            return d
    return None


def _craft_prompt_exact(dish_name: str) -> str:
    return f"""
You are a precise cooking assistant.
Generate a complete, step-by-step recipe IN ENGLISH for EXACTLY:
- Dish: "{dish_name}"
Requirements:
- Include an ingredients list with metric units.
- 5–10 numbered steps, clear and executable.
- Keep authentic to the canonical version of the dish.
Output JSON only:
{{
  "name": "{dish_name}",
  "ingredients": [{{"name": "...","quantity_metric":"..."}} ],
  "steps": ["...", "...", "..."]
}}
""".strip()

def _craft_prompt_variant(base_dish: str, forbid_list: list[str], winner_dish: str) -> str:
    forbid_str = ", ".join(sorted(set([*forbid_list, winner_dish] if winner_dish else forbid_list)))
    return f"""
You are a creative but constrained cooking assistant.

Task (ENGLISH ONLY):
- Create ONE dish that is CULINARILY SIMILAR to "{base_dish}" by cooking technique OR by sharing key ingredients.
- It MUST NOT be any dish in this forbidden list: {forbid_str}.
- If "{base_dish}" is a specific variant (e.g., "Pho Bo"), propose a sibling variant (e.g., "Pho Ga" or a veggie version), but NOT the same as base.

Output (JSON only):
{{
  "name": "NEW DISH NAME (not in forbidden list)",
  "ingredients": [{{"name": "...","quantity_metric":"..."}} ],
  "steps": ["...", "...", "..."],
  "similarity_note": "Explain what is similar to '{base_dish}' (technique or ingredients)."
}}
""".strip()

def _suggest_variant_name(base: str, forbid: set[str]) -> str:
    bl = (base or "").lower()
    if "phở" in bl or "pho" in bl:
        candidates = ["Pho Ga (越南鸡肉粉)", "Pho Nam (牛腩粉)", "Pho Chay (素粉)"]
    elif "bánh canh" in bl:
        candidates = ["Bánh canh cua", "Bánh canh giò heo", "Bánh canh chả cá"]
    elif "bún" in bl:
        candidates = ["Bún thịt nướng", "Bún bò Huế", "Bún chả giò"]
    else:
        candidates = [f"{base} – Chicken Variant", f"{base} – Beef Variant", f"{base} – Veggie Variant"]
    forb = {(x or "").strip().casefold() for x in (forbid or set())}
    for c in candidates:
        if c.strip().casefold() not in forb:
            return c
    i = 1
    while True:
        cand = f"{base} – Variant {i}"
        if cand.casefold() not in forb:
            return cand
        i += 1

def _llm_one_recipe(prompt: str, fallback_name: str | None = None) -> dict:
    """
    Gọi LLM một lần để lấy 1 recipe JSON.
    Nếu lỗi hoặc parse fail -> trả khung recipe tối thiểu.
    """
    try:
        r = client.chat.completions.create(
            model=OPENAI_CONFIG["model"],
            messages=[
                {"role": "system", "content": ENGLISH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt + "\n\nReturn all fields strictly in English."},
            ],
            response_format={"type": "json_object"},
            temperature=0.6,
            max_tokens=900,
        )
        data = json.loads(r.choices[0].message.content or "{}")
        name = (data.get("name") or fallback_name or "").strip() or (fallback_name or "Dish")
        ingredients = data.get("ingredients") or []
        steps = data.get("steps") or []
        # Chuẩn hoá về dạng tối thiểu mà coerce_to_lan_schema hiểu được
        return {
            "name": name,
            "category": "Hot dish",
            "ingredients": [
                {"name": i.get("name",""), "amount": i.get("quantity_metric","")}
                if isinstance(i, dict) else {"name": str(i), "amount": ""}
                for i in ingredients
            ],
            "steps": [s if isinstance(s, str) else (s.get("description") or "") for s in steps],
            "image_url": "",
            "video_url": f"https://www.youtube.com/results?search_query={name.replace(' ', '+')}",
            "similarity_note": "",  # <-- FIX: không dùng biến chưa định nghĩa
        }
    except Exception as e:
        logger.warning("LLM one-recipe failed, fallback. err=%s", e)
        # Fallback recipe khung
        nm = fallback_name or "Dish"
        return {
            "name": nm,
            "category": "Hot dish",
            "ingredients": [{"name": "Ingredient A", "amount": "100g"}],
            "steps": [f"Step {k+1}: Instruction" for k in range(5)],
            "image_url": "",
            "video_url": f"https://www.youtube.com/results?search_query={nm.replace(' ', '+')}",
            "similarity_note": "",
        }

def _generate_plan_wheel_mode(
    family_id: str,
    meal_date: str,
    meal_type: str,
    headcount_hint: int,
    people_payload: list[dict],
    forced_winner: str | None = None,   # <-- NEW: winner ép từ FE (kết quả spin)
) -> dict | None:
    """
    Sinh plan theo luật wheel:
      - Số món = số participants.
      - Winner: EXACT (ưu tiên forced_winner nếu có).
      - Thành viên còn lại: VARIANT từ món có vote cao hơn (tránh trùng wheel & đã sinh).
    Trả về plan_obj dạng gần với schema, sẽ được coerce_to_lan_schema sau đó.
    Đồng thời gắn metadata để FE hiển thị lý do sinh món:
      - winner dish: source="wheel_winner", base_dish=<winner>, reason="Picked by wheel (winner) — proposed by X"
      - variant dish: source="wheel_variant", base_dish=<base>, reason="Similar to <base> — <similarity_note|same style/ingredients>"
    """
    ctx = _wheel_build_context(family_id, meal_date, meal_type)
    participants = ctx["participants"]               # list user_id theo thứ tự gặp
    nominations = ctx["nominations"]                 # user_id -> [{dish,votes,cid}, ...] (đã sort desc)
    # --- Quyết định winner: ưu tiên forced_winner từ FE, nếu không thì ctx['winner_dish']
    winner_dish = (forced_winner or "").strip() or ctx["winner_dish"]
    winner_proposer = ctx["winner_proposer"]
    wheel_names = set(ctx["wheel_all_names"])

    # Nếu forced_winner có giá trị khác với ctx winner, cố tìm proposer tương ứng
    if forced_winner and forced_winner.strip():
        try:
            for c in _wheel_load_candidates(family_id, meal_date, meal_type):
                if (c["name"] or "").strip().lower() == forced_winner.strip().lower():
                    winner_proposer = c.get("proposer_user_id")
                    break
        except Exception:
            pass

    if not participants or not winner_dish:
        # không đủ dữ liệu wheel -> trả None để caller fallback sang _llm_generate_plan
        return None

    target_count = len(participants)

    # Cấm các món đã CHỌN (bắt đầu bằng winner). Những món còn lại vẫn dùng làm base.
    forbid = set([winner_dish]) if winner_dish else set()
    already = set([winner_dish]) if winner_dish else set()

    dishes_out: list[dict] = []

    # ---- 1) Winner → EXACT recipe ----
    prompt_exact = _craft_prompt_exact(winner_dish)
    exact_recipe = _llm_one_recipe(prompt_exact, fallback_name=winner_dish)

    # Lý do/metadata cho winner
    reason_winner = "Picked by wheel (winner)"
    winner_proposer_name = None
    try:
        for c in _wheel_load_candidates(family_id, meal_date, meal_type):
            if (c["name"] or "").strip().lower() == (winner_dish or "").strip().lower():
                winner_proposer_name = (c.get("proposer_name") or "") or c.get("proposer_user_id")
                break
    except Exception:
        winner_proposer_name = None
    if winner_proposer_name:
        reason_winner += f" — proposed by {winner_proposer_name}"

    exact_recipe["source"] = "wheel_winner"
    exact_recipe["base_dish"] = winner_dish
    exact_recipe["reason"] = reason_winner

    dishes_out.append(exact_recipe)
    already.add(exact_recipe["name"])
    forbid.add(exact_recipe["name"])

    # ---- 2) Cho từng member còn lại → VARIANT từ món có vote cao nhất của họ ----
    for uid in participants:
        # nếu chính họ là proposer winner (nếu xác định được) thì bỏ qua
        if winner_proposer and uid == winner_proposer:
            continue

        base = _pick_member_base_dish(nominations.get(uid, []), forbid)
        if not base:
            continue

        prompt_var = _craft_prompt_variant(base, list(forbid), winner_dish)
        recipe = _llm_one_recipe(prompt_var, fallback_name=None)

        name_now = (recipe.get("name") or "").strip()
        if (not name_now
                or name_now.casefold() in {x.casefold() for x in forbid}
                or name_now.casefold() == (base or "").strip().casefold()):
            recipe["name"] = _suggest_variant_name(base, forbid)
            recipe["video_url"] = f"https://www.youtube.com/results?search_query={recipe['name'].replace(' ', '+')}"

        # chống trùng tên với already
        if (recipe.get("name") or "").strip() in already:
            recipe["name"] = f'{recipe.get("name","Variant")}-{len(already)+1}'

        # Gắn lý do/metadata cho variant
        sim = (recipe.get("similarity_note") or "").strip()
        reason = f"Similar to {base} — {sim if sim else 'same style/ingredients'}"
        recipe["source"] = "wheel_variant"
        recipe["base_dish"] = base
        recipe["reason"] = reason

        dishes_out.append(recipe)
        already.add(recipe["name"])
        forbid.add(recipe["name"])

        if len(dishes_out) >= target_count:
            break

    # Compose meta sơ bộ; roles lấy từ people_payload (được coerce sau)
    plan_obj = {
        "meta": {
            "Time": f"{meal_date}, {get_meal_time_by_type(meal_type)}",
            "headcount": max(headcount_hint or 1, len(people_payload) or 1),
            "roles": people_payload,
            "family_id": family_id,
            "family_name": "",
        },
        "dishes": dishes_out,
    }
    return plan_obj


# ================== SCHEMA & PROMPTS ==================

PLAN_SCHEMA_EXAMPLE = {
    "meta": {
        "Time": "2024-01-01",
        "Meal Type": "Dinner",
        "headcount": 3,
        "roles": [
            {"user_name": "Admin", "is_chef": False, "tasks": ["Washing", "Stir-frying"]},
            {"user_name": "member", "is_chef": False, "tasks": ["Preparing ingredients", "Setting the table"]},
            {"user_name": "member", "is_chef": False, "tasks": ["Clearing the table", "Washing dishes"]},
        ],
        "family_id": "FAMILY001",
        "family_name": "Family Name",
    },
    "dishes": [
        {
            "dish1": "",
            "name": "Kung Pao Chicken",
            "category": "Hot dish",
            "ingredients": [
                {"name": "Chicken breast", "amount": "300g"},
                {"name": "Peanuts", "amount": "50g"},
            ],
            "steps": [
                "Step 1: Dice chicken breast and marinate with cooking wine for 10 minutes",
                "Step 2: Heat wok with oil and stir-fry chicken for 5 minutes",
                "Step 3: Add peanuts and stir-fry for 2 minutes",
                "Step 4: Add sauce and cook until thickened",
            ],
            "image_url": "",
            "video_url": "https://www.youtube.com/results?search_query=Kung+Pao+Chicken",
        },
        {
            "dish2": "",
            "name": "Stir-fried Broccoli",
            "category": "Appetizer",
            "ingredients": [
                {"name": "Broccoli", "amount": "200g"},
                {"name": "Garlic", "amount": "3 cloves"},
            ],
            "steps": [
                "Step 1: Blanch broccoli for 2 minutes",
                "Step 2: Heat oil and stir-fry garlic until fragrant",
                "Step 3: Add broccoli and stir-fry for 3 minutes",
                "Step 4: Season with salt and serve",
            ],
            "image_url": "",
            "video_url": "https://www.youtube.com/results?search_query=Stir-fried+Broccoli",
        },
    ],
}

PROMPT_SYSTEM = (
    "You are a family dinner planning assistant. Output JSON in English strictly following the provided schema structure, without any extra text. "
    "All content in the JSON must be in English."
)

PROMPT_RULES = """
Generation Rules (MUST FOLLOW STRICTLY):

CRITICAL: All JSON output MUST be in English! All responses MUST be in English!

1) Dish Quantity:
   - If remarks include explicit dish names: generate exactly those dishes.
   - Otherwise generate N-1 dishes (N=headcount, 2 <= N-1 <= 8).

2) Nutrition balance: include meat/veg; categories among Appetizer/Hot dish/Soup.

3) Each dish needs ingredients (with amounts) and 5-8 detailed steps.
   - image_url must be "".
   - video_url is a YouTube search for the dish.

4) Task Assignment: every participant 2–4 tasks; if is_chef=True include “Stir-frying (Chef)”.

5) Remarks have highest priority; follow strictly.

6) Participants inference allowed; never return empty roles.

7) Names: use user_id/display_name; “member” for virtual members.

8) Dish naming: English only (no non-English words or scripts).
"""

# ================== FALLBACK (Rule-based) ==================

def _fallback_simple_plan(
    submissions: List[Dict[str, Any]], headcount: int, meal_type: str
) -> Dict[str, Any]:
    """
    Rule-based fallback that NOW respects a THEME/Requested dish mentioned in remarks.
    If a theme is found, we build 4 dishes around that theme (2–3 variants + 1–2 sides).
    All output stays in English.
    """
    # --- 1) Extract THEME from remarks (first match wins) ---
    theme = None
    for s in submissions or []:
        remark = (s.get("remark") or "").strip()
        if not remark:
            continue
        m1 = re.search(r'^\s*THEME\s*:\s*(.+)$', remark, flags=re.IGNORECASE | re.MULTILINE)
        m2 = re.search(r'Requested\s+dish\s*:\s*(.+)$', remark, flags=re.IGNORECASE)
        if m1 and not theme:
            theme = m1.group(1).strip()
        elif m2 and not theme:
            theme = m2.group(1).strip()
        if theme:
            break

    # --- 2) Build dish list based on THEME or fallback by style/likes ---
    dishes: List[str] = []
    if theme:
        seed = theme
        seed_low = seed.lower()

        # curated variants for frequent themes; ensure at least 5 then take 4
        if any(k in seed_low for k in ["nui xào", "nui xao", "stir-fried macaroni", "macaroni", "pasta",
                                       "mì xào", "mi xao", "stir-fry noodle", "stir-fried noodle"]):
            variants = [
                "Stir-fried Macaroni with Beef (牛肉意面炒)",
                "Stir-fried Macaroni with Chicken (鸡肉意面炒)",
                "Stir-fried Macaroni with Seafood (什锦海鲜意面炒)",
                "Garlic Butter Vegetables (蒜香黄油时蔬)",
                "Tomato Egg Soup (西红柿鸡蛋汤)",
            ]
        elif any(k in seed_low for k in ["pho", "phở", "pho bo", "pho ga"]):
            variants = [
                "Pho Bo (越南牛肉粉)",
                "Pho Ga (越南鸡肉粉)",
                "Vietnamese Fried Spring Rolls (越南炸春卷)",
                "Pickled Vegetables (腌制小菜)",
                "Beef Salad with Herbs (越式香草牛肉沙拉)",
            ]
        elif "ramen" in seed_low:
            variants = [
                "Shoyu Ramen (酱油拉面)",
                "Miso Ramen (味噌拉面)",
                "Gyoza (日式煎饺)",
                "Edamame (盐煮毛豆)",
                "Chicken Karaage (日式炸鸡)",
            ]
        elif any(k in seed_low for k in ["fried rice", "egg fried rice", "yangzhou"]):
            variants = [
                "Yangzhou Fried Rice (扬州炒饭)",
                "Shrimp & Egg Fried Rice (虾仁蛋炒饭)",
                "Stir-fried Bok Choy (清炒青菜)",
                "Egg Drop Soup (蛋花汤)",
                "Cucumber Salad (拍黄瓜)",
            ]
        else:
            base = _norm_dish_name(seed)
            variants = [
                f"{base} with Beef (牛肉版)",
                f"{base} with Chicken (鸡肉版)",
                f"{base} with Seafood (海鲜版)",
                "Stir-fried Vegetables (清炒时蔬)",
                "Light Soup (清汤)",
            ]

        dishes = variants[:4]  # EXACTLY 4 around the theme

    else:
        # original non-theme branch: simple style/likes based
        likes, dislikes, allergies = [], set(), set()
        style = None
        names = []
        for s in submissions or []:
            prefs = s.get("preferences") or {}
            if isinstance(prefs, str):
                try:
                    prefs = json.loads(prefs)
                except Exception:
                    prefs = {}
            likes += (prefs.get("likes") or [])
            for x in (prefs.get("dislikes") or []):
                if x:
                    dislikes.add(str(x).lower())
            allg = (prefs.get("allergies") or "")
            for x in str(allg).split(","):
                x = x.strip()
                if x:
                    allergies.add(x.lower())
            if not style and prefs.get("food_style"):
                style = prefs["food_style"]
            names.append(s.get("user_id") or s.get("display_name") or "member")

        base_by_style = {
            "chinese": ["Fried rice", "Stir-fried vegetables", "Braised tofu", "Egg drop soup"],
            "vietnamese": ["Broken rice", "Morning glory stir-fry", "Caramelized pork & eggs", "Sour fish soup"],
            "western": ["Roasted chicken", "Mashed potatoes", "Pasta aglio e olio", "Garden salad"],
            "japanese": ["Chicken teriyaki", "Miso soup", "Tamago", "Pickled cucumber"],
        }
        base = base_by_style.get((style or "").lower(), ["Rice", "Sauteed veggies", "Egg omelette", "Tomato soup"])

        def ok(dish: str) -> bool:
            low = dish.lower()
            return all(x not in low for x in dislikes) and all(x not in low for x in allergies)

        dishes = [d for d in base if ok(d)]
        for like in likes:
            if ok(like) and like not in dishes:
                dishes.append(like)

        target = max(2, (headcount or 1) - 1)
        while len(dishes) < target:
            dishes.append(f"Side dish {len(dishes)+1}")
        dishes = dishes[:4]  # keep plan compact

    # --- 3) Simple roles/tasks ---
    tasks_pool = ["Washing", "Preparing ingredients", "Stir-frying (Chef)", "Setting the table", "Clearing the table", "Washing dishes"]
    names_for_roles = [s.get("user_id") or s.get("display_name") or "member" for s in (submissions or [])]
    if not names_for_roles:
        names_for_roles = ["member"] * max(1, headcount or 1)

    roles = []
    for i, name in enumerate(names_for_roles):
        roles.append({
            "user_name": name,
            "is_chef": (i == 0),
            "tasks": [tasks_pool[i % len(tasks_pool)], tasks_pool[(i+2) % len(tasks_pool)]],
        })

    # --- 4) Compose plan JSON (English only) ---
    plan_dict = {
        "meta": {
            "Time": datetime.date.today().isoformat(),
            "Meal Type": meal_type.capitalize(),
            "headcount": headcount or len(names_for_roles) or 1,
            "roles": roles,
            "family_id": "",
            "family_name": "",
        },
        "dishes": [
            {
                "name": f"{dish} ()",
                "category": "Hot dish" if i == 0 else ("Appetizer" if i == 1 else "Soup"),
                "ingredients": [{"name": "Ingredient A", "amount": "100g"}],
                "steps": [f"Step {k+1}: Instruction" for k in range(5)],
                "image_url": "",
                "video_url": f"https://www.youtube.com/results?search_query={dish.replace(' ', '+')}",
            }
            for i, dish in enumerate(dishes[:5])
        ],
    }
    return plan_dict

def _heuristic_theme_dishes(seed: str) -> List[str]:
    """
    Heuristic list of ~5 dishes centered around `seed`:
    - 2–3 variants of the base dish
    - 1–2 simple sides that pair well
    Names in English with optional Chinese/VN in parentheses.
    """
    s = (seed or "").lower()

    # ---- Vietnamese "nui xào" / macaroni stir-fry ----
    if any(k in s for k in ["nui xào", "nui xao", "nui", "macaroni", "stir-fried macaroni", "macaroni stir-fry"]):
        return [
            "Stir-fried Macaroni with Beef (Nui xào bò)",
            "Stir-fried Macaroni with Chicken (Nui xào gà)",
            "Stir-fried Macaroni with Sausage (Nui xào xúc xích)",
            "Garlic Stir-fried Vegetables (Rau xào tỏi)",
            "Tomato Egg Soup (Canh cà chua trứng)"
        ]

    # ---- Pho ----
    if "pho" in s or "phở" in s:
        return [
            "Pho Bo (越南牛肉粉)",
            "Pho Ga (越南鸡肉粉)",
            "Fried Spring Rolls (炸春卷)",
            "Vietnamese Pickled Vegetables (越南腌菜)",
            "Herb Salad (香草沙拉)"
        ]

    # ---- Ramen ----
    if "ramen" in s:
        return [
            "Shoyu Ramen (酱油拉面)",
            "Miso Ramen (味噌拉面)",
            "Gyoza (煎饺)",
            "Edamame (毛豆)",
            "Japanese Cabbage Salad (和风卷心菜沙拉)"
        ]

    # ---- Fried rice ----
    if "fried rice" in s or "yangzhou" in s or "egg fried rice" in s:
        return [
            "Yangzhou Fried Rice (扬州炒饭)",
            "Shrimp Egg Fried Rice (虾仁蛋炒饭)",
            "Stir-fried Bok Choy (清炒青菜)",
            "Egg Drop Soup (蛋花汤)",
            "Cucumber Salad (拍黄瓜)"
        ]

    # ---- Pasta / Spaghetti themes ----
    if any(k in s for k in ["spaghetti", "pasta", "carbonara", "bolognese"]):
        return [
            "Spaghetti Carbonara (培根奶油意面)",
            "Spaghetti Bolognese (博洛尼亚肉酱意面)",
            "Garlic Bread (蒜香面包)",
            "Caesar Salad (凯撒沙拉)",
            "Minestrone Soup (意式杂菜汤)"
        ]

    # ---- Taco themes ----
    if "taco" in s or "tacos" in s:
        return [
            "Beef Tacos (牛肉玉米卷)",
            "Chicken Tacos (鸡肉玉米卷)",
            "Mexican Rice (墨西哥米饭)",
            "Pico de Gallo (番茄莎莎)",
            "Corn Salad (玉米沙拉)"
        ]

    # ---- Curry themes ----
    if "curry" in s:
        return [
            "Chicken Curry (鸡肉咖喱)",
            "Vegetable Curry (蔬菜咖喱)",
            "Steamed Rice (米饭)",
            "Cucumber Raita (酸奶黄瓜酱)",
            "Garlic Naan (蒜香馕)"
        ]

    # Generic stir-fry noodle/rice fallback
    if any(k in s for k in ["xào", "xao", "stir-fry", "stir fry", "noodle", "rice noodle"]):
        return [
            f"{seed} with Beef (牛肉版)",
            f"{seed} with Chicken (鸡肉版)",
            "Stir-fried Vegetables (清炒时蔬)",
            "Tomato Egg Soup (西红柿鸡蛋汤)",
            "Pickled Vegetables (酱腌菜)"
        ]

    # Ultimate fallback
    return [
        f"{seed} – Variant A",
        f"{seed} – Variant B",
        "Stir-fried Vegetables (清炒时蔬)",
        "Tomato Soup (番茄汤)",
        "Cucumber Salad (拍黄瓜)"
    ]


def _propose_dishes_for_theme(theme: str, meal_type: str, headcount: int) -> List[str]:
    """
    Produce ~5 dish names centered around `theme`.
    Prefer LLM; fall back to heuristics. Always return 5 unique items if possible.
    """
    base5 = _heuristic_theme_dishes(theme)

    if OPENAI_CONFIG.get("debug", False):
        return base5

    try:
        system = (
            "You are a menu ideation assistant. Respond ONLY with JSON of the form: "
            "{\"dishes\": [\"name1\", \"name2\", ...]}. English only."
        )
        user = (
            "Center on the given theme. Output 5 dishes total: 2–3 variations of the base theme and 1–2 complementary sides. "
            "Names must be concise. Do NOT add descriptions.\n"
            f"Theme: {theme}\nMeal type: {meal_type}\nHeadcount: {headcount}\n"
            "Return strictly JSON with a `dishes` array."
        )
        resp = client.chat.completions.create(
            model=OPENAI_CONFIG["model"],
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.6,
            max_tokens=300,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        arr = [str(x).strip() for x in (data.get("dishes") or []) if str(x).strip()]
        # de-dup + fill to 5
        seen, out = set(), []
        for x in arr:
            xl = x.lower()
            if xl not in seen:
                seen.add(xl); out.append(x)
            if len(out) >= 5: break
        for x in base5:
            if len(out) >= 5: break
            xl = x.lower()
            if xl not in seen:
                seen.add(xl); out.append(x)
        return out[:5] if out else base5
    except Exception as e:
        logger.warning("Theme proposal LLM failed, using heuristic. err=%s", e)
        return base5

# --- Helpers for theme-first generation (ADD these right before _llm_generate_plan) ---

def _norm_dish_name(s: str) -> str:
    """Normalize dish names (single spaces, trimmed)."""
    return re.sub(r"\s+", " ", (s or "").strip())


def _llm_list_theme_dishes(theme: str) -> List[str]:
    """
    Stage-1: Ask LLM to list ~5 dishes that revolve around the theme.
    Always return 5 strings (fallback if LLM fails or debug=True).
    """
    if OPENAI_CONFIG.get("debug", False):
        seed_low = (theme or "").lower()
        if any(k in seed_low for k in ["nui xào", "nui xao", "stir-fried macaroni", "macaroni", "pasta",
                                       "mì xào", "mi xao", "stir-fry noodle", "stir-fried noodle"]):
            return [
                "Stir-fried Macaroni with Beef (牛肉意面炒)",
                "Stir-fried Macaroni with Chicken (鸡肉意面炒)",
                "Stir-fried Macaroni with Seafood (什锦海鲜意面炒)",
                "Garlic Butter Vegetables (蒜香黄油时蔬)",
                "Tomato Egg Soup (西红柿鸡蛋汤)",
            ]
        return [
            f"{theme} – Variant A",
            f"{theme} – Variant B",
            f"{theme} – Variant C",
            "Simple Side Vegetables",
            "Light Soup",
        ]

    system = (
        "You are a culinary planner. English only. "
        "Respond with JSON only: {\"dishes\":[\"dish1\",\"dish2\",\"dish3\",\"dish4\",\"dish5\"]}. "
        "Dishes must revolve around the given theme: include at least 2 direct variations "
        "(different proteins/broths/styles) and 1–2 complementary sides or soups."
    )
    user = (
        f"Theme: {theme}\n"
        "Return 5 concise dish names in English only. "
        "No explanations, JSON only."
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_CONFIG["model"],
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.6,
            max_tokens=300,
        )
        data = json.loads(resp.choices[0].message.content)
        arr = [ _norm_dish_name(x) for x in (data.get("dishes") or []) if _norm_dish_name(x) ]
        # ensure 5 unique
        uniq: List[str] = []
        for x in arr:
            if x.lower() not in [u.lower() for u in uniq]:
                uniq.append(x)
        while len(uniq) < 5:
            uniq.append(f"{theme} – Variant {len(uniq)+1}")
        return uniq[:5]
    except Exception as e:
        logger.warning("Theme list LLM failed: %s", e)
        return [
            f"{theme} – Variant A",
            f"{theme} – Variant B",
            f"{theme} – Variant C",
            "Simple Side Vegetables",
            "Light Soup",
        ]


def _pick_4_from_theme(theme: str) -> List[str]:
    """
    Pick exactly 4 dishes for the theme using stage-1 list (LLM) with fallback.
    """
    dishes5 = _llm_list_theme_dishes(theme)
    out = [ _norm_dish_name(x) for x in dishes5[:4] ]
    final: List[str] = []
    for x in out:
        if x and x.lower() not in [u.lower() for u in final]:
            final.append(x)
    while len(final) < 4:
        final.append(f"{theme} – Variant {len(final)+1}")
    return final[:4]

def _apply_forced_menu(plan_obj: dict, forced_menu: List[str]) -> dict:
    """
    Overwrite/normalize the LLM JSON so that dish names are EXACTLY the forced_menu (4 items).
    - Giữ lại category/ingredients/steps nếu có, nhưng thay name + video_url.
    - Nếu LLM trả ít hơn 4 món, tự bổ sung khung món tối thiểu.
    - Luôn set đúng 4 món theo thứ tự forced_menu.
    """
    try:
        plan_obj = plan_obj or {}
        dishes = plan_obj.get("dishes") or []
        out = []

        # đảm bảo forced_menu có đúng 4 mục
        fm = [str(x).strip() for x in forced_menu if str(x).strip()]
        while len(fm) < 4:
            fm.append(f"{forced_menu[0]} – Variant {len(fm)+1}")
        fm = fm[:4]

        for i, name in enumerate(fm):
            if i < len(dishes) and isinstance(dishes[i], dict):
                d = dishes[i]
            else:
                # tạo khung mặc định nếu thiếu
                d = {
                    "category": "Hot dish" if i == 0 else ("Appetizer" if i == 1 else "Soup"),
                    "ingredients": [{"name": "Ingredient A", "amount": "100g"}],
                    "steps": [f"Step {k+1}: Instruction" for k in range(5)],
                    "image_url": "",
                    "video_url": "",
                }
            d["name"] = name
            d["video_url"] = f"https://www.youtube.com/results?search_query={name.replace(' ', '+')}"
            out.append(d)

        plan_obj["dishes"] = out
        return plan_obj
    except Exception:
        # an toàn: nếu có lỗi thì trả nguyên
        return plan_obj

# ================== LLM CALLER ==================

def _llm_generate_plan(payload: dict):
    """
    Call LLM and return (plan_obj, raw_text).
    If OPENAI_CONFIG['debug'] = True → use rule-based fallback (no LLM call).
    """
    if OPENAI_CONFIG.get("debug", False):
        logger.info("OPENAI_CONFIG.debug=True -> using rule-based fallback without calling LLM")
        plan_obj = _fallback_simple_plan(payload.get("people") or [], payload.get("headcount") or 1, payload.get("meal_type") or "dinner")
        return plan_obj, json.dumps({"LLM": "mocked_debug"}, ensure_ascii=False)

    try:
        # ---- Extract remarks for THEME and required dishes ----
        required_dishes = []
        theme_seed = None
        for person in payload.get("people", []):
            remark = (person.get("remark") or "").strip()
            if remark:
                required_dishes.append(f"{person.get('display_name', 'member')}: {remark}")
                m1 = re.search(r'^\s*THEME\s*:\s*(.+)$', remark, flags=re.IGNORECASE | re.MULTILINE)
                m2 = re.search(r'Requested\s+dish\s*:\s*(.+)$', remark, flags=re.IGNORECASE)
                if m1 and not theme_seed:
                    theme_seed = m1.group(1).strip()
                elif m2 and not theme_seed:
                    theme_seed = m2.group(1).strip()

        headcount = int(payload.get("headcount") or 0)
        recommended_dish_count = max(2, headcount - 1)

        # ---- Stage-0: reconcile participant hint (unchanged) ----
        expected_participant_hint = ""
        for person in payload.get("people", []):
            remark = (person.get("remark") or "").strip()
            m = re.search(r"(\d+)\s*(?:人|people)", remark, re.IGNORECASE)
            if m:
                expected_count = int(m.group(1))
                if expected_count > headcount:
                    expected_participant_hint = (
                        f"\nIMPORTANT: User remark mentions {expected_count} participants, "
                        f"but only {headcount} submission(s) received."
                    )
                    headcount = expected_count
                    recommended_dish_count = max(2, headcount - 1)
                    break

        # ---- NEW Stage-1: if we have a theme, pick EXACT 4 dish names up front ----
        forced_menu: List[str] = []
        if theme_seed:
            forced_menu = _pick_4_from_theme(theme_seed)
            recommended_dish_count = 4  # lock to 4 dishes

        # ---- Build prompt ----
        user_prompt = f"""Please generate a meal plan for the following family:

**Basic Information:**
- Date: {payload.get('date')}
- Meal Type: {payload.get('meal_type', 'dinner').capitalize()}
- Number of Diners: {headcount}
- Actual Submissions Received: {len(payload.get('people', []))}
- Recommended Number of Dishes: {recommended_dish_count}
- Meal Code: {payload.get('meal_code', 'N/A')}{expected_participant_hint}
"""

        if theme_seed:
            user_prompt += (
                f"**Theme Focus (CRITICAL):** Center the plan around \"{theme_seed}\".\n"
                "Name dishes in English with Chinese in parentheses. Keep video_url as a YouTube search for each dish.\n"
            )
            if forced_menu:
                user_prompt += (
                    "\n**STRICT MENU (OVERRIDE) — USE EXACTLY THESE 4 DISHES:**\n" +
                    "\n".join([f"- {d}" for d in forced_menu]) +
                    "\nDo NOT replace these dish names or add extra mains. "
                    "You may only vary ingredients/steps sensibly.\n"
                )

        if required_dishes:
            total_requested_dishes = 0
            for dish in required_dishes:
                if ":" in dish:
                    total_requested_dishes += len([d.strip() for d in dish.split(":")[1].replace("，", ",").split(",") if d.strip()])
            user_prompt += f"""**CRITICAL: User-Requested Dishes (MUST INCLUDE ALL):**
{chr(10).join([f"- {x}" for x in required_dishes])}
Total requested: {total_requested_dishes or "Parse from remarks"}
"""

        user_prompt += "**Family Members Information:**\n\n"
        for i, person in enumerate(payload.get("people", []), 1):
            user_prompt += f"{i}. **{person.get('display_name', 'member')}**:\n"
            user_prompt += f"   - User Name: {person.get('display_name', 'member')}\n"
            user_prompt += f"   - Is Chef: {'Yes' if person.get('is_chef') else 'No'}\n"
            if person.get("food_style"):
                user_prompt += f"   - Food Style: {person['food_style']}\n"
            if person.get("likes"):
                user_prompt += f"   - Liked Tastes: {', '.join(person['likes'])}\n"
            if person.get("dislikes"):
                user_prompt += f"   - Disliked Tastes: {', '.join(person['dislikes'])}\n"
            if person.get("allergies"):
                user_prompt += f"   - Allergies: {person['allergies']}\n"
            if person.get("remark"):
                user_prompt += f"   - Special Requirements: {person['remark']}\n"
            user_prompt += "\n"

        user_prompt += f"\n{PROMPT_RULES}\n"
        user_prompt += f"\n**Output Format (JSON Schema):**\n```json\n{json.dumps(PLAN_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}\n```\n"
        user_prompt += "\nPlease generate the meal plan following the above schema and rules. Output in JSON format only."

        logger.info(
            "Sending prompt to LLM (headcount=%s, recommended_dishes=%s, theme=%s, forced_menu=%s, required_dishes=%s)",
            headcount, recommended_dish_count, theme_seed, len(forced_menu), len(required_dishes)
        )

        response = client.chat.completions.create(
            model=OPENAI_CONFIG["model"],
            messages=[
                {"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=4000,
        )

        content = response.choices[0].message.content
        plan_obj = json.loads(content)
        if forced_menu:
            plan_obj = _apply_forced_menu(plan_obj, forced_menu)
        logger.info("LLM generated plan with %s dishes", len(plan_obj.get("dishes", [])))
        return plan_obj, content

    except Exception as e:
        msg = str(e)
        logger.error("LLM generation failed: %s", msg)
        keywords = ("401", "Unauthorized", "invalid", "无效", "未授权", "v_api_error")
        if any(k.lower() in msg.lower() for k in keywords):
            logger.warning("LLM auth error -> using rule-based fallback")
            plan_obj = _fallback_simple_plan(payload.get("people") or [], payload.get("headcount") or 1, payload.get("meal_type") or "dinner")
            return plan_obj, json.dumps({"LLM": "fallback_due_to_401", "error": msg}, ensure_ascii=False)
        plan_obj = _fallback_simple_plan(payload.get("people") or [], payload.get("headcount") or 1, payload.get("meal_type") or "dinner")
        return plan_obj, json.dumps({"LLM": "fallback_due_to_error", "error": msg}, ensure_ascii=False)

def _extract_requested_dishes(remark: str) -> list[str]:
    """
    Tìm các món mà user cung cấp từ remark.
    Ưu tiên các format có cấu trúc: 'Requested dish: ...' hoặc 'Dishes: ...'.
    Trả về danh sách tên món (đã trim), rỗng nếu không tìm thấy.
    """
    remark = (remark or "").strip()
    if not remark:
        return []
    # Requested dish: A, B, C
    m = re.search(r"Requested\s*dish(?:es)?\s*:\s*(.+)", remark, flags=re.IGNORECASE)
    if not m:
        # fallback nhẹ: Dishes: ...
        m = re.search(r"Dishes?\s*:\s*(.+)", remark, flags=re.IGNORECASE)
    if not m:
        return []
    raw = m.group(1)
    # tách theo , hoặc ; hoặc | (giữ lại các token không rỗng)
    items = [x.strip() for x in re.split(r"[;,|]", raw) if x.strip()]
    return items

def _has_chosen_role_or_tasks(pref: dict, role_val: str) -> bool:
    """
    Xem user có 'chọn role' hay không:
    - Có role string (khác rỗng và khác 'member' mặc định)
    - HOẶC preferences.is_chef = True
    - HOẶC có ít nhất 1 task trong preferences.tasks
    """
    role_ok = bool((role_val or "").strip() and (role_val or "").strip().lower() != "member")
    is_chef = bool((pref or {}).get("is_chef", False))
    has_tasks = bool((pref or {}).get("tasks") or [])
    return role_ok or is_chef or has_tasks

def _count_participants_from_submissions(submissions: list[dict]) -> int:
    seen = set()
    for sub in submissions or []:
        pref = sub.get("preferences") or {}
        role_val = sub.get("role") or ""
        if isinstance(pref, str):
            try:
                pref = json.loads(pref)
            except Exception:
                pref = {}

        has_role = _has_chosen_role_or_tasks(pref, role_val)
        has_dish = len(_extract_requested_dishes(sub.get("remark") or "")) > 0
        if has_role or has_dish:
            uid = sub.get("user_id") or sub.get("display_name") or f"idx:{id(sub)}"
            seen.add(str(uid))

    # NEW: nếu chưa ai “được tính”, fallback: mỗi submission là 1 participant
    if not seen and submissions:
        for sub in submissions:
            uid = sub.get("user_id") or sub.get("display_name") or f"idx:{id(sub)}"
            seen.add(str(uid))

    return len(seen)

def _count_participants_union(submissions: list[dict], family_id: str | None, meal_date: str | None, meal_type: str | None) -> int:
    """
    Kết hợp:
    - Participants từ submissions (role/chef/tasks hoặc Requested dish)
    - UNION với danh sách proposer_user_id có trong wheel (nếu tra được)
    """
    base = _count_participants_from_submissions(submissions)
    wheel = 0
    try:
        if family_id and meal_date and meal_type:
            ctx = _wheel_build_context(family_id, meal_date, meal_type)
            wheel = len([uid for uid in (ctx.get("participants") or []) if uid])
    except Exception:
        wheel = 0
    return max(base, wheel)  # tránh giảm khi có dữ liệu wheel

def _ensure_video_urls(plan_obj: dict) -> dict:
    try:
        for d in (plan_obj or {}).get("dishes", []) or []:
            name = (d.get("name") or "").strip()
            # đọc cả 2 khoá
            vu_snake = (d.get("video_url") or "").strip()
            vu_camel = (d.get("videoUrl") or "").strip()

            # nếu cả hai rỗng, sinh link YouTube search
            if name and not (vu_snake or vu_camel):
                link = f"https://www.youtube.com/results?search_query={urllib.parse.quote(name)}"
                d["video_url"] = link
                d["videoUrl"] = link
            else:
                # nếu một trong hai có, đồng bộ sang khoá còn lại
                link = vu_snake or vu_camel
                d["video_url"] = link
                d["videoUrl"] = link
    except Exception:
        pass
    return plan_obj

def _mirror_video_field(plan_obj: dict) -> dict:
    """
    Mirror video_url <-> videoUrl để mọi template/frontend đều đọc được.
    Không ném lỗi nếu plan thiếu trường.
    """
    try:
        for d in (plan_obj or {}).get("dishes", []) or []:
            v = (d.get("video_url") or d.get("videoUrl") or "").strip()
            if v:
                d["video_url"] = v
                d["videoUrl"]  = v
    except Exception:
        pass
    return plan_obj

# ================== NEW: SUGGESTIONS ==================
#
# def _infer_food_style(family_id: str, meal_date: str, meal_type: str) -> Optional[str]:
#     """
#     Suy luận style phổ biến từ submissions của phiên hiện tại.
#     """
#     try:
#         rows = db_query(
#             """
#             SELECT preferences
#             FROM info_submissions
#             WHERE family_id=%s AND meal_date=%s AND meal_type=%s
#             """,
#             (family_id, meal_date, meal_type),
#         )
#         for r in rows:
#             prefs = r.get("preferences")
#             if isinstance(prefs, str):
#                 try:
#                     prefs = json.loads(prefs)
#                 except Exception:
#                     prefs = {}
#             if isinstance(prefs, dict) and prefs.get("food_style"):
#                 return str(prefs.get("food_style")).lower()
#     except Exception as e:
#         logger.warning("infer_food_style failed: %s", e)
#     return None
#
# def _resolve_style(style_hint: Optional[str], family_id: str, meal_date: str, meal_type: str) -> Optional[str]:
#     """
#     Dùng style_hint nếu có; nếu không thì suy luận từ submissions.
#     """
#     if style_hint and str(style_hint).strip():
#         return str(style_hint).strip().lower()
#     return _infer_food_style(family_id, meal_date, meal_type)
#
# def _fallback_suggestions(seed: str, style: Optional[str]) -> List[str]:
#     """
#     Rule-based gợi ý 3 món cùng phong cách/kỹ thuật.
#     """
#     # các nhóm cơ bản: style -> danh sách
#     catalog = {
#         "chinese": [
#             "Mapo Tofu (麻婆豆腐)",
#             "Stir-fried Green Beans (干煸四季豆)",
#             "Sweet and Sour Pork (糖醋里脊)",
#             "Garlic Bok Choy (蒜蓉青菜)",
#             "Beef Chow Fun (干炒牛河)"
#         ],
#         "vietnamese": [
#             "Bún thịt nướng",
#             "Gỏi gà xé phay",
#             "Thịt kho tiêu",
#             "Canh bí đỏ tôm",
#             "Cá kho tộ"
#         ],
#         "japanese": [
#             "Tonkatsu (とんかつ)",
#             "Karaage (からあげ)",
#             "Agedashi Tofu (揚げ出し豆腐)",
#             "Spinach Ohitashi (ほうれん草おひたし)",
#             "Gyudon (牛丼)"
#         ],
#         "western": [
#             "Herb Roasted Potatoes",
#             "Creamy Mushroom Pasta",
#             "Grilled Lemon Chicken",
#             "Roasted Vegetables",
#             "Tomato Basil Soup"
#         ],
#         "italian": [
#             "Spaghetti Carbonara",
#             "Margherita Pizza",
#             "Penne Arrabbiata",
#             "Minestrone Soup",
#             "Caprese Salad"
#         ],
#         "mexican": [
#             "Chicken Fajitas",
#             "Beef Tacos",
#             "Chilaquiles",
#             "Mexican Street Corn",
#             "Chicken Enchiladas"
#         ]
#     }
#     pool = catalog.get((style or "").lower(), [])
#     # Nếu không xác định được style → suy luận nhanh từ seed
#     if not pool:
#         low = seed.lower()
#         if any(k in low for k in ["kung pao", "tofu", "bok choy", "chow", "sour", "wok"]):
#             pool = catalog["chinese"]
#         elif any(k in low for k in ["pho", "bun ", "kho", "canh", "goi "]):
#             pool = catalog["vietnamese"]
#         elif any(k in low for k in ["ramen", "teriyaki", "tempura", "katsu", "gyudon"]):
#             pool = catalog["japanese"]
#         elif any(k in low for k in ["pizza", "spaghetti", "penne", "minestrone", "caprese"]):
#             pool = catalog["italian"]
#         elif any(k in low for k in ["taco", "fajita", "enchilada", "chilaquiles"]):
#             pool = catalog["mexican"]
#         else:
#             pool = catalog["western"]
#
#     # Loại seed khỏi pool, lấy 3 món đầu
#     out = []
#     for x in pool:
#         if len(out) >= 3:
#             break
#         if x.strip().lower() != seed.strip().lower():
#             out.append(x)
#     # nếu chưa đủ 3 thì thêm “variants”
#     while len(out) < 3:
#         out.append(f"{seed} – Variant {len(out)+1}")
#     return out[:3]
#
#
# def _llm_suggestions(seed: str, style_hint: Optional[str]) -> List[str]:
#     """
#     Gợi ý 3 món bằng LLM. Luôn trả về list độ dài 3.
#     """
#     if OPENAI_CONFIG.get("debug", False):
#         return _fallback_suggestions(seed, style_hint)
#
#     try:
#         system = "You suggest related dishes. Always respond with JSON: {\"suggestions\": [\"dish1\", \"dish2\", \"dish3\"]}. English only."
#         user = (
#             "Given a seed dish, propose 3 other dishes with similar cuisine style OR cooking technique. "
#             "Avoid duplicates; keep names concise (English name with optional Chinese in parentheses if appropriate). "
#             f"Seed dish: {seed}\n"
#             f"Style hint (optional): {style_hint or ''}\n"
#             "Return JSON only."
#         )
#
#         resp = client.chat.completions.create(
#             model=OPENAI_CONFIG["model"],
#             messages=[
#                 {"role": "system", "content": system},
#                 {"role": "user", "content": user},
#             ],
#             response_format={"type": "json_object"},
#             temperature=0.6,
#             max_tokens=300,
#         )
#         content = resp.choices[0].message.content
#         data = json.loads(content)
#         arr = [str(x).strip() for x in (data.get("suggestions") or []) if str(x).strip()]
#         arr = [x for x in arr if x.lower() != seed.strip().lower()]
#         if len(arr) < 3:
#             # bổ sung từ fallback để đủ 3
#             need = 3 - len(arr)
#             arr.extend(_fallback_suggestions(seed, style_hint)[:need])
#         return arr[:3]
#     except Exception as e:
#         logger.warning("LLM suggestions failed, fallback. err=%s", e)
#         return _fallback_suggestions(seed, style_hint)
#
#
# @router.post("/suggest")
# def suggest_dishes(
#     seed_dish: str = Body(..., embed=True, description="Dish picked by wheel"),
#     family_id: str = Body(..., embed=True),
#     meal_date: str = Body(..., embed=True),
#     meal_type: str = Body(..., embed=True),
#     style: Optional[str] = Body(None, embed=True),  # optional, cho phép FE gửi hint
# ):
#     """
#     Trả về 3 gợi ý món dựa trên seed dish (POST).
#     """
#     try:
#         log_api_call("/plan/suggest", "POST")
#         seed = (seed_dish or "").strip()
#         if not seed:
#             raise HTTPException(400, "seed_dish is required")
#
#         style_hint = _resolve_style(style, family_id, meal_date, meal_type)
#         suggestions = _llm_suggestions(seed, style_hint)
#         return {"ok": True, "seed": seed, "suggestions": suggestions}
#     except HTTPException:
#         raise
#     except Exception as e:
#         log_error(e, "plan.suggest_dishes")
#         raise HTTPException(500, "Internal server error")
#
# @router.get("/suggest")
# def suggest_dishes_get(
#     dish: str = Query(..., description="Seed dish (picked on the wheel)"),
#     meal_type: Optional[str] = Query(None),
#     style: Optional[str] = Query(None, description="Optional cuisine style hint"),
#     family_id: Optional[str] = Query(None),
#     meal_date: Optional[str] = Query(None),
# ):
#     """
#     GET variant cho FE: /api/plan/suggest?dish=...&meal_type=...&style=...&family_id=...&meal_date=...
#     - Nếu có style -> dùng luôn; nếu không, và có đủ (family_id, meal_date, meal_type) -> suy luận từ DB.
#     """
#     try:
#         log_api_call("/plan/suggest", "GET")
#         seed = (dish or "").strip()
#         if not seed:
#             raise HTTPException(400, "dish is required")
#
#         if style and style.strip():
#             style_hint = style.strip().lower()
#         elif family_id and meal_date and meal_type:
#             style_hint = _infer_food_style(family_id, meal_date, meal_type)
#         else:
#             style_hint = None
#
#         suggestions = _llm_suggestions(seed, style_hint)
#         return {"ok": True, "seed": seed, "suggestions": suggestions}
#     except HTTPException:
#         raise
#     except Exception as e:
#         log_error(e, "plan.suggest_dishes_get")
#         raise HTTPException(500, "Internal server error")

@router.get("/active-context")
def get_active_context(
    family_id: str = Query(..., description="Family ID"),
):
    """
    Trả về active meal context cho family:
    - Nếu bảng families.active_meal_date/type có giá trị -> dùng luôn
    - Nếu null -> suy luận theo family_meal_settings và thời điểm hiện tại
    """
    try:
        log_api_call("/plan/active-context", "GET")

        fam = db_query(
            "SELECT active_meal_date, active_meal_type FROM families WHERE family_id=%s",
            (family_id,)
        )
        if not fam:
            raise HTTPException(404, "Family not found")

        active_date = fam[0]["active_meal_date"]
        active_type = fam[0]["active_meal_type"]

        if not active_date or not active_type:
            # fallback theo family_meal_settings và thời gian hiện tại
            ms = db_query(
                "SELECT breakfast_start, lunch_start, dinner_start FROM family_meal_settings WHERE family_id=%s",
                (family_id,)
            )
            from datetime import datetime, time, date
            today = date.today()
            now = datetime.now().time()
            if ms:
                b = ms[0]["breakfast_start"] or time(7,0)
                l = ms[0]["lunch_start"] or time(11,0)
                d = ms[0]["dinner_start"] or time(17,0)
                # đơn giản: nếu < lunch_start => breakfast, < dinner_start => lunch, else dinner
                meal_type = "breakfast" if now < l else ("lunch" if now < d else "dinner")
            else:
                meal_type = "dinner"
            return {"family_id": family_id, "meal_date": today.isoformat(), "meal_type": meal_type}

        return {
            "family_id": family_id,
            "meal_date": active_date.isoformat() if hasattr(active_date, "isoformat") else str(active_date),
            "meal_type": active_type
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "plan.get_active_context")
        raise HTTPException(500, "Internal server error")


@router.post("/active-context")
def set_active_context(
    family_id: str = Query(..., description="Family ID"),
    meal_date: str = Query(..., description="YYYY-MM-DD"),
    meal_type: str = Query(..., description="breakfast|lunch|dinner"),
    user_id: str = Query(..., description="Must be holder"),
):
    """
    Holder đặt “active meal context” cho cả gia đình.
    - Chỉ holder được phép.
    - Ghi vào families.active_meal_date/active_meal_type (+ audit who/when).
    """
    try:
        log_api_call("/plan/active-context", "POST", user_id)

        # chỉ holder
        mem = db_query(
            "SELECT role FROM family_memberships WHERE family_id=%s AND user_id=%s",
            (family_id, user_id)
        )
        if not mem or mem[0]["role"] != "holder":
            raise HTTPException(403, "Only holder can set active meal context")

        # normalize meal_type
        mt = (meal_type or "").lower().strip()
        if mt not in ("breakfast", "lunch", "dinner"):
            raise HTTPException(400, "Invalid meal_type")

        # validate date
        import datetime as _dt
        try:
            _dt.datetime.strptime(meal_date, "%Y-%m-%d")
        except Exception:
            raise HTTPException(400, "Invalid meal_date")

        db_execute(
            """
            UPDATE families
               SET active_meal_date=%s,
                   active_meal_type=%s,
                   active_updated_by=%s,
                   active_updated_at=NOW()
             WHERE family_id=%s
            """,
            (meal_date, mt, user_id, family_id)
        )
        return {"ok": True, "family_id": family_id, "meal_date": meal_date, "meal_type": mt}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "plan.set_active_context")
        raise HTTPException(500, "Internal server error")

# ================== ROUTES ==================

@router.post("/generate")
def generate_plan(req: GenerateRequest):
    """
    Generate meal plan centered around wheel/AI theme (if present in remarks).
    - Nhận anchors/hard_lock từ FE (optional) rồi post-process.
    - Gọi _llm_generate_plan để tôn trọng THEME từ remarks (Wheel/AI hint).
    - Lưu DB và trả về plan_id cho FE redirect.
    """
    try:
        log_api_call("/plan/generate", "POST")

        # --- Validate đầu vào ---
        if not (req.family_id and req.meal_date and req.meal_type):
            raise HTTPException(400, "Missing family_id / meal_date / meal_type")

        # --- Chuẩn bị people cho LLM từ submissions ---
        people = []
        for sub in (req.submissions or []):
            prefs = sub.get("preferences") or {}
            # FE có thể gửi liked_tastes -> map sang likes (LLM dùng key 'likes')
            if "likes" not in prefs and "liked_tastes" in prefs:
                prefs["likes"] = prefs.get("liked_tastes", []) or []

            person = {
                "person_role": sub.get("role") or "member",
                "display_name": sub.get("display_name") or sub.get("user_id") or "member",
                "is_chef": bool(prefs.get("is_chef", False)),
                "tasks": prefs.get("tasks", []),
                "food_style": prefs.get("food_style", ""),
                "likes": prefs.get("likes", []),
                "dislikes": prefs.get("dislikes", []),
                "allergies": prefs.get("allergies", ""),
                # remark rất quan trọng: Wheel/AI hint đã nhét THEME ở đây
                "remark": sub.get("remark", "") or "",
            }
            people.append(person)

        # --- Headcount & time ---
        # Ưu tiên tổng người thực tế từ submissions (nếu FE có gửi participant_count),
        # nếu không có thì rơi về số submission hoặc req.headcount.
        headcount_from_subs = 0
        for sub in (req.submissions or []):
            headcount_from_subs += _norm_participant_count(sub)

        if headcount_from_subs > 0:
            headcount = headcount_from_subs
        else:
            headcount = req.headcount or (len(people) or 1)

        default_time = get_meal_time_by_type(req.meal_type)  # '08:00'/'12:00'/'18:00'
        dinner_time = f"{req.meal_date} {default_time}:00"

        # === Số participant để hiển thị (đúng luật: người có role/chef/tasks HOẶC cung cấp >=1 món)
        participants_count = _count_participants_union(
            req.submissions or [], req.family_id, req.meal_date, req.meal_type
        )


        # === WHEEL-FIRST MODE: nếu phiên có wheel data hợp lệ thì sinh plan theo luật wheel ===
        # Lấy winner ép từ FE qua anchors[0] (nếu có) — chính là kết quả spin.
        forced_winner = None
        if isinstance(req.anchors, list) and req.anchors:
            forced_winner = (req.anchors[0] or "").strip() or None

        wheel_plan_try = _generate_plan_wheel_mode(
            family_id=req.family_id,
            meal_date=req.meal_date,
            meal_type=req.meal_type,
            headcount_hint=headcount,
            people_payload=people,
            forced_winner=forced_winner,   # <-- NEW
        )

        if wheel_plan_try:
            # Chuẩn hoá về LAN schema và LƯU như bình thường, bỏ qua _llm_generate_plan
            family_info = {"family_id": req.family_id, "family_name": ""}
            wheel_raw_dishes = list(wheel_plan_try.get("dishes") or [])
            plan_obj = coerce_to_lan_schema(wheel_plan_try, dinner_time, headcount, family_info=family_info)
            plan_obj = _inject_generation_reasons(wheel_raw_dishes, plan_obj)
            plan_obj = _ensure_video_urls(plan_obj)
            plan_obj = _mirror_video_field(plan_obj)

            try:
                plan_obj.setdefault("meta", {})["participants_display"] = participants_count
                plan_obj["meta"]["headcount"] = participants_count
            except Exception:
                pass

            # Neo anchors nếu có (vẫn hữu ích để pin vị trí/lock)
            if req.anchors:
                plan_obj = postprocess_with_anchors(plan_obj, anchors=req.anchors or [], hard_lock=bool(req.hard_lock))

            html = render_plan_html(plan_obj)
            plan_code = f"{req.family_id}_{int(datetime.datetime.now().timestamp())}"
            meal_code_value = ""

            plan_id = db_execute(
                """
                INSERT INTO plans
                  (plan_code, family_id, meal_type, meal_date, source_date, submission_cnt, plan_json, plan_html, model_raw, meal_code, comment)
                VALUES
                  (%s,        %s,        %s,        %s,        %s,          %s,              %s,        %s,        %s,        %s,       %s)
                """,
                (
                    plan_code,
                    req.family_id,
                    req.meal_type,
                    req.meal_date,
                    req.meal_date,
                    len(people),
                    json.dumps(plan_obj, ensure_ascii=False),
                    html,
                    json.dumps({"mode": "wheel-first"}, ensure_ascii=False),
                    meal_code_value,
                    (req.feedback or "").strip(),
                ),
            )

            return {
                "ok": True,
                "plan_id": plan_id,
                "plan_json": plan_obj,
                "plan_html": html,
                "family_id": req.family_id,
                "meal_date": req.meal_date,
                "meal_type": req.meal_type,
            }

        # --- Gọi LLM (có logic THEME/forced menu bên trong) ---
        payload = {
            "date": req.meal_date,
            "meal_type": req.meal_type,
            "headcount": headcount,
            "people": people,
            "schema": PLAN_SCHEMA_EXAMPLE,  # cho model biết cấu trúc
        }
        plan_obj_raw, model_raw = _llm_generate_plan(payload)
        raw_dishes = list(plan_obj_raw.get("dishes") or [])

        family_info = {"family_id": req.family_id, "family_name": ""}

        plan_obj = coerce_to_lan_schema(plan_obj_raw, dinner_time, headcount, family_info=family_info)
        plan_obj = _inject_generation_reasons(raw_dishes, plan_obj)
        plan_obj = _ensure_video_urls(plan_obj)
        plan_obj = _mirror_video_field(plan_obj)

        # === Display participants = number of dishes ===
        try:
            plan_obj.setdefault("meta", {})["participants_display"] = participants_count
            plan_obj["meta"]["headcount"] = participants_count
        except Exception:
            pass

        # --- Neo anchors (nếu FE có gửi) ---
        if req.anchors:
            plan_obj = postprocess_with_anchors(plan_obj, anchors=req.anchors or [], hard_lock=bool(req.hard_lock))

        # --- Render HTML để xem nhanh ---
        html = render_plan_html(plan_obj)

        # --- Lưu DB và trả về plan_id ---
        plan_code = f"{req.family_id}_{int(datetime.datetime.now().timestamp())}"
        meal_code_value = ""  # legacy off
        plan_id = db_execute(
            """
            INSERT INTO plans
              (plan_code, family_id, meal_type, meal_date, source_date, submission_cnt, plan_json, plan_html, model_raw, meal_code, comment)
            VALUES
              (%s,        %s,        %s,        %s,        %s,          %s,              %s,        %s,        %s,        %s,       %s)
            """,
            (
                plan_code,
                req.family_id,
                req.meal_type,
                req.meal_date,
                req.meal_date,
                len(people),
                json.dumps(plan_obj, ensure_ascii=False),
                html,
                model_raw,
                meal_code_value,
                (req.feedback or "").strip(),
            ),
        )

        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_json": plan_obj,
            "plan_html": html,
            "family_id": req.family_id,
            "meal_date": req.meal_date,
            "meal_type": req.meal_type,
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, "plan.generate")
        raise HTTPException(500, "Internal server error")

@router.get("/id/{plan_id}")
def get_plan(plan_id: int):
    """获取计划详情"""
    try:
        log_api_call(f"/plan/{plan_id}", "GET")

        plan = db_query(
            """
            SELECT id, plan_code, family_id, meal_type, meal_date, source_date, submission_cnt, 
                   plan_json, plan_html, created_at, meal_code, comment
            FROM plans WHERE id = %s
            """,
            (plan_id,),
        )
        if not plan:
            raise HTTPException(404, "Plan not found")

        plan_data = plan[0]
        if plan_data.get("plan_json"):
            try:
                plan_data["plan_json"] = json.loads(plan_data["plan_json"])
            except Exception:
                plan_data["plan_json"] = {}

        return plan_data
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_plan {plan_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/family/{family_id}")
def list_plans(
    family_id: str,
    with_json: bool = Query(False, description="Return plan_json when true"),
):
    """获取家庭的所有计划"""
    try:
        log_api_call(f"/plan/family/{family_id}", "GET")

        if with_json:
            rows = db_query(
                """
                SELECT id, plan_code, created_at, meal_type, meal_date, family_id,
                       submission_cnt, meal_code, comment, plan_json
                FROM plans
                WHERE family_id=%s
                ORDER BY id DESC
                """,
                (family_id,),
            )
            # parse plan_json để FE dùng luôn
            for r in rows or []:
                pj = r.get("plan_json")
                if pj:
                    try:
                        r["plan_json"] = json.loads(pj)
                    except Exception:
                        # giữ nguyên nếu parse lỗi
                        pass
            return rows

        # nhánh mặc định (nhẹ, không trả plan_json)
        rows = db_query(
            """
            SELECT id, plan_code, created_at, meal_type, meal_date, family_id,
                   submission_cnt, meal_code, comment
            FROM plans
            WHERE family_id=%s
            ORDER BY id DESC
            """,
            (family_id,),
        )
        return rows
    except Exception as e:
        log_error(e, f"list_plans for family {family_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/latest")
def get_latest_plan(date: str = Query(None, description="Date in YYYY-MM-DD format")):
    """获取最新计划"""
    try:
        log_api_call("/plan/latest", "GET")

        if not date:
            from datetime import date as _date
            date = _date.today().isoformat()

        rows = db_query("SELECT * FROM plans WHERE meal_date=%s ORDER BY id DESC LIMIT 1", (date,))
        if not rows:
            raise HTTPException(404, "No plan for date")

        r = rows[0]
        if r.get("plan_json"):
            try:
                r["plan_json"] = json.loads(r["plan_json"])
            except Exception:
                r["plan_json"] = {}
        return r
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"get_latest_plan for date {date}")
        raise HTTPException(500, "Internal server error")


@router.post("/ingest")
def ingest_plan(request: PlanIngest):
    """导入外部计划"""
    try:
        log_api_call("/plan/ingest", "POST")

        fam = db_query("SELECT family_name FROM families WHERE family_id=%s", (request.family_id,))
        family_name = fam[0]["family_name"] if fam else ""

        subs_cnt = db_query("SELECT COUNT(*) c FROM info_submissions WHERE family_id=%s", (request.family_id,))[0]["c"]

        lan_plan = coerce_to_lan_schema(
            request.payload,
            dinner_time=f"{request.meal_date} {get_meal_time(request.meal_type)}",
            headcount=request.headcount or subs_cnt,
            family_info={"family_id": request.family_id, "family_name": family_name},
        )
        lan_plan = _ensure_video_urls(lan_plan)
        lan_plan = _mirror_video_field(lan_plan)

        # === Display participants = number of dishes ===
        try:
            dishes_count = len(lan_plan.get("dishes") or [])
            lan_plan.setdefault("meta", {})["participants_display"] = dishes_count
            lan_plan["meta"]["headcount"] = dishes_count
        except Exception:
            pass

        html = render_plan_html(lan_plan)

        plan_code = f"{request.family_id}_{int(datetime.datetime.now().timestamp())}"
        plan_id = db_execute(
            """
            INSERT INTO plans(plan_code,family_id,meal_type,meal_date,source_date,submission_cnt,plan_json,plan_html,model_raw) 
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                plan_code,
                request.family_id,
                request.meal_type,
                request.meal_date,
                request.meal_date,
                subs_cnt,
                json.dumps(lan_plan, ensure_ascii=False),
                html,
                json.dumps(request.payload, ensure_ascii=False),
            ),
        )

        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_json": lan_plan,
            "plan_html": html,
            "family_id": request.family_id,
            "meal_date": request.meal_date,
            "meal_type": request.meal_type,
            "submission_cnt": subs_cnt,
        }
    except Exception as e:
        log_error(e, f"ingest_plan for family {request.family_id}")
        raise HTTPException(500, "Internal server error")


@router.post("/{plan_id}/feedback")
def add_feedback(plan_id: int, feedback: str = Query(..., description="Feedback content")):
    """添加计划反馈"""
    try:
        log_api_call(f"/plan/{plan_id}/feedback", "POST")

        if not feedback:
            raise HTTPException(400, "feedback is required")

        db_execute("INSERT INTO feedbacks (plan_id, content) VALUES (%s, %s)", (plan_id, feedback))
        return {"ok": True, "message": "Feedback added successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"add_feedback for plan {plan_id}")
        raise HTTPException(500, "Internal server error")


@router.delete("/{plan_id}")
def delete_plan(plan_id: int, user_id: str):
    """删除计划（仅holder可以删除）"""
    try:
        log_api_call(f"/plan/{plan_id}", "DELETE", user_id)

        plan = db_query("SELECT family_id FROM plans WHERE id = %s", (plan_id,))
        if not plan:
            raise HTTPException(404, "Plan not found")
        family_id = plan[0]["family_id"]

        membership = db_query(
            "SELECT role FROM family_memberships WHERE family_id = %s AND user_id = %s",
            (family_id, user_id),
        )
        if not membership or membership[0]["role"] != "holder":
            raise HTTPException(403, "Only family holder can delete plans")

        db_execute("DELETE FROM plans WHERE id = %s", (plan_id,))
        logger.info("Plan %s deleted by holder %s", plan_id, user_id)
        return {"ok": True, "message": "Plan deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"delete_plan {plan_id}")
        raise HTTPException(500, "Internal server error")


@router.get("/meal-code/{meal_code}")
def get_plans_by_meal_code(meal_code: str):
    """根据meal code获取相关计划 (legacy)"""
    try:
        log_api_call(f"/plan/meal-code/{meal_code}", "GET")

        plans = db_query(
            """
            SELECT id, plan_code, family_id, meal_type, meal_date, source_date, 
                   submission_cnt, plan_json, plan_html, model_raw, created_at, meal_code, comment
            FROM plans WHERE meal_code = %s
            ORDER BY created_at DESC
            """,
            (meal_code,),
        )
        for plan in plans:
            if plan["plan_json"]:
                plan["plan_json"] = json.loads(plan["plan_json"])
        return plans
    except Exception as e:
        log_error(e, f"get_plans_by_meal_code for meal_code {meal_code}")
        raise HTTPException(500, "Internal server error")


@router.post("/{plan_id}/comment")
def add_comment(plan_id: int, user_name: str = Query(...), comment_text: str = Query(...)):
    """添加评论到计划"""
    try:
        log_api_call(f"/plan/{plan_id}/comment", "POST")

        plan = db_query("SELECT comment FROM plans WHERE id = %s", (plan_id,))
        if not plan:
            raise HTTPException(404, "Plan not found")
        current_comment = plan[0].get("comment") or ""

        new_comment_line = f"{user_name}: {comment_text}"
        updated_comment = (current_comment + "\n" + new_comment_line) if current_comment.strip() else new_comment_line

        db_execute("UPDATE plans SET comment = %s WHERE id = %s", (updated_comment, plan_id))
        logger.info("Comment added to plan %s by %s", plan_id, user_name)
        return {"ok": True, "message": "Comment added successfully", "comment": updated_comment}
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"add_comment for plan {plan_id}")
        raise HTTPException(500, "Internal server error")


@router.post("/{plan_id}/regenerate")
def regenerate_plan(plan_id: int, user_id: str = Query(..., description="User ID for permission check")):
    """基于comment重新生成计划（仅holder可操作）"""
    try:
        log_api_call(f"/plan/{plan_id}/regenerate", "POST", user_id)

        plan = db_query(
            """
            SELECT plan_code, family_id, meal_type, meal_date, source_date, 
                   submission_cnt, plan_json, meal_code, comment
            FROM plans WHERE id = %s
            """,
            (plan_id,),
        )
        if not plan:
            raise HTTPException(404, "Plan not found")
        plan_data = plan[0]

        membership = db_query(
            "SELECT role FROM family_memberships WHERE family_id = %s AND user_id = %s",
            (plan_data["family_id"], user_id),
        )
        if not membership or membership[0]["role"] != "holder":
            raise HTTPException(403, "Only family holder can regenerate plans")

        family = db_query("SELECT family_name FROM families WHERE family_id=%s", (plan_data["family_id"],))
        if not family:
            raise HTTPException(404, "Family not found")
        family_name = family[0]["family_name"]

        # NEW: submissions by (family_id, date, type); fallback to legacy meal_code
        meal_code = plan_data.get("meal_code") or ""
        subs = db_query(
            """
            SELECT * FROM info_submissions
            WHERE family_id=%s AND meal_date=%s AND meal_type=%s
            ORDER BY id ASC
            """,
            (plan_data["family_id"], plan_data["meal_date"], plan_data["meal_type"]),
        )
        if not subs and meal_code:
            subs = db_query("SELECT * FROM info_submissions WHERE meal_code=%s ORDER BY id ASC", (meal_code,))
        if not subs:
            raise HTTPException(400, "No submissions found for regeneration")

        original_plan_json = json.loads(plan_data["plan_json"]) if plan_data.get("plan_json") else {}
        headcount = original_plan_json.get("meta", {}).get("headcount", len(subs))

        roles_lines = []
        comments = plan_data.get("comment", "")
        for sub in subs:
            if isinstance(sub.get("preferences"), str):
                try:
                    preferences = json.loads(sub.get("preferences", "{}"))
                except Exception:
                    preferences = {}
            else:
                preferences = sub.get("preferences", {}) or {}

            # normalize FE keys
            if "likes" not in preferences and "liked_tastes" in preferences:
                preferences["likes"] = preferences.get("liked_tastes", []) or []

            username = sub.get("user_id", "member") or "member"
            remark = sub.get("remark", "")
            if comments:
                remark = (remark + "\n\nPrevious feedback:\n" + comments).strip()
            role_info = {
                "person_role": "member",
                "display_name": username,
                "is_chef": preferences.get("is_chef", False),
                "tasks": preferences.get("tasks", []),
                "food_style": preferences.get("food_style", ""),
                "likes": preferences.get("likes", []),
                "dislikes": preferences.get("dislikes", []),
                "allergies": preferences.get("allergies", ""),
                "remark": remark,
            }
            roles_lines.append(role_info)

        user_content = {
            "date": plan_data["meal_date"],
            "meal_type": plan_data["meal_type"],
            "headcount": headcount,
            "people": roles_lines,
            "schema": PLAN_SCHEMA_EXAMPLE,
        }

        plan_obj_raw, content = _llm_generate_plan(user_content)

        lan_plan = coerce_to_lan_schema(
            plan_obj_raw,
            dinner_time=f"{plan_data['meal_date']} {get_meal_time(plan_data['meal_type'])}",
            headcount=headcount,
            family_info={"family_id": plan_data["family_id"], "family_name": family_name},
        )
        lan_plan = _ensure_video_urls(lan_plan)
        lan_plan = _mirror_video_field(lan_plan)
        # === Display participants = number of dishes ===
        try:
            dishes_count = len(lan_plan.get("dishes") or [])
            lan_plan.setdefault("meta", {})["participants_display"] = dishes_count
            lan_plan["meta"]["headcount"] = dishes_count
        except Exception:
            pass

        html = render_plan_html(lan_plan)

        new_plan_code = f"{plan_data['family_id']}_{int(datetime.datetime.now().timestamp())}"
        new_plan_id = db_execute(
            """
            INSERT INTO plans
              (plan_code,family_id,meal_type,meal_date,source_date,submission_cnt,plan_json,plan_html,model_raw,meal_code,comment) 
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                new_plan_code,
                plan_data["family_id"],
                plan_data["meal_type"],
                plan_data["meal_date"],
                plan_data["source_date"],
                plan_data["submission_cnt"],
                json.dumps(lan_plan, ensure_ascii=False),
                html,
                content,
                meal_code,  # keep legacy value if any (can be None)
                f"Regenerated from plan #{plan_id}",
            ),
        )

        logger.info("Plan regenerated: old_plan_id=%s, new_plan_id=%s", plan_id, new_plan_id)
        return {
            "ok": True,
            "plan_id": new_plan_id,
            "plan_json": lan_plan,
            "plan_html": html,
            "family_id": plan_data["family_id"],
            "meal_date": plan_data["meal_date"],
            "meal_type": plan_data["meal_type"],
        }
    except HTTPException:
        raise
    except Exception as e:
        log_error(e, f"regenerate_plan for plan {plan_id}")
        raise HTTPException(500, "Internal server error")
