"""
Main FastAPI application for the Meal Planner system
"""
import logging
import pathlib
from collections import deque
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Import our modules
from config import APP_CONFIG, CORS_ORIGINS, LOG_DIR
from database import init_database, test_connection
from routes import user, family, submission, plan

# Setup logging
logger = logging.getLogger("meal")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

fh = logging.FileHandler(LOG_DIR / "meal.log", encoding="utf-8")
fh.setFormatter(fmt)
sh = logging.StreamHandler()
sh.setFormatter(fmt)

if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(sh)

# Global debug storage
LLM_DEBUG = deque(maxlen=20)

# Create FastAPI app
app = FastAPI(
    title=APP_CONFIG['title'],
    description=APP_CONFIG['description'],
    version=APP_CONFIG['version'],
    debug=APP_CONFIG['debug']
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/page", StaticFiles(directory="page"), name="page")

# Include routers
app.include_router(user.router)
app.include_router(family.router)
app.include_router(submission.router)
app.include_router(plan.router)

# Health check endpoint
@app.get("/api/health")
def health():
    """Health check endpoint"""
    try:
        db_ok = test_connection()
        return {"ok": True, "db": db_ok}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"ok": False, "db": False, "error": str(e)}

# Root endpoint
@app.get("/")
def root():
    """Root endpoint"""
    return {"message": "Meal Planner API", "version": APP_CONFIG['version']}

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize application on startup"""
    logger.info("Starting Meal Planner API...")
    
    # Initialize database
    if init_database():
        logger.info("Database initialized successfully")
    else:
        logger.error("Failed to initialize database")
    
    logger.info("Meal Planner API started successfully")

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down Meal Planner API...")
    from database import close_pool
    close_pool()
    logger.info("Meal Planner API shutdown complete")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
