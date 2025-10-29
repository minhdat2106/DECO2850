# app/app.py
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

# ===== Import routers (dùng tuyệt đối để ổn định) =====
from app.routes import user, family, submission, plan, meal_code, message, wheel, preferences

# =====================================================================
# FastAPI app
# =====================================================================
app = FastAPI(title="Meal Planner API", version="1.0.0")

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # siết lại nếu muốn
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Static mount ----------
BASE_DIR = Path(__file__).resolve().parent
PAGE_DIR = BASE_DIR / "page"
if PAGE_DIR.exists():
    app.mount("/page", StaticFiles(directory=str(PAGE_DIR), html=False), name="page")
elif Path("page").exists():
    app.mount("/page", StaticFiles(directory="page", html=False), name="page")

# =====================================================================
# Include routers
#  Quy ước: prefix đặt HẾT ở đây.
#  -> Trong các router (user.py, family.py, …) phải là APIRouter() KHÔNG prefix.
#  -> Ví dụ user.py định nghĩa @router.post("/login") thì URL thực tế là /api/user/login
# =====================================================================
app.include_router(user.router,        prefix="/api/user",        tags=["user"])
app.include_router(family.router,      prefix="/api/family",      tags=["family"])
app.include_router(submission.router,  prefix="/api/submission",  tags=["submission"])
app.include_router(plan.router,        prefix="/api/plan",        tags=["plan"])
app.include_router(meal_code.router,   prefix="/api/meal-code",   tags=["meal_code"])
app.include_router(message.router,     prefix="/api/message",     tags=["message"])
app.include_router(wheel.router,       prefix="/api/wheel",       tags=["wheel"])
# Router preferences ĐÃ có prefix nội bộ "/preferences" -> include không cần prefix
app.include_router(preferences.router,                           tags=["preferences"])

# =====================================================================
# Healthcheck & convenience routes
# =====================================================================
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/health")
def api_health_alias():
    return {"ok": True}

@app.get("/")
def root():
    """
    Điều hướng nhanh vào giao diện nếu chạy same-origin
    """
    if (BASE_DIR / "page" / "index.html").exists() or Path("page/index.html").exists():
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
_IMG_CACHE: dict[str, tuple[float, str]] = {}
_IMG_CACHE_TTL = 60 * 60 * 24 * 7  # 7 ngày

def _cache_get(name: str) -> Optional[str]:
    key = (name or "").lower().strip()
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
    if key and url:
        _IMG_CACHE[key] = (time.time() + _IMG_CACHE_TTL, url)

def _wiki_thumb(name: str) -> Optional[str]:
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
            src = (p or {}).get("thumbnail", {}).get("source")
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
            src = (p or {}).get("thumbnail", {}).get("source")
            if _is_https(src):
                return src
    except Exception:
        pass

    return None

def _duckduckgo_first_result_url(q: str) -> Optional[str]:
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
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        meta = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
        if meta:
            src = _first_non_empty(meta.get("content"), meta.get("value"))
            if _is_https(src):
                return src
        # Fallback: <img> lớn
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not _is_https(src):
                continue
            w = (img.get("width") or "").strip()
            h = (img.get("height") or "").strip()
            if (w.isdigit() and int(w) >= 400) or (h.isdigit() and int(h) >= 300):
                return src
    except Exception:
        pass
    return None

def _scrape_dish_image(name: str) -> Optional[str]:
    wiki = _wiki_thumb(name)
    if _is_https(wiki):
        return wiki
    for q in (f"{name} món ăn", f"{name} recipe"):
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
    n = (name or "").strip()
    if not n:
        return {"src": "/page/images/dishes/placeholder.jpg"}
    cached = _cache_get(n)
    if cached:
        return {"src": cached}
    src = _scrape_dish_image(n)
    if not _is_https(src):
        src = f"https://source.unsplash.com/640x400/?{quote_plus(n + ' dish food')}"
    _cache_put(n, src)
    return {"src": src}

@images_router.get("/dish")
def api_images_dish(name: str, square: int = 512):
    n = (name or "").strip()
    if not n:
        return {"src": "/page/images/dishes/placeholder.jpg"}
    cached = _cache_get(n)
    if cached:
        return {"src": cached}
    src = _scrape_dish_image(n)
    if not _is_https(src):
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

app.include_router(images_router)

# =====================================================================
# Error handler mặc định
# =====================================================================
@app.exception_handler(Exception)
async def default_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )

# =====================================================================
# Debug route map khi startup (giúp bắt lỗi 404/405)
# =====================================================================
@app.on_event("startup")
async def _print_routes():
    try:
        for r in app.routes:
            methods = ",".join(sorted((r.methods or [])))
            print(f"[ROUTE] {methods:15} {r.path}")
    except Exception:
        pass

# =====================================================================
# Uvicorn launcher (local)
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run("app.app:app", host="0.0.0.0", port=port, reload=True)
