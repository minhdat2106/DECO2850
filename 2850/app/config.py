"""
Configuration settings for the Meal Planner application
"""
import os
from pathlib import Path

# Set environment variables if not already set
os.environ.setdefault('DB_HOST', '127.0.0.1')
os.environ.setdefault('DB_PORT', '3306')
os.environ.setdefault('DB_USER', 'Meal Planner')
os.environ.setdefault('DB_PASSWORD', 'NJTteam')
os.environ.setdefault('DB_NAME', 'meal_planner')
os.environ.setdefault('OPENAI_API_KEY', 'sk-OXQbbXXNnri4PxQFE3D0B04dF84f4fE1872a6644A0D19b43')
os.environ.setdefault('OPENAI_BASE_URL', 'https://api.v3.cm/v1/')
os.environ.setdefault('OPENAI_MODEL', 'gpt-4o-mini')
os.environ.setdefault('DEBUG_LLM', 'false')

# Database configuration
DB_CONFIG = {
    'host': os.environ.get('DB_HOST'),
    'port': int(os.environ.get('DB_PORT', 3306)),
    'user': os.environ.get('DB_USER'),
    'password': os.environ.get('DB_PASSWORD'),
    'database': os.environ.get('DB_NAME'),
    'charset': 'utf8mb4',
    'collation': 'utf8mb4_unicode_ci',
    'autocommit': True,
    'pool_name': 'meal_planner_pool',
    'pool_size': 10,
    'pool_reset_session': True
}

# OpenAI configuration
OPENAI_CONFIG = {
    'api_key': os.environ.get('OPENAI_API_KEY'),
    'base_url': os.environ.get('OPENAI_BASE_URL'),
    'model': os.environ.get('OPENAI_MODEL'),
    'debug': os.environ.get('DEBUG_LLM', 'false').lower() == 'true'
}

# Application configuration
APP_CONFIG = {
    'title': 'Meal Planner API',
    'description': 'A comprehensive meal planning and family management system',
    'version': '1.0.0',
    'debug': False
}

# Logging configuration
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# CORS configuration
CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:63342",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:63342",
]

# API configuration
API_PREFIX = "/api"
