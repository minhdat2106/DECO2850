# app.py
# -*- coding: utf-8 -*-
import os
import time
from pathlib import Path
from urllib.parse import quote_plus
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

# ---- Routers (giữ nguyên theo cấu trúc dự án của bạn) ----
# Nếu module của bạn ở path khác, chỉnh lại import cho đúng.
from routes import user, family, submission, plan, meal_code, message, wheel, preferences

# =====================================================================
# FastAPI app
# =====================================================================
app = FastAPI(title="Meal Planner API", version="1.0.0")

# ---------- CORS ----------
# Cho phép frontend local phát triển. Bổ sung origin khác nếu cần.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # chỉnh chặt hơn nếu cần
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Static mount ----------
# Frontend của bạn nằm trong thư mục "page/". Ví dụ: /page/index.html
# Nếu bạn đặt thư mục khác, chỉnh lại ở đây.
if Path("page").exists():
    app.mount("/page", StaticFiles(directory="page", html=False), name="page")

# =====================================================================
# Include routers
# =====================================================================
app.include_router(user.router, prefix="/api", tags=["user"])
app.include_router(family.router, prefix="/api", tags=["family"])
app.include_router(submission.router, prefix="/api", tags=["submission"])
app.include_router(plan.router, prefix="/api", tags=["plan"])
app.include_router(meal_code.router, prefix="/api", tags=["meal_code"])
app.include_router(message.router, prefix="/api", tags=["message"])
app.include_router(wheel.router, prefix="/api", tags=["wheel"])
app.include_router(preferences.router, prefix="", tags=["preferences"])  # router này đã tự có prefix "/preferences"

# =====================================================================
# Healthcheck & convenience routes
# =====================================================================

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/health")
def api_health_alias():
    # Alias để khớp với FE (auth.js) đang gọi /api/health
    return {"ok": True}

@app.get("/")
def root():
    """
    Điều hướng nhanh vào giao diện nếu chạy same-origin
    """
    if Path("page/index.html").exists():
        return RedirectResponse(url="/page/index.html", status_code=302)
    return {"message": "Meal Planner API is running."}

# =====================================================================
# Helpers & Scraper (NO DB, NO SLUG)
# =====================================================================

def _is_https(u: Optional[str]) -> bool:
    return bool(u) and str(u).strip().lower().startswith("https://")

