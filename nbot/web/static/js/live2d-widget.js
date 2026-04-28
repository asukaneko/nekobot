(function () {
const live2dModels = [
        { name: 'Pio', path: '/static/live2d/models/Pio/model1.json', layoutWidth: 2.0, position: [0, 8], scale: 0.3 }
    ];

    function stageForModel(model) {
        // Auto-calculate stage size from model's layout.width to avoid squishing.
        // Wider layout 鈫?taller stage to preserve vertical proportions.
        var base = 440;
        var w = base;
        var h = base * (1 + (model.layoutWidth - 2) * 0.36);
        return { width: Math.round(w), height: Math.round(h), right: 0, bottom: 0 };
    }
    let currentLive2dModelIndex = 0;

    const live2dLines = {
        ready: [
            '\u6211\u5728\u53f3\u8fb9\u5f85\u547d\u3002',
            '\u4f1a\u8bdd\u72b6\u6001\u6211\u4f1a\u5e2e\u4f60\u76ef\u7740\u3002',
            '\u53ef\u4ee5\u70b9\u6211\u6253\u5f00\u4e92\u52a8\u83dc\u5355\u3002'
        ],
        idle: [
            '\u6709\u65b0\u6d88\u606f\u6211\u4f1a\u63d0\u9192\u4f60\u3002',
            '\u5f53\u524d\u9875\u9762\u8fd8\u5728\u6b63\u5e38\u8fd0\u884c\u3002',
            '\u6211\u53ef\u4ee5\u548c AI \u56de\u590d\u72b6\u6001\u8054\u52a8\u3002',
            '\u5982\u679c\u56de\u590d\u5f88\u957f\uff0c\u6211\u4f1a\u5728\u7ed3\u675f\u65f6\u63d0\u9192\u3002',
            '\u73b0\u5728\u53ea\u4f7f\u7528 Pio \u6a21\u578b\u3002'
        ],
        copy: [
            '\u5df2\u590d\u5236\u5230\u526a\u8d34\u677f\u3002',
            '\u8fd9\u6bb5\u5185\u5bb9\u6211\u5e2e\u4f60\u8bb0\u4e0b\u6765\u4e86\u3002'
        ],
        model: [
            '\u5df2\u5207\u6362\u5230\u4e0b\u4e00\u4e2a\u672c\u5730\u6a21\u578b\u3002',
            '\u6362\u4e2a\u89d2\u8272\u7ee7\u7eed\u966a\u4f60\u3002'
        ]
    };

    // Motion definitions 鈥?mapped to Pio model's .mtn files
    var live2dMotions = {
        // Emotion-tagged
        happy:   ['Success', 'Touch Dere1', 'Touch Dere2', 'Touch Dere3', 'Touch Dere5'],
        sad:     ['Fail', 'Sleeping'],
        surprised: ['WakeUp'],
        shy:     ['Touch Dere4', 'Touch Dere6', 'Breath4'],
        naughty: ['Sukebei1', 'Sukebei2', 'Sukebei3'],
        // Generic
        tap:     ['Touch1', 'Touch2', 'Touch3', 'Touch4', 'Touch5', 'Touch6'],
        idle:    ['Breath1', 'Breath2', 'Breath3', 'Breath5', 'Breath7', 'Breath8']
    };

    function pickMotion(group) {
        var list = live2dMotions[group];
        if (!list || !list.length) list = live2dMotions.tap;
        return list[Math.floor(Math.random() * list.length)];
    }

    function playLive2dMotion(motionName) {
        if (!motionName) return;
        try {
            var oml2d = window.__nbotLive2d;
            if (!oml2d) return;
            // oh-my-live2d exposes stage 鈫?internalModel for Cubism2
            var stage = oml2d.stage || oml2d;
            var internalModel = stage?.internalModel || stage?.currentModel || stage?._model;
            if (internalModel && typeof internalModel.motion === 'function') {
                internalModel.motion('', motionName);
                return;
            }
            if (internalModel && internalModel.internalModel && typeof internalModel.internalModel.motion === 'function') {
                internalModel.internalModel.motion('', motionName);
                return;
            }
        } catch (e) {
            // Ignore 鈥?motion is cosmetic
        }
    }

    // Detect emotion from AI reply text and play matching motion
    function playMotionForText(text) {
        if (!text) return;
        var t = String(text).toLowerCase();
        var group = 'idle';
        if (/开心|高兴|不错|完成|成功|太好了|谢谢|happy|great|nice|success/.test(t)) {
            group = 'happy';
        } else if (/失败|错误|异常|抱歉|难过|问题|不行|sad|error|failed/.test(t)) {
            group = 'sad';
        } else if (/突然|竟然|注意|发现|哇|surprise|unexpected/.test(t)) {
            group = 'surprised';
        } else if (/害羞|不好意思|悄悄|shy/.test(t)) {
            group = 'shy';
        } else if (/调皮|偷偷|玩笑|naughty|joke/.test(t)) {
            group = 'naughty';
        }
        playLive2dMotion(pickMotion(group));
    }
    // Menu options cache 鈥?seeded with static fallbacks, refreshed by AI in background
    var live2dMenuCache = {
        greeting: null,
        options: ['\u968f\u4fbf\u804a\u804a', '\u770b\u770b\u72b6\u6001', '\u8bb2\u4e2a\u7b11\u8bdd', '\u6700\u8fd1\u5728\u5fd9\u4ec0\u4e48'],
        lastFetch: 0,
        ttl: 120000,   // refresh every 2 minutes
        fetching: false
    };

    function pickLine(group) {
        const lines = live2dLines[group] || live2dLines.idle;
        return lines[Math.floor(Math.random() * lines.length)];
    }

    function installLive2dOverrides() {
        if (document.getElementById('nbot-live2d-overrides')) return;
        const style = document.createElement('style');
        style.id = 'nbot-live2d-overrides';
        style.textContent = [
            '#oml2d-stage { background: transparent !important; overflow: visible !important; cursor: grab; user-select: none; touch-action: none; }',
            '#oml2d-stage.is-dragging { cursor: grabbing; }',
            '#oml2d-canvas { background: transparent !important; }',
            '#oml2d-menus, #oml2d-statusBar { display: none !important; }',
            '#oml2d-tips { top: -68px !important; left: 0 !important; right: 0 !important; margin-left: auto !important; margin-right: auto !important; }',
            '#nbot-live2d-menu { position: fixed; z-index: 10000; display: none; width: 184px; padding: 8px; border: 1px solid rgba(148,163,184,0.18); border-radius: 10px; background: rgba(15,23,42,0.94); box-shadow: 0 18px 48px rgba(0,0,0,0.36), inset 0 1px 0 rgba(255,255,255,0.06); backdrop-filter: blur(14px); color: #e6edf3; }',
            '#nbot-live2d-menu.is-visible { display: block; animation: nbot-live2d-menu-in 120ms ease-out; }',
            '#nbot-live2d-menu::after { content: ""; position: absolute; right: -6px; top: 30px; width: 10px; height: 10px; transform: rotate(45deg); background: rgba(15,23,42,0.94); border-top: 1px solid rgba(148,163,184,0.18); border-right: 1px solid rgba(148,163,184,0.18); }',
            '.nbot-live2d-menu-title { display: flex; align-items: center; justify-content: space-between; padding: 4px 6px 8px; border-bottom: 1px solid rgba(148,163,184,0.14); margin-bottom: 6px; }',
            '.nbot-live2d-menu-title strong { font-size: 13px; font-weight: 700; color: #f8fafc; }',
            '.nbot-live2d-menu-title span { font-size: 11px; color: #94a3b8; }',
            '#nbot-live2d-menu button { display: grid; width: 100%; grid-template-columns: 28px 1fr; align-items: center; gap: 8px; border: 0; border-radius: 8px; padding: 8px 9px; color: #dbeafe; background: transparent; font-size: 13px; text-align: left; cursor: pointer; }',
            '#nbot-live2d-menu button:hover { background: rgba(56,189,248,0.13); color: #f8fafc; }',
            '#nbot-live2d-menu button[data-action="reset"] { color: #cbd5e1; }',
            '.nbot-live2d-menu-icon { width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; border-radius: 7px; background: rgba(56,189,248,0.12); color: #38bdf8; }',
            '#nbot-live2d-menu button:hover .nbot-live2d-menu-icon { background: rgba(56,189,248,0.22); }',
            '.nbot-live2d-chat-row { display: flex; align-items: center; gap: 5px; margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(148,163,184,0.14); }',
            '.nbot-live2d-chat-input { flex: 1; min-width: 0; border: 0; border-radius: 6px; padding: 5px 7px; font-size: 11px; color: #e6edf3; background: rgba(255,255,255,0.06); outline: none; font-family: inherit; }',
            '.nbot-live2d-chat-input::placeholder { color: rgba(148,163,184,0.5); font-size: 11px; }',
            '.nbot-live2d-chat-input:focus { background: rgba(255,255,255,0.1); }',
            '#nbot-live2d-menu .nbot-live2d-chat-send { flex: 0 0 26px; width: 26px; min-width: 26px; max-width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; grid-template-columns: none; gap: 0; border: 0; border-radius: 6px; background: rgba(56,189,248,0.15); color: #38bdf8; cursor: pointer; padding: 0; margin: 0; box-sizing: border-box; line-height: 1; text-align: center; }',
            '.nbot-live2d-chat-send:hover { background: rgba(56,189,248,0.28); }',
            '#nbot-live2d-menu .nbot-live2d-chat-send svg { width: 14px; height: 14px; flex: 0 0 14px; display: block; margin: 0; transform: none; }',
            '@keyframes nbot-live2d-menu-in { from { opacity: 0; transform: translateY(4px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }'
        ].join('\n');
        document.head.appendChild(style);
    }

    function getSavedStagePosition() {
        try {
            const saved = JSON.parse(localStorage.getItem('NBOT_LIVE2D_STAGE_POSITION') || 'null');
            if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
                return saved;
            }
        } catch (error) {
            console.warn('Failed to read Live2D stage position:', error);
        }
        return null;
    }

    function saveStagePosition(left, top) {
        // Position is intentionally session-only so every browser refresh starts centered.
    }

    function clearSavedStagePosition() {
        try {
            localStorage.removeItem('NBOT_LIVE2D_STAGE_POSITION');
        } catch (error) {
            console.warn('Failed to clear Live2D stage position:', error);
        }
    }

    function applyDefaultStagePosition(stage) {
        const left = (window.innerWidth - stage.offsetWidth) / 2;
        const top = (window.innerHeight - stage.offsetHeight) / 2;
        return applyStagePosition(stage, left, top);
    }

    function getStageRightSlack(stage) {
        return Math.max(0, stage.offsetWidth - 120);
    }

    function getStageBottomSlack(stage) {
        return Math.max(0, stage.offsetHeight - 120);
    }

    function getDefaultStageBottomSlack(stage) {
        return Math.max(0, stage.offsetHeight - 190);
    }

    function applyStagePosition(stage, left, top) {
        const maxLeft = Math.max(0, window.innerWidth - stage.offsetWidth + getStageRightSlack(stage));
        const maxTop = Math.max(0, window.innerHeight - stage.offsetHeight + getStageBottomSlack(stage));
        const nextLeft = Math.min(Math.max(0, left), maxLeft);
        const nextTop = Math.min(Math.max(0, top), maxTop);
        stage.style.left = `${nextLeft}px`;
        stage.style.top = `${nextTop}px`;
        stage.style.right = 'auto';
        stage.style.bottom = 'auto';
        stage.style.transform = 'none';
        return { left: nextLeft, top: nextTop };
    }

    async function fetchLive2dMenuOptions() {
        if (live2dMenuCache.fetching) return null; // avoid concurrent fetches
        live2dMenuCache.fetching = true;
        try {
            const response = await fetch('/api/live2d/menu-options', {
                method: 'POST',
                headers: getAuthHeaders(),
                body: JSON.stringify({
                    frontend: getFrontendStatus(),
                    live2d: getLive2dStatus()
                })
            });
            const data = await response.json();
            var greeting = data.greeting || null;
            var options = Array.isArray(data.options) ? data.options : [];
            // Update cache
            if (options.length) {
                live2dMenuCache.options = options;
            }
            if (greeting) {
                live2dMenuCache.greeting = greeting;
            }
            live2dMenuCache.lastFetch = Date.now();
            live2dMenuCache.fetching = false;
            return { greeting: greeting, options: options };
        } catch (error) {
            console.warn('Live2D menu options fetch failed:', error);
            live2dMenuCache.fetching = false;
            return null; // caller should fall back to cache
        }
    }

    function renderDynamicMenuItems(container, options) {
        // Remove old dynamic items
        container.querySelectorAll('.nbot-live2d-dynamic-item').forEach(function (el) { el.remove(); });
        if (!options.length) return;

        var divider = container.querySelector('.nbot-live2d-dynamic-divider');
        if (!divider) {
            divider = document.createElement('div');
            divider.className = 'nbot-live2d-dynamic-divider';
            divider.style.cssText = 'margin:4px 6px;border-top:1px solid rgba(148,163,184,0.12);';
            container.appendChild(divider);
        }
        divider.style.display = 'block';

        options.forEach(function (opt, idx) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'nbot-live2d-dynamic-item';
            btn.dataset.action = 'dynamic-talk';
            btn.dataset.topic = opt;
            btn.innerHTML = '<span class="nbot-live2d-menu-icon"><svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 7.2h7.6l-6 4.8 2.4 7.2-6.4-4.8-6.4 4.8 2.4-7.2-6-4.8h7.6z"/></svg></span><span>' + opt + '</span>';
            container.appendChild(btn);
        });
    }

    function ensureLive2dMenu() {
        let menu = document.getElementById('nbot-live2d-menu');
        if (menu) return menu;

        menu = document.createElement('div');
        menu.id = 'nbot-live2d-menu';
        var buttons = [
            '<div class="nbot-live2d-menu-title"><strong>Live2D</strong><span>assistant</span></div>'
        ];
        if (live2dModels.length >= 2) {
            buttons.push('<button type="button" data-action="switch"><span class="nbot-live2d-menu-icon"><i class="fas fa-sync-alt"></i></span><span>\u5207\u6362\u6a21\u578b</span></button>');
        }
        buttons.push(
            '<button type="button" data-action="talk"><span class="nbot-live2d-menu-icon"><i class="fas fa-comment-dots"></i></span><span>\u968f\u673a\u8bf4\u8bdd</span></button>',
            '<button type="button" data-action="status"><span class="nbot-live2d-menu-icon"><i class="fas fa-robot"></i></span><span>AI \u72b6\u6001</span></button>',
            '<button type="button" data-action="reset"><span class="nbot-live2d-menu-icon"><i class="fas fa-compress-arrows-alt"></i></span><span>\u56de\u5230\u53f3\u4e0b\u89d2</span></button>',
            '<div class="nbot-live2d-chat-row"><input class="nbot-live2d-chat-input" type="text" placeholder="\u5bf9\u7740\u6211\u8bf4\u70b9\u4ec0\u4e48..."><button type="button" class="nbot-live2d-chat-send" title="\u53d1\u9001"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></div>'
        );
        menu.innerHTML = buttons.join('');

        function sendChatInput() {
            const input = menu.querySelector('.nbot-live2d-chat-input');
            if (!input) return;
            const text = input.value.trim();
            if (!text) return;
            input.value = '';
            window.__nbotLive2dAiTalk(text);
            hideLive2dMenu();
        }

        menu.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && event.target.classList.contains('nbot-live2d-chat-input')) {
                event.preventDefault();
                sendChatInput();
            }
        });

        menu.addEventListener('click', (event) => {
            const sendBtn = event.target.closest('.nbot-live2d-chat-send');
            if (sendBtn) {
                sendChatInput();
                return;
            }
            const btn = event.target.closest('button');
            if (!btn) return;
            const action = btn.dataset.action;
            const stage = document.getElementById('oml2d-stage');
            if (action === 'switch') {
                window.__nbotLive2dSwitchModel();
            } else if (action === 'talk') {
                window.__nbotLive2dAiTalk();
            } else if (action === 'dynamic-talk') {
                const topic = btn.dataset.topic;
                if (topic) {
                    window.__nbotLive2dAiTalk(topic);
                }
            } else if (action === 'status') {
                const app = window.__nbotVueApp;
                if (app && app.currentSession) {
                    window.__nbotLive2dSay(`\u5f53\u524d\u4f1a\u8bdd\uff1a${app.currentSession.name || app.currentSession.id}`, 4200, 4);
                } else {
                    window.__nbotLive2dSay('\u8fd8\u6ca1\u6709\u9009\u4e2d\u4f1a\u8bdd\u3002', 3200, 4);
                }
            } else if (action === 'reset' && stage) {
                clearSavedStagePosition();
                const pos = applyDefaultStagePosition(stage);
                saveStagePosition(pos.left, pos.top);
                window.__nbotLive2dSay('\u5df2\u56de\u5230\u53f3\u4e0b\u89d2\u3002', 2600, 4);
            }
            hideLive2dMenu();
        });

        document.body.appendChild(menu);
        document.addEventListener('pointerdown', (event) => {
            if (!menu.classList.contains('is-visible')) return;
            if (menu.contains(event.target) || document.getElementById('oml2d-stage')?.contains(event.target)) return;
            hideLive2dMenu();
        });
        return menu;
    }

    function hideLive2dMenu() {
        var menu = document.getElementById('nbot-live2d-menu');
        if (menu) {
            menu.classList.remove('is-visible');
            // Clean up dynamic items when closing
            var divider = menu.querySelector('.nbot-live2d-dynamic-divider');
            if (divider) { divider.style.display = 'none'; }
            menu.querySelectorAll('.nbot-live2d-dynamic-item').forEach(function (el) { el.remove(); });
        }
    }

    function showLive2dMenu(stage) {
        // Play a random tap motion when opening the menu
        playLive2dMotion(pickMotion('tap'));

        const menu = ensureLive2dMenu();
        const rect = stage.getBoundingClientRect();
        menu.classList.add('is-visible');
        const menuWidth = menu.offsetWidth || 184;
        const menuHeight = menu.offsetHeight || 196;
        const preferLeft = rect.left - menuWidth - 12;
        const fallbackLeft = rect.right + 12;
        const left = preferLeft > 8 ? preferLeft : Math.min(fallbackLeft, window.innerWidth - menuWidth - 8);
        const top = Math.min(Math.max(8, rect.top + Math.max(8, rect.height * 0.18)), window.innerHeight - menuHeight - 8);
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;

        // Clean old dynamic items
        var dynDivider = menu.querySelector('.nbot-live2d-dynamic-divider');
        if (dynDivider) { dynDivider.style.display = 'none'; }
        menu.querySelectorAll('.nbot-live2d-dynamic-item').forEach(function (el) { el.remove(); });

        // 1) Render cached options IMMEDIATELY 鈥?guarantees options on every open
        if (live2dMenuCache.options.length) {
            renderDynamicMenuItems(menu, live2dMenuCache.options);
        }

        // Auto-focus chat input
        var chatInput = menu.querySelector('.nbot-live2d-chat-input');
        if (chatInput) {
            chatInput.value = '';
            setTimeout(function () { chatInput.focus(); }, 50);
        }

        // 2) Background refresh if cache is stale (and no fetch in flight)
        var isStale = (Date.now() - live2dMenuCache.lastFetch) > live2dMenuCache.ttl;
        if (isStale && !live2dMenuCache.fetching) {
            fetchLive2dMenuOptions().then(function (result) {
                if (!menu.classList.contains('is-visible')) return;
                if (result) {
                    if (result.greeting) {
                        window.__nbotLive2dSay(result.greeting, 4200, 5);
                    }
                    if (result.options.length) {
                        renderDynamicMenuItems(menu, result.options);
                    }
                }
            }).catch(function () {
                // Silently ignore 鈥?cached options already shown
            });
        }
    }

    function enableStageDrag() {
        const stage = document.getElementById('oml2d-stage');
        if (!stage || stage.dataset.nbotDragReady === 'true') return;
        stage.dataset.nbotDragReady = 'true';

        clearSavedStagePosition();
        applyDefaultStagePosition(stage);

        let dragging = false;
        let startX = 0;
        let startY = 0;
        let startLeft = 0;
        let startTop = 0;
        let didMove = false;

        stage.addEventListener('pointerdown', (event) => {
            if (event.button !== undefined && event.button !== 0) return;
            const rect = stage.getBoundingClientRect();
            dragging = true;
            didMove = false;
            startX = event.clientX;
            startY = event.clientY;
            startLeft = rect.left;
            startTop = rect.top;
            stage.classList.add('is-dragging');
            stage.setPointerCapture?.(event.pointerId);
        });

        stage.addEventListener('pointermove', (event) => {
            if (!dragging) return;
            const dx = event.clientX - startX;
            const dy = event.clientY - startY;
            if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
                didMove = true;
            }
            applyStagePosition(stage, startLeft + dx, startTop + dy);
        });

        function endDrag(event) {
            if (!dragging) return;
            dragging = false;
            stage.classList.remove('is-dragging');
            stage.releasePointerCapture?.(event.pointerId);
            const rect = stage.getBoundingClientRect();
            saveStagePosition(rect.left, rect.top);
        }

        stage.addEventListener('pointerup', endDrag);
        stage.addEventListener('pointercancel', endDrag);
        stage.addEventListener('click', (event) => {
            if (didMove) {
                event.preventDefault();
                event.stopPropagation();
                didMove = false;
                hideLive2dMenu();
                return;
            }
            event.preventDefault();
            event.stopPropagation();
            showLive2dMenu(stage);
        }, true);

        window.addEventListener('resize', () => {
            const rect = stage.getBoundingClientRect();
            const pos = applyStagePosition(stage, rect.left, rect.top);
            saveStagePosition(pos.left, pos.top);
        });
    }

    function waitForStageDrag() {
        let attempts = 0;
        const timer = window.setInterval(() => {
            attempts += 1;
            enableStageDrag();
            if (document.getElementById('oml2d-stage')?.dataset.nbotDragReady === 'true' || attempts > 50) {
                window.clearInterval(timer);
            }
        }, 200);
    }

    window.__nbotLive2dReady = false;
    window.__nbotLive2dQueue = window.__nbotLive2dQueue || [];
    window.__nbotLive2dSay = function (message, duration, priority) {
        if (!message) return;
        if (window.__nbotLive2dReady && window.__nbotLive2d && typeof window.__nbotLive2d.tipsMessage === 'function') {
            window.__nbotLive2d.tipsMessage(message, duration || 3600, priority || 4);
            return;
        }
        window.__nbotLive2dQueue.push({ message, duration: duration || 3600, priority: priority || 4 });
    };

    window.__nbotLive2dRandomSay = function (group, duration, priority) {
        window.__nbotLive2dSay(pickLine(group), duration, priority);
    };

    function getAuthHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('auth_token');
        if (token) {
            headers.Authorization = `Bearer ${token}`;
        }
        return headers;
    }

    function getFrontendStatus() {
        const app = window.__nbotVueApp;
        const messages = Array.isArray(app?.currentMessages) ? app.currentMessages : [];
        const currentSession = app?.currentSession || null;
        return {
            currentPage: app?.currentPage || null,
            chatTab: app?.chatTab || null,
            socketConnected: !!app?.socketConnected,
            isLoading: !!app?.isLoading,
            loadingSessionId: app?.loadingSessionId || null,
            currentSession: currentSession ? {
                id: currentSession.id || null,
                name: currentSession.name || null,
                type: currentSession.type || null,
                message_count: messages.length
            } : null,
            currentMessagesCount: messages.length,
            recentMessageRoles: messages.slice(-5).map((message) => message.role || message.sender || 'unknown')
        };
    }

    function getLive2dStatus() {
        const model = live2dModels[currentLive2dModelIndex] || live2dModels[0];
        const stage = document.getElementById('oml2d-stage');
        const rect = stage?.getBoundingClientRect();
        return {
            model: model?.name || null,
            modelPath: model?.path || null,
            position: rect ? {
                left: Math.round(rect.left),
                top: Math.round(rect.top),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
            } : null
        };
    }

    window.__nbotLive2dAiTalk = async function (topic) {
        const hasTopic = topic && typeof topic === 'string' && topic.trim();
        if (hasTopic) {
            window.__nbotLive2dSay('...', 2000, 5);
        } else {
            window.__nbotLive2dSay('\u6211\u770b\u4e00\u4e0b\u73b0\u5728\u7684\u72b6\u6001...', 2800, 6);
        }
        try {
            const body = {
                frontend: getFrontendStatus(),
                live2d: getLive2dStatus()
            };
            if (hasTopic) {
                body.topic = topic.trim();
            }
            const response = await fetch('/api/live2d/random-talk', {
                method: 'POST',
                headers: getAuthHeaders(),
                body: JSON.stringify(body)
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || `HTTP ${response.status}`);
            }
            window.__nbotLive2dSay(data.message, 5600, 7);
            playMotionForText(data.message);
        } catch (error) {
            console.warn('Live2D AI talk failed:', error);
            window.__nbotLive2dRandomSay('idle', 4200, 4);
        }
    };

    window.__nbotLive2dComment = async function (recentMessages) {
        // Called by the main page when AI finishes a reply in the current session.
        // recentMessages: array of {role, content} from the current chat (last ~5 rounds)
        if (!Array.isArray(recentMessages) || recentMessages.length === 0) return;
        try {
            const response = await fetch('/api/live2d/comment', {
                method: 'POST',
                headers: getAuthHeaders(),
                body: JSON.stringify({
                    recent_messages: recentMessages,
                    frontend: getFrontendStatus(),
                    live2d: getLive2dStatus()
                })
            });
            const data = await response.json();
            if (data.success && data.message) {
                window.__nbotLive2dSay(data.message, 5600, 5);
                playMotionForText(data.message);
            }
        } catch (error) {
            console.warn('Live2D comment failed:', error);
        }
    };

    window.__nbotLive2dSwitchModel = function () {
        if (live2dModels.length < 2) {
            return; // nothing to switch
        }
        if (!window.__nbotLive2d || typeof window.__nbotLive2d.loadNextModel !== 'function') {
            return;
        }
        try {
            window.__nbotLive2d.loadNextModel();
            currentLive2dModelIndex = (currentLive2dModelIndex + 1) % live2dModels.length;
            window.__nbotLive2dRandomSay('model', 3200, 4);
        } catch (e) {
            console.warn('Live2D model switch failed:', e);
            // Try to reload current model
            if (window.__nbotLive2d && typeof window.__nbotLive2d.reloadModel === 'function') {
                try { window.__nbotLive2d.reloadModel(); } catch (_) {}
            }
        }
    };

    function flushLive2dQueue() {
        if (!window.__nbotLive2d || typeof window.__nbotLive2d.tipsMessage !== 'function') {
            return;
        }
        const queued = window.__nbotLive2dQueue.splice(0, window.__nbotLive2dQueue.length);
        queued.slice(-4).forEach((item) => {
            window.__nbotLive2d.tipsMessage(item.message, item.duration, item.priority);
        });
    }

    function oml2dConfigForModels(models) {
        return {
            dockedPosition: 'right',
            mobileDisplay: false,
            sayHello: false,
            transitionTime: 800,
            models: models.map(function (m) { return {
                name: m.name, path: m.path, position: m.position,
                scale: m.scale, stageStyle: stageForModel(m)
            }; }),
            tips: {
                style: {
                    width: 270, maxWidth: 320, minHeight: 48,
                    fontSize: '14px', lineHeight: '1.5', color: '#e6edf3',
                    backgroundColor: 'rgba(13, 17, 23, 0.88)',
                    border: '1px solid rgba(255, 255, 255, 0.12)',
                    boxShadow: '0 14px 36px rgba(0, 0, 0, 0.28)',
                    wordBreak: 'break-word', overflowWrap: 'break-word',
                    whiteSpace: 'normal', overflow: 'hidden', textOverflow: 'ellipsis'
                },
                welcomeTips: {
                    duration: 4200, priority: 3,
                    message: {
                        daybreak: '\u65e9\u4e0a\u597d\uff0c\u6211\u5728\u53f3\u8fb9\u5f85\u547d\u3002',
                        morning: '\u4e0a\u5348\u597d\uff0c\u9009\u4e2d\u4f1a\u8bdd\u540e\u6211\u4f1a\u8ddf\u7740 AI \u72b6\u6001\u63d0\u9192\u4f60\u3002',
                        noon: '\u4e2d\u5348\u597d\uff0c\u8bb0\u5f97\u4f11\u606f\u4e00\u4e0b\u3002',
                        afternoon: '\u4e0b\u5348\u597d\uff0c\u6211\u4f1a\u5728\u8fd9\u91cc\u966a\u4f60\u770b AI \u56de\u590d\u3002',
                        dusk: '\u508d\u665a\u4e86\uff0c\u4eca\u5929\u4e5f\u8f9b\u82e6\u4e86\u3002',
                        night: '\u665a\u4e0a\u597d\uff0c\u6709\u65b0\u56de\u590d\u6211\u4f1a\u63d0\u9192\u4f60\u3002',
                        lateNight: '\u591c\u6df1\u4e86\uff0c\u6211\u8fd8\u5728\u53f3\u8fb9\u5f85\u547d\u3002',
                        weeHours: '\u5df2\u7ecf\u5f88\u665a\u4e86\uff0c\u6ce8\u610f\u4f11\u606f\u3002'
                    }
                },
                idleTips: { duration: 5000, interval: 15000, priority: 1, message: live2dLines.idle },
                copyTips: { duration: 3000, priority: 2, message: live2dLines.copy }
            },
            statusBar: { disable: true, loadingMessage: 'Live2D loading...', loadSuccessMessage: 'Live2D ready', loadFailMessage: 'Live2D load failed', reloadMessage: 'Retry' },
            menus: { disable: true }
        };
    }

    function setupOml2d(oml2d) {
        installLive2dOverrides();
        waitForStageDrag();
        window.__nbotLive2d = oml2d;
        window.__nbotLive2dReady = true;
        flushLive2dQueue();
        localStorage.removeItem('NBOT_LIVE2D_DISABLED');
        window.__nbotLive2dRandomSay('ready', 3800, 3);
        fetchLive2dMenuOptions().catch(function () {});
    }

    function initLive2d() {
        if (!window.OML2D || typeof window.OML2D.loadOml2d !== 'function') return;

        try {
            var oml2d = window.OML2D.loadOml2d(oml2dConfigForModels(live2dModels));
            if (oml2d) {
                setupOml2d(oml2d);
                currentLive2dModelIndex = 0;
                return;
            }
        } catch (e) {
            console.warn('Live2D init failed:', e);
        }
    }

    function loadLive2d() {
        if (window.OML2D) {
            initLive2d();
            return;
        }

        const script = document.createElement('script');
        script.src = '/static/vendor/oh-my-live2d.min.js';
        script.async = true;
        script.onload = initLive2d;
        script.onerror = function () {
            console.warn('Failed to load Live2D widget script.');
        };
        document.head.appendChild(script);
    }

    if (document.readyState === 'complete') {
        loadLive2d();
    } else {
        window.addEventListener('load', loadLive2d, { once: true });
    }

    // Keep Live2D visible while the widget is being reset to a single local Pio model.
    window.__nbotLive2dSetEnabled = function (enabled) {
        localStorage.removeItem('NBOT_LIVE2D_DISABLED');
        var stage = document.getElementById('oml2d-stage');
        if (stage) {
            stage.style.display = '';
            var tips = document.getElementById('oml2d-tips');
            if (tips) tips.style.display = '';
        }
    };
})();



