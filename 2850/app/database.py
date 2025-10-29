import os
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse
import logging

logger = logging.getLogger("meal")

connection_pool = None

def init_database():
    global connection_pool
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("Missing DATABASE_URL environment variable")

        result = urlparse(db_url)
        connection_pool = pool.SimpleConnectionPool(
            1, 10,
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port,
            database=result.path[1:],
            sslmode="require"
        )
        logger.info("PostgreSQL connection pool initialized.")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize DB: {e}")
        return False


def get_connection():
    global connection_pool
    if connection_pool is None:
        init_database()
    return connection_pool.getconn()


def release_connection(conn):
    global connection_pool
    if connection_pool:
        connection_pool.putconn(conn)


def db_query(sql, params=None):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows
    except Exception as e:
        logger.error(f"Query error: {e}")
        raise
    finally:
        if conn:
            release_connection(conn)


def db_execute(sql, params=None):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.error(f"Execute error: {e}")
        raise
    finally:
        if conn:
            release_connection(conn)
