"""
Database connection and query utilities
"""
import logging
from typing import List, Dict, Any

import mysql.connector
from mysql.connector import pooling

from config import DB_CONFIG

logger = logging.getLogger("meal")

# Global connection pool
connection_pool = None


def init_database() -> bool:
    """Initialize database connection pool"""
    global connection_pool
    try:
        # Thêm timeout ngắn để không bao giờ treo khi DB lỗi
        db_args = dict(DB_CONFIG)
        db_args.setdefault("connection_timeout", 3)  # giây

        connection_pool = pooling.MySQLConnectionPool(**db_args)
        logger.info("Database connection pool initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize database connection pool: {e}")
        return False


def get_connection():
    """Get a connection from the pool"""
    global connection_pool
    if connection_pool is None:
        ok = init_database()
        if not ok:
            raise RuntimeError("Database pool is not available")
    return connection_pool.get_connection()


def db_query(sql: str, params: tuple = None) -> List[Dict[str, Any]]:
    """
    Execute a SELECT query and return results as list of dictionaries

    Args:
        sql: SQL query string
        params: Query parameters tuple

    Returns:
        List of dictionaries representing query results
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        return rows
    except Exception as e:
        logger.error(f"Database query error: {e}")
        logger.error(f"SQL: {sql}")
        logger.error(f"Params: {params}")
        raise
    finally:
        if conn:
            conn.close()


def db_execute(sql: str, params: tuple = None) -> int:
    """
    Execute an INSERT/UPDATE/DELETE query and return the last row ID

    Args:
        sql: SQL query string
        params: Query parameters tuple

    Returns:
        Last row ID for INSERT operations, affected rows for others
    """
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()

        # Return last row ID for INSERT, affected rows for others
        if sql.strip().upper().startswith('INSERT'):
            return cur.lastrowid
        else:
            return cur.rowcount
    except Exception as e:
        logger.error(f"Database execute error: {e}")
        logger.error(f"SQL: {sql}")
        logger.error(f"Params: {params}")
        raise
    finally:
        if conn:
            conn.close()


def test_connection() -> bool:
    """Test database connection"""
    try:
        result = db_query("SELECT 1 as test")
        return len(result) > 0 and result[0]['test'] == 1
    except Exception as e:
        logger.error(f"Database connection test failed: {e}")
        return False


def close_pool():
    """Close all connections in the pool"""
    global connection_pool
    if connection_pool:
        # mysql-connector pool không có close_all; GC sẽ dọn dẹp
        connection_pool = None
        logger.info("Database connection pool closed")
