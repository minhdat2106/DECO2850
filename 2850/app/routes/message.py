"""
Message and notification API routes
"""
import json
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from database import db_query, db_execute
from utils import log_api_call, log_error

logger = logging.getLogger("meal")
router = APIRouter(prefix="/api/messages", tags=["message"])

@router.get("/user/{user_id}")
def get_user_messages(user_id: str):
    """获取用户的所有消息"""
    try:
        log_api_call(f"/messages/user/{user_id}", "GET")
        
        messages = db_query("""
            SELECT id, type, title, content, action_url, read_status, created_at
            FROM messages 
            WHERE user_id = %s 
            ORDER BY created_at DESC
        """, (user_id,))
        
        # 转换数据格式
        result = []
        for msg in messages:
            result.append({
                "id": msg["id"],
                "type": msg["type"],
                "title": msg["title"],
                "content": msg["content"],
                "action_url": msg["action_url"],
                "read": msg["read_status"],
                "timestamp": msg["created_at"]
            })
        
        return result
    except Exception as e:
        log_error(e, f"get_user_messages for user {user_id}")
        raise HTTPException(500, "Internal server error")

@router.get("/user/{user_id}/unread-count")
def get_unread_message_count(user_id: str):
    """获取用户未读消息数量"""
    try:
        log_api_call(f"/messages/user/{user_id}/unread-count", "GET")
        
        result = db_query("""
            SELECT COUNT(*) as count
            FROM messages 
            WHERE user_id = %s AND read_status = false
        """, (user_id,))
        
        count = result[0]["count"] if result else 0
        return {"unread_count": count}
    except Exception as e:
        log_error(e, f"get_unread_message_count for user {user_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/{message_id}/read")
def mark_message_read(message_id: int):
    """标记消息为已读"""
    try:
        log_api_call(f"/messages/{message_id}/read", "POST")
        
        db_execute("""
            UPDATE messages 
            SET read_status = true 
            WHERE id = %s
        """, (message_id,))
        
        return {"ok": True, "message": "Message marked as read"}
    except Exception as e:
        log_error(e, f"mark_message_read {message_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/user/{user_id}/read-all")
def mark_all_messages_read(user_id: str):
    """标记用户所有消息为已读"""
    try:
        log_api_call(f"/messages/user/{user_id}/read-all", "POST")
        
        db_execute("""
            UPDATE messages 
            SET read_status = true 
            WHERE user_id = %s
        """, (user_id,))
        
        return {"ok": True, "message": "All messages marked as read"}
    except Exception as e:
        log_error(e, f"mark_all_messages_read for user {user_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/send")
def send_message(
    user_id: str,
    message_type: str,
    title: str,
    content: str,
    action_url: str = None
):
    """发送消息给用户"""
    try:
        log_api_call("/messages/send", "POST")
        
        message_id = db_execute("""
            INSERT INTO messages (user_id, type, title, content, action_url, read_status, created_at)
            VALUES (%s, %s, %s, %s, %s, false, %s)
        """, (user_id, message_type, title, content, action_url, datetime.now()))
        
        return {"ok": True, "message_id": message_id, "message": "Message sent successfully"}
    except Exception as e:
        log_error(e, f"send_message to user {user_id}")
        raise HTTPException(500, "Internal server error")

@router.post("/plan-generated")
def notify_plan_generated(
    family_id: str,
    plan_id: int,
    meal_type: str,
    meal_date: str,
    meal_code: str = None
):
    """通知相关用户计划已生成"""
    try:
        log_api_call("/messages/plan-generated", "POST")
        
        # 使用set来存储需要通知的用户ID，自动去重
        user_ids_to_notify = set()
        user_info = {}  # 存储用户信息 {user_id: display_name}
        
        # 1. 如果有meal_code，获取提交了submission的用户
        if meal_code:
            submission_users = db_query("""
                SELECT DISTINCT user_id, display_name
                FROM info_submissions 
                WHERE meal_code = %s
            """, (meal_code,))
            
            for user in submission_users:
                user_ids_to_notify.add(user["user_id"])
                user_info[user["user_id"]] = user["display_name"]
        
        # 2. 获取该家庭的所有成员
        family_members = db_query("""
            SELECT user_id, display_name
            FROM family_memberships 
            WHERE family_id = %s
        """, (family_id,))
        
        for member in family_members:
            user_ids_to_notify.add(member["user_id"])
            if member["user_id"] not in user_info:
                user_info[member["user_id"]] = member["display_name"]
        
        if not user_ids_to_notify:
            return {"ok": True, "message": "No members to notify"}
        
        # 获取家庭名称
        family = db_query("SELECT family_name FROM families WHERE family_id = %s", (family_id,))
        family_name = family[0]["family_name"] if family else family_id
        
        # 为每个用户发送通知（已去重）
        notification_count = 0
        for user_id in user_ids_to_notify:
            title = f"Meal Plan Generated - {meal_type.title()}"
            content = f"Your {meal_type} plan for {family_name} on {meal_date} has been generated successfully! Click to view the detailed meal plan."
            action_url = f"history.html?tab=plans&planId={plan_id}"
            
            db_execute("""
                INSERT INTO messages (user_id, type, title, content, action_url, read_status, created_at)
                VALUES (%s, %s, %s, %s, %s, false, %s)
            """, (user_id, "plan_generated", title, content, action_url, datetime.now()))
            
            notification_count += 1
        
        logger.info(f"Plan {plan_id} notifications sent to {notification_count} users (submission users + family members, deduplicated)")
        return {"ok": True, "message": f"Notifications sent to {notification_count} users"}
    except Exception as e:
        log_error(e, f"notify_plan_generated for family {family_id}")
        raise HTTPException(500, "Internal server error")
