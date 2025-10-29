// Global Notification System
// This script checks for notifications and displays badges

let notificationCheckInterval = null;

// Badge styles
const badgeStyle = `
    .notification-badge {
        position: absolute;
        top: -4px;
        right: -4px;
        background-color: #ef4444;
        color: white;
        border-radius: 50%;
        width: 18px;
        height: 18px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 10px;
        font-weight: bold;
        z-index: 10;
        animation: pulse 2s infinite;
    }
    
    @keyframes pulse {
        0%, 100% {
            transform: scale(1);
        }
        50% {
            transform: scale(1.1);
        }
    }
    
    .has-notification {
        position: relative;
    }
`;

// Add styles to page
function addBadgeStyles() {
    if (!document.getElementById('notification-badge-styles')) {
        const style = document.createElement('style');
        style.id = 'notification-badge-styles';
        style.textContent = badgeStyle;
        document.head.appendChild(style);
    }
}

// Create or update badge
function updateBadge(element, count) {
    if (!element) return;
    
    // Remove existing badge
    const existingBadge = element.querySelector('.notification-badge');
    if (existingBadge) {
        existingBadge.remove();
    }
    
    // Add new badge if count > 0
    if (count > 0) {
        element.classList.add('has-notification');
        const badge = document.createElement('span');
        badge.className = 'notification-badge';
        badge.textContent = count > 99 ? '99+' : count;
        element.appendChild(badge);
    } else {
        element.classList.remove('has-notification');
    }
}

// Check for unread messages
async function checkUnreadMessages(userId) {
    try {
        const response = await fetch(`${API_BASE}/messages/user/${userId}/unread-count`);
        if (response.ok) {
            const data = await response.json();
            return data.unread_count || 0;
        }
    } catch (error) {
        console.error('Error checking unread messages:', error);
    }
    return 0;
}

// Check for new family members
async function checkNewMembers(userId) {
    try {
        // 获取用户所在的所有家庭
        const familiesResponse = await fetch(`${API_BASE}/user/${userId}/families`);
        if (!familiesResponse.ok) return 0;
        
        const families = await familiesResponse.json();
        if (families.length === 0) return 0;
        
        // 获取每个家庭的成员
        let allCurrentMembers = [];
        for (const family of families) {
            const membersResponse = await fetch(`${API_BASE}/family/${family.family_id}/members`);
            if (membersResponse.ok) {
                const members = await membersResponse.json();
                allCurrentMembers.push(...members.map(m => ({
                    family_id: family.family_id,
                    user_id: m.user_id
                })));
            }
        }
        
        // 生成当前成员列表的标识符
        const currentMembersKey = allCurrentMembers
            .map(m => `${m.family_id}:${m.user_id}`)
            .sort()
            .join(',');
        
        // 从localStorage获取上次查看的成员列表
        const lastViewedKey = `family_members_${userId}`;
        const lastViewedMembers = localStorage.getItem(lastViewedKey);
        
        // 如果是首次查看，保存当前列表，返回0
        if (!lastViewedMembers) {
            localStorage.setItem(lastViewedKey, currentMembersKey);
            return 0;
        }
        
        // 比较成员列表，计算新成员数量
        const lastMemberIds = new Set(lastViewedMembers.split(','));
        const currentMemberIds = new Set(currentMembersKey.split(','));
        
        let newMembersCount = 0;
        for (const memberId of currentMemberIds) {
            if (!lastMemberIds.has(memberId)) {
                newMembersCount++;
            }
        }
        
        return newMembersCount;
    } catch (error) {
        console.error('Error checking new members:', error);
    }
    return 0;
}

// Update all notifications
async function updateAllNotifications(userId) {
    if (!userId) return;
    
    const [unreadMessages, newMembers] = await Promise.all([
        checkUnreadMessages(userId),
        checkNewMembers(userId)
    ]);
    
    // Update message badge
    const messageLink = document.querySelector('a[href="messages.html"]');
    if (messageLink) {
        updateBadge(messageLink, unreadMessages);
    }
    
    // Update manage family badge
    const manageFamilyLink = document.querySelector('a[href="manage_family.html"]');
    if (manageFamilyLink) {
        updateBadge(manageFamilyLink, newMembers);
    }
    
    // Update user profile badge (shows if any notification exists)
    const userProfileBtn = document.getElementById('userProfileBtn');
    if (userProfileBtn) {
        const totalNotifications = unreadMessages + newMembers;
        updateBadge(userProfileBtn, totalNotifications);
    }
    
    return {
        unreadMessages,
        newMembers
    };
}

// Start notification checking
function startNotificationCheck(userId) {
    if (!userId) return;
    
    // Add badge styles
    addBadgeStyles();
    
    // Initial check
    updateAllNotifications(userId);
    
    // Check every 30 seconds
    if (notificationCheckInterval) {
        clearInterval(notificationCheckInterval);
    }
    
    notificationCheckInterval = setInterval(() => {
        updateAllNotifications(userId);
    }, 30000); // 30 seconds
}

// Stop notification checking
function stopNotificationCheck() {
    if (notificationCheckInterval) {
        clearInterval(notificationCheckInterval);
        notificationCheckInterval = null;
    }
}

// Clear notification for a specific type
async function clearNotification(type, userId) {
    if (type === 'messages') {
        // Mark all messages as read
        try {
            await fetch(`${API_BASE}/messages/user/${userId}/read-all`, {
                method: 'POST'
            });
        } catch (error) {
            console.error('Error clearing messages:', error);
        }
    } else if (type === 'family') {
        // 更新localStorage中的成员列表为当前列表
        try {
            const familiesResponse = await fetch(`${API_BASE}/user/${userId}/families`);
            if (familiesResponse.ok) {
                const families = await familiesResponse.json();
                let allCurrentMembers = [];
                
                for (const family of families) {
                    const membersResponse = await fetch(`${API_BASE}/family/${family.family_id}/members`);
                    if (membersResponse.ok) {
                        const members = await membersResponse.json();
                        allCurrentMembers.push(...members.map(m => ({
                            family_id: family.family_id,
                            user_id: m.user_id
                        })));
                    }
                }
                
                const currentMembersKey = allCurrentMembers
                    .map(m => `${m.family_id}:${m.user_id}`)
                    .sort()
                    .join(',');
                
                localStorage.setItem(`family_members_${userId}`, currentMembersKey);
            }
        } catch (error) {
            console.error('Error clearing family notification:', error);
        }
    }
    
    // Refresh notifications
    updateAllNotifications(userId);
}

// Export functions
window.NotificationSystem = {
    start: startNotificationCheck,
    stop: stopNotificationCheck,
    update: updateAllNotifications,
    clear: clearNotification
};

