// Shared variables for NbotMethods
window.__nbotApi = axios.create({ baseURL: '', timeout: 60000 });
window.__nbotApi.interceptors.request.use((config) => {
    const token = localStorage.getItem('auth_token');
    if (token) {
        config.headers = config.headers || {};
        config.headers.Authorization = 'Bearer ' + token;
    }
    return config;
});

window.__nbotSocket = io(window.location.origin, {
    path: '/socket.io',
    transports: ['polling', 'websocket'],
    autoConnect: false,
    auth: function (cb) { cb({ token: localStorage.getItem('auth_token') || '' }); },
    upgrade: true,
    reconnection: true,
    reconnectionAttempts: 10,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    timeout: 20000
});

window.__nbotCopyCodeBlock = function copyCodeBlock(id) {
    var pre = document.getElementById(id);
    if (!pre) return;
    var text = pre.innerText;
    var btn = pre.previousElementSibling && pre.previousElementSibling.querySelector('.code-copy-btn');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
            if (btn) {
                btn.innerHTML = '<i class="fas fa-check"></i> 已复制';
                setTimeout(function () { btn.innerHTML = '<i class="fas fa-copy"></i> 复制'; }, 2000);
            }
        }).catch(function () {
            window.__nbotFallbackCopyText(text, btn);
        });
    } else {
        window.__nbotFallbackCopyText(text, btn);
    }
};

window.__nbotFallbackCopyText = function fallbackCopyText(text, btn) {
    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
        var successful = document.execCommand('copy');
        if (successful && btn) {
            btn.innerHTML = '<i class="fas fa-check"></i> 已复制';
            setTimeout(function () { btn.innerHTML = '<i class="fas fa-copy"></i> 复制'; }, 2000);
        }
    } catch (err) {
        console.error('复制失败:', err);
    }
    document.body.removeChild(textarea);
};

window.__nbotConnectSocketWithAuth = function connectSocketWithAuth() {
    window.__nbotSocket.auth = { token: localStorage.getItem('auth_token') || '' };
    if (!window.__nbotSocket.connected) {
        window.__nbotSocket.connect();
    }
};
