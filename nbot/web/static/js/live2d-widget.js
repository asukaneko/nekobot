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

    var live2dMotionOrder = [
        'Breath1', 'Breath2', 'Breath3', 'Breath4', 'Breath5', 'Breath6', 'Breath7', 'Breath8',
        'Fail', 'Sleeping', 'Success',
        'Sukebei1', 'Sukebei2', 'Sukebei3',
        'Touch Dere1', 'Touch Dere2', 'Touch Dere3', 'Touch Dere4', 'Touch Dere5', 'Touch Dere6',
        'Touch1', 'Touch2', 'Touch3', 'Touch4', 'Touch5', 'Touch6',
        'WakeUp'
    ];

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
            var motionIndex = live2dMotionOrder.indexOf(motionName);
            var stage = oml2d.stage || oml2d;
            if (motionIndex >= 0 && stage && typeof stage.playMotion === 'function') {
                stage.playMotion('', motionIndex);
                return;
            }
            if (motionIndex >= 0 && stage?.model && typeof stage.model.motion === 'function') {
                stage.model.motion('', motionIndex);
                return;
            }
            if (motionIndex >= 0 && stage?.model && typeof stage.model.startMotion === 'function') {
                stage.model.startMotion('', motionIndex);
                return;
            }
            var internalModel = stage?.internalModel || stage?.currentModel || stage?._model;
            if (internalModel && typeof internalModel.motion === 'function') {
                internalModel.motion('', motionIndex >= 0 ? motionIndex : motionName);
                return;
            }
            if (internalModel && internalModel.internalModel && typeof internalModel.internalModel.motion === 'function') {
                internalModel.internalModel.motion('', motionIndex >= 0 ? motionIndex : motionName);
                return;
            }
            console.warn('Live2D motion API not found:', motionName, motionIndex, oml2d);
        } catch (e) {
            console.warn('Live2D motion failed:', motionName, e);
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
            '#oml2d-stage { background: transparent !important; overflow: visible !important; user-select: none; pointer-events: none; }',
            '#oml2d-stage.is-dragging { cursor: grabbing; }',
            '#oml2d-canvas { background: transparent !important; pointer-events: none !important; }',
            '.nbot-live2d-hitbox { position: fixed; width: 190px; height: 250px; z-index: 9999; cursor: grab; touch-action: none; pointer-events: auto; background: transparent; }',
            '.nbot-live2d-hitbox.is-dragging { cursor: grabbing; }',
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
            '.nbot-live2d-motion-panel { position: fixed; z-index: 10000; display: none; width: 260px; max-height: min(420px, calc(100vh - 24px)); padding: 9px; border: 1px solid rgba(148,163,184,0.18); border-radius: 10px; background: rgba(15,23,42,0.96); box-shadow: 0 18px 48px rgba(0,0,0,0.36), inset 0 1px 0 rgba(255,255,255,0.06); backdrop-filter: blur(14px); overflow: auto; }',
            '.nbot-live2d-motion-panel.is-visible { display: block; animation: nbot-live2d-menu-in 120ms ease-out; }',
            '.nbot-live2d-motion-group { margin-bottom: 9px; }',
            '.nbot-live2d-motion-group:last-child { margin-bottom: 0; }',
            '.nbot-live2d-motion-label { display: flex; align-items: center; gap: 6px; padding: 2px 1px 6px; color: #93c5fd; font-size: 11px; font-weight: 700; }',
            '.nbot-live2d-motion-label::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: #38bdf8; box-shadow: 0 0 10px rgba(56,189,248,0.5); }',
            '.nbot-live2d-motion-grid { display: flex; flex-wrap: wrap; gap: 6px; }',
            '.nbot-live2d-motion-panel .nbot-live2d-motion-item { flex: 1 1 calc(50% - 6px); min-width: 86px; max-width: 100%; padding: 6px 8px; border: 1px solid rgba(56,189,248,0.16); border-radius: 7px; color: #dbeafe; background: rgba(15,23,42,0.72); box-shadow: inset 0 1px 0 rgba(255,255,255,0.045); font-size: 11px; line-height: 1.2; text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }',
            '.nbot-live2d-motion-panel .nbot-live2d-motion-item:hover { color: #f8fafc; background: rgba(56,189,248,0.18); border-color: rgba(56,189,248,0.34); }',
            '#nbot-live2d-history-panel { position: fixed; z-index: 10001; display: none; width: min(360px, calc(100vw - 24px)); max-height: min(420px, calc(100vh - 24px)); padding: 10px; border: 1px solid rgba(148,163,184,0.2); border-radius: 10px; background: rgba(15,23,42,0.96); box-shadow: 0 18px 52px rgba(0,0,0,0.42), inset 0 1px 0 rgba(255,255,255,0.06); backdrop-filter: blur(16px); color: #e6edf3; overflow: hidden; }',
            '#nbot-live2d-history-panel.is-visible { display: flex; flex-direction: column; animation: nbot-live2d-menu-in 120ms ease-out; }',
            '.nbot-live2d-history-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 2px 2px 8px; border-bottom: 1px solid rgba(148,163,184,0.14); }',
            '.nbot-live2d-history-head strong { font-size: 13px; color: #f8fafc; }',
            '.nbot-live2d-history-head span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #94a3b8; font-size: 11px; }',
            '.nbot-live2d-history-close { flex: 0 0 26px; width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 7px; background: rgba(148,163,184,0.1); color: #cbd5e1; cursor: pointer; }',
            '.nbot-live2d-history-body { overflow: auto; padding-top: 8px; display: flex; flex-direction: column; gap: 7px; }',
            '.nbot-live2d-history-item { display: grid; grid-template-columns: 42px 1fr; gap: 8px; padding: 8px; border-radius: 8px; background: rgba(255,255,255,0.045); }',
            '.nbot-live2d-history-role { color: #38bdf8; font-size: 11px; font-weight: 700; text-transform: uppercase; }',
            '.nbot-live2d-history-text { color: #dbeafe; font-size: 12px; line-height: 1.45; word-break: break-word; }',
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
        const fallback = stageForModel(live2dModels[currentLive2dModelIndex] || live2dModels[0]);
        const width = stage.offsetWidth || fallback.width || 440;
        const height = stage.offsetHeight || fallback.height || 440;
        // 定位到右下角，向左上偏移一些避免紧贴屏幕边缘
        const offsetX = 95;
        const offsetY = 60;
        const left = window.innerWidth - width + getStageRightSlack(stage) - offsetX;
        const top = window.innerHeight - height + getDefaultStageBottomSlack(stage) - offsetY;
        return applyStagePosition(stage, left, top);
    }

    function scheduleDefaultStagePosition(stage) {
        let attempts = 0;
        const place = () => {
            if (!stage || !document.body.contains(stage)) return;
            attempts += 1;
            applyDefaultStagePosition(stage);
            const rect = stage.getBoundingClientRect();
            const hasStableSize = rect.width > 120 && rect.height > 120;
            const isInsideViewport = rect.right > 0 && rect.bottom > 0 && rect.left < window.innerWidth && rect.top < window.innerHeight;
            if ((!hasStableSize || !isInsideViewport) && attempts < 24) {
                window.setTimeout(place, attempts < 8 ? 120 : 250);
            }
        };
        window.requestAnimationFrame(place);
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
        syncStageHitbox(stage);
        return { left: nextLeft, top: nextTop };
    }

    function syncStageHitbox(stage) {
        const hitbox = document.getElementById('nbot-live2d-hitbox');
        if (!stage || !hitbox) return;
        const rect = stage.getBoundingClientRect();
        const hitboxWidth = hitbox.offsetWidth || 190;
        const hitboxHeight = hitbox.offsetHeight || 250;
        const left = rect.left + (rect.width / 2) - (hitboxWidth / 2);
        const top = rect.bottom - hitboxHeight - 4;
        hitbox.style.left = `${Math.round(left)}px`;
        hitbox.style.top = `${Math.round(top)}px`;
    }

    function getLive2dVisualBounds(stage) {
        const canvas = stage?.querySelector('canvas') || document.getElementById('oml2d-canvas');
        const rect = canvas?.getBoundingClientRect?.() || stage?.getBoundingClientRect?.();
        if (!rect) return null;

        if (canvas && canvas.width > 0 && canvas.height > 0) {
            try {
                const ctx = canvas.getContext('2d', { willReadFrequently: true });
                if (!ctx) throw new Error('2d context unavailable');
                const width = canvas.width;
                const height = canvas.height;
                const step = Math.max(2, Math.floor(Math.max(width, height) / 180));
                const pixels = ctx.getImageData(0, 0, width, height).data;
                let minX = width;
                let minY = height;
                let maxX = 0;
                let maxY = 0;
                let found = false;
                for (let y = 0; y < height; y += step) {
                    for (let x = 0; x < width; x += step) {
                        if (pixels[((y * width + x) * 4) + 3] > 8) {
                            found = true;
                            if (x < minX) minX = x;
                            if (y < minY) minY = y;
                            if (x > maxX) maxX = x;
                            if (y > maxY) maxY = y;
                        }
                    }
                }
                if (found) {
                    const scaleX = rect.width / width;
                    const scaleY = rect.height / height;
                    const pad = 10;
                    return {
                        left: rect.left + minX * scaleX - pad,
                        top: rect.top + minY * scaleY - pad,
                        right: rect.left + maxX * scaleX + pad,
                        bottom: rect.top + maxY * scaleY + pad,
                    };
                }
            } catch (error) {
                try {
                    const gl = canvas.getContext('webgl2')
                        || canvas.getContext('webgl')
                        || canvas.getContext('experimental-webgl');
                    if (!gl) throw new Error('webgl context unavailable');
                    const width = canvas.width;
                    const height = canvas.height;
                    const step = Math.max(2, Math.floor(Math.max(width, height) / 180));
                    const pixels = new Uint8Array(width * height * 4);
                    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
                    let minX = width;
                    let minY = height;
                    let maxX = 0;
                    let maxY = 0;
                    let found = false;
                    for (let y = 0; y < height; y += step) {
                        for (let x = 0; x < width; x += step) {
                            if (pixels[((y * width + x) * 4) + 3] > 8) {
                                found = true;
                                if (x < minX) minX = x;
                                if (y < minY) minY = y;
                                if (x > maxX) maxX = x;
                                if (y > maxY) maxY = y;
                            }
                        }
                    }
                    if (found) {
                        const scaleX = rect.width / width;
                        const scaleY = rect.height / height;
                        const pad = 12;
                        return {
                            left: rect.left + minX * scaleX - pad,
                            top: rect.top + (height - maxY) * scaleY - pad,
                            right: rect.left + maxX * scaleX + pad,
                            bottom: rect.top + (height - minY) * scaleY + pad,
                        };
                    }
                } catch (webglError) {
                    // Fall back below when canvas pixels cannot be read.
                }
            }
        }

        return {
            left: rect.left + rect.width * 0.18,
            top: rect.top + rect.height * 0.12,
            right: rect.left + rect.width * 0.82,
            bottom: rect.top + rect.height * 0.98,
        };
    }

    function isInsideLive2dVisual(stage, clientX, clientY) {
        const bounds = getLive2dVisualBounds(stage);
        if (!bounds) return false;

        const width = bounds.right - bounds.left;
        const height = bounds.bottom - bounds.top;
        const interactiveBounds = {
            left: bounds.left,
            top: bounds.top,
            right: bounds.left + width * 0.68,
            bottom: bounds.top + height * 0.78,
        };

        return clientX >= interactiveBounds.left
            && clientX <= interactiveBounds.right
            && clientY >= interactiveBounds.top
            && clientY <= interactiveBounds.bottom;
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

    function escapeHtml(value) {
        return String(value || '').replace(/[&<>"']/g, function (ch) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
        });
    }

    function ensureMotionPanel(menu) {
        var panel = menu.querySelector('.nbot-live2d-motion-panel');
        if (panel) return panel;

        panel = document.createElement('div');
        panel.className = 'nbot-live2d-motion-panel';
        const labels = {
            happy: '\u5f00\u5fc3',
            sad: '\u4f4e\u843d',
            surprised: '\u60ca\u8bb6',
            shy: '\u5bb3\u7f9e',
            naughty: '\u8c03\u76ae',
            tap: '\u70b9\u51fb',
            idle: '\u5f85\u673a'
        };
        panel.innerHTML = Object.keys(live2dMotions).map(function (group) {
            const motions = live2dMotions[group] || [];
            return '<div class="nbot-live2d-motion-group">'
                + '<div class="nbot-live2d-motion-label">' + (labels[group] || group) + '</div>'
                + '<div class="nbot-live2d-motion-grid">'
                + motions.map(function (motion) {
                    return '<button type="button" class="nbot-live2d-motion-item" data-action="play-motion" data-motion="' + escapeHtml(motion) + '" title="' + escapeHtml(motion) + '">' + escapeHtml(motion) + '</button>';
                }).join('')
                + '</div></div>';
        }).join('');
        document.body.appendChild(panel);
        // 子面板按钮点击事件
        panel.addEventListener('click', function (event) {
            var btn = event.target.closest('button');
            if (!btn) {
                // 点击空白区域关闭子面板
                panel.classList.remove('is-visible');
                return;
            }
            var action = btn.dataset.action;
            if (action === 'play-motion') {
                var motion = btn.dataset.motion;
                if (motion) {
                    playLive2dMotion(motion);
                    window.__nbotLive2dSay('\u64ad\u653e\u52a8\u4f5c\uff1a' + motion, 1800, 4);
                }
                panel.classList.remove('is-visible');
                hideLive2dMenu();
            }
        });
        return panel;
    }

    function positionMotionPanel(menu, anchorButton, panel) {
        const menuRect = menu.getBoundingClientRect();
        const anchorRect = anchorButton.getBoundingClientRect();
        const panelWidth = panel.offsetWidth || 260;
        const panelHeight = panel.offsetHeight || 360;
        const gap = 10;
        const rightSpace = window.innerWidth - menuRect.right;
        const leftSpace = menuRect.left;
        const openRight = rightSpace >= panelWidth + gap || rightSpace >= leftSpace;
        const left = openRight
            ? Math.min(window.innerWidth - panelWidth - 8, menuRect.right + gap)
            : Math.max(8, menuRect.left - panelWidth - gap);
        const top = Math.min(
            Math.max(8, anchorRect.top - 8),
            window.innerHeight - panelHeight - 8
        );
        panel.style.left = left + 'px';
        panel.style.top = top + 'px';
    }

    function toggleMotionPanel(menu, anchorButton) {
        const panel = ensureMotionPanel(menu);
        const shouldShow = !panel.classList.contains('is-visible');
        panel.classList.toggle('is-visible', shouldShow);
        if (shouldShow) {
            positionMotionPanel(menu, anchorButton, panel);
        }
    }

    function cleanHistoryText(text) {
        var value = String(text || '').replace(/\s+/g, ' ').trim();
        if (value.length > 180) {
            value = value.slice(0, 177).trim() + '...';
        }
        return value;
    }

    async function fetchLive2dHistory() {
        const response = await fetch('/api/live2d/history?limit=20', {
            method: 'GET',
            headers: getAuthHeaders()
        });
        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || 'Failed to load Live2D history');
        }
        return Array.isArray(data.messages) ? data.messages : [];
    }

    function hideLive2dHistoryPanel() {
        var panel = document.getElementById('nbot-live2d-history-panel');
        if (panel) {
            panel.classList.remove('is-visible');
        }
    }

    function ensureLive2dHistoryPanel() {
        var panel = document.getElementById('nbot-live2d-history-panel');
        if (panel) return panel;

        panel = document.createElement('div');
        panel.id = 'nbot-live2d-history-panel';
        panel.innerHTML = [
            '<div class="nbot-live2d-history-head">',
            '<div><strong>\u4f1a\u8bdd\u8bb0\u5f55</strong><span></span></div>',
            '<button type="button" class="nbot-live2d-history-close" title="\u5173\u95ed"><i class="fas fa-times"></i></button>',
            '</div>',
            '<div class="nbot-live2d-history-body"></div>'
        ].join('');
        panel.querySelector('.nbot-live2d-history-close').addEventListener('click', hideLive2dHistoryPanel);
        document.body.appendChild(panel);
        return panel;
    }

    async function showLive2dHistoryPanel(anchorRect) {
        let messages = [];
        try {
            messages = (await fetchLive2dHistory()).filter(function (msg) {
                return msg && msg.content;
            }).slice(-12);
        } catch (error) {
            console.warn('Live2D history fetch failed:', error);
            window.__nbotLive2dSay('\u770b\u677f\u5a18\u4f1a\u8bdd\u8bb0\u5f55\u8bfb\u53d6\u5931\u8d25\u3002', 3200, 4);
            return;
        }
        if (!messages.length) {
            window.__nbotLive2dSay('\u6682\u65f6\u8fd8\u6ca1\u6709\u548c\u770b\u677f\u5a18\u7684\u4f1a\u8bdd\u8bb0\u5f55\u3002', 3400, 4);
            return;
        }

        var panel = ensureLive2dHistoryPanel();
        var title = panel.querySelector('.nbot-live2d-history-head span');
        var body = panel.querySelector('.nbot-live2d-history-body');
        title.textContent = '\u770b\u677f\u5a18\u6700\u8fd1\u5bf9\u8bdd';
        body.innerHTML = messages.map(function (msg) {
            var role = msg.role === 'user' ? '\u6211' : (msg.role === 'assistant' ? 'AI' : (msg.role || 'sys'));
            var text = cleanHistoryText(msg.content);
            return '<div class="nbot-live2d-history-item"><div class="nbot-live2d-history-role">' + role + '</div><div class="nbot-live2d-history-text">' + text.replace(/[&<>"']/g, function (ch) {
                return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
            }) + '</div></div>';
        }).join('');

        panel.classList.add('is-visible');
        var width = panel.offsetWidth || 360;
        var height = panel.offsetHeight || 420;
        var left = Math.min(Math.max(12, anchorRect.left - width - 12), window.innerWidth - width - 12);
        var top = Math.min(Math.max(12, anchorRect.top), window.innerHeight - height - 12);
        panel.style.left = left + 'px';
        panel.style.top = top + 'px';
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
            '<button type="button" data-action="history"><span class="nbot-live2d-menu-icon"><i class="fas fa-history"></i></span><span>\u67e5\u770b\u4f1a\u8bdd\u8bb0\u5f55</span></button>',
            '<button type="button" data-action="motions"><span class="nbot-live2d-menu-icon"><i class="fas fa-running"></i></span><span>\u9009\u62e9\u52a8\u4f5c</span></button>',
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
            } else if (action === 'motions') {
                toggleMotionPanel(menu, btn);
                return;
            } else if (action === 'play-motion') {
                const motion = btn.dataset.motion;
                if (motion) {
                    playLive2dMotion(motion);
                    window.__nbotLive2dSay(`\u64ad\u653e\u52a8\u4f5c\uff1a${motion}`, 1800, 4);
                }
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
            } else if (action === 'history') {
                showLive2dHistoryPanel(btn.getBoundingClientRect());
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
            if (
                menu.contains(event.target)
                || document.querySelector('.nbot-live2d-motion-panel')?.contains(event.target)
                || document.getElementById('oml2d-stage')?.contains(event.target)
                || document.getElementById('nbot-live2d-hitbox')?.contains(event.target)
            ) return;
            hideLive2dMenu();
        });
        document.addEventListener('pointerdown', (event) => {
            var panel = document.getElementById('nbot-live2d-history-panel');
            if (!panel || !panel.classList.contains('is-visible')) return;
            if (panel.contains(event.target) || menu.contains(event.target)) return;
            hideLive2dHistoryPanel();
        });
        return menu;
    }

    function hideLive2dMenu() {
        var menu = document.getElementById('nbot-live2d-menu');
        if (menu) {
            menu.classList.remove('is-visible');
            document.querySelector('.nbot-live2d-motion-panel')?.classList.remove('is-visible');
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

    function ensureStageHitbox(stage) {
        let hitbox = document.getElementById('nbot-live2d-hitbox');
        if (hitbox) return hitbox;
        hitbox = document.createElement('div');
        hitbox.id = 'nbot-live2d-hitbox';
        hitbox.className = 'nbot-live2d-hitbox';
        hitbox.setAttribute('aria-label', 'Live2D interaction area');
        document.body.appendChild(hitbox);
        syncStageHitbox(stage);
        return hitbox;
    }

    function enableStageDrag() {
        const stage = document.getElementById('oml2d-stage');
        if (!stage || stage.dataset.nbotDragReady === 'true') return;
        stage.dataset.nbotDragReady = 'true';

        clearSavedStagePosition();
        scheduleDefaultStagePosition(stage);

        let dragging = false;
        let startX = 0;
        let startY = 0;
        let startLeft = 0;
        let startTop = 0;
        let didMove = false;
        let swallowNextClick = false;

        document.addEventListener('pointerdown', (event) => {
            if (event.button !== undefined && event.button !== 0) return;
            if (!isInsideLive2dVisual(stage, event.clientX, event.clientY)) return;
            event.preventDefault();
            event.stopPropagation();
            const rect = stage.getBoundingClientRect();
            dragging = true;
            didMove = false;
            swallowNextClick = true;
            startX = event.clientX;
            startY = event.clientY;
            startLeft = rect.left;
            startTop = rect.top;
            stage.classList.add('is-dragging');
        }, true);

        document.addEventListener('pointermove', (event) => {
            if (!dragging) return;
            event.preventDefault();
            event.stopPropagation();
            const dx = event.clientX - startX;
            const dy = event.clientY - startY;
            if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
                didMove = true;
            }
            applyStagePosition(stage, startLeft + dx, startTop + dy);
        }, true);

        function endDrag(event) {
            if (!dragging) return;
            event.preventDefault();
            event.stopPropagation();
            dragging = false;
            stage.classList.remove('is-dragging');
            const rect = stage.getBoundingClientRect();
            saveStagePosition(rect.left, rect.top);
        }

        document.addEventListener('pointerup', endDrag, true);
        document.addEventListener('pointercancel', endDrag, true);
        document.addEventListener('click', (event) => {
            if (!swallowNextClick && !isInsideLive2dVisual(stage, event.clientX, event.clientY)) return;
            swallowNextClick = false;
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
            syncStageHitbox(stage);
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
    window.__nbotLive2dQueuePlaying = false;
    window.__nbotLive2dAutoTalkTimer = null;
    window.__nbotLive2dAiTalking = false;

    function playNextLive2dMessage() {
        if (window.__nbotLive2dQueuePlaying) return;
        if (!window.__nbotLive2dReady || !window.__nbotLive2d || typeof window.__nbotLive2d.tipsMessage !== 'function') return;

        const item = window.__nbotLive2dQueue.shift();
        if (!item) return;

        window.__nbotLive2dQueuePlaying = true;
        const duration = item.duration || 3600;
        window.__nbotLive2d.tipsMessage(item.message, duration, item.priority || 4);
        window.setTimeout(() => {
            window.__nbotLive2dQueuePlaying = false;
            playNextLive2dMessage();
        }, duration + 220);
    }

    window.__nbotLive2dSay = function (message, duration, priority) {
        if (!message) return;
        window.__nbotLive2dQueue.push({ message, duration: duration || 3600, priority: priority || 4 });
        playNextLive2dMessage();
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

    window.__nbotLive2dAiTalk = async function (topic, options) {
        if (window.__nbotLive2dAiTalking) return;
        window.__nbotLive2dAiTalking = true;
        options = options || {};
        const hasTopic = topic && typeof topic === 'string' && topic.trim();
        if (!options.silentStart) {
            if (hasTopic) {
                window.__nbotLive2dSay('...', 2000, 5);
            } else {
                window.__nbotLive2dSay('\u6211\u770b\u4e00\u4e0b\u73b0\u5728\u7684\u72b6\u6001...', 2800, 6);
            }
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
        } finally {
            window.__nbotLive2dAiTalking = false;
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
        playNextLive2dMessage();
    }

    function startLive2dAutoTalk() {
        if (window.__nbotLive2dAutoTalkTimer) return;
        window.__nbotLive2dAutoTalkTimer = window.setInterval(() => {
            if (!window.__nbotLive2dReady || window.__nbotLive2dAiTalking) return;
            if (document.hidden) return;
            if (document.getElementById('nbot-live2d-menu')?.classList.contains('is-visible')) return;
            if (document.getElementById('nbot-live2d-history-panel')?.classList.contains('is-visible')) return;
            window.__nbotLive2dRandomSay('idle', 4200, 2);
        }, 30000);
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
                    width: 330, maxWidth: 'min(330px, calc(100vw - 32px))', minHeight: 54, maxHeight: 190,
                    fontSize: '14px', lineHeight: '1.5', color: '#e6edf3',
                    backgroundColor: 'rgba(13, 17, 23, 0.88)',
                    border: '1px solid rgba(255, 255, 255, 0.12)',
                    boxShadow: '0 14px 36px rgba(0, 0, 0, 0.28)',
                    wordBreak: 'break-word', overflowWrap: 'break-word',
                    whiteSpace: 'normal', overflow: 'auto', textOverflow: 'clip'
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
        startLive2dAutoTalk();
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
        if (localStorage.getItem('NBOT_LIVE2D_DISABLED') === '1') return;
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
        var stage = document.getElementById('oml2d-stage');
        var tips = document.getElementById('oml2d-tips');
        if (enabled) {
            localStorage.removeItem('NBOT_LIVE2D_DISABLED');
            if (stage) stage.style.display = '';
            if (tips) tips.style.display = '';
        } else {
            localStorage.setItem('NBOT_LIVE2D_DISABLED', '1');
            if (stage) stage.style.display = 'none';
            if (tips) tips.style.display = 'none';
        }
    };
})();




