// User Header Component
// This script provides a consistent user header for all pages

function addUserHeader() {
    // Check if user header already exists
    if (document.getElementById('userProfileBtn')) {
        return;
    }

    // Create user header HTML
    const userHeaderHTML = `
        <!-- User Info Header -->
        <div class="flex justify-between items-center mb-6 fade-in">
            <div></div>
            <div class="relative">
                <button id="userProfileBtn" class="flex items-center space-x-3 bg-white bg-opacity-20 backdrop-blur-sm rounded-lg p-3 hover:bg-opacity-30 transition-all">
                    <div class="w-10 h-10 bg-white bg-opacity-30 rounded-full flex items-center justify-center">
                        <span id="userInitial" class="text-lg font-bold text-white">U</span>
                    </div>
                    <div class="text-left">
                        <div class="text-white font-semibold" id="userName">User</div>
                        <div class="text-blue-100 text-xs" id="userRole">Member</div>
                    </div>
                    <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path>
                    </svg>
                </button>
                
                <!-- User Dropdown -->
                <div id="userDropdown" class="dropdown-content">
                    <a href="edit_profile.html" class="dropdown-item">
                        <span class="mr-2">✏️</span>Edit Profile
                    </a>
                    <a href="manage_family.html" class="dropdown-item">
                        <span class="mr-2">👨‍👩‍👧‍👦</span>Manage My Family
                    </a>
                    <a href="messages.html" class="dropdown-item">
                        <span class="mr-2">💬</span>Messages
                        <span id="messageCount" class="ml-2 bg-red-500 text-white text-xs rounded-full px-2 py-1 hidden">0</span>
                    </a>
                    <hr class="my-1">
                    <a href="#" id="logoutBtn" class="dropdown-item text-red-600">
                        <span class="mr-2">🚪</span>Logout
                    </a>
                </div>
            </div>
        </div>
    `;

    // Add CSS styles if not already present
    if (!document.getElementById('user-header-styles')) {
        const style = document.createElement('style');
        style.id = 'user-header-styles';
        style.textContent = `
            .dropdown {
                position: relative;
                display: inline-block;
            }
            .dropdown-content {
                display: none;
                position: absolute;
                right: 0;
                background-color: white;
                min-width: 200px;
                box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.2);
                z-index: 1;
                border-radius: 8px;
                overflow: hidden;
            }
            .dropdown-content.show {
                display: block;
            }
            .dropdown-item {
                color: black;
                padding: 12px 16px;
                text-decoration: none;
                display: block;
                transition: background-color 0.2s;
            }
            .dropdown-item:hover {
                background-color: #f1f5f9;
            }
        `;
        document.head.appendChild(style);
    }

    // Find the main content container and insert user header
    const mainContent = document.querySelector('.min-h-screen .max-w-2xl, .min-h-screen .max-w-4xl, .min-h-screen .max-w-6xl');
    if (mainContent) {
        const firstChild = mainContent.firstElementChild;
        if (firstChild && !firstChild.id.includes('userProfileBtn')) {
            mainContent.insertAdjacentHTML('afterbegin', userHeaderHTML);
        }
    }
}

function setupUserHeader() {
    // Check if user is logged in
    const userData = localStorage.getItem('currentUser');
    if (!userData) {
        window.location.href = 'index.html';
        return;
    }

    const currentUser = JSON.parse(userData);
    
    // Update user information display
    function updateUserInfo() {
        if (currentUser) {
            const userNameEl = document.getElementById('userName');
            const userInitialEl = document.getElementById('userInitial');
            if (userNameEl) userNameEl.textContent = currentUser.user_name;
            if (userInitialEl) userInitialEl.textContent = currentUser.user_name.charAt(0).toUpperCase();
        }
    }

    // Setup event listeners
    function setupEventListeners() {
        // User dropdown
        const userProfileBtn = document.getElementById('userProfileBtn');
        const userDropdown = document.getElementById('userDropdown');
        const logoutBtn = document.getElementById('logoutBtn');

        if (userProfileBtn && userDropdown) {
            userProfileBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                userDropdown.classList.toggle('show');
            });

            // Close dropdown when clicking outside
            document.addEventListener('click', (e) => {
                if (!userProfileBtn.contains(e.target)) {
                    userDropdown.classList.remove('show');
                }
            });
        }

        if (logoutBtn) {
            logoutBtn.addEventListener('click', (e) => {
                e.preventDefault();
                // Clear all user data
                localStorage.removeItem('currentUser');
                localStorage.removeItem('userFamilies');
                localStorage.removeItem('selectedFamily');
                window.location.href = 'index.html';
            });
        }
    }

    updateUserInfo();
    setupEventListeners();
}

// Auto-initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    addUserHeader();
    setupUserHeader();
});

// Also run on window load as fallback
window.addEventListener('load', () => {
    addUserHeader();
    setupUserHeader();
});
