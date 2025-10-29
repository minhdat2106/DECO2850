/**
 * Authentication utilities for Meal Planner
 */

// ---------- Robust API base auto-detect ----------

const PROTOCOL = (location.protocol || 'http:');

let _resolveApiReady = null;
window.API_READY = new Promise((res) => { _resolveApiReady = res; });

// KHÔNG mặc định same-origin nữa; sẽ kiểm tra /api/health trước
let API_BASE = null;

function setApiBase(base) {
  API_BASE = base;
  window.API_BASE = API_BASE;
  if (_resolveApiReady) {
    _resolveApiReady();
    _resolveApiReady = null;
  }
}

async function getApiBase() {
  if (!API_BASE) await window.API_READY;
  return API_BASE;
}

(async function detectApiBase() {
  const candidates = [];

  // 1) Ưu tiên thử same-origin nếu đang mở qua http(s)
  if (PROTOCOL.startsWith('http') && location.origin) {
    candidates.push(`${location.origin}/api`);
  }

  // 2) Thêm các port dev phổ biến
  const ports = [8900, 8765, 8000];
  const hosts = ['127.0.0.1', 'localhost'];
  for (const h of hosts) for (const p of ports) {
    const base = `${PROTOCOL}//${h}:${p}/api`;
    if (!candidates.includes(base)) candidates.push(base);
  }

  const fetchWithTimeout = (base, ms = 2500) =>
    new Promise((resolve, reject) => {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), ms);
      fetch(base + '/health', { signal: ctrl.signal, cache: 'no-store' })
        .then(r => { clearTimeout(t); r.ok ? resolve(base) : reject(new Error(`bad status ${r.status}`)); })
        .catch(err => { clearTimeout(t); reject(err); });
    });

  try {
    const winner = await Promise.any(candidates.map(base => fetchWithTimeout(base)));
    setApiBase(winner);
  } catch (_) {
    // Fallback cuối
    const fallback = `${PROTOCOL}//127.0.0.1:8900/api`;
    console.warn('[MealPlanner] No API host responded; falling back to', fallback);
    setApiBase(fallback);
  }

  console.log('[MealPlanner] API_BASE =', API_BASE);
})();

// ------------- Utilities below -----------------

/**
 * Get storage key for cross-page sharing
 * @param {string} key - Base key name
 * @returns {string} - Key for cross-page sharing
 */
function getTabKey(key) {
  return key; // 直接返回 key，不加 tab 前缀，实现跨页面共享
}

/**
 * Check if user is logged in and verify with server
 * @returns {Promise<Object|null>} User data if valid, null if not
 */
async function checkUserAuth() {
  try {
    // Đảm bảo API_BASE đã sẵn sàng
    if (!API_BASE) await window.API_READY;

    const userData = sessionStorage.getItem(getTabKey('currentUser'));
    if (!userData) {
      return null;
    }
    const user = JSON.parse(userData);

    const base = await getApiBase();
    const url = `${base}/user/${encodeURIComponent(user.user_id)}`;
    const response = await fetch(url, { method: 'GET', cache: 'no-store' });

    if (!response.ok) {
      clearUserData();
      return null;
    }
    return user;
  } catch (error) {
    console.error('Error checking user auth:', error);
    clearUserData();
    return null;
  }
}

/**
 * Clear all user data from sessionStorage for this tab
 */
function clearUserData() {
  sessionStorage.removeItem(getTabKey('currentUser'));
  sessionStorage.removeItem(getTabKey('userFamilies'));
  sessionStorage.removeItem(getTabKey('selectedFamily'));
  sessionStorage.removeItem(getTabKey('mealCodeData'));
}

/**
 * Redirect to login page
 */
function redirectToLogin() {
  clearUserData();
  window.location.href = 'index.html';
}

/**
 * Logout user and redirect to login
 */
function logout() {
  clearUserData();
  window.location.href = 'index.html';
}

/**
 * Initialize authentication for a page
 * @param {Function} onSuccess - Callback when user is authenticated
 * @param {Function} onError - Callback when authentication fails
 */
async function initAuth(onSuccess, onError) {
  // Đợi API_BASE sẵn sàng trước khi check
  if (!API_BASE) await window.API_READY;

  const user = await checkUserAuth();
  if (user) {
    if (onSuccess) onSuccess(user);
  } else {
    if (onError) onError();
    else redirectToLogin();
  }
}

/**
 * Update user info display elements
 * @param {Object} user - User data object
 */
function updateUserDisplay(user) {
  const userNameElements = document.querySelectorAll('[data-user-name]');
  userNameElements.forEach(el => {
    el.textContent = user.user_name;
  });

  const userInitialElements = document.querySelectorAll('[data-user-initial]');
  userInitialElements.forEach(el => {
    el.textContent = user.user_name.charAt(0).toUpperCase();
  });
}

/**
 * Setup logout button event listeners
 */
function setupLogoutButtons() {
  const logoutButtons = document.querySelectorAll('[data-logout]');
  logoutButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      logout();
    });
  });
}

// Xuất các biến/hàm hữu ích
window.API_BASE = API_BASE;          // luôn được cập nhật trong setApiBase
window.API_READY = window.API_READY; // promise để đợi base sẵn sàng
window.getApiBase = getApiBase;      // helper mới
window.initAuth = initAuth;
window.updateUserDisplay = updateUserDisplay;
window.setupLogoutButtons = setupLogoutButtons;