def _first_non_empty(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

# ---- In-memory cache (name -> (expire_ts, url)) ----
_IMG_CACHE = {}
_IMG_CACHE_TTL = 60 * 60 * 24 * 7  # 7 ngày

def _cache_get(name: str) -> Optional[str]:
    key = (name or "").lower().strip()
    if not key:
        return None
    rec = _IMG_CACHE.get(key)
    if not rec:
        return None
    exp, url = rec
    if time.time() > exp:
        _IMG_CACHE.pop(key, None)
        return None
    return url

def _cache_put(name: str, url: str) -> None:
    key = (name or "").lower().strip()
    if not key or not url:
        return
    _IMG_CACHE[key] = (time.time() + _IMG_CACHE_TTL, url)

def _wiki_thumb(name: str) -> Optional[str]:
    """
    Thử lấy thumbnail từ Wikipedia (ưu tiên vi, fallback en).
    Dùng API chính thức để ổn định.
    """
    q = (name or "").strip()
    if not q:
        return None

    # vi.wikipedia
    vi_url = (
        "https://vi.wikipedia.org/w/api.php"
        f"?action=query&titles={quote_plus(q)}&prop=pageimages"
        "&format=json&pithumbsize=800&redirects=1"
    )
    try:
        r = requests.get(vi_url, timeout=6)
        j = r.json()
        pages = j.get("query", {}).get("pages", {})
        for p in pages.values():
            thumb = (p or {}).get("thumbnail", {})
            src = thumb.get("source")
            if _is_https(src):
                return src
    except Exception:
        pass

    # en.wikipedia
    en_url = (
        "https://en.wikipedia.org/w/api.php"
        f"?action=query&titles={quote_plus(q)}&prop=pageimages"
        "&format=json&pithumbsize=800&redirects=1"
    )
    try:
        r = requests.get(en_url, timeout=6)
        j = r.json()
        pages = j.get("query", {}).get("pages", {})
        for p in pages.values():
            thumb = (p or {}).get("thumbnail", {})
            src = thumb.get("source")
            if _is_https(src):
                return src
    except Exception:
        pass

    return None

def _duckduckgo_first_result_url(q: str) -> Optional[str]:
    """
    Tìm link kết quả đầu tiên từ DuckDuckGo HTML (miễn phí).
    """
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        a = soup.select_one("a.result__a")
        if a and a.get("href"):
            return a["href"]
    except Exception:
        pass
    return None

def _page_og_image(url: str) -> Optional[str]:
    """
    Lấy <meta property='og:image'> từ trang đích; nếu không có,
    lấy <img> lớn đầu tiên.
    """
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        meta = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
        if meta:
            src = _first_non_empty(meta.get("content"), meta.get("value"))
            if _is_https(src):
                return src

        # Fallback: ảnh <img> lớn
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not _is_https(src):
                continue
            # loại icon nhỏ bằng heuristic width/height nếu có
            w = (img.get("width") or "").strip()
            h = (img.get("height") or "").strip()
            if (w.isdigit() and int(w) >= 400) or (h.isdigit() and int(h) >= 300):
                return src
    except Exception:
        pass
    return None

def _scrape_dish_image(name: str) -> Optional[str]:
    """
    Chiến lược: Wikipedia (vi -> en) -> kết quả web đầu tiên -> og:image
    """
    # 1) Wikipedia: thường có ảnh chuẩn & hợp lý
    wiki = _wiki_thumb(name)
    if _is_https(wiki):
        return wiki

    # 2) Tìm kiếm web miễn phí
    queries = [f"{name} món ăn", f"{name} recipe"]
    for q in queries:
        link = _duckduckgo_first_result_url(q)
        if not link:
            continue
        og = _page_og_image(link)
        if _is_https(og):
            return og

    return None

# =====================================================================
# ENDPOINTS ẢNH MÓN ĂN (KHÔNG DÙNG SLUG, KHÔNG DÙNG DB)
# =====================================================================

images_router = APIRouter(prefix="/api/images", tags=["images"])

@images_router.get("/scrape")
def scrape_image_for_dish(name: str):
    """
    Trả về {src: "<https url>"} cho tên món ăn.
    - Dùng requests + bs4 + Wikipedia API.
    - Cache bộ nhớ 7 ngày.
    """
    n = (name or "").strip()
    if not n:
        return {"src": "/page/images/dishes/placeholder.jpg"}

    cached = _cache_get(n)
    if cached:
        return {"src": cached}

    src = _scrape_dish_image(n)
    # Nếu vẫn không tìm được, fallback Unsplash (miễn phí, không cần API key)
    if not _is_https(src):
        src = f"https://source.unsplash.com/640x400/?{quote_plus(n + ' dish food')}"

    _cache_put(n, src)
    return {"src": src}

@images_router.get("/dish")
def api_images_dish(name: str, square: int = 512):
    """
    Giữ endpoint ngắn gọn cho FE: /api/images/dish?name=...
    Không dùng slug. Ưu tiên kết quả scraper; nếu không có thì dùng Unsplash.
    """
    n = (name or "").strip()
    if not n:
        return {"src": "/page/images/dishes/placeholder.jpg"}

    cached = _cache_get(n)
    if cached:
        return {"src": cached}

    # thử wiki/bs4
    src = _scrape_dish_image(n)
    if not _is_https(src):
        # ép về kích thước vuông nếu FE cần
        try:
            s = int(square)
        except Exception:
            s = 512
        s = min(max(s, 64), 1600)
        src = f"https://source.unsplash.com/{s}x{s}/?{quote_plus(n + ' dish food')}"

    _cache_put(n, src)
    return {"src": src}

@images_router.get("/for-dish")
def api_images_for_dish(name: str, w: int = 640, h: int = 400):
    """
    Endpoint tương thích cách gọi cũ: /api/images/for-dish?name=&w=&h=
    Không dùng slug; chạy qua scraper trước, nếu không được thì Unsplash.
    """
    n = (name or "").strip()
    if not n:
        return {"src": "/page/images/dishes/placeholder.jpg"}

    cached = _cache_get(n)
    if cached:
        return {"src": cached}

    src = _scrape_dish_image(n)
    if not _is_https(src):
        try:
            w = int(w); h = int(h)
        except Exception:
            w, h = 640, 400
        w = min(max(w, 64), 1920)
        h = min(max(h, 64), 1080)
        src = f"https://source.unsplash.com/{w}x{h}/?{quote_plus(n + ' dish food')}"

    _cache_put(n, src)
    return {"src": src}

# Gắn router ảnh vào app
app.include_router(images_router)

# =====================================================================
# (TÙY CHỌN) Các endpoint tương thích cũ
# =====================================================================
# Nếu các file HTML cũ gọi /api/plans… bạn có thể ánh xạ nhẹ sang router mới.
# Bỏ comment nếu cần dùng (tuỳ dự án của bạn).
#
# from fastapi import Depends
# @app.get("/api/plans")
# def compat_list_plans():
#     # Gợi ý: gọi hàm/SQL thực sự trong routes.plan thay vì lặp lại logic
#     return {"detail": "Use /api/plan/family/{family_id}?with_json=1 instead."}

# =====================================================================
# Error handler mẫu (tuỳ chọn)
# =====================================================================
@app.exception_handler(Exception)
async def default_exception_handler(request: Request, exc: Exception):
    # Log ở đây nếu cần
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )

# =====================================================================
# Uvicorn launcher
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    # Port mặc định 8765 để khớp file auth.js của bạn
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)

