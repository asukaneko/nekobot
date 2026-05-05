const api = window.__nbotApi;
const socket = window.__nbotSocket;
const copyCodeBlock = window.__nbotCopyCodeBlock;
const connectSocketWithAuth = window.__nbotConnectSocketWithAuth;

const NbotMethods = {
                async refreshPushState() {
                    if (!window.NekoPush) {
                        this.pushSupported = false;
                        this.pushPermission = 'unsupported';
                        this.pushSubscribed = false;
                        return;
                    }
                    const state = await window.NekoPush.state();
                    this.pushSupported = state.supported;
                    this.pushPermission = state.permission;
                    this.pushSubscribed = state.subscribed;
                    this.pushSecureContext = !!state.secureContext;
                },

                async togglePushNotifications() {
                    if (!window.NekoPush || this.pushBusy) return;
                    if (!this.pushSupported) {
                        this.showToast(this.pushSecureContext ? 'Current browser does not support Web Push' : 'Browser notifications require HTTPS or localhost', 'error');
                        return;
                    }
                    this.pushBusy = true;
                    try {
                        if (this.pushSubscribed) {
                            await window.NekoPush.disable();
                            this.pushSubscribed = false;
                            this.showToast('Browser notifications disabled', 'info');
                        } else {
                            await window.NekoPush.enable(this.currentSession?.id || '');
                            this.pushSubscribed = true;
                            await api.post('/api/push/test', {
                                session_id: this.currentSession?.id || '',
                                body: 'NekoBot browser notifications are enabled.'
                            });
                            this.showToast('Browser notifications enabled', 'success');
                        }
                        await this.refreshPushState();
                    } catch (e) {
                        this.showToast(e.message || 'Failed to update browser notifications', 'error');
                    } finally {
                        this.pushBusy = false;
                    }
                },

                updateWebVisibility() {
                    if (!socket || !socket.connected) return;
                    socket.emit('web_visibility', {
                        session_id: this.currentSession?.id || '',
                        visible: document.visibilityState === 'visible' && this.currentPage === 'chat'
                    });
                },
                // 语言切换
                changeLanguage(lang) {
                    this.$setLanguage(lang);
                    this.currentLanguage = lang;
                    // 强制刷新页面以应用新语言
                    this.$forceUpdate();
                    this.showToast(this.$t('language.changed'), 'success');
                },

                handleGlobalModalOverlayClick(event) {
                    if (this.themeSettings.closeModalOnOverlayClick) {
                        return;
                    }
                    if (!(event.target instanceof HTMLElement)) {
                        return;
                    }
                    if (!event.target.classList.contains('modal-overlay')) {
                        return;
                    }
                    event.stopPropagation();
                },

                // 消息样式编辑方法
                openMessageStyleEditor() {
                    this.showMessageStyleModal = true;
                    this.$nextTick(() => this.updateStylePreview());
                },

                updateStylePreview() {
                    // 实时预览由计算属性自动处理
                    this.$forceUpdate();
                    // 更新滑条进度显示
                    this.$nextTick(() => {
                        this.updateRangeProgress();
                    });
                },

                updateRangeProgress() {
                    // 更新所有滑条的进度条颜色
                    const ranges = document.querySelectorAll('.form-range');
                    ranges.forEach(range => {
                        const min = parseFloat(range.min) || 0;
                        const max = parseFloat(range.max) || 100;
                        const value = parseFloat(range.value) || 0;
                        const progress = ((value - min) / (max - min)) * 100;
                        range.style.setProperty('--progress', progress + '%');
                    });
                },

                saveMessageStyle() {
                    // 保存到 localStorage
                    localStorage.setItem('messageFontFamily', this.messageStyle.fontFamily);
                    localStorage.setItem('messageFontSize', this.messageStyle.fontSize);
                    localStorage.setItem('messageLineHeight', this.messageStyle.lineHeight);
                    localStorage.setItem('messageParagraphSpacing', this.messageStyle.paragraphSpacing);
                    localStorage.setItem('messageTextColor', this.messageStyle.textColor);
                    localStorage.setItem('userBubbleColor', this.messageStyle.userBubbleColor);
                    localStorage.setItem('assistantBubbleColor', this.messageStyle.assistantBubbleColor);

                    this.showMessageStyleModal = false;
                    this.showToast('消息样式已保存', 'success');

                    // 应用样式到当前聊天
                    this.applyMessageStyles();
                },

                resetMessageStyle() {
                    this.messageStyle = {
                        fontFamily: 'system-ui, -apple-system, sans-serif',
                        fontSize: 14,
                        lineHeight: 1.6,
                        paragraphSpacing: 12,
                        textColor: '',
                        userBubbleColor: '',
                        assistantBubbleColor: ''
                    };
                    this.updateStylePreview();
                },

                applyMessageStyles() {
                    // 创建样式标签
                    let styleEl = document.getElementById('message-custom-styles');
                    if (!styleEl) {
                        styleEl = document.createElement('style');
                        styleEl.id = 'message-custom-styles';
                        document.head.appendChild(styleEl);
                    }

                    const textColor = this.messageStyle.textColor || 'inherit';
                    const userBubbleColor = this.messageStyle.userBubbleColor || '';
                    const assistantBubbleColor = this.messageStyle.assistantBubbleColor || '';

                    styleEl.textContent = `
                        .message-content,
                        .message-content .markdown-body,
                        .message-content .markdown-body p,
                        .message-content .markdown-body li {
                            font-family: ${this.messageStyle.fontFamily} !important;
                            font-size: ${this.messageStyle.fontSize}px !important;
                            line-height: ${this.messageStyle.lineHeight} !important;
                            color: ${textColor} !important;
                        }
                        .message-content .markdown-body p {
                            margin-bottom: ${this.messageStyle.paragraphSpacing}px !important;
                        }
                        .message-content .markdown-body p:last-child {
                            margin-bottom: 0 !important;
                        }
                        ${userBubbleColor ? `.message.user .message-content { background: ${userBubbleColor} !important; }` : ''}
                        ${assistantBubbleColor ? `.message.assistant .message-content { background: ${assistantBubbleColor} !important; }` : ''}
                    `;
                },

                // 聊天背景设置方法
                openChatBackgroundEditor() {
                    this.showChatBackgroundModal = true;
                },

                saveChatBackground() {
                    // 保存到 localStorage
                    localStorage.setItem('chatBackgroundType', this.chatBackground.type);
                    localStorage.setItem('chatBackgroundColor', this.chatBackground.color);
                    localStorage.setItem('chatBackgroundImage', this.chatBackground.image);
                    localStorage.setItem('chatBackgroundOpacity', this.chatBackground.opacity);
                    localStorage.setItem('chatBackgroundBlur', this.chatBackground.blur);
                    localStorage.setItem('chatBackgroundUsePortrait', this.chatBackground.usePortrait);
                    localStorage.setItem('chatBackgroundPosX', this.chatBackground.posX);
                    localStorage.setItem('chatBackgroundPosY', this.chatBackground.posY);

                    this.showChatBackgroundModal = false;
                    this.showToast('聊天背景已保存', 'success');

                    // 应用背景
                    this.applyChatBackground();
                },

                // 开始拖动背景
                startBackgroundDrag(e) {
                    if (this.chatBackground.type !== 'image' && this.chatBackground.type !== 'portrait') return;
                    
                    this.isDraggingBackground = true;
                    const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                    const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                    
                    this.backgroundDragStart = { x: clientX, y: clientY };
                    this.backgroundDragStartPos = { 
                        x: this.chatBackground.posX, 
                        y: this.chatBackground.posY 
                    };
                },

                // 拖动背景
                onBackgroundDrag(e) {
                    if (!this.isDraggingBackground) return;
                    e.preventDefault();
                    
                    const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                    const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                    
                    // 计算拖动距离（转换为百分比）
                    const deltaX = (clientX - this.backgroundDragStart.x) / 3; // 调整灵敏度
                    const deltaY = (clientY - this.backgroundDragStart.y) / 3;
                    
                    // 更新位置（限制在 0-100 范围内）
                    this.chatBackground.posX = Math.max(0, Math.min(100, this.backgroundDragStartPos.x + deltaX));
                    this.chatBackground.posY = Math.max(0, Math.min(100, this.backgroundDragStartPos.y + deltaY));
                },

                // 停止拖动背景
                stopBackgroundDrag() {
                    this.isDraggingBackground = false;
                },

                resetChatBackground() {
                    this.chatBackground = {
                        type: 'none',
                        color: '#1a1a2e',
                        image: '',
                        posX: 50,
                        posY: 50,
                        opacity: 20,
                        blur: 0,
                        usePortrait: false
                    };
                },

                // 清除聊天背景
                clearChatBackground() {
                    // 移除背景元素
                    const oldBg = document.getElementById('chat-background-layer');
                    if (oldBg) {
                        oldBg.remove();
                    }
                    // 清除样式
                    const styleEl = document.getElementById('chat-background-styles');
                    if (styleEl) {
                        styleEl.textContent = '';
                    }
                },

                applyChatBackground() {
                    const opacity = this.chatBackground.opacity / 100;
                    const blur = this.chatBackground.blur;

                    // 获取或创建样式标签
                    let styleEl = document.getElementById('chat-background-styles');
                    if (!styleEl) {
                        styleEl = document.createElement('style');
                        styleEl.id = 'chat-background-styles';
                        document.head.appendChild(styleEl);
                    }

                    // 移除旧的背景元素
                    const oldBg = document.getElementById('chat-background-layer');
                    if (oldBg) {
                        oldBg.remove();
                    }

                    if (this.chatBackground.type === 'none') {
                        styleEl.textContent = '';
                        return;
                    }

                    let bgUrl = '';
                    let bgSize = 'cover';
                    let bgRepeat = 'no-repeat';

                    if (this.chatBackground.type === 'color') {
                        // 纯色背景
                        styleEl.textContent = `
                            .messages-container {
                                background-image: none !important;
                                background-color: ${this.chatBackground.color} !important;
                            }
                        `;
                        return;
                    } else if (this.chatBackground.type === 'image' && this.chatBackground.image) {
                        bgUrl = this.chatBackground.image;
                        bgSize = 'cover';
                    } else if (this.chatBackground.type === 'portrait') {
                        const portraitUrl = this.currentSession?.sender_portrait || this.personality?.portrait || '';
                        if (portraitUrl) {
                            bgUrl = portraitUrl;
                            bgSize = 'contain';
                        } else {
                            styleEl.textContent = '';
                            return;
                        }
                    }

                    if (!bgUrl) {
                        styleEl.textContent = '';
                        return;
                    }

                    // 获取背景位置
                    const posX = this.chatBackground.posX || 50;
                    const posY = this.chatBackground.posY || 50;

                    // 找到聊天主区域
                    const chatMain = document.querySelector('.chat-main');
                    if (!chatMain) return;

                    // 确保 chat-main 是相对定位
                    chatMain.style.position = 'relative';

                    // 创建背景层 - 使用 absolute 定位覆盖整个 chat-main
                    const bgLayer = document.createElement('div');
                    bgLayer.id = 'chat-background-layer';
                    bgLayer.style.cssText = `
                        position: absolute;
                        top: 0;
                        left: 0;
                        width: 100%;
                        height: 100%;
                        background-image: url('${bgUrl}');
                        background-size: ${bgSize};
                        background-position: ${posX}% ${posY}%;
                        background-repeat: ${bgRepeat};
                        opacity: ${opacity};
                        ${blur > 0 ? `filter: blur(${blur}px);` : ''}
                        pointer-events: none;
                        z-index: 0;
                    `;

                    // 插入到 chat-main 的开头
                    chatMain.insertBefore(bgLayer, chatMain.firstChild);

                    // 设置样式确保其他元素在背景层之上
                    styleEl.textContent = `
                        .chat-main {
                            position: relative;
                        }
                        
                        .chat-header {
                            position: relative;
                            z-index: 1;
                        }
                        
                        .messages-container {
                            position: relative;
                            z-index: 1;
                            background: transparent !important;
                        }
                        
                        .chat-input-area {
                            position: relative;
                            z-index: 1;
                        }
                        
                        /* 给消息内容添加半透明背景，确保文字可读 */
                        .message.assistant .message-content {
                            background: color-mix(in srgb, var(--bg-tertiary) 92%, transparent) !important;
                            backdrop-filter: blur(8px);
                            -webkit-backdrop-filter: blur(8px);
                        }
                        
                        /* 用户消息保持原有颜色 */
                        .message.user .message-content {
                            background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%) !important;
                            backdrop-filter: none;
                            -webkit-backdrop-filter: none;
                        }
                    `;
                },

                handleChatBackgroundImageUpload(event) {
                    const file = event.target.files[0];
                    if (!file) return;

                    const reader = new FileReader();
                    reader.onload = (e) => {
                        this.chatBackground.image = e.target.result;
                        this.chatBackground.type = 'image';
                    };
                    reader.readAsDataURL(file);
                },

                // 文件上传菜单方法
                toggleFileMenu() {
                    this.showFileMenu = !this.showFileMenu;
                },
                closeFileMenu() {
                    this.showFileMenu = false;
                },
                triggerFileUpload() {
                    this.showFileMenu = false;
                    this.$refs.fileInput.click();
                },
                triggerWorkspaceUpload() {
                    this.showFileMenu = false;
                    this.$refs.workspaceFileInput.click();
                },

                // 会话刷新方法
                async refreshSessionMessages(sessionId) {
                    // 如果当前查看的正是这个会话，刷新消息
                    if (this.currentSession && this.currentSession.id === sessionId) {
                        // 刷新消息不强制滚动，避免打扰用户
                        await this.loadMessages(false);
                    }
                    // 同时刷新会话列表（更新最后消息时间等）
                    await this.loadSessions();
                },

                // Context Indicator Methods
                isProgressMessage(msg) {
                    if (!msg) return false;
                    if (msg.is_progress || msg.is_progress_message) return true;

                    const messageType = msg.message_type || msg.type || msg.metadata?.message_type;
                    if (messageType === 'progress') return true;

                    const content = String(msg.content || '');
                    const compactContent = content.replace(/\s+/g, '');
                    const isShortAssistantNotice = msg.role === 'assistant'
                        && !msg.file
                        && compactContent.length <= 40;
                    if (
                        msg.role === 'assistant'
                        && !msg.file
                        && (
                            content.includes('\u5904\u7406\u5b8c\u6210')
                            || content.includes('AI \u6b63\u5728\u5904\u7406')
                            || (isShortAssistantNotice && /[\u2705\u2611]/u.test(content))
                        )
                    ) {
                        return true;
                    }
                    return msg.role === 'assistant'
                        && !msg.file
                        && (content.includes('处理完成') || content.includes('AI 正在处理'));
                },

                getContextStatMessages() {
                    if (!this.currentSession) return [];

                    const contextMessages = [];
                    const systemPrompt = this.currentSession.system_prompt || this.aiConfig.system_prompt || '';
                    if (systemPrompt) {
                        contextMessages.push({
                            role: 'system',
                            content: systemPrompt
                        });
                    }

                    // 不再按条数限制，返回所有可见消息，由后端按 token 预算裁剪
                    const visibleRoles = new Set(['user', 'assistant', 'tool', 'system']);
                    const historyMessages = (this.currentMessages || [])
                        .filter(msg => msg && !msg.hide_in_web && visibleRoles.has(msg.role || ''));

                    return contextMessages.concat(historyMessages);
                },

                estimateTextTokens(text) {
                    const value = String(text || '');
                    if (!value) return 0;

                    const cjkMatches = value.match(/[\u3400-\u9fff\uf900-\ufaff]/g) || [];
                    const cjkCount = cjkMatches.length;
                    const nonCjkText = value.replace(/[\u3400-\u9fff\uf900-\ufaff]/g, ' ');
                    const wordLikeCount = (nonCjkText.match(/[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/g) || []).length;

                    return Math.ceil(cjkCount * 1.05 + wordLikeCount * 0.75);
                },

                estimateMessageTokens(msg) {
                    if (!msg) return 0;

                    let tokenCount = 4 + this.estimateTextTokens(msg.role || '');
                    tokenCount += this.estimateTextTokens(msg.content || '');

                    if (Array.isArray(msg.attachments) && msg.attachments.length) {
                        const attachmentText = msg.attachments
                            .map(file => [
                                file.name || file.filename || '',
                                file.type || file.mime_type || '',
                                file.extracted_text || file.content || ''
                            ].join(' '))
                            .join('\n');
                        tokenCount += this.estimateTextTokens(attachmentText);
                    }

                    return tokenCount;
                },

                updateContextStats() {
                    const contextMessages = this.getContextStatMessages();
                    if (!this.currentSession || !contextMessages.length) {
                        this.contextCharCount = 0;
                        this.contextTokenEstimate = 0;
                        this.contextUsage = 0;
                        this.contextMessageCount = 0;
                        return;
                    }
                    
                    // 计算总字符数
                    this.contextMessageCount = contextMessages.length;

                    this.contextCharCount = contextMessages.reduce((total, msg) => {
                        const content = typeof msg.content === 'string' ? msg.content : '';
                        const role = msg.role || '';
                        return total + role.length + content.length;
                    }, 0);
                    
                    // 粗略估算token数 (中文字符约2个token，英文约0.75个)
                    this.contextTokenEstimate = Math.ceil(
                        this.currentMessages.reduce((total, msg) => {
                            const content = typeof msg.content === 'string' ? msg.content : '';
                            // 简单估算：每4个字符约等于1个token
                            return total + Math.ceil(content.length / 4);
                        }, 0)
                    );
                    
                    // 计算使用百分比（基于 Token 数量，使用 max_context_length 作为限制）
                    this.contextTokenEstimate = contextMessages.reduce(
                        (total, msg) => total + this.estimateMessageTokens(msg),
                        0
                    );

                    const maxTokens = this.aiConfig.max_context_length || 100000;
                    const estimatedTokens = this.contextTokenEstimate || 0;
                    this.contextUsage = Math.min(100, (estimatedTokens / maxTokens) * 100);
                },
                
                async compressContext() {
                    if (this.isCompressing || !this.currentSession) return;

                    this.isCompressing = true;
                    try {
                        // 调用后端API压缩上下文
                        const res = await api.post(`/api/sessions/${this.currentSession.id}/compress`, {});

                        if (res.data.success) {
                            // 保存总结信息到会话
                            if (res.data.summary) {
                                if (!this.currentSession.historySummary) {
                                    this.currentSession.historySummary = [];
                                }
                                this.currentSession.historySummary.push({
                                    time: new Date().toISOString(),
                                    summary: res.data.summary,
                                    compressedCount: res.data.compressed_count
                                });
                            }
                            // 重新加载会话消息，强制滚动到底部显示新结构
                            await this.loadMessages(true);
                            this.updateContextStats();
                            this.showToast('上下文压缩成功', 'success');
                        } else {
                            this.showToast(res.data.error || '压缩失败', 'error');
                        }
                    } catch (e) {
                        console.error('Compress context error:', e);
                        this.showToast('压缩上下文失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isCompressing = false;
                    }
                },
                
                async login() {
                    if (!this.username || !this.password) return;
                    this.isLoading = true;
                    try {
                        // 验证密码
                        const res = await api.post('/api/login', {
                            username: this.username,
                            password: this.password
                        });
                        
                        if (res.data.success) {
                            // 使用 localStorage 存储登录信息（支持长时间免登录）
                            localStorage.setItem('username', this.username);
                            if (res.data.token) {
                                localStorage.setItem('auth_token', res.data.token);
                            }
                            this.isLoggedIn = true;
                            connectSocketWithAuth();
                            this.password = ''; // 清空密码
                            await this.loadHomeData();
                            this.showToast('登录成功', 'success');
                        } else {
                            this.showToast(res.data.message || '密码错误', 'error');
                        }
                    } catch (e) {
                        this.showToast('登录失败: ' + (e.response?.data?.message || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async logout() {
                    // 调用后端登出 API 删除 Token
                    const token = localStorage.getItem('auth_token');
                    if (token) {
                        try {
                            await api.post('/api/logout', { token });
                        } catch (e) {
                            console.error('登出失败:', e);
                        }
                    }
                    
                    // 清除本地存储
                    localStorage.removeItem('username');
                    localStorage.removeItem('auth_token');
                    if (socket.connected) {
                        socket.disconnect();
                    }
                    
                    this.isLoggedIn = false;
                    this.username = '';
                    this.password = '';
                    this.thinkingCards = [];
                    this.orphanCards = {};
                    this.currentMessages = [];
                    this.showToast('已登出', 'info');
                },
                
                navigateTo(page, event) {
                    if (this.isChatOnlyMode && page !== 'chat') {
                        this.goDashboard(page);
                        return;
                    }
                    // 添加点击动画效果
                    if (event && event.currentTarget) {
                        const navItem = event.currentTarget;
                        navItem.classList.add('clicked');
                        setTimeout(() => {
                            navItem.classList.remove('clicked');
                        }, 400);
                    }

                    this.currentPage = page;
                    if (this.isChatOnlyMode) {
                        localStorage.setItem('nbot_home_page', 'chat');
                    } else {
                        localStorage.setItem('nbot_dashboard_page', page);
                    }
                    this.isMobileMenuOpen = false;
                    this.isMobileChatPickerOpen = false;
                    this.loadPageData(page);
                    // 如果是样式编辑页面或人格设置页面，初始化滑条进度
                    if (page === 'message-style' || page === 'personality') {
                        this.$nextTick(() => {
                            this.updateRangeProgress();
                        });
                    }
                },

                async openLatestChat() {
                    // 确保会话列表已加载
                    if (this.sessions.length === 0) {
                        await this.loadSessions();
                    }
                    // 过滤非临时、非归档的 Web/频道会话，取最新创建的
                    const activeSessions = this.sessions
                        .filter(s => !s._isTemp && !s.archived)
                        .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
                    if (activeSessions.length > 0) {
                        const latest = activeSessions[0];
                        // 切换到对应的 tab
                        if (latest.type === 'cli') {
                            await this.switchChatTab('cli');
                        } else if (latest.type && latest.type.startsWith('qq')) {
                            await this.switchChatTab(latest.type === 'qq_group' ? 'qq_group' : 'qq_private');
                        } else if (latest.channel_id) {
                            await this.switchChatTab('channel_' + latest.channel_id);
                        } else {
                            await this.switchChatTab('web');
                        }
                        this.currentPage = 'chat';
                        this.openSession(latest);
                    } else {
                        // 没有会话时直接打开聊天页
                        this.navigateTo('chat');
                    }
                },

                toggleSidebar() {
                    this.isSidebarCollapsed = !this.isSidebarCollapsed;
                },

                goDashboard(page = 'dashboard') {
                    localStorage.setItem('nbot_dashboard_page', page);
                    window.location.href = '/dashboard';
                },

                toggleChatList() {
                    this.isChatListCollapsed = !this.isChatListCollapsed;
                    localStorage.setItem('nbot_chat_list_collapsed', this.isChatListCollapsed ? 'true' : 'false');
                },

                toggleChatHeader() {
                    this.isChatHeaderHidden = !this.isChatHeaderHidden;
                    this.showChatViewMenu = false;
                    localStorage.setItem('nbot_chat_header_hidden', this.isChatHeaderHidden ? 'true' : 'false');
                },

                toggleChatViewMenu() {
                    this.showChatViewMenu = !this.showChatViewMenu;
                },

                closeChatViewMenu() {
                    this.showChatViewMenu = false;
                },

                updateChatHorizontalMargin() {
                    const margin = Math.min(300, Math.max(0, Number(this.chatHorizontalMargin) || 0));
                    this.chatHorizontalMargin = margin;
                    localStorage.setItem('nbot_chat_horizontal_margin', String(margin));
                },

                toggleMobileMenu() {
                    this.isMobileMenuOpen = !this.isMobileMenuOpen;
                    console.log('[MobileMenu] Toggle:', this.isMobileMenuOpen);
                },

                openMobileChatPicker() {
                    this.isMobileChatPickerOpen = true;
                },

                closeMobileChatPicker() {
                    this.isMobileChatPickerOpen = false;
                },

                // 频道下拉菜单方法
                toggleChannelDropdown() {
                    this.showChannelDropdown = !this.showChannelDropdown;
                },

                openChannelDropdown() {
                    this.showChannelDropdown = true;
                },

                closeChannelDropdown() {
                    this.showChannelDropdown = false;
                },

                // 频道抽屉方法（用于移动端）
                openChannelDrawer() {
                    this.showChannelDrawer = true;
                },

                closeChannelDrawer() {
                    this.showChannelDrawer = false;
                },

                // 获取当前频道的渐变颜色
                getCurrentChannelGradient() {
                    const tab = this.chatTab || 'web';
                    // 如果是自定义频道
                    if (tab.startsWith('channel_')) {
                        const channelId = tab.replace('channel_', '');
                        const channel = this.channels.find(ch => ch.id === channelId);
                        if (channel) {
                            return this.getChannelGradient(channel);
                        }
                    }
                    const gradients = {
                        web: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
                        cli: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
                        qq_private: 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
                        qq_group: 'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)'
                    };
                    return gradients[tab] || 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)';
                },

                // 获取当前频道的图标
                getCurrentChannelIcon() {
                    const tab = this.chatTab || 'web';
                    // 如果是自定义频道
                    if (tab.startsWith('channel_')) {
                        const channelId = tab.replace('channel_', '');
                        const channel = this.channels.find(ch => ch.id === channelId);
                        if (channel) {
                            return this.getChannelIcon(channel);
                        }
                    }
                    const icons = {
                        web: 'fas fa-globe',
                        cli: 'fas fa-terminal',
                        qq_private: 'fab fa-qq',
                        qq_group: 'fas fa-users'
                    };
                    return icons[tab] || 'fas fa-globe';
                },

                // 获取当前频道名称
                getCurrentChannelName() {
                    const tab = this.chatTab || 'web';
                    // 如果是自定义频道
                    if (tab.startsWith('channel_')) {
                        const channelId = tab.replace('channel_', '');
                        const channel = this.channels.find(ch => ch.id === channelId);
                        if (channel) {
                            return channel.name;
                        }
                    }
                    const names = {
                        web: 'Web',
                        cli: 'CLI 终端',
                        qq_private: 'QQ 私聊',
                        qq_group: 'QQ 群聊'
                    };
                    return names[tab] || 'Web';
                },

                // 获取频道的渐变颜色
                getChannelGradient(channel) {
                    const gradients = {
                        telegram: 'linear-gradient(135deg, #0088cc 0%, #00a8e6 100%)',
                        feishu: 'linear-gradient(135deg, #3370ff 0%, #5b8aff 100%)',
                        feishu_ws: 'linear-gradient(135deg, #00d6b9 0%, #00f5d4 100%)',
                        custom: 'linear-gradient(135deg, #ff6b6b 0%, #feca57 100%)'
                    };
                    return gradients[channel.type] || gradients.custom;
                },

                // 获取频道的图标
                getChannelIcon(channel) {
                    const icons = {
                        telegram: 'fab fa-telegram',
                        feishu: 'fas fa-paper-plane',
                        feishu_ws: 'fas fa-bolt',
                        custom: 'fas fa-plug'
                    };
                    return icons[channel.type] || icons.custom;
                },

                // 切换到注册频道标签
                switchToChannelTab(channel) {
                    this.chatTab = 'channel_' + channel.id;
                    this.currentChannelTab = channel;
                    this.currentSession = null;
                    this.currentQqId = null;
                    // 加载该频道的会话列表
                    this.loadChannelSessions(channel.id);
                },

                // 加载频道的会话列表
                async loadChannelSessions(channelId) {
                    try {
                        // 过滤出属于该频道的会话
                        const res = await api.get('/api/sessions');
                        this.sessions = res.data.sessions || [];
                    } catch (e) {
                        console.error('Failed to load channel sessions:', e);
                    }
                },

                getMobileChatTitle() {
                    if (this.currentSession) return this.currentSession.name || '当前会话';
                    if (this.currentQqId) {
                        return `${this.chatTab === 'qq_private' ? 'QQ私聊' : 'QQ群聊'} ${this.currentQqId}`;
                    }
                    if (this.chatTab === 'cli') return 'CLI 会话';
                    if (this.chatTab === 'qq_private') return '选择 QQ 私聊';
                    if (this.chatTab === 'qq_group') return '选择 QQ 群聊';
                    return '选择 Web 会话';
                },

                getMobileChatMeta() {
                    if (this.currentSession) {
                        const type = this.currentSession.type === 'cli' ? 'CLI' : 'Web';
                        return `${type} · ${this.currentMessages.length} 条消息`;
                    }
                    if (this.currentQqId) return `${this.currentQqMessages.length} 条消息`;
                    return '点按切换会话';
                },

                getMobileChatIcon() {
                    if (this.currentSession?.type === 'cli' || this.chatTab === 'cli') return 'fas fa-terminal';
                    if (this.currentSession?.type?.startsWith('qq') || this.chatTab === 'qq_private') return 'fab fa-qq';
                    if (this.chatTab === 'qq_group') return 'fas fa-users';
                    return 'fas fa-comment';
                },

                async refreshCurrentChat() {
                    if (this.currentSession) {
                        // 手动刷新时强制滚动到底部
                        await this.loadMessages(true);
                    } else if (this.currentQqId) {
                        await this.selectQqChat(this.chatTab === 'qq_private' ? 'private' : 'group', this.currentQqId);
                    } else {
                        await this.loadPageData('chat');
                    }
                },

                async loadHomeData() {
                    this.appDataReady = false;
                    try {
                        await Promise.all([
                            this.loadSessions(),
                            this.loadSettings(),
                            this.loadPersonality(),
                            this.loadPersonalityPresets(),
                            this.loadCustomPersonalityPresets(),
                            this.loadCommandCatalog(),
                            this.loadChannels(),
                            this.loadAIModels()
                        ]);
                        this.showOnboarding = this.isChatOnlyMode && this.shouldShowOnboarding();
                        if (this.showOnboarding && this.personality) {
                            this.onboardingPersonality = {
                                name: this.personality.name || '',
                                systemPrompt: this.personality.systemPrompt || '',
                                firstMessage: this.personality.firstMessage || ''
                            };
                        }
                        if (this.currentPage === 'chat') {
                            await this.enterChatHome();
                        } else {
                            await this.loadPageData(this.currentPage);
                        }
                        // 应用聊天背景（只在有当前会话时）
                        this.$nextTick(() => {
                            if (this.currentSession) {
                                this.applyChatBackground();
                            }
                        });
                    } finally {
                        this.appDataReady = true;
                    }
                },

                async enterChatHome() {
                    this.currentPage = 'chat';
                    localStorage.setItem('nbot_home_page', 'chat');
                    // 清除当前会话和聊天背景
                    this.currentSession = null;
                    this.clearChatBackground();
                    await Promise.all([
                        this.loadSessions(),
                        this.loadCommandCatalog()
                    ]);
                },

                shouldShowOnboarding() {
                    const onboarding = this.settings?.onboarding || {};
                    return !onboarding.completed && !onboarding.skipped;
                },

                async updateOnboardingSettings(patch) {
                    const onboarding = {
                        ...(this.settings?.onboarding || {}),
                        ...patch
                    };
                    this.settings = {
                        ...this.settings,
                        onboarding
                    };
                    await api.put('/api/settings', { onboarding });
                },

                async skipOnboarding() {
                    this.isLoading = true;
                    try {
                        await this.updateOnboardingSettings({
                            completed: false,
                            skipped: true,
                            skipped_at: new Date().toISOString()
                        });
                        this.showOnboarding = false;
                        await this.enterChatHome();
                    } catch (e) {
                        this.showToast('Failed to skip onboarding: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async advanceOnboarding() {
                    if (this.onboardingStep === 2) {
                        const saved = await this.saveOnboardingAIModel();
                        if (!saved) return;
                    } else if (this.onboardingStep === 3) {
                        const saved = await this.saveOnboardingPersonality();
                        if (!saved) return;
                    }
                    if (this.onboardingStep < 4) {
                        this.onboardingStep += 1;
                    }
                },

                requireOnboardingFields(form, fields) {
                    const missing = fields.filter(field => !String(form[field.key] || '').trim());
                    if (!missing.length) {
                        return true;
                    }
                    this.showToast(this.$t('onboarding.validation_required', {
                        fields: missing.map(field => this.$t(field.labelKey)).join(this.currentLanguage === 'en' ? ', ' : '、')
                    }), 'error');
                    return false;
                },

                async saveOnboardingAIModel() {
                    const form = this.onboardingAI || {};
                    const hasInput = ['api_key', 'base_url', 'model'].some(key => String(form[key] || '').trim());
                    if (!hasInput && this.hasChatModelConfigured) {
                        return true;
                    }
                    if (!this.requireOnboardingFields(form, [
                        { key: 'provider', labelKey: 'onboarding.provider' },
                        { key: 'api_key', labelKey: 'onboarding.api_key' },
                        { key: 'base_url', labelKey: 'onboarding.base_url' },
                        { key: 'model', labelKey: 'onboarding.model' }
                    ])) {
                        return false;
                    }
                    this.isLoading = true;
                    try {
                        const res = await api.post('/api/ai-models', {
                            name: form.model ? `Chat - ${form.model}` : 'Chat Model',
                            purpose: 'chat',
                            provider: form.provider || 'openai',
                            provider_type: form.provider_type || 'openai_compatible',
                            api_key: form.api_key || '',
                            base_url: form.base_url || '',
                            model: form.model || '',
                            enabled: true,
                            supports_tools: true,
                            supports_reasoning: true,
                            supports_stream: true,
                            temperature: 0.7,
                            max_tokens: 2000,
                            max_context_length: 100000
                        });
                        const modelId = res.data?.model?.id || res.data?.id;
                        if (modelId) {
                            await api.post(`/api/ai-models/${modelId}/apply`, { purpose: 'chat' });
                        }
                        await this.loadAIModels();
                        this.showToast('Chat model saved', 'success');
                        return true;
                    } catch (e) {
                        this.showToast('Failed to save AI model: ' + (e.response?.data?.error || e.message), 'error');
                        return false;
                    } finally {
                        this.isLoading = false;
                    }
                },

                async saveOnboardingPersonality() {
                    const form = this.onboardingPersonality || {};
                    if (!this.requireOnboardingFields(form, [
                        { key: 'name', labelKey: 'onboarding.personality_name' },
                        { key: 'systemPrompt', labelKey: 'onboarding.system_prompt' },
                        { key: 'firstMessage', labelKey: 'onboarding.first_message' }
                    ])) {
                        return false;
                    }
                    this.isLoading = true;
                    try {
                        this.personality = {
                            ...this.personality,
                            name: form.name || this.personality.name || 'NekoBot',
                            systemPrompt: form.systemPrompt || this.personality.systemPrompt || '',
                            firstMessage: form.firstMessage || this.personality.firstMessage || '',
                            state: this.personality.state || { affection: 50, mood: 'happy' }
                        };
                        await api.put('/api/personality', {
                            ...this.personality,
                            _manualSystemPrompt: true
                        });
                        this.activePersonality = { ...this.personality };
                        this.personalityHasUnsavedChanges = false;
                        this.showToast('Personality saved', 'success');
                        return true;
                    } catch (e) {
                        this.showToast('Failed to save personality: ' + (e.response?.data?.error || e.message), 'error');
                        return false;
                    } finally {
                        this.isLoading = false;
                    }
                },

                async finishOnboarding() {
                    this.isLoading = true;
                    try {
                        const savedPersonality = await this.saveOnboardingPersonality();
                        if (!savedPersonality) return;
                        const res = await api.post('/api/sessions', {
                            name: 'First chat',
                            type: 'web',
                            user_id: this.username,
                            system_prompt: this.personality.systemPrompt || this.personality.prompt || '',
                            first_message: this.personality.firstMessage || '',
                            sender_name: this.personality.name || 'NekoBot',
                            sender_avatar: this.personality.avatar || '',
                            sender_portrait: this.personality.portrait || ''
                        });
                        const session = res.data.session;
                        this.sessions = [
                            session,
                            ...this.sessions.filter(item => item.id !== session.id)
                        ];
                        await this.updateOnboardingSettings({
                            completed: true,
                            skipped: false,
                            completed_at: new Date().toISOString()
                        });
                        this.showOnboarding = false;
                        this.chatTab = 'web';
                        await this.selectSession(session);
                        this.currentPage = 'chat';
                        localStorage.setItem('nbot_home_page', 'chat');
                        localStorage.setItem('nbot_home_page', 'chat');
                    } catch (e) {
                        this.showToast('Failed to finish onboarding: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async loadAllData() {
                    await Promise.all([
                        this.loadStats(),
                        this.loadSessions(),
                        this.loadTaskCenter(),
                        this.loadWorkflows(),
                        this.loadHeartbeat(),
                        this.loadPersonality(),
                        this.loadPersonalityPresets(),
                        this.loadCustomPersonalityPresets(),
                        this.loadMemory(),
                        this.loadKnowledge(),
                        this.loadAIConfig(),
                        this.loadAIModels(),
                        this.loadCommandCatalog(),
                        this.loadTokenStats(),
                        this.loadLogs(),
                        this.loadSettings(),
                        this.loadSkills(),
                        this.loadTools()
                    ]);
                },
                
                async loadPageData(page) {
                    switch(page) {
                        case 'dashboard':
                            await this.loadStats();
                            await this.loadRecentActivities();
                            // 延迟初始化图表，确保 DOM 已渲染
                            setTimeout(() => this.initCharts(), 100);
                            break;
                        case 'chat':
                        case 'sessions':
                            await Promise.all([
                                this.loadSessions(),
                                this.loadCommandCatalog()
                            ]);
                            break;
                        case 'workflows':
                            await this.loadWorkflows();
                            break;
                        case 'task-center':
                            await this.loadTaskCenter();
                            break;
                        case 'personality':
                            // 如果有未保存的修改，不重新加载服务器数据，保留本地修改
                            if (!this.personalityHasUnsavedChanges) {
                                await this.loadPersonality();
                            }
                            await this.loadPersonalityPresets();
                            break;
                        case 'memory':
                            await this.loadMemory();
                            break;
                        case 'heartbeat':
                            await this.loadHeartbeat();
                            break;
                        case 'knowledge':
                            await this.loadKnowledge();
                            break;
                        case 'ai-config':
                            await this.loadAIConfig();
                            await this.loadAIModels();
                            break;
                        case 'tokens':
                            await this.loadTokenStats();
                            break;
                        case 'logs':
                            await this.loadLogs();
                            break;
                        case 'settings':
                            await this.loadSettings();
                            break;
                        case 'skills':
                            await this.loadSkills();
                            break;
                        case 'tools':
                            await this.loadTools();
                            break;
                        case 'channels':
                            await this.loadChannels();
                            break;
                    }
                },
                
                async refreshData() {
                    this.isLoading = true;
                    try {
                        // 如果在聊天页面且有当前会话，重新加载会话消息
                        if (this.currentPage === 'chat' && this.currentSession) {
                            // 手动刷新时强制滚动到底部
                            await this.loadMessages(true);
                            this.showToast('会话已刷新', 'success');
                        } else if (this.currentPage === 'chat' && this.currentQqId) {
                            // 重新加载 QQ 会话消息
                            const type = this.chatTab === 'qq_private' ? 'private' : 'group';
                            const res = await api.get(`/api/qq/messages/${type}/${this.currentQqId}`);
                            this.currentQqMessages = res.data.messages || [];
                            this.showToast('会话已刷新', 'success');
                        } else {
                            await this.loadPageData(this.currentPage);
                            this.showToast('数据已刷新', 'success');
                        }
                    } catch (e) {
                        this.showToast('刷新失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                // API Calls
                async loadStats() {
                    try {
                        const res = await api.get('/api/stats');
                        this.stats = res.data;
                        // 加载图表数据
                        this.loadChartData();
                    } catch (e) {
                        console.error('Failed to load stats:', e);
                    }
                },
                
                // Chart Methods
                async initCharts() {
                    this.$nextTick(async () => {
                        if (this.$refs.trendChart) {
                            this.trendChart = echarts.init(this.$refs.trendChart);
                            await this.updateTrendChart();
                        }
                        if (this.$refs.platformChart) {
                            this.platformChart = echarts.init(this.$refs.platformChart);
                            await this.updatePlatformChart();
                        }
                    });
                },
                
                async updateTrendChart() {
                    if (!this.trendChart) return;
                    
                    const data = await this.fetchTrendData();
                    const option = {
                        backgroundColor: 'transparent',
                        tooltip: {
                            trigger: 'axis',
                            backgroundColor: 'rgba(22, 27, 34, 0.95)',
                            borderColor: '#30363d',
                            textStyle: { color: '#e6edf3' },
                            formatter: function(params) {
                                return `<div style="font-size:12px;color:#8b949e;margin-bottom:4px;">${params[0].axisValue}</div>
                                        <div style="font-size:13px;color:#e6edf3;">
                                            <span style="display:inline-block;width:8px;height:8px;background:${params[0].color};border-radius:50%;margin-right:6px;"></span>
                                            消息数: ${params[0].value}
                                        </div>`;
                            }
                        },
                        grid: {
                            left: '3%',
                            right: '4%',
                            bottom: '3%',
                            top: '15%',
                            containLabel: true
                        },
                        xAxis: {
                            type: 'category',
                            boundaryGap: false,
                            data: data.times,
                            axisLine: { lineStyle: { color: '#30363d' } },
                            axisLabel: { 
                                color: '#8b949e', 
                                fontSize: 11,
                                interval: this.selectedPeriod === 'month' ? 4 : 'auto'
                            },
                            axisTick: { show: false }
                        },
                        yAxis: {
                            type: 'value',
                            axisLine: { show: false },
                            axisLabel: { color: '#8b949e', fontSize: 11 },
                            splitLine: { lineStyle: { color: '#21262d' } },
                            minInterval: 1
                        },
                        series: [{
                            name: '消息数',
                            type: 'line',
                            smooth: true,
                            symbol: 'circle',
                            symbolSize: data.times.length > 24 ? 4 : 6,
                            sampling: 'average',
                            itemStyle: { color: '#ec4899' },
                            areaStyle: {
                                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                    { offset: 0, color: 'rgba(236, 72, 153, 0.4)' },
                                    { offset: 1, color: 'rgba(236, 72, 153, 0.05)' }
                                ])
                            },
                            data: data.values
                        }]
                    };
                    this.trendChart.setOption(option);
                },
                
                async updatePlatformChart() {
                    if (!this.platformChart) return;
                    
                    const data = await this.fetchPlatformData();
                    const option = {
                        backgroundColor: 'transparent',
                        tooltip: {
                            trigger: 'item',
                            backgroundColor: 'rgba(22, 27, 34, 0.95)',
                            borderColor: '#30363d',
                            textStyle: { color: '#e6edf3' },
                            formatter: '{b}: {c} ({d}%)'
                        },
                        legend: {
                            orient: 'vertical',
                            right: '2%',
                            top: 'middle',
                            textStyle: { color: '#8b949e', fontSize: 11 },
                            itemWidth: 10,
                            itemHeight: 10,
                            itemGap: 8
                        },
                        series: [{
                            name: '平台消息',
                            type: 'pie',
                            radius: ['40%', '65%'],
                            center: ['30%', '50%'],
                            avoidLabelOverlap: false,
                            itemStyle: {
                                borderRadius: 6,
                                borderColor: '#161b22',
                                borderWidth: 2
                            },
                            label: { show: false },
                            emphasis: {
                                label: {
                                    show: true,
                                    fontSize: 14,
                                    fontWeight: 'bold',
                                    color: '#e6edf3'
                                }
                            },
                            labelLine: { show: false },
                            data: data
                        }]
                    };
                    this.platformChart.setOption(option);
                },
                
                async fetchTrendData() {
                    try {
                        const res = await api.get(`/api/stats/messages?period=${this.selectedPeriod}`);
                        this.messageTrendData = res.data;
                        return {
                            times: res.data.labels || [],
                            values: res.data.values || []
                        };
                    } catch (e) {
                        console.error('Failed to load trend data:', e);
                        return { times: [], values: [] };
                    }
                },
                
                async fetchPlatformData() {
                    try {
                        const res = await api.get('/api/stats/platforms');
                        this.platformStatsData = res.data;
                        return res.data;
                    } catch (e) {
                        console.error('Failed to load platform data:', e);
                        return [];
                    }
                },
                
                loadChartData() {
                    this.$nextTick(() => {
                        this.initCharts();
                    });
                },
                
                async changeTimePeriod(period) {
                    this.selectedPeriod = period;
                    await this.updateTrendChart();
                },
                
                handleResize() {
                    if (this.trendChart) this.trendChart.resize();
                    if (this.platformChart) this.platformChart.resize();
                },

                // Theme Methods
                initTheme() {
                    // 从 localStorage 加载主题设置
                    const savedTheme = localStorage.getItem('themeSettings');
                    if (savedTheme) {
                        try {
                            const settings = JSON.parse(savedTheme);
                            this.themeSettings = { ...this.themeSettings, ...settings };
                        } catch (e) {
                            console.error('Failed to load theme settings:', e);
                        }
                    }
                    this.applyTheme();
                },

                applyTheme() {
                    const root = document.documentElement;
                    const body = document.body;

                    // 应用主题模式
                    if (this.themeSettings.mode === 'light') {
                        document.documentElement.setAttribute('data-theme', 'light');
                    } else {
                        document.documentElement.removeAttribute('data-theme');
                    }

                    // 应用主题颜色
                    const primaryColor = this.themeSettings.primaryColor;
                    root.style.setProperty('--accent-primary', primaryColor);

                    // 计算次要颜色（稍微亮一点的版本）
                    const secondaryColor = this.adjustBrightness(primaryColor, 20);
                    root.style.setProperty('--accent-secondary', secondaryColor);

                    // 计算悬停颜色（稍微暗一点的版本）
                    const hoverColor = this.adjustBrightness(primaryColor, -20);
                    root.style.setProperty('--accent-hover', hoverColor);

                    // 应用背景图片
                    if (this.themeSettings.bgImage) {
                        body.classList.add('has-bg-image');
                        body.style.setProperty('--bg-image', `url(${this.themeSettings.bgImage})`);
                        const opacity = this.themeSettings.bgOpacity / 100;
                        const overlayColor = this.themeSettings.mode === 'light'
                            ? `rgba(245, 247, 251, ${Math.min(0.82, opacity)})`
                            : `rgba(13, 17, 23, ${opacity})`;
                        body.style.setProperty('--bg-overlay', overlayColor);
                    } else {
                        body.classList.remove('has-bg-image');
                        body.style.removeProperty('--bg-image');
                        body.style.removeProperty('--bg-overlay');
                    }

                    // 应用卡片透明度
                    const cardOpacity = this.themeSettings.cardOpacity / 100;
                    const cardBg = this.themeSettings.mode === 'light'
                        ? `rgba(255, 255, 255, ${Math.max(0.82, cardOpacity)})`
                        : `rgba(22, 27, 34, ${cardOpacity})`;
                    root.style.setProperty('--bg-card', cardBg);

                    // 更新图表颜色
                    this.updateChartColors();

                    // 更新所有滑块进度条（如果主题面板打开）
                    this.$nextTick(() => {
                        const modalBody = document.querySelector('.modal-overlay .modal-body');
                        if (modalBody) {
                            const ranges = modalBody.querySelectorAll('input[type="range"].form-range');
                            ranges.forEach(range => {
                                const min = parseFloat(range.min) || 0;
                                const max = parseFloat(range.max) || 100;
                                const value = parseFloat(range.value) || 0;
                                const progress = ((value - min) / (max - min)) * 100;
                                range.style.setProperty('--progress', progress + '%');
                            });
                        }
                    });
                },

                adjustBrightness(color, percent) {
                    const num = parseInt(color.replace('#', ''), 16);
                    const amt = Math.round(2.55 * percent);
                    const R = (num >> 16) + amt;
                    const G = (num >> 8 & 0x00FF) + amt;
                    const B = (num & 0x0000FF) + amt;
                    return '#' + (0x1000000 + (R < 255 ? R < 1 ? 0 : R : 255) * 0x10000 +
                        (G < 255 ? G < 1 ? 0 : G : 255) * 0x100 +
                        (B < 255 ? B < 1 ? 0 : B : 255))
                        .toString(16).slice(1);
                },

                setThemeMode(mode) {
                    this.themeSettings.mode = mode;
                    this.applyTheme();
                },

                setPrimaryColor(color) {
                    this.themeSettings.primaryColor = color;
                    this.applyTheme();
                },

                setBgImage(url) {
                    this.themeSettings.bgImage = url;
                    this.applyTheme();
                },

                triggerBgUpload() {
                    this.$refs.bgImageInput.click();
                },

                handleBgUpload(event) {
                    const file = event.target.files[0];
                    if (!file) return;

                    const reader = new FileReader();
                    reader.onload = (e) => {
                        this.themeSettings.bgImage = e.target.result;
                        this.applyTheme();
                    };
                    reader.readAsDataURL(file);
                },

                updateBgOpacity(e) {
                    this.applyTheme();
                    // 更新滑块进度条
                    if (e && e.target) {
                        const range = e.target;
                        const min = parseFloat(range.min) || 0;
                        const max = parseFloat(range.max) || 100;
                        const value = parseFloat(range.value) || 0;
                        const progress = ((value - min) / (max - min)) * 100;
                        range.style.setProperty('--progress', progress + '%');
                    }
                },

                updateCardOpacity(e) {
                    this.applyTheme();
                    // 更新滑块进度条
                    if (e && e.target) {
                        const range = e.target;
                        const min = parseFloat(range.min) || 0;
                        const max = parseFloat(range.max) || 100;
                        const value = parseFloat(range.value) || 0;
                        const progress = ((value - min) / (max - min)) * 100;
                        range.style.setProperty('--progress', progress + '%');
                    }
                },

                updateChartColors() {
                    if (this.trendChart) {
                        this.trendChart.setOption({
                            series: [{
                                itemStyle: { color: this.themeSettings.primaryColor },
                                areaStyle: {
                                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                        { offset: 0, color: this.themeSettings.primaryColor + '66' },
                                        { offset: 1, color: this.themeSettings.primaryColor + '0D' }
                                    ])
                                }
                            }]
                        });
                    }
                },

                saveThemeSettings() {
                    localStorage.setItem('themeSettings', JSON.stringify(this.themeSettings));
                    this.showToast('主题设置已保存', 'success');
                    this.showThemePanel = false;
                },

                resetTheme() {
                    this.themeSettings = {
                        mode: 'dark',
                        primaryColor: '#ec4899',
                        bgImage: '',
                        bgOpacity: 20,
                        cardOpacity: 75,
                        closeModalOnOverlayClick: false
                    };
                    this.applyTheme();
                    localStorage.removeItem('themeSettings');
                    this.showToast('已恢复默认主题', 'success');
                },

                initThemeSliders() {
                    // 初始化主题设置面板的滑块进度条
                    this.$nextTick(() => {
                        // 使用更精确的选择器找到主题面板中的滑块
                        const modalBody = document.querySelector('.modal-overlay .modal-body');
                        if (modalBody) {
                            const ranges = modalBody.querySelectorAll('input[type="range"].form-range');
                            ranges.forEach(range => {
                                const min = parseFloat(range.min) || 0;
                                const max = parseFloat(range.max) || 100;
                                const value = parseFloat(range.value) || 0;
                                const progress = ((value - min) / (max - min)) * 100;
                                range.style.setProperty('--progress', progress + '%');
                            });
                        }
                    });
                },

                async loadSessions() {
                    // 如果正在删除会话，跳过本次刷新
                    if (this._isDeletingSession) {
                        return;
                    }
                    
                    try {
                        const res = await api.get('/api/sessions');
                        const serverSessions = res.data;
                        
                        // 保留本地临时会话
                        const localTempSessions = this.sessions.filter(s => s._isTemp);
                        
                        // 合并：临时会话 + 服务器会话
                        this.sessions = [...localTempSessions, ...serverSessions];
                    } catch (e) {
                        console.error('Failed to load sessions:', e);
                        this.showToast('加载会话失败', 'error');
                    }
                },

                async loadCommandCatalog() {
                    try {
                        const res = await api.get('/api/commands');
                        this.commandCatalog = Array.isArray(res.data?.commands) ? res.data.commands : [];
                    } catch (e) {
                        console.error('Failed to load commands:', e);
                        this.commandCatalog = [];
                    }
                },
                
                async loadWorkflows() {
                    try {
                        const res = await api.get('/api/workflows');
                        this.workflows = res.data;
                    } catch (e) {
                        console.error('Failed to load workflows:', e);
                    }
                },

                async loadTaskCenter() {
                    try {
                        const res = await api.get('/api/task-center');
                        this.taskCenterItems = Array.isArray(res.data?.items) ? res.data.items : [];
                    } catch (e) {
                        console.error('Failed to load task center:', e);
                        this.taskCenterItems = [];
                    }
                },

                async loadHeartbeat() {
                    try {
                        // 先确保会话和 QQ 目标已加载
                        await Promise.all([
                            this.sessions.length === 0 ? this.loadSessions() : Promise.resolve(),
                            this.loadQqPrivateUsers(),
                            this.loadQqGroups()
                        ]);

                        // 生成可用目标列表（必须在目标数据加载后）
                        this.generateAvailableTargets();
                        
                        // 加载配置
                        const res = await api.get('/api/heartbeat');
                        this.heartbeatConfig = { ...this.heartbeatConfig, ...res.data };
                        const derivedWebTarget = this.heartbeatConfig.target_session_id
                            ? `web:${this.heartbeatConfig.target_session_id}`
                            : null;
                        let normalizedTargets = Array.isArray(this.heartbeatConfig.targets)
                            ? [...this.heartbeatConfig.targets]
                            : [];
                        if (derivedWebTarget && !normalizedTargets.includes(derivedWebTarget)) {
                            normalizedTargets.unshift(derivedWebTarget);
                        }
                        this.heartbeatConfig.targets = [...new Set(normalizedTargets)];
                        
                        // 加载内容
                        const contentRes = await api.get('/api/heartbeat/content', {
                            params: { file: this.heartbeatConfig.content_file }
                        });
                        this.heartbeatContent = contentRes.data.content || '';
                    } catch (e) {
                        console.error('Failed to load heartbeat:', e);
                    }
                },

                generateAvailableTargets() {
                    const groups = {
                        qq_groups: {
                            title: 'QQ 群组',
                            icon: 'fas fa-users',
                            targets: []
                        },
                        qq_private: {
                            title: 'QQ 私聊用户',
                            icon: 'fas fa-user',
                            targets: []
                        },
                        web_sessions: {
                            title: 'Web / CLI 会话',
                            icon: 'fas fa-comments',
                            targets: []
                        }
                    };

                    console.log('Generating targets from sessions:', this.sessions);
                    console.log('QQ Private Users:', this.qqPrivateUsers);
                    console.log('QQ Groups:', this.qqGroups);

                    // 添加 QQ 群组
                    this.qqGroups.forEach(group => {
                        groups.qq_groups.targets.push({
                            id: `qq_group:${group.group_id}`,
                            name: group.group_name || `群 ${group.group_id}`,
                            icon: 'fas fa-users'
                        });
                    });

                    // 添加 QQ 私聊用户
                    this.qqPrivateUsers.forEach(user => {
                        groups.qq_private.targets.push({
                            id: `qq_private:${user.user_id}`,
                            name: user.nickname || `用户 ${user.user_id}`,
                            icon: 'fas fa-user'
                        });
                    });

                    // 添加 Web / CLI 会话
                    this.sessions.filter(s => ['web', 'cli'].includes(s.type)).forEach(s => {
                        groups.web_sessions.targets.push({
                            id: `web:${s.id}`,
                            name: s.name || `会话 ${s.id.substring(0, 8)}`,
                            icon: s.type === 'cli' ? 'fas fa-terminal' : 'fas fa-comments'
                        });
                    });

                    // 只保留有目标的分组
                    this.availableTargets = Object.values(groups).filter(g => g.targets.length > 0);
                    console.log('Available target groups:', this.availableTargets);
                },

                async saveHeartbeatConfig() {
                    try {
                        const webTargets = (this.heartbeatConfig.targets || []).filter(target => String(target).startsWith('web:'));
                        const payload = {
                            ...this.heartbeatConfig,
                            target_session_id: webTargets.length ? webTargets[0].split(':', 2)[1] : ''
                        };
                        await api.put('/api/heartbeat', payload);
                        this.heartbeatConfig.target_session_id = payload.target_session_id;
                        this.showToast('配置已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async toggleHeartbeatTarget(targetId) {
                    const currentTargets = Array.isArray(this.heartbeatConfig.targets)
                        ? [...this.heartbeatConfig.targets]
                        : [];
                    const alreadySelected = currentTargets.includes(targetId);
                    let nextTargets = currentTargets.filter(target => target !== targetId);

                    if (!alreadySelected) {
                        if (String(targetId).startsWith('web:')) {
                            nextTargets = nextTargets.filter(target => !String(target).startsWith('web:'));
                        }
                        nextTargets.push(targetId);
                    }

                    this.heartbeatConfig.targets = nextTargets;
                    await this.saveHeartbeatConfig();
                },

                async saveHeartbeatContent() {
                    try {
                        await api.put('/api/heartbeat/content', {
                            content: this.heartbeatContent,
                            file: this.heartbeatConfig.content_file
                        });
                        this.showToast('内容已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async runHeartbeatNow() {
                    this.isLoading = true;
                    try {
                        await api.post('/api/heartbeat/run');
                        this.showToast('Heartbeat 已触发执行', 'success');
                        // 刷新状态
                        setTimeout(() => this.loadHeartbeat(), 2000);
                    } catch (e) {
                        this.showToast('执行失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async loadPersonality() {
                    try {
                        const res = await api.get('/api/personality');
                        this.personality = res.data;
                        this.activePersonality = { ...res.data };
                        this.personalityTagsInput = (this.personality.tags || []).join(' ');
                        // 重新应用聊天背景（personality.portrait 可能已更新）
                        this.applyChatBackground();
                    } catch (e) {
                        console.error('Failed to load personality:', e);
                    }
                },
                
                async loadPersonalityPresets() {
                    try {
                        const res = await api.get('/api/personality/presets');
                        this.personalityPresets = res.data;
                    } catch (e) {
                        console.error('Failed to load personality presets:', e);
                    }
                },

                // Skills 配置方法
                async loadSkills() {
                    try {
                        const res = await api.get('/api/skills');
                        this.skills = res.data || [];
                    } catch (e) {
                        console.error('Failed to load skills:', e);
                    }
                },

                // 处理 Skill 文件夹上传
                async handleSkillFolderUpload(event) {
                    const files = event.target.files;
                    if (!files || files.length === 0) return;

                    // 获取文件夹名称（从第一个文件的路径推断）
                    const firstFile = files[0];
                    const pathParts = firstFile.webkitRelativePath.split('/');
                    const folderName = pathParts[0];

                    // 检查是否包含 SKILL.md 文件
                    const skillMdFile = Array.from(files).find(f =>
                        f.webkitRelativePath.endsWith('SKILL.md') ||
                        f.webkitRelativePath.endsWith('skill.md')
                    );

                    if (!skillMdFile) {
                        this.showToast('未找到 SKILL.md 文件，请确保上传的是有效的 Skill 文件夹', 'error');
                        event.target.value = '';
                        return;
                    }

                    this.isLoading = true;
                    this.showToast(`正在上传 Skill 文件夹: ${folderName}...`, 'info');

                    try {
                        // 读取 SKILL.md 内容
                        const skillMdContent = await skillMdFile.text();

                        // 解析 SKILL.md 获取配置信息
                        const skillConfig = this.parseSkillMd(skillMdContent);

                        // 构建 FormData
                        const formData = new FormData();
                        formData.append('folder_name', folderName);
                        formData.append('skill_md', skillMdContent);
                        formData.append('skill_config', JSON.stringify(skillConfig));

                        // 添加所有文件
                        for (const file of files) {
                            // 保留相对路径
                            const relativePath = file.webkitRelativePath.substring(folderName.length + 1);
                            formData.append('files', file, relativePath);
                        }

                        // 发送到后端
                        const res = await api.post('/api/skills/upload-folder', formData, {
                            headers: {
                                'Content-Type': 'multipart/form-data'
                            }
                        });

                        if (res.data.success) {
                            this.showToast(`Skill "${skillConfig.name || folderName}" 上传成功！`, 'success');
                            await this.loadSkills();
                            await this.loadSkillsStorage();
                        } else {
                            this.showToast(res.data.error || '上传失败', 'error');
                        }
                    } catch (e) {
                        console.error('上传 Skill 文件夹失败:', e);
                        this.showToast('上传失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                        event.target.value = '';
                    }
                },

                // 解析 SKILL.md 文件内容
                parseSkillMd(content) {
                    const config = {
                        name: '',
                        description: '',
                        aliases: [],
                        parameters: {}
                    };

                    try {
                        // 提取名称（第一个 # 标题）
                        const titleMatch = content.match(/^#\s+(.+)$/m);
                        if (titleMatch) {
                            config.name = titleMatch[1].trim();
                        }

                        // 提取描述（第一个段落）
                        const descMatch = content.match(/^#\s+.+\n\n(.+?)(?:\n\n|\n##|$)/s);
                        if (descMatch) {
                            config.description = descMatch[1].trim().substring(0, 200);
                        }

                        // 提取别名（如果有 Aliases 部分）
                        const aliasesMatch = content.match(/(?:##?\s*(?:别名|Aliases)[\s\S]*?)(?:[-*]\s*(.+)(?:\n|$))+/i);
                        if (aliasesMatch) {
                            const aliasesText = content.substring(aliasesMatch.index);
                            const aliasItems = aliasesText.matchAll(/[-*]\s*(.+)/g);
                            for (const match of aliasItems) {
                                const alias = match[1].trim();
                                if (alias && !alias.startsWith('#')) {
                                    config.aliases.push(alias);
                                }
                            }
                        }

                        // 提取参数配置（如果有 Parameters 部分）
                        const paramsMatch = content.match(/(?:##?\s*(?:参数|Parameters)[\s\S]*?)(?:\n##|\n\n#|$)/i);
                        if (paramsMatch) {
                            const paramsText = content.substring(paramsMatch.index, paramsMatch.index + paramsMatch[0].length);
                            const paramMatches = paramsText.matchAll(/[-*]\s*`?(\w+)`?\s*[:\-]\s*(.+)/g);
                            for (const match of paramMatches) {
                                const key = match[1].trim();
                                const value = match[2].trim();
                                if (key && value) {
                                    config.parameters[key] = value;
                                }
                            }
                        }
                    } catch (e) {
                        console.error('解析 SKILL.md 失败:', e);
                    }

                    return config;
                },

                openSkillModal(skill = null) {
                    if (skill) {
                        this.editingSkill = skill;
                        this.skillForm = {
                            id: skill.id,
                            name: skill.name,
                            description: skill.description,
                            aliases: skill.aliases || [],
                            aliasesText: skill.aliases ? skill.aliases.join(', ') : '',
                            enabled: skill.enabled,
                            parameters: skill.parameters || {},
                            skillMd: skill.skill_md || ''
                        };
                    } else {
                        this.editingSkill = null;
                        this.skillForm = {
                            id: null,
                            name: '',
                            description: '',
                            aliases: [],
                            aliasesText: '',
                            enabled: true,
                            parameters: {},
                            skillMd: ''
                        };
                    }
                    this.showSkillModal = true;
                },

                async saveSkill() {
                    this.isLoading = true;
                    try {
                        // 处理别名
                        const aliases = this.skillForm.aliasesText
                            ? this.skillForm.aliasesText.split(',').map(s => s.trim()).filter(s => s)
                            : [];

                        // 构建基本数据（实现配置在 SKILL.md 中管理）
                        const data = {
                            name: this.skillForm.name,
                            description: this.skillForm.description,
                            aliases: aliases,
                            enabled: this.skillForm.enabled,
                            parameters: this.skillForm.parameters || {},
                            skill_md: this.skillForm.skillMd || ''
                        };

                        if (this.editingSkill) {
                            await api.put(`/api/skills/${this.editingSkill.id}`, data);
                            this.showSkillModal = false;
                            await this.loadSkills();
                            this.showToast('Skill 已更新', 'success');
                        } else {
                            const res = await api.post('/api/skills', data);
                            this.showSkillModal = false;
                            await this.loadSkills();
                            await this.loadSkillsStorage();
                            if (res.data?.storage_created) {
                                this.showToast(`Skill 已创建，存储空间已自动创建`, 'success');
                            } else {
                                this.showToast('Skill 已创建', 'success');
                            }
                        }
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async deleteSkill(id) {
                    this.showConfirm({
                        title: '删除 Skill',
                        message: '确定要删除这个 Skill 吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/skills/${id}`);
                                await this.loadSkills();
                                await this.loadSkillsStorage();
                                this.showToast('Skill 已删除（包括存储空间）', 'success');
                            } catch (e) {
                                console.error('删除 Skill 失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async toggleSkill(skill) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/skills/${skill.id}/toggle`);
                        await this.loadSkills();
                        this.showToast(`Skill 已${!skill.enabled ? '启用' : '禁用'}`, 'success');
                    } catch (e) {
                        this.showToast('操作失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // Skills 存储方法
                    async loadSkillsStorage() {
                        this.showSkillsStoragePanel = true;
                        this.isLoading = true;
                        try {
                            const res = await api.get('/api/skills/storage');
                            this.skillsStorage = res.data || [];
                        } catch (e) {
                            console.error('Failed to load skills storage:', e);
                            this.showToast('加载存储空间失败', 'error');
                        } finally {
                            this.isLoading = false;
                        }
                    },

                    async viewSkillStorage(skill) {
                        this.currentSkillStorageName = skill.name;
                        await this.viewSkillStorageDetail(skill.name);
                    },

                    async viewSkillStorageDetail(skillName) {
                        this.isLoading = true;
                        try {
                            const res = await api.get(`/api/skills/storage/${encodeURIComponent(skillName)}`);
                            this.currentSkillStorageName = skillName;
                            this.currentSkillStorageFiles = res.data?.files || [];
                            this.showSkillStorageModal = true;
                        } catch (e) {
                            console.error('Failed to load skill storage detail:', e);
                            this.showToast('加载存储详情失败: ' + (e.response?.data?.error || e.message), 'error');
                        } finally {
                            this.isLoading = false;
                        }
                    },

                    async viewSkillScript(skillName, fileName) {
                        this.isLoading = true;
                        try {
                            // 使用新的文件 API 来读取任意文件
                            const res = await api.get(`/api/skills/storage/${encodeURIComponent(skillName)}/file/${encodeURIComponent(fileName)}`);
                            this.editingSkillScriptName = fileName;
                            this.skillScriptContent = res.data?.content || '';
                            this.showSkillScriptModal = true;
                        } catch (e) {
                            console.error('Failed to load script:', e);
                            this.showToast('加载脚本失败: ' + (e.response?.data?.error || e.message), 'error');
                        } finally {
                            this.isLoading = false;
                        }
                    },

                    async editSkillScript(skillName, fileName) {
                        this.isLoading = true;
                        try {
                            const res = await api.get(`/api/skills/storage/${encodeURIComponent(skillName)}/file/${encodeURIComponent(fileName)}`);
                            this.editingSkillScriptName = fileName;
                            this.skillScriptContent = res.data?.content || '';
                            this.currentSkillStorageName = skillName;
                            this.showSkillScriptModal = true;
                        } catch (e) {
                            console.error('Failed to load script for edit:', e);
                            this.showToast('加载脚本失败: ' + (e.response?.data?.error || e.message), 'error');
                        } finally {
                            this.isLoading = false;
                        }
                    },

                    async saveSkillScript() {
                        if (!this.skillScriptContent.trim()) {
                            this.showToast('脚本内容不能为空', 'error');
                            return;
                        }
                        if (!this.currentSkillStorageName) {
                            this.showToast('未选择 Skill', 'error');
                            return;
                        }
                        this.isLoading = true;
                        try {
                            const fileName = this.editingSkillScriptName;
                            console.log('Saving file:', {
                                skillName: this.currentSkillStorageName,
                                fileName: fileName,
                                contentLength: this.skillScriptContent.length
                            });
                            // 使用新的文件 API 来保存任意文件
                            const response = await api.post(`/api/skills/storage/${encodeURIComponent(this.currentSkillStorageName)}/file/${encodeURIComponent(fileName)}`, {
                                content: this.skillScriptContent
                            });
                            console.log('File saved successfully:', response);
                            this.showToast('文件已保存', 'success');
                            this.showSkillScriptModal = false;
                            await this.viewSkillStorageDetail(this.currentSkillStorageName);
                        } catch (e) {
                            console.error('Failed to save script:', e);
                            const errorMsg = e.response?.data?.error || e.message || '未知错误';
                            this.showToast('保存失败: ' + errorMsg, 'error');
                        } finally {
                            this.isLoading = false;
                        }
                    },

                    closeSkillScriptModal() {
                        this.showSkillScriptModal = false;
                        this.editingSkillScriptName = '';
                        this.skillScriptContent = '';
                    },

                    openNewScriptModal() {
                        this.newScriptName = '';
                        this.newScriptExtension = 'py';
                        this.showNewScriptModal = true;
                    },

                    closeNewScriptModal() {
                        this.showNewScriptModal = false;
                        this.newScriptName = '';
                        this.newScriptExtension = 'py';
                    },

                    // 通用输入模态框方法
                    showInput(config) {
                        this.inputModalConfig = {
                            title: config.title || '输入',
                            message: config.message || '',
                            placeholder: config.placeholder || '',
                            defaultValue: config.defaultValue || '',
                            required: config.required || false,
                            onConfirm: config.onConfirm || null
                        };
                        this.inputModalValue = config.defaultValue || '';
                        this.showInputModal = true;
                    },

                    closeInputModal() {
                        this.showInputModal = false;
                        this.inputModalValue = '';
                        this.inputModalConfig = {
                            title: '',
                            message: '',
                            placeholder: '',
                            defaultValue: '',
                            required: false,
                            onConfirm: null
                        };
                    },

                    confirmInputModal() {
                        if (this.inputModalConfig.required && !this.inputModalValue.trim()) {
                            return;
                        }
                        const value = this.inputModalValue;
                        const onConfirm = this.inputModalConfig.onConfirm;
                        this.closeInputModal();
                        if (onConfirm && typeof onConfirm === 'function') {
                            onConfirm(value);
                        }
                    },

                    confirmNewScript() {
                        if (!this.newScriptName.trim() || !this.newScriptExtension.trim()) {
                            return;
                        }
                        const scriptName = this.newScriptName.trim();
                        const extension = this.newScriptExtension.trim().replace(/^\./, '');
                        // 脚本文件放到 scripts/ 目录下，其他文件放到根目录
                        const isScriptFile = ['py', 'js', 'ts', 'sh', 'bash'].includes(extension.toLowerCase());
                        this.editingSkillScriptName = isScriptFile ? `scripts/${scriptName}.${extension}` : `${scriptName}.${extension}`;

                        if (isScriptFile) {
                            this.skillScriptContent = `# 新建脚本: ${scriptName}.${extension}

def main(params):
    """主函数"""
    # params: 从调用方传递的参数
    return {"success": True, "message": "Hello World"}
`;
                        } else if (['json', 'yaml', 'yml'].includes(extension.toLowerCase())) {
                            this.skillScriptContent = JSON.stringify({
                                "name": scriptName,
                                "version": "1.0.0"
                            }, null, 2);
                        } else {
                            this.skillScriptContent = `# ${scriptName}.${extension}
# 自定义脚本文件
`;
                        }

                        this.showNewScriptModal = false;
                        this.showSkillScriptModal = true;
                    },

                    async deleteSkillStorage(skillName) {
                        this.showConfirm({
                            title: '删除存储空间',
                            message: `确定要删除 Skill "${skillName}" 的存储空间吗？此操作不可恢复！`,
                            confirmText: '删除',
                            icon: 'fa-trash',
                            iconColor: 'var(--danger)',
                            danger: true,
                            action: async () => {
                                this.isLoading = true;
                                try {
                                    await api.delete(`/api/skills/storage/${encodeURIComponent(skillName)}`);
                                    this.showToast('存储空间已删除', 'success');
                                    await this.loadSkillsStorage();
                                } catch (e) {
                                    console.error('Failed to delete skill storage:', e);
                                    this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                                } finally {
                                    this.isLoading = false;
                                }
                            }
                        });
                    },

                    createSkillScript() {
                        this.openNewScriptModal();
                    },

                    // Tools 配置方法
                async loadTools() {
                    try {
                        const res = await api.get('/api/tools');
                        this.tools = res.data || [];
                    } catch (e) {
                        console.error('Failed to load tools:', e);
                    }
                },

                openToolModal(tool = null) {
                    if (tool) {
                        this.editingTool = tool;
                        const impl = tool.implementation || {};
                        this.toolForm = {
                            id: tool.id,
                            name: tool.name,
                            description: tool.description,
                            enabled: tool.enabled,
                            parameters: tool.parameters || {},
                            implementationType: impl.type || '',
                            implementation: { ...impl },
                            implementationHeadersText: impl.headers ? JSON.stringify(impl.headers, null, 2) : '',
                            implementationBodyText: impl.body ? JSON.stringify(impl.body, null, 2) : ''
                        };
                    } else {
                        this.editingTool = null;
                        this.toolForm = {
                            id: null,
                            name: '',
                            description: '',
                            enabled: true,
                            parameters: {},
                            implementationType: '',
                            implementation: {},
                            implementationHeadersText: '',
                            implementationBodyText: ''
                        };
                    }
                    this.showToolModal = true;
                },

                async saveTool() {
                    this.isLoading = true;
                    try {
                        // 构建 implementation
                        let implementation = null;
                        if (this.toolForm.implementationType) {
                            implementation = {
                                type: this.toolForm.implementationType
                            };

                            if (this.toolForm.implementationType === 'http') {
                                implementation.method = this.toolForm.implementation.method || 'GET';
                                implementation.url = this.toolForm.implementation.url || '';
                                implementation.response_path = this.toolForm.implementation.response_path || '';

                                // 解析 Headers
                                if (this.toolForm.implementationHeadersText) {
                                    try {
                                        implementation.headers = JSON.parse(this.toolForm.implementationHeadersText);
                                    } catch (e) {
                                        implementation.headers = {};
                                    }
                                }

                                // 解析 Body
                                if (this.toolForm.implementationBodyText) {
                                    try {
                                        implementation.body = JSON.parse(this.toolForm.implementationBodyText);
                                    } catch (e) {
                                        implementation.body = {};
                                    }
                                }
                            } else if (this.toolForm.implementationType === 'static') {
                                implementation.response = this.toolForm.implementation.response || '';
                            } else if (this.toolForm.implementationType === 'python') {
                                implementation.code = this.toolForm.implementation.code || '';
                            } else if (this.toolForm.implementationType === 'minimax_web_search') {
                                implementation.api_key = this.toolForm.implementation.api_key || '{{minimax_api_key}}';
                                implementation.model = this.toolForm.implementation.model || 'MiniMax-Text-01';
                            }
                        }

                        const data = {
                            name: this.toolForm.name,
                            description: this.toolForm.description,
                            enabled: this.toolForm.enabled,
                            parameters: this.toolForm.parameters,
                            implementation: implementation
                        };

                        if (this.editingTool) {
                            await api.put(`/api/tools/${this.editingTool.id}`, data);
                        } else {
                            await api.post('/api/tools', data);
                        }

                        this.showToolModal = false;
                        await this.loadTools();
                        this.showToast('Tool 已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async deleteTool(id) {
                    this.showConfirm({
                        title: '删除 Tool',
                        message: '确定要删除这个 Tool 吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/tools/${id}`);
                                await this.loadTools();
                                this.showToast('Tool 已删除', 'success');
                            } catch (e) {
                                console.error('删除 Tool 失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async toggleTool(tool) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/tools/${tool.id}/toggle`);
                        await this.loadTools();
                        this.showToast(`Tool 已${!tool.enabled ? '启用' : '禁用'}`, 'success');
                    } catch (e) {
                        this.showToast('操作失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async loadChannels() {
                    try {
                        const res = await api.get('/api/channels');
                        this.channels = res.data.channels || [];
                        const presetsRes = await api.get('/api/channels/presets');
                        this.channelPresets = presetsRes.data.presets || [];
                    } catch (e) {
                        console.error('加载频道失败:', e);
                        this.showToast('加载频道失败', 'error');
                    }
                },

                openChannelModal(channel = null) {
                    this.editingChannel = channel;
                    this.selectedChannelPreset = '';
                    if (channel) {
                        this.channelForm = {
                            id: channel.id,
                            name: channel.name || '',
                            type: channel.type || 'custom',
                            transport: channel.transport || '',
                            description: channel.description || '',
                            enabled: channel.enabled !== false,
                            configText: JSON.stringify(channel.config || {}, null, 2),
                            capabilitiesText: JSON.stringify(channel.capabilities || {}, null, 2)
                        };
                    } else {
                        this.channelForm = {
                            id: '',
                            name: '',
                            type: 'custom',
                            transport: '',
                            description: '',
                            enabled: true,
                            configText: '{}',
                            capabilitiesText: '{}'
                        };
                    }
                    this.showChannelModal = true;
                },

                applyChannelPresetById(presetId) {
                    if (!presetId) return;
                    const fallbackTelegramPreset = {
                        id: 'telegram',
                        name: 'Telegram',
                        type: 'telegram',
                        transport: 'webhook',
                        description: 'Telegram Bot Webhook 频道',
                        config: {
                            bot_token_env: 'TELEGRAM_BOT_TOKEN',
                            secret_token_env: 'TELEGRAM_WEBHOOK_SECRET',
                            webhook_url: ''
                        },
                        capabilities: {
                            supports_stream: false,
                            supports_progress_updates: false,
                            supports_file_send: false,
                            supports_stop: false
                        }
                    };
                    const preset = this.channelPresets.find(item => item.id === presetId)
                        || (presetId === 'telegram' ? fallbackTelegramPreset : null);
                    if (!preset) return;

                    this.editingChannel = null;
                    this.selectedChannelPreset = preset.id;
                    this.channelForm = {
                        id: preset.id,
                        name: preset.name || preset.id,
                        type: preset.type || 'custom',
                        transport: preset.transport || '',
                        description: preset.description || '',
                        enabled: preset.enabled !== false,
                        configText: JSON.stringify(preset.config || {}, null, 2),
                        capabilitiesText: JSON.stringify(preset.capabilities || {}, null, 2)
                    };
                    this.showChannelModal = true;
                },

                buildChannelPayload() {
                    let config = {};
                    let capabilities = {};
                    try {
                        config = this.channelForm.configText ? JSON.parse(this.channelForm.configText) : {};
                    } catch (e) {
                        throw new Error('配置 JSON 格式不正确');
                    }
                    try {
                        capabilities = this.channelForm.capabilitiesText ? JSON.parse(this.channelForm.capabilitiesText) : {};
                    } catch (e) {
                        throw new Error('能力 JSON 格式不正确');
                    }
                    return {
                        id: this.channelForm.id,
                        name: this.channelForm.name,
                        type: this.channelForm.type,
                        transport: this.channelForm.transport,
                        description: this.channelForm.description,
                        enabled: this.channelForm.enabled,
                        config,
                        capabilities
                    };
                },

                async saveChannel() {
                    this.isLoading = true;
                    try {
                        const payload = this.buildChannelPayload();
                        if (this.editingChannel) {
                            await api.put(`/api/channels/${this.editingChannel.id}`, payload);
                        } else {
                            await api.post('/api/channels', payload);
                        }
                        this.showChannelModal = false;
                        await this.loadChannels();
                        this.showToast('频道已保存', 'success');
                    } catch (e) {
                        this.showToast(e.response?.data?.error || e.message || '保存频道失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async toggleChannel(channel) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/channels/${channel.id}/toggle`);
                        await this.loadChannels();
                        this.showToast('频道已更新', 'success');
                    } catch (e) {
                        this.showToast(e.response?.data?.error || '切换频道状态失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async setTelegramWebhook(channel) {
                    const webhookUrl = channel.config?.webhook_url;
                    if (!webhookUrl) {
                        this.showToast('请先在频道配置 JSON 中填写 webhook_url', 'error');
                        return;
                    }
                    this.isLoading = true;
                    try {
                        await api.post(`/api/channels/telegram/${channel.id}/set-webhook`, {
                            webhook_url: webhookUrl
                        });
                        this.showToast('Telegram Webhook 已设置', 'success');
                    } catch (e) {
                        this.showToast(e.response?.data?.error || '设置 Telegram Webhook 失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async deleteChannel(channel) {
                    this.showConfirm({
                        title: '删除频道',
                        message: `确定删除频道「${channel.name}」吗？`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/channels/${channel.id}`);
                                await this.loadChannels();
                                this.showToast('频道已删除', 'success');
                            } catch (e) {
                                this.showToast(e.response?.data?.error || '删除频道失败', 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async loadMemory() {
                    try {
                        const res = await api.get('/api/memory');
                        this.memories = res.data.memories || [];
                        this.longTermMemories = res.data.long_term || [];
                        this.shortTermMemories = res.data.short_term || [];
                    } catch (e) {
                        console.error('Failed to load memory:', e);
                    }
                },
                
                async loadKnowledge() {
                    try {
                        const res = await api.get('/api/knowledge');
                        this.knowledgeDocs = res.data;
                    } catch (e) {
                        console.error('Failed to load knowledge:', e);
                    }
                },
                
                async loadAIConfig() {
                    try {
                        const res = await api.get('/api/ai-config');
                        this.aiConfig = { ...this.aiConfig, ...res.data };
                        this.aiConfig.provider_type = this.aiConfig.provider_type || this.getProviderTypeByProvider(this.aiConfig.provider);
                        const availableModelValues = (this.availableModels || []).map(model => model.value);
                        this.aiConfig.custom_model = availableModelValues.includes(this.aiConfig.model)
                            ? ''
                            : (this.aiConfig.model || '');
                        if (typeof this.aiConfig.supports_tools !== 'boolean' ||
                            typeof this.aiConfig.supports_reasoning !== 'boolean' ||
                            typeof this.aiConfig.supports_stream !== 'boolean') {
                            this.applyProviderCapabilities(this.aiConfig);
                        } else {
                            this.syncProviderMetadata(this.aiConfig);
                        }
                        this.updateContextStats();
                    } catch (e) {
                        console.error('Failed to load AI config:', e);
                    }
                },

                async loadAIModels() {
                    this.aiModelsLoaded = false;
                    try {
                        const res = await api.get('/api/ai-models');
                        this.aiModels = res.data.models || [];
                        this.activeModelId = res.data.active_model_id;
                        // 同时加载各用途的活跃模型
                        await this.loadActiveModelsByPurpose();
                    } catch (e) {
                        console.error('Failed to load AI models:', e);
                    } finally {
                        this.aiModelsLoaded = true;
                    }
                },

                async loadTokenStats() {
                    try {
                        const params = new URLSearchParams();
                        params.append('dateRange', this.tokenFilter.dateRange);
                        const res = await api.get(`/api/tokens?${params.toString()}`);
                        this.tokenStats = { ...this.tokenStats, ...res.data };
                        this.tokenHistory = res.data.history || [];
                        await this.loadTokenRankings();
                        this.updateTokenTrendChart();
                    } catch (e) {
                        console.error('Failed to load token stats:', e);
                    }
                },

                async loadTokenRankings() {
                    try {
                        const res = await api.get('/api/tokens/rankings');
                        const rankings = res.data;

                        // 会话排行已包含会话名称
                        const sessions = rankings.sessions || [];
                        const maxSession = sessions[0]?.value || 1;
                        this.tokenRankings.sessions = sessions.map(s => ({
                            ...s,
                            percentage: (s.value / maxSession) * 100
                        }));

                        // 处理模型排行
                        const models = rankings.models || [];
                        const maxModel = models[0]?.value || 1;
                        this.tokenRankings.models = models.map(m => ({
                            ...m,
                            percentage: (m.value / maxModel) * 100
                        }));

                        // 处理用户排行
                        const users = rankings.users || [];
                        const maxUser = users[0]?.value || 1;
                        this.tokenRankings.users = users.map(u => ({
                            ...u,
                            percentage: (u.value / maxUser) * 100
                        }));
                    } catch (e) {
                        console.error('Failed to load token rankings:', e);
                    }
                },

                setTokenDateRange(range) {
                    this.tokenFilter.dateRange = range;
                    this.loadTokenStats();
                },

                refreshTokenStats() {
                    this.loadTokenStats();
                },

                updateTokenTrendChart() {
                    if (!this.$refs.tokenTrendChart) return;

                    if (!this.tokenTrendChart) {
                        this.tokenTrendChart = echarts.init(this.$refs.tokenTrendChart);
                    }

                    // 使用真实的历史数据
                    const history = this.tokenHistory.slice(-7); // 最近7天
                    const dates = history.map(h => {
                        const date = new Date(h.date);
                        return `${date.getMonth() + 1}/${date.getDate()}`;
                    });
                    const values = history.map(h => {
                        if (this.tokenFilter.unit === 'cost') {
                            return parseFloat(h.cost) || 0;
                        }
                        return (h.input || 0) + (h.output || 0);
                    });

                    const option = {
                        backgroundColor: 'transparent',
                        tooltip: {
                            trigger: 'axis',
                            backgroundColor: 'rgba(22, 27, 34, 0.95)',
                            borderColor: '#30363d',
                            textStyle: { color: '#e6edf3' },
                            formatter: (params) => {
                                const p = params[0];
                                const unit = this.tokenFilter.unit === 'cost' ? '$' : '';
                                return `${p.axisValue}<br/>${p.marker} ${p.value} ${unit}`;
                            }
                        },
                        grid: {
                            left: '3%',
                            right: '4%',
                            bottom: '3%',
                            top: '10%',
                            containLabel: true
                        },
                        xAxis: {
                            type: 'category',
                            data: dates,
                            axisLine: { lineStyle: { color: '#30363d' } },
                            axisLabel: { color: '#8b949e', fontSize: 11 }
                        },
                        yAxis: {
                            type: 'value',
                            axisLine: { show: false },
                            axisLabel: {
                                color: '#8b949e',
                                fontSize: 11,
                                formatter: (value) => {
                                    if (this.tokenFilter.unit === 'cost') {
                                        return '$' + value.toFixed(2);
                                    }
                                    return value >= 1000 ? (value / 1000).toFixed(1) + 'k' : value;
                                }
                            },
                            splitLine: { lineStyle: { color: '#21262d' } }
                        },
                        series: [{
                            type: 'line',
                            smooth: true,
                            data: values,
                            itemStyle: { color: this.themeSettings.primaryColor },
                            areaStyle: {
                                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                    { offset: 0, color: this.themeSettings.primaryColor + '40' },
                                    { offset: 1, color: this.themeSettings.primaryColor + '05' }
                                ])
                            }
                        }]
                    };

                    this.tokenTrendChart.setOption(option, true);
                },

                exportTokenData() {
                    const data = this.tokenHistory.map(h => ({
                        日期: h.date,
                        消息数: h.message_count || 0,
                        输入Token: h.input,
                        输出Token: h.output,
                        总计: h.input + h.output,
                        费用: h.cost
                    }));

                    const csv = this.convertToCSV(data);
                    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(blob);
                    link.download = `token_usage_${new Date().toISOString().split('T')[0]}.csv`;
                    link.click();
                },

                convertToCSV(data) {
                    if (data.length === 0) return '';
                    const headers = Object.keys(data[0]);
                    const rows = data.map(row => headers.map(h => row[h]).join(','));
                    return [headers.join(','), ...rows].join('\n');
                },
                
                async loadLogs() {
                    try {
                        const res = await api.get('/api/logs');
                        this.logs = res.data;
                    } catch (e) {
                        console.error('Failed to load logs:', e);
                    }
                },

                async loadRecentActivities() {
                    try {
                        const res = await api.get('/api/logs');
                        const importantLogs = (res.data || []).filter(log => log.important === true);
                        this.recentActivities = importantLogs.slice(-10).reverse();
                    } catch (e) {
                        console.error('Failed to load recent activities:', e);
                    }
                },
                
                async loadSettings() {
                    try {
                        const res = await api.get('/api/settings');
                        const currentFeatures = this.settings.features || {};
                        const loadedSettings = res.data || {};
                        this.settings = {
                            ...this.settings,
                            ...loadedSettings,
                            features: {
                                ...currentFeatures,
                                ...(loadedSettings.features || {})
                            }
                        };
                        // 同步 Live2D 显隐状态
                        if (this.settings.features && window.__nbotLive2dSetEnabled) {
                            window.__nbotLive2dSetEnabled(this.settings.features.live2d);
                        }
                        this.updateContextStats();
                    } catch (e) {
                        console.error('Failed to load settings:', e);
                    }
                },
                
                // Chat Functions
                async selectSession(session) {
                    const previousSessionId = this.currentSession?.id;

                    // 切换会话时触发消息区淡出
                    this.sessionSwitching = true;

                    // 切换到新会话，清除所有状态
                    this.currentSession = session;
                    this.updateWebVisibility();
                    if (window.__nbotLive2dSay) {
                        window.__nbotLive2dSay(`\u5df2\u5207\u6362\u5230\u300c${session.name || '\u5f53\u524d\u4f1a\u8bdd'}\u300d\u3002`, 3200, 3);
                    }
                    this.currentQqId = null;
                    this.currentQqMessages = [];
                    this.currentMessages = [];
                    
                    // 清除所有加载/生成状态
                    this.isTyping = false;
                    // 如果之前的会话正在加载，保持加载状态以便切换回来时恢复
                    // isLoading 会在下面根据 loadingSessionId 恢复
                    
                    // 必须清空进度卡片和孤儿卡片，防止跨会话污染
                    this.thinkingCards = [];
                    this.orphanCards = {};
                    
                    // 中断之前的消息加载（如果有）
                    if (this.messageRefreshTimer) {
                        clearInterval(this.messageRefreshTimer);
                        this.messageRefreshTimer = null;
                    }
                    
                    // 延迟一下再加载新会话的消息，确保状态已清理
                    await new Promise(resolve => setTimeout(resolve, 50));
                    
                    socket.emit('leave_session');
                    socket.emit('join_session', { session_id: session.id });
                    // 切换会话时强制滚动到底部
                    await this.loadMessages(true);
                    this.updateContextStats();

                    // 消息加载完成，淡入
                    this.sessionSwitching = false;

                    // 设置新的消息刷新定时器（每 2 秒）
                    this.messageRefreshTimer = setInterval(() => {
                        if (this.currentSession && this.currentPage === 'chat') {
                            // 定时刷新不强制滚动，避免打扰用户查看历史消息
                            this.loadMessages(false);
                        }
                    }, 2000);
                    
                    // 恢复当前会话的加载状态（如果这个会话正在生成）
                    this.isLoading = (this.loadingSessionId === session.id);
                    // 恢复打字状态，确保龙骨加载动画在切换回正在生成的会话时正确显示
                    this.isTyping = (this.loadingSessionId === session.id);
                    // 重新应用聊天背景（切换会话后 sender_portrait 可能不同）
                    this.applyChatBackground();
                },
                
                async switchChatTab(tab) {
                    this.chatTab = tab;
                    this.currentSession = null;
                    this.currentQqId = null;
                    this.currentQqMessages = [];
                    
                    if (tab === 'qq_private') {
                        await this.loadQqPrivateUsers();
                    } else if (tab === 'qq_group') {
                        await this.loadQqGroups();
                    }
                    
                    // 清除之前的消息刷新定时器
                    if (this.messageRefreshTimer) {
                        clearInterval(this.messageRefreshTimer);
                    }
                },
                
                async loadQqPrivateUsers() {
                    try {
                        const res = await api.get('/api/qq/users');
                        this.qqPrivateUsers = res.data.users || [];
                    } catch (e) {
                        console.error('Failed to load QQ users:', e);
                    }
                },
                
                async loadQqGroups() {
                    try {
                        const res = await api.get('/api/qq/groups');
                        this.qqGroups = res.data.groups || [];
                    } catch (e) {
                        console.error('Failed to load QQ groups:', e);
                    }
                },
                
                async selectQqChat(type, id) {
                    this.isMobileChatPickerOpen = false;
                    // 清空Web会话状态，避免同时显示
                    this.currentSession = null;
                    this.currentMessages = [];
                    this.currentQqId = id;
                    this.currentQqMessages = [];
                    
                    try {
                        const res = await api.get(`/api/qq/messages/${type}/${id}`);
                        this.currentQqMessages = res.data.messages || [];
                    } catch (e) {
                        console.error('Failed to load QQ messages:', e);
                    }
                    
                    // 设置刷新定时器
                    if (this.messageRefreshTimer) {
                        clearInterval(this.messageRefreshTimer);
                    }
                    this.messageRefreshTimer = setInterval(async () => {
                        if (this.currentQqId && this.currentPage === 'chat') {
                            try {
                                const res = await api.get(`/api/qq/messages/${type}/${id}`);
                                this.currentQqMessages = res.data.messages || [];
                            } catch (e) {}
                        }
                    }, 2000);
                },
                
                async loadMessages(forceScroll = false) {
                    if (!this.currentSession) return;

                    // 如果是临时会话，跳过加载消息
                    if (this.currentSession._isTemp || this.currentSession.id.startsWith('temp_')) {
                        return;
                    }

                    try {
                        const res = await api.get(`/api/sessions/${this.currentSession.id}/messages`);
                        const newMessages = res.data;

                        // 保留现有的 thinking_cards 数据（避免被刷新覆盖）
                        newMessages.forEach(newMsg => {
                            const existingMsg = this.currentMessages.find(m => m.id === newMsg.id);
                            if (existingMsg && existingMsg.thinking_cards) {
                                newMsg.thinking_cards = existingMsg.thinking_cards;
                            }
                            if (existingMsg && existingMsg.change_cards) {
                                newMsg.change_cards = existingMsg.change_cards;
                            }
                        });

                        const normalizedMessages = newMessages.map(msg => {
                            if (!msg.thinking_cards || !msg.thinking_cards.length) return msg;
                            return {
                                ...msg,
                                thinking_cards: msg.thinking_cards.map(card => ({
                                    ...card,
                                    content: this.normalizeDisplayText(card.content || ''),
                                    steps: (card.steps || []).map(step => ({
                                        ...step,
                                        name: this.normalizeDisplayText(step.name || ''),
                                        detail: this.normalizeDisplayText(step.detail || '')
                                    }))
                                }))
                            };
                        });
                        const streamingMessages = this.currentMessages.filter(msg =>
                            msg.is_streaming ||
                            this.streamTypeQueues[msg.id] ||
                            this.streamEndPending[msg.id]
                        );
                        streamingMessages.forEach(streamMsg => {
                            if (!normalizedMessages.some(msg => msg.id === streamMsg.id)) {
                                normalizedMessages.push(streamMsg);
                            }
                        });
                        this.currentMessages = normalizedMessages;
                        this.updateContextStats();
                        // 只有在强制滚动或用户没有手动滚动时才滚动到底部
                        this.$nextTick(() => this.scrollToBottom(forceScroll));
                    } catch (e) {
                        console.error('Failed to load messages:', e);
                    }
                },
                
                async createNewSession() {
                    if (this.isLoading) return;
                    this.isLoading = true;
                    try {
                        const defaultName = '新会话';
                        const res = await api.post('/api/sessions', {
                            name: defaultName,
                            type: 'web',
                            user_id: this.username,
                            system_prompt: this.personality.systemPrompt || this.personality.prompt,
                            first_message: this.personality.firstMessage || '',
                            sender_name: this.personality.name || 'NekoBot',
                            sender_avatar: this.personality.avatar || '',
                            sender_portrait: this.personality.portrait || ''
                        });
                        const newSession = { ...res.data.session, _isNew: true };
                        this.sessions = [
                            ...this.sessions.filter(session => session.id !== newSession.id),
                            newSession
                        ];
                        this.chatTab = 'web';
                        await this.selectSession(newSession);
                        setTimeout(() => {
                            const session = this.sessions.find(s => s.id === newSession.id);
                            if (session) {
                                session._isNew = false;
                            }
                        }, 1500);
                        this.showToast('已创建新对话', 'success');
                    } catch (e) {
                        console.error('Failed to create session:', e);
                        this.showToast('创建新对话失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }

                },

                downloadJson(data, filename) {
                    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = filename;
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                    URL.revokeObjectURL(url);
                },

                async exportCurrentSession() {
                    if (!this.currentSession?.id) {
                        this.showToast('请先选择一个会话', 'warning');
                        return;
                    }
                    try {
                        const res = await api.get(`/api/sessions/${this.currentSession.id}/export`);
                        const safeName = (this.currentSession.name || this.currentSession.id).replace(/[\\/:*?"<>|]/g, '_');
                        this.downloadJson(res.data, `session_${safeName}_${new Date().toISOString().slice(0, 10)}.json`);
                        this.showToast('会话已导出', 'success');
                    } catch (e) {
                        this.showToast('导出会话失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async exportSession(session) {
                    if (!session?.id) {
                        this.showToast('请选择要导出的会话', 'warning');
                        return;
                    }
                    try {
                        const res = await api.get(`/api/sessions/${session.id}/export`);
                        const safeName = (session.name || session.id).replace(/[\\/:*?"<>|]/g, '_');
                        this.downloadJson(res.data, `session_${safeName}_${new Date().toISOString().slice(0, 10)}.json`);
                        this.showToast('会话已导出', 'success');
                    } catch (e) {
                        this.showToast('导出会话失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async exportSelectedOrVisibleSessions() {
                    const source = this.selectedSessions.length > 0
                        ? this.sessions.filter(s => this.selectedSessions.includes(s.id))
                        : this.managedSessions;
                    const ids = source.map(s => s.id).filter(Boolean);
                    if (!ids.length) {
                        this.showToast('没有可导出的会话', 'warning');
                        return;
                    }
                    try {
                        const res = await api.get(`/api/sessions/export?ids=${encodeURIComponent(ids.join(','))}`);
                        this.downloadJson(res.data, `sessions_export_${new Date().toISOString().slice(0, 10)}.json`);
                        this.showToast(`已导出 ${res.data.total || ids.length} 个会话`, 'success');
                    } catch (e) {
                        this.showToast('导出会话失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                triggerSessionImport() {
                    const ref = this.$refs.sessionImportInput;
                    const input = Array.isArray(ref) ? ref[0] : ref;
                    if (input) {
                        input.value = '';
                        input.click();
                    }
                },

                async handleSessionImportFile(event) {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    try {
                        const text = await file.text();
                        const parsed = JSON.parse(text);
                        const res = await api.post('/api/sessions/import', parsed);
                        await this.loadSessions();
                        const imported = res.data.imported || 0;
                        const failed = res.data.failed || 0;
                        if (imported > 0) {
                            this.showToast(`成功导入 ${imported} 个会话${failed ? `，失败 ${failed} 个` : ''}`, failed ? 'warning' : 'success');
                        } else {
                            this.showToast('没有导入任何会话', 'warning');
                        }
                    } catch (e) {
                        this.showToast('导入会话失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        if (event.target) event.target.value = '';
                    }
                },

                getSessionPendingQueue(sessionId) {
                    if (!sessionId) return [];
                    if (!this.pendingMessageQueues[sessionId]) {
                        this.pendingMessageQueues = {
                            ...this.pendingMessageQueues,
                            [sessionId]: []
                        };
                    }
                    return this.pendingMessageQueues[sessionId];
                },

                getCurrentSessionPendingCount() {
                    const sessionId = this.currentSession?.id;
                    if (!sessionId) return 0;
                    return (this.pendingMessageQueues[sessionId] || []).length;
                },

                getCurrentSessionPendingQueueItems() {
                    const sessionId = this.currentSession?.id;
                    if (!sessionId) return [];
                    return (this.pendingMessageQueues[sessionId] || []).map((item, index) => ({
                        id: item.id,
                        session_id: sessionId,
                        index: index + 1,
                        created_at: item.createdAt,
                        file_count: (item.files || []).length,
                        content_preview: item.content
                            ? item.content
                            : ((item.files || []).length > 0 ? '仅发送附件' : '空消息')
                    }));
                },

                enqueuePendingMessage(sessionId, payload) {
                    const queue = [...this.getSessionPendingQueue(sessionId), payload];
                    this.pendingMessageQueues = {
                        ...this.pendingMessageQueues,
                        [sessionId]: queue
                    };
                    // 立即尝试处理队列
                    this.$nextTick(() => this.processPendingQueue(sessionId));
                    return queue.length;
                },

                dequeuePendingMessage(sessionId) {
                    const queue = [...(this.pendingMessageQueues[sessionId] || [])];
                    if (!queue.length) return null;
                    const nextPayload = queue.shift();
                    this.pendingMessageQueues = {
                        ...this.pendingMessageQueues,
                        [sessionId]: queue
                    };
                    return nextPayload;
                },

                async processPendingQueue(sessionId) {
                    if (!sessionId || this.isProcessingQueuedMessage) return;

                    const queue = this.pendingMessageQueues[sessionId] || [];
                    if (!queue.length) return;

                    // 智能检测：如果最后一条消息是AI回复且isLoading仍为true，自动重置状态
                    if (this.isLoading && this.currentSession?.id === sessionId) {
                        const lastMessage = this.currentMessages[this.currentMessages.length - 1];
                        const isTimeout = this.loadingStartTime && (Date.now() - this.loadingStartTime > 120000); // 2分钟超时

                        if (lastMessage && lastMessage.role === 'assistant') {
                            console.log('[Queue] 检测到AI已回复但isLoading仍为true，自动重置状态');
                            this.isLoading = false;
                            this.loadingSessionId = null;
                            this.loadingStartTime = null;
                            localStorage.removeItem('nbot_loading_session_id');
                            localStorage.removeItem('nbot_loading_start_time');
                        } else if (isTimeout) {
                            console.log('[Queue] 生成超时，自动重置状态');
                            this.isLoading = false;
                            this.loadingSessionId = null;
                            this.loadingStartTime = null;
                            localStorage.removeItem('nbot_loading_session_id');
                            localStorage.removeItem('nbot_loading_start_time');
                            this.showToast('生成响应超时，请重试', 'warning');
                        } else {
                            // 如果正在生成中，等待一段时间后重试
                            setTimeout(() => this.processPendingQueue(sessionId), 500);
                            return;
                        }
                    }

                    const nextPayload = this.dequeuePendingMessage(sessionId);
                    if (!nextPayload) return;

                    this.isProcessingQueuedMessage = true;
                    try {
                        await this.sendPreparedMessage(nextPayload);
                    } finally {
                        this.isProcessingQueuedMessage = false;
                        // 发送完成后，继续检查队列
                        this.$nextTick(() => this.processPendingQueue(sessionId));
                    }
                },

                buildPendingMessagePayload(content, files, sessionId) {
                    const clonedFiles = (files || []).map(file => ({ ...file }));
                    return {
                        id: 'queued_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
                        sessionId,
                        content,
                        files: clonedFiles,
                        createdAt: new Date().toISOString()
                    };
                },

                removePendingMessage(sessionId, queuedId) {
                    const queue = [...(this.pendingMessageQueues[sessionId] || [])];
                    const nextQueue = queue.filter(item => item.id !== queuedId);
                    this.pendingMessageQueues = {
                        ...this.pendingMessageQueues,
                        [sessionId]: nextQueue
                    };
                    this.showToast('已移出待发送队列', 'success');
                },

                async sendPreparedMessage(payload) {
                    const sessionId = payload.sessionId || this.currentSession?.id;
                    if (!sessionId) return;

                    const content = (payload.content || '').trim();
                    const files = payload.files || [];

                    if (!content && files.length === 0) {
                        this.showToast('请输入消息或选择文件', 'warning');
                        return;
                    }

                    const tempId = 'local_' + Date.now();
                    const isCurrentSession = this.currentSession?.id === sessionId;

                    this.isLoading = true;
                    this.loadingSessionId = sessionId;
                    this.loadingStartTime = Date.now();
                    // 持久化加载状态到localStorage，以便页面刷新后恢复
                    localStorage.setItem('nbot_loading_session_id', sessionId);
                    localStorage.setItem('nbot_loading_start_time', Date.now().toString());
                    let uploadedFilesInfo = [];

                    if (files.length > 0) {
                        try {
                            for (const file of files) {
                                let uploadFile;

                                if (file._file) {
                                    uploadFile = file._file;
                                } else if (file.data) {
                                    const base64Data = file.data.split(',')[1];
                                    const byteCharacters = atob(base64Data);
                                    const byteNumbers = new Array(byteCharacters.length);
                                    for (let i = 0; i < byteCharacters.length; i++) {
                                        byteNumbers[i] = byteCharacters.charCodeAt(i);
                                    }
                                    const byteArray = new Uint8Array(byteNumbers);
                                    uploadFile = new Blob([byteArray], { type: file.type });
                                } else {
                                    this.showToast('文件数据丢失，请重新选择', 'error');
                                    continue;
                                }

                                const formData = new FormData();
                                formData.append('file', uploadFile, file.name);
                                formData.append('session_id', sessionId);

                                const res = await api.post('/api/upload', formData, {
                                    headers: { 'Content-Type': 'multipart/form-data' }
                                });

                                if (!res.data.success) {
                                    throw new Error(res.data.error || '上传失败');
                                }

                                uploadedFilesInfo.push({
                                    name: res.data.filename,
                                    type: file.type,
                                    size: res.data.size,
                                    path: res.data.path,
                                    url: res.data.url || res.data.path,
                                    content: res.data.content,
                                    preview: file.preview || res.data.path
                                });
                            }
                        } catch (e) {
                            console.error('文件上传失败:', e);
                            this.showToast('文件上传失败: ' + e.message, 'error');
                            this.isLoading = false;
                            this.loadingSessionId = null;
                            localStorage.removeItem('nbot_loading_session_id');
                            localStorage.removeItem('nbot_loading_start_time');
                            return;
                        }
                    }

                    const userMessage = {
                        id: tempId,
                        role: 'user',
                        content: content,
                        timestamp: new Date().toISOString(),
                        source: 'web',
                        session_id: sessionId,
                        attachments: [...uploadedFilesInfo.map(f => ({
                            name: f.name,
                            type: f.type,
                            size: f.size,
                            path: f.path,
                            url: f.url || f.path,
                            preview: f.preview,
                            content: f.content
                        }))]
                    };

                    if (isCurrentSession) {
                        this.currentMessages.push(userMessage);
                        // 用户发送消息时强制滚动到底部
                        this.$nextTick(() => this.scrollToBottom(true));
                    }

                    this.isTyping = true;
                    this.isLoading = true;

                    try {
                        socket.emit('send_message', {
                            session_id: sessionId,
                            content: userMessage.content,
                            sender: this.username,
                            attachments: uploadedFilesInfo,
                            tempId: tempId
                        });
                    } catch (e) {
                        this.isTyping = false;
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');
                        this.showToast('发送失败', 'error');
                        console.error('发送消息失败:', e);
                    }
                },
                
                async sendMessage() {
                    if (!this.currentSession) return;

                    const content = this.inputMessage.trim();
                    const files = this.uploadedFiles;

                    if (!content && files.length === 0) {
                        this.showToast('请输入消息或选择文件', 'warning');
                        return;
                    }

                    const payload = this.buildPendingMessagePayload(
                        content,
                        files,
                        this.currentSession.id
                    );

                    this.inputMessage = '';
                    this.uploadedFiles = [];

                    if (this.isLoading && this.loadingSessionId === this.currentSession.id) {
                        const queueLength = this.enqueuePendingMessage(this.currentSession.id, payload);
                        if (window.__nbotLive2dSay) {
                            window.__nbotLive2dSay('\u5df2\u52a0\u5165\u5f53\u524d\u4f1a\u8bdd\u7684\u53d1\u9001\u961f\u5217\u3002', 3200, 4);
                        }
                        this.showToast(`已加入待发送队列（第 ${queueLength} 条）`, 'info');
                        return;
                    }

                    if (window.__nbotLive2dSay) {
                        const hasFiles = files.length > 0;
                        window.__nbotLive2dSay(hasFiles ? '\u6587\u4ef6\u548c\u6d88\u606f\u5df2\u9001\u51fa\uff0c\u6211\u7b49 AI \u56de\u590d\u3002' : '\u6d88\u606f\u5df2\u9001\u51fa\uff0c\u6211\u7b49 AI \u56de\u590d\u3002', 3600, 4);
                    }
                    await this.sendPreparedMessage(payload);
                },
                
                handleFileSelect(event) {
                    const files = event.target.files;
                    if (!files || files.length === 0) return;
                    
                    for (const file of files) {
                        if (file.size > 10 * 1024 * 1024) {
                            this.showToast('文件过大，最大支持10MB', 'error');
                            continue;
                        }
                        
                        // 对于图片，创建本地预览 URL
                        if (file.type.startsWith('image/')) {
                            const localUrl = URL.createObjectURL(file);
                            this.uploadedFiles.push({
                                name: file.name,
                                type: file.type,
                                size: file.size,
                                data: null,
                                preview: localUrl,  // 使用本地 URL 预览
                                _file: file  // 保存原始 File 对象用于上传
                            });
                        } else {
                            // 非图片文件：不生成预览
                            this.uploadedFiles.push({
                                name: file.name,
                                type: file.type,
                                size: file.size,
                                data: null,
                                preview: null,
                                _file: file  // 保存原始 File 对象用于上传
                            });
                        }
                    }
                    
                    // 清空文件输入框，以便重复选择同一文件
                    event.target.value = '';
                },
                
                removeFile(index) {
                    const file = this.uploadedFiles[index];
                    // 释放本地 URL（如果是 blob URL）
                    if (file && file.preview && file.preview.startsWith('blob:')) {
                        URL.revokeObjectURL(file.preview);
                    }
                    this.uploadedFiles.splice(index, 1);
                },

                /**
                 * 处理文件上传到工作区（不触发AI）
                 */
                async handleWorkspaceFileUpload(event) {
                    const files = event.target.files;
                    if (!files || files.length === 0) return;

                    // 检查是否有当前会话
                    if (!this.currentSession) {
                        this.showToast('请先选择一个会话', 'warning');
                        event.target.value = '';
                        return;
                    }

                    const sessionId = this.currentSession.id;
                    let successCount = 0;
                    let failCount = 0;

                    for (const file of files) {
                        // 检查文件大小（限制50MB）
                        if (file.size > 50 * 1024 * 1024) {
                            this.showToast(`文件 ${file.name} 过大，最大支持50MB`, 'error');
                            failCount++;
                            continue;
                        }

                        try {
                            const formData = new FormData();
                            formData.append('file', file);

                            // 显示上传中提示
                            this.showToast(`正在上传 ${file.name}...`, 'info');

                            // 调用工作区上传API
                            const res = await api.post(`/api/sessions/${sessionId}/workspace/upload`, formData, {
                                headers: { 'Content-Type': 'multipart/form-data' }
                            });

                            if (res.data.success) {
                                successCount++;
                                this.showToast(`文件 ${file.name} 已上传到工作区`, 'success');
                            } else {
                                failCount++;
                                this.showToast(`上传 ${file.name} 失败: ${res.data.error || '未知错误'}`, 'error');
                            }
                        } catch (e) {
                            failCount++;
                            console.error('上传文件到工作区失败:', e);
                            this.showToast(`上传 ${file.name} 失败: ${e.message}`, 'error');
                        }
                    }

                    // 清空文件输入框，以便重复选择同一文件
                    event.target.value = '';

                    // 显示汇总信息
                    if (successCount > 0 && failCount === 0) {
                        this.showToast(`成功上传 ${successCount} 个文件到工作区`, 'success');
                    } else if (successCount > 0 && failCount > 0) {
                        this.showToast(`上传完成：${successCount} 个成功，${failCount} 个失败`, 'warning');
                    }
                },

                handleInputMessageChange() {
                    if (this.commandQuery !== '/') {
                        this.selectedCommandCategory = null;
                    }
                    if (!this.showCommandCategorySuggestions && !this.showCommandSuggestions) {
                        this.activeCommandSuggestionIndex = 0;
                        return;
                    }
                    const visibleCount = this.showCommandCategorySuggestions
                        ? this.commandCategoryCatalog.length
                        : this.filteredCommandCatalog.length;
                    if (this.activeCommandSuggestionIndex >= visibleCount) {
                        this.activeCommandSuggestionIndex = 0;
                    }
                },

                handleChatInputKeydown(e) {
                    if (this.showCommandCategorySuggestions || this.showCommandSuggestions) {
                        const visibleCount = this.showCommandCategorySuggestions
                            ? this.commandCategoryCatalog.length
                            : this.filteredCommandCatalog.length;
                        if (e.key === 'ArrowDown') {
                            e.preventDefault();
                            this.activeCommandSuggestionIndex =
                                (this.activeCommandSuggestionIndex + 1) % visibleCount;
                            return;
                        }
                        if (e.key === 'ArrowUp') {
                            e.preventDefault();
                            this.activeCommandSuggestionIndex =
                                (this.activeCommandSuggestionIndex - 1 + visibleCount) % visibleCount;
                            return;
                        }
                        if (e.key === 'Tab') {
                            e.preventDefault();
                            if (this.showCommandCategorySuggestions) {
                                const category = this.commandCategoryCatalog[this.activeCommandSuggestionIndex];
                                if (category) {
                                    this.selectCommandCategory(category.name);
                                }
                            } else {
                                const command = this.filteredCommandCatalog[this.activeCommandSuggestionIndex];
                                if (command) {
                                    this.applyCommandSuggestion(command);
                                }
                            }
                            return;
                        }
                        if (e.key === 'Backspace' && this.selectedCommandCategory && this.commandQuery === '/' && !this.inputMessage.slice(1)) {
                            this.clearCommandCategory();
                            return;
                        }
                        if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            if (this.showCommandCategorySuggestions) {
                                const category = this.commandCategoryCatalog[this.activeCommandSuggestionIndex];
                                if (category) {
                                    this.selectCommandCategory(category.name);
                                }
                            } else {
                                const command = this.filteredCommandCatalog[this.activeCommandSuggestionIndex];
                                if (command) {
                                    this.applyCommandSuggestion(command);
                                } else {
                                    this.sendMessage();
                                }
                            }
                            return;
                        }
                    }

                    if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        this.sendMessage();
                    }
                },

                applyCommandSuggestion(command) {
                    if (!command) return;
                    this.inputMessage = `${command.name} `;
                    this.selectedCommandCategory = null;
                    this.activeCommandSuggestionIndex = 0;
                    this.$nextTick(() => {
                        if (this.$refs.chatInput) {
                            this.$refs.chatInput.focus();
                        }
                    });
                },

                selectCommandCategory(categoryName) {
                    this.selectedCommandCategory = categoryName;
                    this.activeCommandSuggestionIndex = 0;
                    this.$nextTick(() => {
                        if (this.$refs.chatInput) {
                            this.$refs.chatInput.focus();
                        }
                    });
                },

                clearCommandCategory() {
                    this.selectedCommandCategory = null;
                    this.activeCommandSuggestionIndex = 0;
                    this.$nextTick(() => {
                        if (this.$refs.chatInput) {
                            this.$refs.chatInput.focus();
                        }
                    });
                },
                
                async stopGeneration() {
                    if (!this.isLoading) return;
                    const stoppedSessionId = this.currentSession?.id || this.loadingSessionId;

                    // 立即更新 UI 状态，给用户即时反馈
                    this.isLoading = false;
                    this.loadingSessionId = null;
                    this.loadingStartTime = null;
                    localStorage.removeItem('nbot_loading_session_id');
                    localStorage.removeItem('nbot_loading_start_time');
                    this.showToast('正在停止生成...', 'info');

                    try {
                        await api.post('/api/stop', {
                            session_id: this.currentSession?.id
                        });
                        this.showToast('已停止生成', 'success');
                        this.processPendingQueue(stoppedSessionId);
                    } catch (e) {
                        console.error('停止生成失败:', e);
                        this.showToast('停止失败: ' + (e.response?.data?.error || e.message), 'error');
                        // 如果停止失败，恢复 loading 状态
                        this.isLoading = true;
                        this.loadingSessionId = this.currentSession?.id;
                        this.loadingStartTime = Date.now();
                        localStorage.setItem('nbot_loading_session_id', this.currentSession?.id || '');
                        localStorage.setItem('nbot_loading_start_time', Date.now().toString());
                    }
                },

                // ========== 语音功能 ==========
                async toggleRecording() {
                    if (this.isTranscribing) {
                        return;
                    }
                    if (this.isRecording) {
                        await this.stopRecording();
                    } else {
                        await this.startRecording();
                    }
                },

                async startRecording() {
                    try {
                        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                        this.mediaRecorder = new MediaRecorder(stream);
                        this.audioChunks = [];

                        this.mediaRecorder.ondataavailable = (event) => {
                            if (event.data.size > 0) {
                                this.audioChunks.push(event.data);
                            }
                        };

                        this.mediaRecorder.onstop = async () => {
                            const audioBlob = new Blob(this.audioChunks, { type: 'audio/webm' });
                            await this.transcribeAudio(audioBlob);
                            // 停止所有音轨
                            stream.getTracks().forEach(track => track.stop());
                        };

                        this.mediaRecorder.start();
                        this.isRecording = true;
                        this.showToast('开始录音，请说话...', 'info');
                    } catch (err) {
                        console.error('录音失败:', err);
                        this.showToast('无法访问麦克风: ' + err.message, 'error');
                    }
                },

                async stopRecording() {
                    if (this.mediaRecorder && this.isRecording) {
                        this.mediaRecorder.stop();
                        this.isRecording = false;
                        this.showToast('录音结束，正在识别...', 'info');
                    }
                },

                async transcribeAudio(audioBlob) {
                    this.isTranscribing = true;
                    try {
                        const formData = new FormData();
                        formData.append('audio', audioBlob, 'recording.webm');

                        const res = await api.post('/api/stt/transcribe', formData, {
                            headers: { 'Content-Type': 'multipart/form-data' }
                        });

                        if (res.data.success && res.data.text) {
                            this.inputMessage += (this.inputMessage ? ' ' : '') + res.data.text;
                            this.showToast('语音识别成功', 'success');
                            // 自动调整输入框高度
                            this.$nextTick(() => {
                                this.autoResizeTextarea();
                            });
                        } else {
                            this.showToast('语音识别失败: ' + (res.data.error || '未知错误'), 'error');
                        }
                    } catch (e) {
                        console.error('语音识别失败:', e);
                        this.showToast('语音识别失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isTranscribing = false;
                    }
                },

                async speakText(text) {
                    if (!this.ttsEnabled) return;
                    
                    try {
                        const res = await api.post('/api/tts/synthesize', {
                            text: text,
                            voice: 'zh-CN-XiaoxiaoNeural',
                            speed: 1.0
                        });

                        if (res.data.success && res.data.audio_url) {
                            const audio = new Audio(res.data.audio_url);
                            audio.play();
                        }
                    } catch (e) {
                        console.error('语音合成失败:', e);
                    }
                },

                toggleTTS() {
                    this.ttsEnabled = !this.ttsEnabled;
                    localStorage.setItem('ttsEnabled', this.ttsEnabled);
                    this.showToast(this.ttsEnabled ? '语音播报已开启' : '语音播报已关闭', 'info');
                },

                toggleThinkingCards() {
                    this.showThinkingCard = !this.showThinkingCard;
                    localStorage.setItem('showThinkingCard', this.showThinkingCard);
                    this.showToast(this.showThinkingCard ? '进度卡片已显示' : '进度卡片已隐藏', 'info');
                },

                toggleLive2d() {
                    this.settings.features.live2d = !this.settings.features.live2d;
                    if (window.__nbotLive2dSetEnabled) {
                        window.__nbotLive2dSetEnabled(this.settings.features.live2d);
                    }
                    this.showToast(this.settings.features.live2d ? 'Live2D 看板娘已开启' : 'Live2D 看板娘已关闭', 'info');
                },

                // ========== API Key 管理 ==========
                async loadApiKeys() {
                    try {
                        const res = await api.get('/api/api-keys');
                        if (res.data.success) {
                            this.apiKeys = res.data.keys || [];
                        }
                    } catch (e) {
                        console.error('加载API Keys失败:', e);
                    }
                },

                openApiKeyManager() {
                    this.showApiKeyManager = true;
                    this.loadApiKeys();
                },

                closeApiKeyManager() {
                    this.showApiKeyManager = false;
                    this.resetApiKeyForm();
                },

                resetApiKeyForm() {
                    this.apiKeyForm = {
                        id: null,
                        name: '',
                        key: ''
                    };
                },

                editApiKey(key) {
                    this.apiKeyForm = {
                        id: key.id,
                        name: key.name,
                        key: '' // 不显示已有的key值
                    };
                },

                async saveApiKey() {
                    if (!this.apiKeyForm.name.trim()) {
                        this.showToast('请输入API Key名称', 'error');
                        return;
                    }
                    if (!this.apiKeyForm.key.trim() && !this.apiKeyForm.id) {
                        this.showToast('请输入API Key', 'error');
                        return;
                    }

                    this.isSavingApiKey = true;
                    try {
                        if (this.apiKeyForm.id) {
                            // 更新
                            const res = await api.put(`/api/api-keys/${this.apiKeyForm.id}`, {
                                name: this.apiKeyForm.name,
                                key: this.apiKeyForm.key
                            });
                            if (res.data.success) {
                                this.showToast('API Key已更新', 'success');
                                this.resetApiKeyForm();
                                await this.loadApiKeys();
                            }
                        } else {
                            // 创建
                            const res = await api.post('/api/api-keys', {
                                name: this.apiKeyForm.name,
                                key: this.apiKeyForm.key
                            });
                            if (res.data.success) {
                                this.showToast('API Key已保存', 'success');
                                this.resetApiKeyForm();
                                await this.loadApiKeys();
                            }
                        }
                    } catch (e) {
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isSavingApiKey = false;
                    }
                },

                async deleteApiKey(key) {
                    if (!confirm(`确定要删除API Key "${key.name}" 吗？`)) {
                        return;
                    }
                    try {
                        const res = await api.delete(`/api/api-keys/${key.id}`);
                        if (res.data.success) {
                            this.showToast('API Key已删除', 'success');
                            await this.loadApiKeys();
                        }
                    } catch (e) {
                        this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async getApiKeyValue(keyId) {
                    try {
                        const res = await api.get(`/api/api-keys/${keyId}`);
                        if (res.data.success && res.data.key) {
                            return res.data.key.key;
                        }
                    } catch (e) {
                        console.error('获取API Key失败:', e);
                    }
                    return null;
                },

                async applyApiKeyToModel(keyId) {
                    const keyValue = await this.getApiKeyValue(keyId);
                    if (keyValue) {
                        this.modelForm.api_key = keyValue;
                        this.showToast('API Key已应用', 'success');
                    }
                },

                async clearSession() {
                    this.showConfirm({
                        title: '清空会话',
                        message: '确定要清空当前会话的所有消息吗？此操作不可恢复。',
                        confirmText: '清空',
                        icon: 'fa-trash-alt',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/sessions/${this.currentSession.id}/messages`);
                                if (this.currentMessages.length > 0 && this.currentMessages[0].role === 'system') {
                                    this.currentMessages = [this.currentMessages[0]];
                                } else {
                                    this.currentMessages = [];
                                }
                                this.showToast('会话已清空', 'success');
                            } catch (e) {
                                console.error('清空会话失败:', e);
                                this.showToast('清空失败', 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },
                
                async deleteSession() {
                    if (!this.currentSession) return;

                    this.showConfirm({
                        title: '删除会话',
                        message: '确定要删除这个会话吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            const sessionId = this.currentSession.id;
                            const deletedSession = this.currentSession;
                            const sessionIndex = this.sessions.findIndex(s => s.id === sessionId);

                            // 设置删除标志，防止定时刷新干扰
                            this._isDeletingSession = true;

                            // 立即从UI中移除（乐观更新）
                            this.sessions = this.sessions.filter(s => s.id !== sessionId);
                            this.currentSession = null;
                            this.currentMessages = [];
                            this.showToast('会话已删除', 'success');

                            try {
                                // 调用API删除
                                await api.delete(`/api/sessions/${sessionId}`);
                                // 删除成功，延迟清除标志（给服务器同步时间）
                                setTimeout(() => {
                                    this._isDeletingSession = false;
                                }, 3000);
                            } catch (e) {
                                console.error('删除会话失败:', e);
                                // 恢复会话到原来位置
                                if (sessionIndex !== -1) {
                                    this.sessions.splice(sessionIndex, 0, deletedSession);
                                } else {
                                    this.sessions.push(deletedSession);
                                }
                                this.currentSession = deletedSession;
                                this.showToast('删除失败，已恢复会话', 'error');
                                // 失败时立即清除标志
                                this._isDeletingSession = false;
                            }
                        }
                    });
                },
                
                // QQ 会话管理方法
                async viewQqSessionDetails(type, session) {
                    try {
                        let targetSession;
                        let sessionId;
                        let sessionType;
                        
                        if (type === 'current') {
                            if (!this.currentQqId) {
                                this.showToast('请先选择一个会话', 'warning');
                                return;
                            }
                            targetSession = this.chatTab === 'qq_private' 
                                ? this.qqPrivateUsers.find(u => u.user_id === this.currentQqId)
                                : this.qqGroups.find(g => g.group_id === this.currentQqId);
                            sessionId = this.currentQqId;
                            sessionType = this.chatTab;
                        } else {
                            targetSession = session;
                            sessionId = session.user_id || session.group_id;
                            sessionType = type === 'private' ? 'qq_private' : 'qq_group';
                        }
                        
                        if (!targetSession) {
                            this.showToast('会话不存在', 'error');
                            return;
                        }
                        
                        // 构建会话详情对象
                        this.viewingSession = {
                            id: sessionId,
                            name: targetSession.name || `QQ${sessionType === 'qq_private' ? '私聊' : '群聊'}`,
                            type: sessionType,
                            user_id: sessionType === 'qq_private' ? sessionId : null,
                            group_id: sessionType === 'qq_group' ? sessionId : null,
                            message_count: targetSession.message_count || 0,
                            created_at: targetSession.created_at || targetSession.last_time || new Date().toISOString(),
                            last_time: targetSession.last_time || targetSession.created_at || new Date().toISOString(),
                            last_message: targetSession.last_message || '无'
                        };
                        
                        // 如果是QQ会话，加载完整的消息历史
                        if (sessionType === 'qq_private' || sessionType === 'qq_group') {
                            try {
                                const qqType = sessionType === 'qq_private' ? 'private' : 'group';
                                const res = await api.get(`/api/qq/messages/${qqType}/${sessionId}`);
                                if (res.data && Array.isArray(res.data)) {
                                    this.viewingSession.messages = res.data;
                                }
                            } catch (e) {
                                console.error('加载QQ消息历史失败:', e);
                            }
                        }
                        
                        this.showSessionDetailsModal = true;
                    } catch (e) {
                        console.error('获取QQ会话详情失败:', e);
                        this.showToast('获取会话详情失败', 'error');
                    }
                },
                
                async confirmDeleteQqSession(type, session) {
                    let targetSession;
                    let sessionId;
                    
                    if (type === 'current') {
                        if (!this.currentQqId) {
                            this.showToast('请先选择一个会话', 'warning');
                            return;
                        }
                        targetSession = this.chatTab === 'qq_private'
                            ? this.qqPrivateUsers.find(u => u.user_id === this.currentQqId)
                            : this.qqGroups.find(g => g.group_id === this.currentQqId);
                        sessionId = this.currentQqId;
                    } else {
                        targetSession = session;
                        sessionId = session.user_id || session.group_id;
                    }
                    
                    if (!targetSession) {
                        this.showToast('会话不存在', 'error');
                        return;
                    }
                    
                    const sessionName = type === 'current' 
                        ? `当前${this.chatTab === 'qq_private' ? 'QQ私聊' : 'QQ群聊'} (${sessionId})`
                        : targetSession.name || `QQ${type === 'private' ? '私聊' : '群聊'} (${sessionId})`;
                    
                    this.showConfirm({
                        title: '删除 QQ 会话',
                        message: `确定要删除 ${sessionName} 的所有消息记录吗？此操作不可恢复。`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            try {
                                // 删除后端数据
                                const endpoint = type === 'private' || type === 'current' && this.chatTab === 'qq_private' ? 'private' : 'group';
                                await api.delete(`/api/qq/messages/${endpoint}/${sessionId}`);
                                
                                // 从列表中移除
                                if (type === 'private') {
                                    this.qqPrivateUsers = this.qqPrivateUsers.filter(u => u.user_id !== sessionId);
                                } else {
                                    this.qqGroups = this.qqGroups.filter(g => g.group_id !== sessionId);
                                }
                                
                                // 如果删除的是当前会话，清空视图
                                if (type === 'current' || sessionId === this.currentQqId) {
                                    this.currentQqId = null;
                                    this.currentQqMessages = [];
                                }
                                
                                this.showToast('会话已删除', 'success');
                            } catch (e) {
                                console.error('删除QQ会话失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            }
                        }
                    });
                },

                // Web 会话详情
                async viewWebSessionDetails(session) {
                    try {
                        const res = await api.get('/api/sessions/' + session.id);
                        const fullSession = res.data;
                        if (fullSession.error) {
                            this.showToast('会话不存在或已被删除', 'error');
                            return;
                        }
                        this.viewingSession = {
                            id: fullSession.id || session.id,
                            name: fullSession.name || session.name,
                            type: fullSession.type || session.type,
                            user_id: fullSession.user_id || session.user_id || '',
                            created_at: fullSession.created_at || session.created_at,
                            message_count: fullSession.message_count || session.message_count || 0,
                            system_prompt: fullSession.system_prompt || session.system_prompt || '',
                            archived: fullSession.archived || false,
                            channel_id: fullSession.channel_id || '',
                            messages: fullSession.messages || []
                        };
                        this.showSessionDetailsModal = true;
                    } catch (e) {
                        console.error('获取会话详情失败:', e);
                        this.showToast('获取详情失败', 'error');
                    }
                },

                // Web 会话删除
                deleteWebSession(session) {
                    this.showConfirm({
                        title: '删除会话',
                        message: '确定要删除会话 "' + session.name + '" 吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            try {
                                await api.delete('/api/sessions/' + session.id);
                                this.sessions = this.sessions.filter(s => s.id !== session.id);
                                if (this.currentSession && this.currentSession.id === session.id) {
                                    this.currentSession = null;
                                    this.currentMessages = [];
                                }
                                this.showToast('会话已删除', 'success');
                            } catch (e) {
                                console.error('删除会话失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            }
                        }
                    });
                },

                handleQqMessagesScroll() {
                    const container = this.$refs.qqMessagesContainer;
                    if (!container) return;
                    
                    const scrollTop = container.scrollTop;
                    const scrollHeight = container.scrollHeight;
                    const clientHeight = container.clientHeight;
                    
                    // 当滚动距离底部超过100px时，显示滑到底部按钮
                    const distanceFromBottom = scrollHeight - scrollTop - clientHeight;
                    this.showQqScrollButton = distanceFromBottom > 100;
                },
                
                scrollQqToBottom() {
                    const container = this.$refs.qqMessagesContainer;
                    if (!container) return;
                    
                    container.scrollTo({
                        top: container.scrollHeight,
                        behavior: 'smooth'
                    });
                    
                    // 滚动后隐藏按钮
                    this.showQqScrollButton = false;
                },

                toggleSelectAllSessions(event) {
                    if (event.target.checked) {
                        this.selectedSessions = this.managedSessions.map(s => s.id);
                    } else {
                        this.selectedSessions = [];
                    }
                },
                
                batchDeleteSessions() {
                    if (this.selectedSessions.length === 0) return;
                    
                    const count = this.selectedSessions.length;
                    this.showConfirm({
                        title: '批量删除会话',
                        message: `确定要删除选中的 ${count} 个会话吗？此操作不可恢复。`,
                        confirmText: `删除 ${count} 个会话`,
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            const deletedIds = [...this.selectedSessions];
                            const deletedSessions = this.sessions.filter(s => deletedIds.includes(s.id));
                            
                            // 从列表中移除
                            this.sessions = this.sessions.filter(s => !deletedIds.includes(s.id));
                            this.selectedSessions = [];
                            
                            // 如果当前会话被删除，清空
                            if (this.currentSession && deletedIds.includes(this.currentSession.id)) {
                                this.currentSession = null;
                                this.currentMessages = [];
                            }
                            
                            this.showToast(`已删除 ${count} 个会话`, 'success');
                            
                            // 逐个删除后端会话
                            let failedCount = 0;
                            for (const sessionId of deletedIds) {
                                try {
                                    await api.delete(`/api/sessions/${sessionId}`);
                                } catch (e) {
                                    failedCount++;
                                    console.error(`删除会话 ${sessionId} 失败:`, e);
                                }
                            }
                            
                            if (failedCount > 0) {
                                this.showToast(`部分会话删除同步失败，请刷新页面`, 'warning');
                            }
                        }
                    });
                },
                
                async openSession(session) {
                    // 根据会话类型和频道自动切换 chatTab
                    const channelId = session.channel_id || session.metadata?.channel_id;
                    if (channelId) {
                        const channelExists = this.registeredChannels.some(ch => ch.id === channelId);
                        if (channelExists) {
                            this.chatTab = 'channel_' + channelId;
                        } else {
                            // 频道已删除，回退到会话原始类型
                            this.chatTab = session.type || 'web';
                        }
                    } else {
                        this.chatTab = session.type || 'web';
                    }
                    this.currentPage = 'chat';
                    this.isMobileChatPickerOpen = false;
                    await this.selectSession(session);
                },
                
                editSession(session) {
                    this.editingSession = { ...session };
                    this.showEditSessionModal = true;
                },

                async archiveSession(session) {
                    if (!session?.id) return;
                    try {
                        await api.post(`/api/sessions/${session.id}/archive`);
                        session.archived = true;
                        session.archived_at = new Date().toISOString();
                        if (this.currentSession?.id === session.id) {
                            this.currentSession.archived = true;
                            this.currentSession.archived_at = session.archived_at;
                        }
                        this.selectedSessions = this.selectedSessions.filter(id => id !== session.id);
                        this.showToast('\u4f1a\u8bdd\u5df2\u5f52\u6863', 'success');
                    } catch (e) {
                        this.showToast('Failed to archive session: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async restoreSession(session) {
                    if (!session?.id) return;
                    try {
                        await api.post(`/api/sessions/${session.id}/restore`);
                        session.archived = false;
                        session.archived_at = null;
                        if (this.currentSession?.id === session.id) {
                            this.currentSession.archived = false;
                            this.currentSession.archived_at = null;
                        }
                        this.selectedSessions = this.selectedSessions.filter(id => id !== session.id);
                        this.showToast('\u4f1a\u8bdd\u5df2\u6062\u590d', 'success');
                    } catch (e) {
                        this.showToast('Failed to restore session: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },
                
                async viewSessionDetails(session) {
                    // 先直接展示已有数据，再后台加载完整详情
                    this.viewingSession = {
                        id: session.id,
                        name: session.name,
                        type: session.type,
                        user_id: session.user_id || '',
                        created_at: session.created_at,
                        message_count: session.message_count || 0,
                        system_prompt: session.system_prompt || '',
                        archived: session.archived || false,
                        channel_id: session.channel_id || '',
                    };
                    this.showSessionDetailsModal = true;

                    // 后台加载完整数据（含消息列表）
                    try {
                        const res = await api.get(`/api/sessions/${session.id}`);
                        if (res.data && !res.data.error) {
                            this.viewingSession = {
                                ...this.viewingSession,
                                ...res.data,
                                message_count: res.data.message_count || this.viewingSession.message_count,
                            };
                        }
                    } catch (e) {
                        console.error('加载会话完整详情失败:', e);
                    }
                },
               
                copySessionRawData() {
                    if (!this.viewingSession) return;
                    
                    const data = JSON.stringify(this.viewingSession, null, 2);
                    this.copyToClipboard(data);
                },
                

                
                async saveSessionEdit() {
                    this.isLoading = true;
                    try {
                        await api.put(`/api/sessions/${this.editingSession.id}`, this.editingSession);
                        await this.loadSessions();
                        this.showEditSessionModal = false;
                        this.showToast('会话已更新', 'success');
                    } catch (e) {
                        this.showToast('更新失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                // Confirm Modal Functions
                showConfirm(config) {
                    this.confirmModalConfig = {
                        title: config.title || '确认操作',
                        message: config.message || '确定要执行这个操作吗？',
                        confirmText: config.confirmText || '确认',
                        icon: config.icon || 'fa-exclamation-circle',
                        iconColor: config.iconColor || 'var(--warning)',
                        danger: config.danger || false,
                        action: config.action,
                        data: config.data || null
                    };
                    this.showConfirmModal = true;
                },
                
                confirmAction() {
                    if (this.confirmModalConfig.action) {
                        this.confirmModalConfig.action(this.confirmModalConfig.data);
                    }
                    this.showConfirmModal = false;
                },
                
                cancelConfirm() {
                    this.showConfirmModal = false;
                },
                
                // 旧版确认对话框方法（用于兼容）
                confirmDialogAction() {
                    if (this.confirmDialogConfig.onConfirm) {
                        this.confirmDialogConfig.onConfirm();
                    }
                    this.showConfirmDialog = false;
                },
                
                cancelDialogConfirm() {
                    if (this.confirmDialogConfig.onCancel) {
                        this.confirmDialogConfig.onCancel();
                    }
                    this.showConfirmDialog = false;
                },

                // 扩展编辑器方法
                openExpandEditor(content = '') {
                    this.expandEditorContent = content;
                    this.showExpandEditor = true;
                    // 聚焦到文本框
                    this.$nextTick(() => {
                        if (this.$refs.expandEditorTextarea) {
                            this.$refs.expandEditorTextarea.focus();
                        }
                    });
                },

                closeExpandEditor() {
                    this.showExpandEditor = false;
                    this.expandEditorContent = '';
                    this.expandEditorMode = 'edit';
                },

                // 工作区浏览器
                openWorkspaceBrowser() {
                    this.showWorkspaceBrowser = true;
                    this.workspaceCurrentPath = '';
                    this.workspaceFiles = [];
                    this.closeFileMenu();
                    this.loadWorkspaceFiles();
                },
                
                closeWorkspaceBrowser() {
                    this.showWorkspaceBrowser = false;
                },
                
                switchWorkspaceScope(scope) {
                    this.workspaceScope = scope;
                    this.workspaceCurrentPath = '';
                    this.loadWorkspaceFiles();
                },
                
                async loadWorkspaceFiles() {
                    if (this.workspaceScope === 'shared') {
                        // 加载共享工作区
                        this.loadingWorkspaceFiles = true;
                        try {
                            const path = this.workspaceCurrentPath;
                            const url = path 
                                ? `/api/workspace/shared/files?path=${encodeURIComponent(path)}`
                                : `/api/workspace/shared/files`;
                            
                            const res = await api.get(url);
                            if (res.data.files) {
                                this.workspaceFiles = res.data.files.map(f => ({
                                    name: f.name,
                                    type: f.type,
                                    size: f.size,
                                    path: f.path,
                                    scope: 'shared',
                                    reference: !!f.reference
                                }));
                            }
                        } catch (e) {
                            console.error('加载共享工作区文件失败:', e);
                            this.workspaceFiles = [];
                        } finally {
                            this.loadingWorkspaceFiles = false;
                        }
                    } else {
                        // 加载私有工作区
                        if (!this.currentSession?.id) return;
                        
                        this.loadingWorkspaceFiles = true;
                        try {
                            const sessionId = this.currentSession.id;
                            const path = this.workspaceCurrentPath;
                            const url = path 
                                ? `/api/sessions/${sessionId}/workspace/files?path=${encodeURIComponent(path)}`
                                : `/api/sessions/${sessionId}/workspace/files`;
                            
                            const res = await api.get(url);
                            if (res.data.files) {
                                this.workspaceFiles = res.data.files.map(f => ({
                                    name: f.name,
                                    type: f.type,
                                    size: f.size,
                                    path: f.path,
                                    scope: 'private',
                                    reference: !!f.reference
                                }));
                            }
                        } catch (e) {
                            console.error('加载工作区文件失败:', e);
                            this.workspaceFiles = [];
                        } finally {
                            this.loadingWorkspaceFiles = false;
                        }
                    }
                },
                
                navigateToFolder(path) {
                    this.workspaceCurrentPath = path;
                    this.loadWorkspaceFiles();
                },
                
                navigateToParent() {
                    if (!this.workspaceCurrentPath) return;
                    const parts = this.workspaceCurrentPath.split('/').filter(p => p);
                    parts.pop();
                    this.workspaceCurrentPath = parts.join('/');
                    this.loadWorkspaceFiles();
                },
                
                onDragOverParent(event) {
                    if (!this.draggingFile) return;
                    event.dataTransfer.dropEffect = 'move';
                },
                
                onDragEnterParent(event) {
                    if (!this.draggingFile) return;
                    this.dragOverItem = '__parent__';
                },
                
                onDragLeaveParent(event) {
                    if (this.dragOverItem === '__parent__') {
                        this.dragOverItem = null;
                    }
                },
                
                async onDropToParent(event) {
                    this.dragOverItem = null;
                    if (!this.draggingFile) return;
                    
                    // 计算上级目录
                    const parts = this.workspaceCurrentPath.split('/').filter(p => p);
                    parts.pop();
                    const parentPath = parts.join('/');
                    
                    try {
                        const filename = this.draggingFile.path;
                        const fromScope = this.draggingFileScope || this.workspaceScope;
                        const toScope = this.workspaceScope;
                        let url, requestData;
                        
                        // 跨工作区移动到上级
                        if (fromScope !== toScope) {
                            if (fromScope === 'private' && toScope === 'shared') {
                                url = `/api/sessions/${this.currentSession.id}/workspace/files/${encodeURIComponent(filename)}/move-to-shared`;
                                requestData = { target: parentPath };
                            } else if (fromScope === 'shared' && toScope === 'private') {
                                url = `/api/workspace/shared/files/${encodeURIComponent(filename)}/move-to-private`;
                                requestData = { session_id: this.currentSession.id, target: parentPath };
                            }
                        } else {
                            // 同工作区移动
                            if (this.workspaceScope === 'shared') {
                                url = `/api/workspace/shared/files/${encodeURIComponent(filename)}/move`;
                            } else {
                                url = `/api/sessions/${this.currentSession.id}/workspace/files/${encodeURIComponent(filename)}/move`;
                            }
                            requestData = { target: parentPath };
                        }
                        
                        const res = await api.post(url, requestData);
                        
                        if (res.data.success) {
                            this.showToast('文件已移动到上级目录', 'success');
                            this.loadWorkspaceFiles();
                        } else {
                            this.showToast(res.data.error || '移动失败', 'error');
                        }
                    } catch (e) {
                        this.showToast('移动失败', 'error');
                    }
                    
                    this.draggingFile = null;
                    this.draggingFileScope = null;
                },
                
                previewWorkspaceFile(item) {
                    // 不关闭工作区浏览器，直接打开文件预览
                    const path = item.path;
                    
                    if (this.workspaceScope === 'shared') {
                        // 共享工作区文件预览
                        this.showFilePreview = true;
                        this.filePreviewData = {
                            filename: item.name,
                            path: path,
                            type: '',
                            content: '',
                            url: `/api/workspace/shared/files/${encodeURIComponent(path)}`,
                            loading: true,
                            error: '',
                            truncated: false,
                            extracted_length: 0,
                            original_length: 0
                        };
                        this.loadSharedFilePreview(path);
                    } else {
                        // 私有工作区文件预览
                        this.previewFile(this.currentSession?.id, path);
                    }
                },
                
                async loadSharedFilePreview(path) {
                    try {
                        const res = await api.get(`/api/workspace/shared/files/${encodeURIComponent(path)}`);
                        this.filePreviewData.loading = false;
                        
                        if (res.data) {
                            if (res.data.type === 'image') {
                                // 图片文件
                                this.filePreviewData.type = 'image';
                                this.filePreviewData.url = res.data.url;
                            } else if (res.data.content !== undefined) {
                                // 文本内容
                                this.filePreviewData.content = res.data.content;
                            } else if (res.data.error) {
                                this.filePreviewData.error = res.data.error;
                            }
                        }
                    } catch (e) {
                        console.error('加载共享文件预览失败:', e);
                        this.filePreviewData.error = '加载失败: ' + (e.message || '未知错误');
                        this.filePreviewData.loading = false;
                    }
                },
                
                insertWorkspaceFile(item) {
                    // 把文件路径直接插入到输入框，区分共享和私有
                    let prefix = '';
                    if (this.workspaceScope === 'shared') {
                        prefix = '[共享] ';
                    } else {
                        prefix = '[私有] ';
                    }
                    
                    const path = this.workspaceCurrentPath 
                        ? `${this.workspaceCurrentPath}/${item.name}`
                        : item.name;
                    
                    const fullPath = prefix + path;
                    
                    if (this.inputMessage) {
                        this.inputMessage += '\n' + fullPath;
                    } else {
                        this.inputMessage = fullPath;
                    }
                    
                    this.closeWorkspaceBrowser();
                },
                
                refreshWorkspaceFiles() {
                    this.loadWorkspaceFiles();
                },
                
                async createFolder() {
                    if (!this.newFolderName.trim()) return;
                    
                    try {
                        let url, data;
                        if (this.workspaceScope === 'shared') {
                            url = '/api/workspace/shared/folders';
                            data = {
                                name: this.newFolderName.trim(),
                                path: this.workspaceCurrentPath
                            };
                        } else {
                            if (!this.currentSession?.id) return;
                            url = `/api/sessions/${this.currentSession.id}/workspace/folders`;
                            data = {
                                name: this.newFolderName.trim(),
                                path: this.workspaceCurrentPath
                            };
                        }
                        
                        const res = await api.post(url, data);
                        
                        if (res.data.success) {
                            this.showToast('文件夹创建成功', 'success');
                            this.showCreateFolderModal = false;
                            this.newFolderName = '';
                            this.loadWorkspaceFiles();
                        } else {
                            this.showToast(res.data.error || '创建失败', 'error');
                        }
                    } catch (e) {
                        this.showToast('创建文件夹失败', 'error');
                    }
                },
                
                async deleteWorkspaceItem(item) {
                    this.showConfirm({
                        title: '删除' + (item.type === 'directory' ? '文件夹' : '文件'),
                        message: `确定要删除 "${item.name}" ${item.type === 'directory' ? '及其所有内容' : ''} 吗？此操作不可恢复。`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--danger)',
                        danger: true,
                        action: async () => {
                            try {
                                const filename = item.path;
                                let url;
                                if (this.workspaceScope === 'shared') {
                                    url = `/api/workspace/shared/files/${encodeURIComponent(filename)}`;
                                } else {
                                    url = `/api/sessions/${this.currentSession.id}/workspace/files/${encodeURIComponent(filename)}`;
                                }
                                const res = await api.delete(url);
                                
                                if (res.data.success) {
                                    this.showToast('删除成功', 'success');
                                    this.loadWorkspaceFiles();
                                } else {
                                    this.showToast(res.data.error || '删除失败', 'error');
                                }
                            } catch (e) {
                                this.showToast('删除失败', 'error');
                            }
                        }
                    });
                },
                
                onDragFileStart(event, item) {
                    if (item.reference) {
                        event.preventDefault();
                        return;
                    }
                    this.draggingFile = item;
                    this.draggingFileScope = item.scope || this.workspaceScope;
                    event.dataTransfer.effectAllowed = 'move';
                    event.dataTransfer.setData('text/plain', item.path);
                    event.target.classList.add('dragging');
                },
                
                onDragFileEnd(event) {
                    this.draggingFile = null;
                    this.draggingFileScope = null;
                    this.dragOverItem = null;
                    event.target.classList.remove('dragging');
                },
                
                onWorkspaceDragOver(event, item) {
                    if (item.type !== 'directory') return;
                    if (!this.draggingFile || this.draggingFile.path === item.path) return;
                    event.dataTransfer.dropEffect = 'move';
                },
                
                onWorkspaceDragEnter(event, item) {
                    if (item.type !== 'directory') return;
                    if (!this.draggingFile || this.draggingFile.path === item.path) return;
                    this.dragOverItem = item.path;
                },
                
                onWorkspaceDragLeave(event, item) {
                    if (this.dragOverItem === item.path) {
                        this.dragOverItem = null;
                    }
                },
                
                async onWorkspaceDrop(event, targetItem) {
                    this.dragOverItem = null;
                    
                    if (targetItem.type !== 'directory' || !this.draggingFile) return;
                    if (this.draggingFile.path === targetItem.path) return;
                    
                    // 不能移动到自身目录
                    if (targetItem.path.startsWith(this.draggingFile.path + '/')) {
                        this.showToast('不能将文件夹移动到自身目录下', 'error');
                        return;
                    }
                    
                    try {
                        const filename = this.draggingFile.path;
                        const target = targetItem.path;
                        const fromScope = this.draggingFileScope || this.workspaceScope;
                        const toScope = this.workspaceScope;
                        let url, requestData;
                        
                        // 跨工作区移动
                        if (fromScope !== toScope) {
                            if (fromScope === 'private' && toScope === 'shared') {
                                // 私有 -> 共享
                                url = `/api/sessions/${this.currentSession.id}/workspace/files/${encodeURIComponent(filename)}/move-to-shared`;
                                requestData = { target: target };
                            } else if (fromScope === 'shared' && toScope === 'private') {
                                // 共享 -> 私有
                                url = `/api/workspace/shared/files/${encodeURIComponent(filename)}/move-to-private`;
                                requestData = { session_id: this.currentSession.id, target: target };
                            }
                        } else {
                            // 同工作区移动
                            if (this.workspaceScope === 'shared') {
                                url = `/api/workspace/shared/files/${encodeURIComponent(filename)}/move`;
                            } else {
                                url = `/api/sessions/${this.currentSession.id}/workspace/files/${encodeURIComponent(filename)}/move`;
                            }
                            requestData = { target: target };
                        }
                        
                        const res = await api.post(url, requestData);
                        
                        if (res.data.success) {
                            this.showToast('文件已移动', 'success');
                            this.loadWorkspaceFiles();
                        } else {
                            this.showToast(res.data.error || '移动失败', 'error');
                        }
                    } catch (e) {
                        this.showToast('移动失败', 'error');
                    }
                    
                    this.draggingFile = null;
                    this.draggingFileScope = null;
                },
                
                applyExpandEditor() {
                    const content = this.expandEditorContent;
                    this.showExpandEditor = false;
                    this.expandEditorContent = '';
                    this.expandEditorMode = 'edit';
                    // 将内容设置到主输入框
                    this.inputMessage = content;
                },
                
                // 文件预览
                async previewFile(msgOrSessionId, filename) {
                    // 支持两种调用方式：
                    // 1. previewFile(msg) - 传入消息对象
                    // 2. previewFile(sessionId, filename) - 传入 sessionId 和 filename（向后兼容）
                    let msg, sessionId, originalFilename;
                    if (typeof msgOrSessionId === 'object' && msgOrSessionId !== null) {
                        msg = msgOrSessionId;
                        sessionId = msg.session_id;
                        originalFilename = msg.file?.name;
                    } else {
                        sessionId = msgOrSessionId;
                        originalFilename = filename;
                    }
                    
                    const file = msg?.file;
                    const safeName = file?.safe_name;
                    
                    this.showFilePreview = true;
                    this.filePreviewData = {
                        sessionId: sessionId,
                        filename: originalFilename,
                        type: '',
                        content: '',
                        url: '',
                        loading: true,
                        error: '',
                        truncated: false,
                        extracted_length: 0,
                        original_length: 0
                    };
                    
                    try {
                        const encodedFilename = encodeURIComponent(originalFilename);
                        
                        // 如果有 safe_name（static/files 目录中的文件），使用新的 API
                        if (safeName) {
                            return this.previewStaticFile(safeName, originalFilename);
                        }
                        
                        // docx 文件特殊处理：获取 blob 并在前端渲染
                        if (this.isDocxFile(originalFilename)) {
                            const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}`, {
                                responseType: 'blob'
                            });
                            if (res.data) {
                                this.filePreviewData.loading = false;
                                this.filePreviewData.blob = res.data;
                                this.$nextTick(() => {
                                    this.renderDocx(res.data);
                                });
                            } else {
                                this.filePreviewData.error = '无法加载文档';
                                this.filePreviewData.loading = false;
                            }
                            return;
                        }
                        
                        // excel 文件特殊处理：获取 blob 并在前端渲染
                        if (this.isExcelFile(originalFilename)) {
                            const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}`, {
                                responseType: 'blob'
                            });
                            if (res.data) {
                                this.filePreviewData.loading = false;
                                this.filePreviewData.blob = res.data;
                                this.$nextTick(() => {
                                    this.renderExcel(res.data);
                                });
                            } else {
                                this.filePreviewData.error = '无法加载表格';
                                this.filePreviewData.loading = false;
                            }
                            return;
                        }
                        
                        // PDF 文件特殊处理：获取 blob 并在前端渲染
                        if (this.isPdfFile(originalFilename)) {
                            const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}`, {
                                responseType: 'blob'
                            });
                            if (res.data) {
                                this.filePreviewData.loading = false;
                                this.filePreviewData.blob = res.data;
                                this.$nextTick(() => {
                                    this.renderPdf(res.data);
                                });
                            } else {
                                this.filePreviewData.error = '无法加载 PDF';
                                this.filePreviewData.loading = false;
                            }
                            return;
                        }
                        
                        // PPTX 文件特殊处理：获取 blob 并在前端渲染
                        if (this.isPptxFile(originalFilename)) {
                            const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}`, {
                                responseType: 'blob'
                            });
                            if (res.data) {
                                this.filePreviewData.loading = false;
                                this.filePreviewData.blob = res.data;
                                this.$nextTick(() => {
                                    this.renderPptx(res.data);
                                });
                            } else {
                                this.filePreviewData.error = '无法加载 PPTX';
                                this.filePreviewData.loading = false;
                            }
                            return;
                        }
                        
                        // HTML 文件特殊处理：获取文本内容并渲染
                        if (this.isHtmlFile(originalFilename)) {
                            const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}`, {
                                responseType: 'text'
                            });
                            if (res.data) {
                                this.filePreviewData.content = res.data;
                                this.filePreviewData.loading = false;
                                this.$nextTick(() => {
                                    this.renderHtml(res.data);
                                });
                            } else {
                                this.filePreviewData.error = '无法加载页面';
                                this.filePreviewData.loading = false;
                            }
                            return;
                        }
                        
                        // 其他文件使用后端解析
                        const res = await api.get(`/api/sessions/${sessionId}/workspace/files/${encodedFilename}/preview`);
                        if (res.data.success) {
                            this.filePreviewData = {
                                ...this.filePreviewData,
                                ...res.data,
                                loading: false
                            };
                        } else {
                            this.filePreviewData.error = res.data.error || '预览失败';
                            this.filePreviewData.loading = false;
                        }
                    } catch (e) {
                        console.error('文件预览失败:', e);
                        this.filePreviewData.error = '预览失败: ' + (e.response?.data?.error || e.message);
                        this.filePreviewData.loading = false;
                    }
                },
                
                // 预览 static/files 目录中的文件
                async previewStaticFile(safeName, originalFilename) {
                    try {
                        const res = await api.get(`/api/files/${encodeURIComponent(safeName)}/preview`);
                        
                        if (res.data.success) {
                            // 如果需要前端渲染（PDF、PPTX、DOCX、Excel），获取 blob
                            if (res.data.is_blob) {
                                const fileRes = await api.get(`/static/files/${encodeURIComponent(safeName)}`, {
                                    responseType: 'blob'
                                });
                                if (fileRes.data) {
                                    this.filePreviewData = {
                                        ...this.filePreviewData,
                                        ...res.data,
                                        blob: fileRes.data,
                                        loading: false
                                    };
                                    
                                    // 调用对应的渲染函数
                                    this.$nextTick(() => {
                                        const fileType = res.data.type?.toLowerCase();
                                        if (fileType === 'pdf') {
                                            this.renderPdf(fileRes.data);
                                        } else if (fileType === 'pptx' || fileType === 'ppt') {
                                            this.renderPptx(fileRes.data);
                                        } else if (fileType === 'docx' || fileType === 'doc') {
                                            this.renderDocx(fileRes.data);
                                        } else if (fileType === 'xlsx' || fileType === 'xls') {
                                            this.renderExcel(fileRes.data);
                                        }
                                    });
                                } else {
                                    this.filePreviewData.error = '无法加载文件';
                                    this.filePreviewData.loading = false;
                                }
                                return;
                            }
                            
                            this.filePreviewData = {
                                ...this.filePreviewData,
                                ...res.data,
                                filename: originalFilename || this.filePreviewData.filename,
                                safe_name: safeName,
                                url: res.data.url || `/static/files/${encodeURIComponent(safeName)}`,
                                download_url: res.data.download_url || `/static/files/${encodeURIComponent(safeName)}`,
                                loading: false
                            };
                            
                            // 如果是图片类型，渲染图片
                            if (res.data.type === 'image') {
                                this.$nextTick(() => {
                                    const img = this.$refs.filePreviewModal?.querySelector('img');
                                    if (img) {
                                        img.src = res.data.url;
                                    }
                                });
                            } else if (this.isHtmlFile(originalFilename || this.filePreviewData.filename)) {
                                this.$nextTick(() => {
                                    this.renderHtml(res.data.content || '');
                                });
                            }
                        } else {
                            this.filePreviewData.error = res.data.error || '预览失败';
                            this.filePreviewData.loading = false;
                        }
                    } catch (e) {
                        console.error('静态文件预览失败:', e);
                        this.filePreviewData.error = '预览失败: ' + (e.response?.data?.error || e.message);
                        this.filePreviewData.loading = false;
                    }
                },
                
                // docx 渲染器缓存
                _docxRenderer: null,
                
                // 获取 docx 渲染器（ESM 动态加载）
                async getDocxRenderer() {
                    if (this._docxRenderer) return this._docxRenderer;
                    
                    const cdns = [
                        'https://esm.sh/docx-preview@0.3.3',
                        'https://cdn.jsdelivr.net/npm/docx-preview@0.3.3/+esm',
                        'https://unpkg.com/docx-preview@0.3.3/dist/docx-preview.esm.js',
                    ];
                    
                    let lastErr;
                    for (const url of cdns) {
                        try {
                            const mod = await import(url);
                            if (typeof mod.renderAsync === 'function') {
                                this._docxRenderer = mod.renderAsync;
                                return this._docxRenderer;
                            }
                        } catch (e) { lastErr = e; }
                    }
                    throw new Error('无法加载 docx-preview 库');
                },
                
                // 渲染 docx 文件 (使用 docx-preview)
                async renderDocx(blob) {
                    this.$nextTick(() => {
                        const container = this.$refs.docxContainer;
                        if (!container) {
                            this.filePreviewData.error = '预览容器未就绪';
                            return;
                        }
                        container.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:40px;">正在加载预览...</p>';
                    });
                    
                    try {
                        const renderAsync = await this.getDocxRenderer();
                        this.$nextTick(() => {
                            const container = this.$refs.docxContainer;
                            container.innerHTML = '';
                            
                            blob.arrayBuffer().then(ab => {
                                renderAsync(ab, container, null, {
                                    className: 'docx',
                                    inWrapper: true,
                                    breakPages: true,
                                    useBase64URL: true,
                                    renderChanges: false,
                                    renderHeaders: true,
                                    renderFooters: true,
                                }).then(() => {
                                    // 成功渲染
                                }).catch(err => {
                                    container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">渲染失败: ' + err.message + '</p>';
                                });
                            });
                        });
                    } catch (err) {
                        this.filePreviewData.error = '无法加载预览库: ' + err.message;
                    }
                },
                
                // HTML 转义辅助方法
                escapeHtml(text) {
                    if (!text) return '';
                    const div = document.createElement('div');
                    div.textContent = text;
                    return div.innerHTML;
                },
                
                // 渲染 excel 文件 (使用 xlsx 库)
                renderExcel(blob) {
                    this.$nextTick(() => {
                        const container = this.$refs.excelContainer;
                        if (!container) {
                            this.filePreviewData.error = '预览容器未就绪';
                            return;
                        }
                        container.innerHTML = '';
                        
                        // 检查 xlsx 库
                        const XLSXLib = window.XLSX || window.XLSX;
                        if (typeof XLSXLib === 'undefined') {
                            // 动态加载 xlsx 库
                            this.loadXlsxLibrary().then(() => {
                                this.doRenderExcel(blob, this.$refs.excelContainer);
                            }).catch(err => {
                                this.filePreviewData.error = '无法加载 xlsx 库: ' + err.message;
                            });
                        } else {
                            this.doRenderExcel(blob, container);
                        }
                    });
                },
                
                // 动态加载 xlsx 库
                async loadXlsxLibrary() {
                    if (window.XLSX) return;
                    
                    const script = document.createElement('script');
                    script.src = '/static/vendor/xlsx.full.min.js';
                    document.head.appendChild(script);
                    
                    return new Promise((resolve, reject) => {
                        script.onload = () => resolve();
                        script.onerror = () => reject(new Error('xlsx 库加载失败'));
                    });
                },
                
                // 执行 excel 渲染
                doRenderExcel(blob, container) {
                    const XLSXLib = window.XLSX;
                    
                    blob.arrayBuffer().then(ab => {
                        const workbook = XLSXLib.read(new Uint8Array(ab), { type: 'array' });
                        const sheetNames = workbook.SheetNames;
                        
                        if (sheetNames.length === 0) {
                            container.innerHTML = '<p>表格为空</p>';
                            return;
                        }
                        
                        // 渲染第一个工作表
                        this.renderExcelSheet(workbook, sheetNames[0], container);
                        
                        // 如果有多个工作表，添加切换按钮
                        if (sheetNames.length > 1) {
                            const tabsWrapper = document.createElement('div');
                            tabsWrapper.id = 'excel-tabs-wrapper';
                            tabsWrapper.style.cssText = 'margin-bottom: 10px; display: flex; gap: 5px; flex-wrap: wrap;';
                            
                            sheetNames.forEach((name, idx) => {
                                const btn = document.createElement('button');
                                btn.textContent = name;
                                btn.dataset.sheet = name;
                                btn.style.cssText = 'padding: 5px 15px; border: 1px solid #ddd; background: ' + (idx === 0 ? '#1e4060; color: white;' : '#fff; color: #333;') + '; border-radius: 4px; cursor: pointer;';
                                btn.onclick = () => {
                                    // 切换到对应工作表
                                    this.doRenderExcelSwitchSheet(workbook, name, container, tabsWrapper, sheetNames);
                                };
                                tabsWrapper.appendChild(btn);
                            });
                            container.insertBefore(tabsWrapper, container.firstChild);
                        }
                    }).catch(err => {
                        this.filePreviewData.error = '表格解析失败: ' + err.message;
                    });
                },
                
                // 切换 Excel 工作表
                doRenderExcelSwitchSheet(workbook, sheetName, container, tabsWrapper, sheetNames) {
                    // 更新 tab 样式
                    tabsWrapper.querySelectorAll('button').forEach((btn, idx) => {
                        const isActive = btn.dataset.sheet === sheetName;
                        btn.style.background = isActive ? '#1e4060' : '#fff';
                        btn.style.color = isActive ? 'white' : '#333';
                    });
                    
                    // 渲染工作表
                    this.renderExcelSheet(workbook, sheetName, container);
                },
                
                // 渲染单个 excel 工作表
                renderExcelSheet(workbook, sheetName, container) {
                    const XLSXLib = window.XLSX;
                    const sheet = workbook.Sheets[sheetName];
                    const ref = sheet['!ref'];
                    
                    if (!ref) {
                        container.innerHTML = '<div style="padding:24px;color:var(--text-muted);">该 Sheet 为空</div>';
                        return;
                    }
                    
                    const rng = XLSX.utils.decode_range(ref);
                    const rows = rng.e.r, cols = rng.e.c;
                    const letters = Array.from({length: cols + 1}, (_, c) => XLSX.utils.encode_col(c));
                    
                    let html = '<div style="overflow:auto;background:#fff;border:1px solid var(--border);border-radius:4px;"><table style="border-collapse:collapse;font-family:var(--mono);font-size:12px;min-width:100%;white-space:nowrap;">';
                    html += '<thead><tr><th style="background:#edf1f5;color:#1e4060;font-weight:600;padding:7px 14px;text-align:center;border-bottom:1.5px solid #c8d8e8;border-right:1.5px solid #c8d8e8;position:sticky;top:0;z-index:1;min-width:48px;">#</th>';
                    letters.forEach(l => { html += `<th style="background:#edf1f5;color:#1e4060;font-weight:600;padding:7px 14px;text-align:center;border-bottom:1.5px solid #c8d8e8;border-right:1px solid var(--border-light);position:sticky;top:0;z-index:1;">${l}</th>`; });
                    html += '</tr></thead><tbody>';
                    
                    for (let r = 0; r <= rows; r++) {
                        html += `<tr><td style="padding:6px 14px;background:#f4f5f7;color:#1e4060;font-weight:500;text-align:center;border-bottom:1px solid var(--border-light);border-right:1.5px solid #c8d8e8;font-size:11px;min-width:48px;">${r + 1}</td>`;
                        for (let c = 0; c <= cols; c++) {
                            const cell = sheet[XLSX.utils.encode_cell({r, c})];
                            const val = cell ? XLSX.utils.format_cell(cell) : '';
                            html += `<td style="padding:6px 14px;border-bottom:1px solid var(--border-light);border-right:1px solid var(--border-light);max-width:320px;overflow:hidden;text-overflow:ellipsis;color:#1a1814;" title="${this.escapeHtml(String(val ?? ''))}">${this.escapeHtml(String(val ?? ''))}</td>`;
                        }
                        html += '</tr>';
                    }
                    html += '</tbody></table></div>';
                    container.innerHTML = html;
                },
                
                // PDF.js 渲染器缓存
                _pdfJsLib: null,
                
                // 加载 PDF.js 库
                async loadPdfJs() {
                    if (this._pdfJsLib) return this._pdfJsLib;
                    
                    return new Promise((resolve, reject) => {
                        const script = document.createElement('script');
                        script.src = '/static/vendor/pdf.min.js';
                        script.onload = () => {
                            // 设置 worker
                            window.pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdf.worker.min.js';
                            this._pdfJsLib = window.pdfjsLib;
                            resolve(this._pdfJsLib);
                        };
                        script.onerror = () => reject(new Error('PDF.js 库加载失败'));
                        document.head.appendChild(script);
                    });
                },
                
                // 渲染 PDF 文件
                async renderPdf(blob) {
                    this.$nextTick(() => {
                        const container = this.$refs.pdfContainer;
                        if (!container) {
                            this.filePreviewData.error = '预览容器未就绪';
                            return;
                        }
                        container.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:40px;">正在加载 PDF...</p>';
                    });
                    
                    try {
                        const pdfjsLib = await this.loadPdfJs();
                        
                        blob.arrayBuffer().then(ab => {
                            pdfjsLib.getDocument({ data: ab }).promise.then(pdf => {
                                const numPages = pdf.numPages;
                                const container = this.$refs.pdfContainer;
                                container.innerHTML = '';
                                
                                // 渲染每一页
                                const renderPromises = [];
                                for (let i = 1; i <= numPages; i++) {
                                    renderPromises.push(this.renderPdfPage(pdf, i, container));
                                }
                                
                                Promise.all(renderPromises).catch(err => {
                                    container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">渲染失败: ' + err.message + '</p>';
                                });
                            }).catch(err => {
                                container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">PDF 加载失败: ' + err.message + '</p>';
                            });
                        });
                    } catch (err) {
                        this.filePreviewData.error = '无法加载 PDF 预览库: ' + err.message;
                    }
                },
                
                // 渲染单个 PDF 页面
                renderPdfPage(pdf, pageNum, container) {
                    return pdf.getPage(pageNum).then(page => {
                        const scale = 1.5;
                        const viewport = page.getViewport({ scale });
                        
                        const canvas = document.createElement('canvas');
                        const context = canvas.getContext('2d');
                        canvas.height = viewport.height;
                        canvas.width = viewport.width;
                        canvas.style.display = 'block';
                        canvas.style.margin = '0 auto 16px';
                        canvas.style.boxShadow = '0 2px 8px rgba(0,0,0,0.1)';
                        
                        const pageDiv = document.createElement('div');
                        pageDiv.style.cssText = 'text-align:center;margin-bottom:16px;';
                        const pageLabel = document.createElement('div');
                        pageLabel.textContent = `第 ${pageNum} 页`;
                        pageLabel.style.cssText = 'font-family:var(--mono);font-size:12px;color:var(--text-muted);margin-bottom:8px;';
                        pageDiv.appendChild(pageLabel);
                        pageDiv.appendChild(canvas);
                        container.appendChild(pageDiv);
                        
                        return page.render({
                            canvasContext: context,
                            viewport: viewport
                        }).promise;
                    });
                },
                
                // PPTX.js 渲染器缓存
                _pptxJsLib: null,
                
                // 加载 pptx-preview 库
                async loadPptxJs() {
                    if (this._pptxJsLib) return this._pptxJsLib;
                    
                    const cdns = [
                        'https://esm.sh/pptx-preview@1.0.4',
                        'https://cdn.jsdelivr.net/npm/pptx-preview@1.0.4/+esm',
                    ];
                    
                    let lastErr;
                    for (const url of cdns) {
                        console.log('[PPTX] 尝试从 CDN 加载:', url);
                        try {
                            const mod = await import(url);
                            console.log('[PPTX] 模块加载成功:', url, '导出:', Object.keys(mod));
                            if (mod.init) {
                                this._pptxJsLib = mod.init;
                                return this._pptxJsLib;
                            }
                            if (mod.default && typeof mod.default === 'function') {
                                this._pptxJsLib = mod.default;
                                return this._pptxJsLib;
                            }
                        } catch (e) { 
                            console.error('[PPTX] 加载失败:', url, e);
                            lastErr = e; 
                        }
                    }
                    throw new Error('无法加载 pptx-preview 库');
                },
                
                // PPTX 渲染（通过后端转换为 PDF）
                async renderPptx(blob) {
                    this.$nextTick(() => {
                        const container = this.$refs.pptxContainer;
                        if (!container) {
                            this.filePreviewData.error = '预览容器未就绪';
                            return;
                        }
                        container.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:40px;">正在将 PPTX 转换为 PDF...</p>';
                    });
                    
                    try {
                        // 上传到后端转换为 PDF
                        const formData = new FormData();
                        formData.append('file', blob, this.filePreviewData.filename || 'presentation.pptx');
                        
                        const response = await fetch('/api/workspace/convert/pptx-to-pdf', {
                            method: 'POST',
                            body: formData
                        });
                        
                        if (response.ok) {
                            const pdfBlob = await response.blob();
                            const container = this.$refs.pptxContainer;
                            container.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:40px;">正在渲染 PDF...</p>';
                            
                            // 直接渲染 PDF 到 pptxContainer
                            try {
                                const pdfjsLib = await this.loadPdfJs();
                                pdfBlob.arrayBuffer().then(ab => {
                                    pdfjsLib.getDocument({ data: ab }).promise.then(pdf => {
                                        container.innerHTML = '';
                                        const renderPromises = [];
                                        for (let i = 1; i <= pdf.numPages; i++) {
                                            renderPromises.push(this.renderPdfPage(pdf, i, container));
                                        }
                                        Promise.all(renderPromises).catch(err => {
                                            container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">渲染失败: ' + err.message + '</p>';
                                        });
                                        this.filePreviewData.loading = false;
                                    }).catch(err => {
                                        container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">PDF 加载失败: ' + err.message + '</p>';
                                        this.filePreviewData.loading = false;
                                    });
                                });
                            } catch (err) {
                                container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">PDF 渲染库加载失败: ' + err.message + '</p>';
                                this.filePreviewData.loading = false;
                            }
                        } else {
                            const data = await response.json();
                            const container = this.$refs.pptxContainer;
                            const blob = this.filePreviewData.blob;
                            const filename = this.filePreviewData.filename || 'presentation.pptx';
                            container.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:40px;">' +
                                'PPTX 预览需要服务器安装 LibreOffice<br>' +
                                '<small style="color:#999;">' + (data.detail || data.error) + '</small><br><br>' +
                                '<button id="btn-download-pptx" style="padding:8px 16px;cursor:pointer;">下载 PPTX 文件</button>' +
                                '</p>';
                            document.getElementById('btn-download-pptx').onclick = () => {
                                const url = URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = url;
                                link.download = filename;
                                link.click();
                                URL.revokeObjectURL(url);
                            };
                            this.filePreviewData.loading = false;
                        }
                    } catch (err) {
                        const container = this.$refs.pptxContainer;
                        container.innerHTML = '<p style="color:red;text-align:center;padding:40px;">PPTX 转换失败: ' + err.message + '</p>';
                        this.filePreviewData.loading = false;
                    }
                },
                
                // 渲染 HTML 文件
                renderHtml(content) {
                    this.$nextTick(() => {
                        const container = this.$refs.htmlContainer;
                        if (!container) {
                            console.error('html 容器未找到');
                            this.filePreviewData.error = '预览容器未就绪，请重试';
                            return;
                        }
                        container.innerHTML = '';
                        const iframe = document.createElement('iframe');
                        iframe.className = 'html-preview-frame';
                        iframe.setAttribute('sandbox', 'allow-same-origin allow-scripts allow-popups allow-forms');
                        iframe.srcdoc = content;
                        container.appendChild(iframe);
                    });
                },
                
                closeFilePreview() {
                    this.showFilePreview = false;
                    this.filePreviewMaximized = false;
                },
                
                startResize(e) {
                    if (this.filePreviewMaximized) return;
                    e.preventDefault();
                    this._resizeStartX = e.clientX;
                    this._resizeStartY = e.clientY;
                    this._resizeStartWidth = this.filePreviewWidth;
                    this._resizeStartHeight = this.filePreviewHeight;
                    
                    const onMouseMove = (e) => {
                        const dx = e.clientX - this._resizeStartX;
                        const dy = e.clientY - this._resizeStartY;
                        this.filePreviewWidth = Math.max(400, this._resizeStartWidth + dx);
                        this.filePreviewHeight = Math.max(300, this._resizeStartHeight + dy);
                    };
                    
                    const onMouseUp = () => {
                        document.removeEventListener('mousemove', onMouseMove);
                        document.removeEventListener('mouseup', onMouseUp);
                    };
                    
                    document.addEventListener('mousemove', onMouseMove);
                    document.addEventListener('mouseup', onMouseUp);
                },
                
                downloadFile(fileData) {
                    console.log('downloadFile called:', JSON.stringify(fileData));
                    // 优先级：blob缓存 > download_url > url > path > session文件下载 > content兜底
                    
                    // 1. 优先使用缓存的 blob 数据
                    if (fileData && fileData.blob) {
                        const url = window.URL.createObjectURL(fileData.blob);
                        const link = document.createElement('a');
                        link.href = url;
                        link.download = fileData.filename || fileData.name || 'download';
                        document.body.appendChild(link);
                        link.click();
                        document.body.removeChild(link);
                        window.URL.revokeObjectURL(url);
                        return;
                    }
                    
                    // 2. download_url → fetch + Blob 下载
                    if (fileData && fileData.download_url) {
                        api.get(fileData.download_url, { responseType: 'blob' })
                            .then(response => {
                                const blob = response.data;
                                const blobUrl = window.URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = blobUrl;
                                link.download = fileData.filename || fileData.name || 'download';
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                window.URL.revokeObjectURL(blobUrl);
                            })
                            .catch(e => {
                                console.error('下载失败:', e);
                                this.showToast('下载失败', 'error');
                            });
                        return;
                    }
                    
                    // 3. url → 直接 fetch 附件 URL 下载
                    if (fileData && fileData.url) {
                        const filename = fileData.filename || fileData.name || 'download';
                        api.get(fileData.url, { responseType: 'blob' })
                            .then(response => {
                                const blob = new Blob([response.data]);
                                const blobUrl = window.URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = blobUrl;
                                link.download = filename;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                window.URL.revokeObjectURL(blobUrl);
                            })
                            .catch(e => {
                                console.error('下载失败:', e);
                                this.showToast('下载失败', 'error');
                            });
                        return;
                    }
                    
                    // 4. path / file_path → 调用后端 API 下载
                    if (fileData && (fileData.path || fileData.file_path)) {
                        const filePath = encodeURIComponent(fileData.path || fileData.file_path);
                        const filename = fileData.filename || fileData.name || 'download';
                        api.get(`/api/workspace/download?path=${filePath}`, { responseType: 'blob' })
                            .then(response => {
                                const blob = new Blob([response.data]);
                                const blobUrl = window.URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = blobUrl;
                                link.download = filename;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                window.URL.revokeObjectURL(blobUrl);
                            })
                            .catch(e => {
                                console.error('下载失败:', e);
                                this.showToast('下载失败', 'error');
                            });
                        return;
                    }
                    
                    // 5. 其他情况：优先尝试用 sessionId + filename 下载原始文件
                    if (fileData && fileData.sessionId && fileData.filename) {
                        const filename = encodeURIComponent(fileData.filename);
                        const originalFilename = fileData.filename;
                        const url = `/api/sessions/${fileData.sessionId}/workspace/files/${filename}`;
                        api.get(url, { responseType: 'blob' })
                            .then(response => {
                                const blob = new Blob([response.data]);
                                const blobUrl = window.URL.createObjectURL(blob);
                                const link = document.createElement('a');
                                link.href = blobUrl;
                                link.download = originalFilename;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                window.URL.revokeObjectURL(blobUrl);
                            })
                            .catch(e => {
                                console.error('下载失败:', e);
                                // 只有原始文件下载失败时，才退回预览内容下载
                                if (fileData.content) {
                                    const blob = new Blob([fileData.content], { type: 'text/plain;charset=utf-8' });
                                    const blobUrl = window.URL.createObjectURL(blob);
                                    const link = document.createElement('a');
                                    link.href = blobUrl;
                                    link.download = fileData.filename || fileData.name || 'download.txt';
                                    document.body.appendChild(link);
                                    link.click();
                                    document.body.removeChild(link);
                                    window.URL.revokeObjectURL(blobUrl);
                                } else {
                                    this.showToast('下载失败', 'error');
                                }
                            });
                        return;
                    }

                    // 6. 最后兜底：仅当拿不到真实文件时，才用预览内容下载
                    if (fileData && fileData.content) {
                        const blob = new Blob([fileData.content], { type: 'text/plain;charset=utf-8' });
                        const url = window.URL.createObjectURL(blob);
                        const link = document.createElement('a');
                        link.href = url;
                        link.download = fileData.filename || fileData.name || 'download.txt';
                        document.body.appendChild(link);
                        link.click();
                        document.body.removeChild(link);
                        window.URL.revokeObjectURL(url);
                        return;
                    }
                    
                    this.showToast('文件下载链接不可用', 'error');
                },
                
                // Workflow Functions
                getTaskCenterIcon(kind) {
                    if (kind === 'heartbeat') return 'fas fa-heartbeat';
                    if (kind === 'workflow') return 'fas fa-project-diagram';
                    return 'fas fa-calendar-check';
                },

                getTaskCenterKindLabel(kind) {
                    if (kind === 'heartbeat') return '系统任务';
                    if (kind === 'workflow') return '工作流';
                    return '自定义任务';
                },

                getTaskCenterTriggerLabel(item) {
                    if (!item) return '未知';
                    if (item.trigger === 'interval') {
                        return `每 ${item.config?.interval_minutes || 60} 分钟`;
                    }
                    if (item.trigger === 'date') {
                        return item.config?.run_at || '单次执行';
                    }
                    if (item.trigger === 'cron') {
                        return item.config?.cron || '0 8 * * *';
                    }
                    return item.trigger || '手动触发';
                },

                getSessionDisplayName(sessionId) {
                    const session = this.sessions.find(s => s.id === sessionId);
                    if (!session) return sessionId;
                    const prefix = session.type === 'qq_group'
                        ? 'QQ群'
                        : session.type === 'qq_private'
                            ? 'QQ私聊'
                            : session.type === 'cli'
                                ? 'CLI终端'
                                : 'Web';
                    return `${prefix} · ${session.name || `会话 ${session.id.substring(0, 8)}`}`;
                },

                getTaskCenterTargetSessions() {
                    return this.sessions.filter(s => ['web', 'cli', 'qq_group', 'qq_private'].includes(s.type));
                },

                async openTaskCenterEditor(item) {
                    if (item.kind === 'heartbeat') {
                        this.navigateTo('heartbeat');
                        return;
                    }
                    if (item.kind === 'workflow') {
                        if (!this.workflows.length) {
                            await this.loadWorkflows();
                        }
                        const workflow = this.workflows.find(w => w.id === item.id);
                        if (workflow) {
                            this.openWorkflowModal(workflow);
                        } else {
                            this.navigateTo('workflows');
                        }
                        return;
                    }
                    this.openTaskCenterModal(item);
                },

                openTaskCenterModal(task = null) {
                    if (task) {
                        this.editingTaskCenterItem = task;
                        this.taskCenterForm = {
                            id: task.id,
                            name: task.name || '',
                            description: task.description || '',
                            enabled: task.enabled !== false,
                            trigger: task.trigger || 'interval',
                            config: {
                                interval_minutes: task.config?.interval_minutes || 60,
                                cron: task.config?.cron || '0 8 * * *',
                                run_at: task.config?.run_at || ''
                            },
                            target_session_id: task.target_session_id || '',
                            prompt: task.prompt || ''
                        };
                    } else {
                        this.editingTaskCenterItem = null;
                        this.taskCenterForm = {
                            id: null,
                            name: '',
                            description: '',
                            enabled: true,
                            trigger: 'interval',
                            config: {
                                interval_minutes: 60,
                                cron: '0 8 * * *',
                                run_at: ''
                            },
                            target_session_id: this.currentSession?.id || '',
                            prompt: ''
                        };
                    }
                    this.showTaskCenterModal = true;
                },

                async saveTaskCenterTask() {
                    this.isLoading = true;
                    try {
                        const payload = {
                            name: this.taskCenterForm.name,
                            description: this.taskCenterForm.description,
                            enabled: this.taskCenterForm.enabled,
                            trigger: this.taskCenterForm.trigger,
                            config: { ...this.taskCenterForm.config },
                            target_session_id: this.taskCenterForm.target_session_id,
                            prompt: this.taskCenterForm.prompt
                        };

                        if (this.editingTaskCenterItem) {
                            await api.put(`/api/task-center/${this.editingTaskCenterItem.id}`, payload);
                            this.showToast('任务已更新', 'success');
                        } else {
                            await api.post('/api/task-center', payload);
                            this.showToast('任务已创建', 'success');
                        }

                        this.showTaskCenterModal = false;
                        await this.loadTaskCenter();
                    } catch (e) {
                        console.error('Failed to save task center task:', e);
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async toggleTaskCenterItem(item) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/task-center/${item.id}/toggle`);
                        await Promise.all([this.loadTaskCenter(), this.loadHeartbeat(), this.loadWorkflows()]);
                        this.showToast(`任务已${item.enabled ? '停用' : '启用'}`, 'success');
                    } catch (e) {
                        console.error('Failed to toggle task center item:', e);
                        this.showToast('操作失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async runTaskCenterItem(item) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/task-center/${item.id}/run`);
                        this.showToast('任务已开始执行', 'success');
                        setTimeout(() => this.loadTaskCenter(), 1200);
                    } catch (e) {
                        console.error('Failed to run task center item:', e);
                        this.showToast('执行失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async deleteTaskCenterItem(item) {
                    this.showConfirm({
                        title: '删除任务',
                        message: `确定要删除“${item.name}”吗？此操作不可恢复。`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/task-center/${item.id}`);
                                await this.loadTaskCenter();
                                this.showToast('任务已删除', 'success');
                            } catch (e) {
                                console.error('Failed to delete task center item:', e);
                                this.showToast('删除失败', 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                getTriggerIcon(trigger) {
                    const type = this.workflowTriggerTypes.find(t => t.value === trigger);
                    return type ? type.icon : 'fas fa-question';
                },
                
                getTriggerLabel(trigger) {
                    const type = this.workflowTriggerTypes.find(t => t.value === trigger);
                    return type ? type.label : trigger;
                },
                
                getTargetLabel(targetType) {
                    const target = this.workflowTargetTypes.find(t => t.value === targetType);
                    return target ? target.label : targetType;
                },
                
                openWorkflowModal(workflow = null) {
                    if (workflow) {
                        // 编辑模式
                        this.editingWorkflow = workflow;
                        this.workflowForm = {
                            id: workflow.id,
                            name: workflow.name,
                            description: workflow.description || '',
                            enabled: workflow.enabled,
                            trigger: workflow.trigger || 'manual',
                            config: {
                                cron: workflow.config?.cron || '0 8 * * *',
                                keywords: workflow.config?.keywords || '',
                                target_type: workflow.config?.target_type || 'none',
                                target_id: workflow.config?.target_id || '',
                                max_history: workflow.config?.max_history || 10
                            }
                        };
                    } else {
                        // 新建模式
                        this.editingWorkflow = null;
                        this.workflowForm = {
                            id: null,
                            name: '',
                            description: '',
                            enabled: true,
                            trigger: 'manual',
                            config: {
                                cron: '0 8 * * *',
                                keywords: '',
                                target_type: 'none',
                                target_id: '',
                                max_history: 10
                            }
                        };
                    }
                    this.showWorkflowModal = true;
                },
                
                async saveWorkflow() {
                    this.isLoading = true;
                    try {
                        const data = {
                            name: this.workflowForm.name,
                            description: this.workflowForm.description,
                            enabled: this.workflowForm.enabled,
                            trigger: this.workflowForm.trigger,
                            config: { ...this.workflowForm.config }
                        };
                        
                        if (this.editingWorkflow) {
                            await api.put(`/api/workflows/${this.editingWorkflow.id}`, data);
                            this.showToast('工作流已更新', 'success');
                        } else {
                            await api.post('/api/workflows', data);
                            this.showToast('工作流创建成功', 'success');
                        }
                        
                        this.showWorkflowModal = false;
                        await this.loadWorkflows();
                    } catch (e) {
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                async deleteWorkflow(id) {
                    this.showConfirm({
                        title: '删除工作流',
                        message: '确定要删除这个工作流吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/workflows/${id}`);
                                this.showWorkflowModal = false;
                                await this.loadWorkflows();
                                this.showToast('工作流已删除', 'success');
                            } catch (e) {
                                console.error('删除工作流失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },
                
                async toggleWorkflow(workflow) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/workflows/${workflow.id}/toggle`);
                        await this.loadWorkflows();
                        this.showToast(`工作流已${!workflow.enabled ? '启用' : '禁用'}`, 'success');
                    } catch (e) {
                        this.showToast('操作失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                async executeWorkflow(workflow) {
                    this.showInput({
                        title: '执行工作流',
                        message: '请输入工作流任务内容（可选）：',
                        placeholder: '输入任务内容...',
                        defaultValue: workflow.description || '',
                        onConfirm: async (userContent) => {
                            this.isLoading = true;
                            try {
                                await api.post(`/api/workflows/${workflow.id}/execute`, {
                                    content: userContent || workflow.description || '请执行工作流任务'
                                });
                                this.showToast('工作流执行已启动', 'success');
                            } catch (e) {
                                this.showToast('执行失败', 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },
                
                // AI Generate Workflow - 使用工具调用
                async generateWorkflowWithAi() {
                    this.isLoading = true;
                    try {
                        // 使用 AI 工具调用 API，让 AI 直接创建工作流
                        const messages = [
                            { role: 'user', content: this.aiGeneratePrompt }
                        ];
                        
                        const res = await api.post('/api/ai/tools', { messages });
                        
                        // 检查是否有工具调用
                        if (res.data.tool_calls && res.data.tool_calls.length > 0) {
                            const toolCall = res.data.tool_calls[0];
                            
                            if (toolCall.name === 'create_workflow' && toolCall.result.success) {
                                this.aiGeneratedWorkflow = toolCall.result.workflow;
                                this.showToast('AI 已成功创建工作流', 'success');
                            } else {
                                this.showToast('AI 生成失败：' + (toolCall.result.error || '未知错误'), 'error');
                            }
                        } else {
                            // AI 没有调用工具，显示 AI 的回复内容
                            this.showToast(res.data.content || 'AI 没有创建工作流', 'info');
                        }
                        
                        // 刷新工作流列表
                        await this.loadWorkflows();
                    } catch (e) {
                        this.showToast('生成失败，请重试', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                async saveAiGeneratedWorkflow() {
                    if (!this.aiGeneratedWorkflow) return;
                    
                    // AI 已经通过工具调用创建了工作流，这里只需要关闭模态框
                    this.showAiGenerateModal = false;
                    this.aiGeneratedWorkflow = null;
                    this.aiGeneratePrompt = '';
                    await this.loadWorkflows();
                    this.showToast('工作流已创建', 'success');
                },
                
                // Personality Functions
                async savePersonality() {
                    this.isLoading = true;
                    try {
                        await api.put('/api/personality', {
                            ...this.personality,
                            _manualSystemPrompt: this._manualSystemPrompt || false
                        });
                        this.activePersonality = { ...this.personality };
                        this.personalityHasUnsavedChanges = false;
                        this.showToast('人格设置已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                loadPersonalityPreset(preset) {
                    this.showConfirmDialogFn({
                        title: '加载人格预设',
                        message: `确定要加载预设人格 "${preset.name}" 吗？`,
                        onConfirm: () => {
                            this.personality = {
                                name: preset.name || '',
                                description: preset.description || '',
                                avatar: preset.avatar || preset.icon || '',
                                portrait: preset.portrait || '',
                                tags: preset.tags || [],
                                systemPrompt: preset.systemPrompt || preset.prompt || '',
                                basicInfo: preset.basicInfo || '',
                                personality: preset.personality || '',
                                scenario: preset.scenario || '',
                                firstMessage: preset.firstMessage || '',
                                exampleDialogues: preset.exampleDialogues || '',
                                responseFormat: preset.responseFormat || '',
                                rules: preset.rules || [],
                                state: preset.state || { affection: 50, mood: '开心' }
                            };
                            this.personalityTagsInput = (this.personality.tags || []).join(' ');
                            this._manualSystemPrompt = false;
                            this.personalityHasUnsavedChanges = true;
                            this.showToast('已加载预设人格，请保存以应用', 'success');
                        }
                    });
                },

                previewCompiledPrompt() {
                    const p = this.personality;
                    let prompt = '';

                    if (p.name) prompt += `【角色名称】${p.name}\n`;
                    if (p.basicInfo) prompt += `【基本信息】\n${p.basicInfo}\n`;
                    if (p.personality) prompt += `【性格特点】${p.personality}\n`;
                    if (p.scenario) prompt += `【背景设定】${p.scenario}\n`;
                    if (p.responseFormat) prompt += `【回复格式】${p.responseFormat}\n`;
                    if (p.rules && p.rules.length > 0) {
                        prompt += `【行为规则】\n`;
                        p.rules.forEach((rule, i) => {
                            if (rule) prompt += `${i + 1}. ${rule}\n`;
                        });
                    }
                    if (p.exampleDialogues) prompt += `【示例对话】\n${p.exampleDialogues}\n`;

                    // 角色状态
                    const state = p.state || {};
                    if (Object.keys(state).length > 0) {
                        prompt += '\n【角色当前状态】\n';
                        if ('affection' in state) prompt += `好感度: ${state.affection}/100\n`;
                        if ('mood' in state) prompt += `心情: ${state.mood}\n`;
                    }

                    if (prompt) {
                        prompt = `你是角色 "${p.name || '未命名'}"。\n\n` + prompt;
                    } else {
                        prompt = '请定义你的角色设定。';
                    }

                    // 替换模板变量 {{user}} -> 当前登录用户名, {{char}} -> 角色名称
                    if (this.username) {
                        prompt = prompt.replace(/\{\{user\}\}/g, this.username);
                    }
                    if (p.name) {
                        prompt = prompt.replace(/\{\{char\}\}/g, p.name);
                    }

                    this.infoModalConfig = {
                        title: 'Prompt 预览',
                        message: `<pre style="white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.6;">${this.escapeHtml(prompt)}</pre>`,
                        confirmText: '关闭'
                    };
                    this.showInfoModal = true;
                },

                regenerateSystemPrompt() {
                    const p = this.personality;
                    let prompt = '';

                    if (p.name) prompt += `【角色名称】${p.name}\n`;
                    if (p.basicInfo) prompt += `【基本信息】\n${p.basicInfo}\n`;
                    if (p.personality) prompt += `【性格特点】${p.personality}\n`;
                    if (p.scenario) prompt += `【背景设定】${p.scenario}\n`;
                    if (p.responseFormat) prompt += `【回复格式】${p.responseFormat}\n`;
                    if (p.rules && p.rules.length > 0) {
                        prompt += `【行为规则】\n`;
                        p.rules.forEach((rule, i) => {
                            if (rule) prompt += `${i + 1}. ${rule}\n`;
                        });
                    }
                    if (p.exampleDialogues) prompt += `【示例对话】\n${p.exampleDialogues}\n`;

                    // 角色状态
                    const state = p.state || {};
                    if (Object.keys(state).length > 0) {
                        prompt += '\n【角色当前状态】\n';
                        if ('affection' in state) prompt += `好感度: ${state.affection}/100\n`;
                        if ('mood' in state) prompt += `心情: ${state.mood}\n`;
                    }

                    if (prompt) {
                        prompt = `你是角色 "${p.name || '未命名'}"。\n\n` + prompt;
                        this.personality.systemPrompt = prompt;
                        this._manualSystemPrompt = false;
                        this.personalityHasUnsavedChanges = true;
                        this.showToast('已重新生成系统提示词', 'success');
                    } else {
                        this.showToast('请先填写角色设定', 'warning');
                    }
                },

                addPersonalityRule() {
                    if (!this.personality.rules) {
                        this.personality.rules = [];
                    }
                    this.personality.rules.push('');
                    this.personalityHasUnsavedChanges = true;
                },

                removePersonalityRule(index) {
                    if (this.personality.rules) {
                        this.personality.rules.splice(index, 1);
                        this.personalityHasUnsavedChanges = true;
                    }
                },

                updatePersonalityTags() {
                    const input = this.personalityTagsInput || '';
                    this.personality.tags = input.split(/\s+/).filter(tag => tag.trim());
                    this.personalityHasUnsavedChanges = true;
                },

                escapeHtml(text) {
                    const div = document.createElement('div');
                    div.textContent = text;
                    return div.innerHTML;
                },

                // 自定义确认对话框方法
                showConfirmDialogFn(config) {
                    this.confirmDialogConfig = {
                        title: config.title || '确认',
                        message: config.message || '',
                        onConfirm: config.onConfirm || null,
                        onCancel: config.onCancel || null
                    };
                    this.showConfirmDialog = true;
                },

                // 自定义人格预设方法
                openAddPersonalityPresetModal() {
                    this.newPersonalityPreset = {
                        name: '',
                        description: '',
                        avatar: '🎭',
                        tags: [],
                        personality: '',
                        scenario: '',
                        firstMessage: '',
                        exampleDialogues: '',
                        responseFormat: '',
                        rules: [],
                        state: { affection: 50, mood: '开心' }
                    };
                    this.showAddPersonalityPresetModal = true;
                },

                closeAddPersonalityPresetModal() {
                    this.showAddPersonalityPresetModal = false;
                },

                // 新建角色 - 清空编辑器
                createNewPersonality() {
                    this.personality = {
                        name: '',
                        description: '',
                        avatar: '',
                        tags: [],
                        systemPrompt: '',
                        basicInfo: '',
                        personality: '',
                        scenario: '',
                        firstMessage: '',
                        exampleDialogues: '',
                        responseFormat: '',
                        rules: [],
                        state: { affection: 50, mood: '开心' }
                    };
                    this.personalityTagsInput = '';
                    this._manualSystemPrompt = false;
                    this.personalityHasUnsavedChanges = false;
                    this.showToast('请在左侧编辑器中填写角色信息，然后点击保存', 'info');
                },

                // 选择头像
                selectAvatar(icon) {
                    if (icon) {
                        this.personality.avatar = icon;
                        this.personalityHasUnsavedChanges = true;
                        this.showToast('头像已选择', 'success');
                    }
                },

                // 查看立绘大图
                viewPortrait(portraitUrl) {
                    if (portraitUrl) {
                        this.portraitViewerUrl = portraitUrl;
                        this.showPortraitViewer = true;
                    }
                },

                // 保存当前角色到自定义预设
                async savePersonalityAsPreset() {
                    if (!this.personality.name) {
                        this.showToast('请填写角色名称', 'error');
                        return;
                    }

                    try {
                        const presetData = {
                            name: this.personality.name,
                            description: this.personality.description || '',
                            avatar: this.personality.avatar || '',
                            portrait: this.personality.portrait || '',
                            tags: this.personality.tags || [],
                            basicInfo: this.personality.basicInfo || '',
                            personality: this.personality.personality || '',
                            scenario: this.personality.scenario || '',
                            firstMessage: this.personality.firstMessage || '',
                            exampleDialogues: this.personality.exampleDialogues || '',
                            responseFormat: this.personality.responseFormat || '',
                            rules: this.personality.rules || [],
                            state: this.personality.state || { affection: 50, mood: '开心' }
                        };
                        const res = await api.post('/api/personality/custom-presets', presetData);
                        this.customPersonalityPresets.push(res.data);
                        this.showToast('角色卡已保存到"我的角色卡"', 'success');
                    } catch (e) {
                        console.error('保存角色卡失败:', e);
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async addCustomPersonalityPreset() {
                    if (!this.newPersonalityPreset.name) {
                        this.showToast('请填写角色名称', 'error');
                        return;
                    }

                    try {
                        const presetData = {
                            name: this.newPersonalityPreset.name,
                            description: this.newPersonalityPreset.description || '',
                            avatar: this.newPersonalityPreset.avatar || '🎭',
                            tags: this.newPersonalityPreset.tags || [],
                            personality: this.newPersonalityPreset.personality || '',
                            scenario: this.newPersonalityPreset.scenario || '',
                            firstMessage: this.newPersonalityPreset.firstMessage || '',
                            exampleDialogues: this.newPersonalityPreset.exampleDialogues || '',
                            responseFormat: this.newPersonalityPreset.responseFormat || '',
                            rules: this.newPersonalityPreset.rules || [],
                            state: this.newPersonalityPreset.state || { affection: 50, mood: '开心' }
                        };
                        const res = await api.post('/api/personality/custom-presets', presetData);
                        this.customPersonalityPresets.push(res.data);
                        this.showToast('自定义角色卡已添加', 'success');
                        this.closeAddPersonalityPresetModal();
                    } catch (e) {
                        console.error('添加自定义人格预设失败:', e);
                        this.showToast('添加失败: ' + (e.response?.data?.error || e.message), 'error');
                    }
                },

                async deleteCustomPersonalityPreset(preset, index) {
                    this.showConfirmDialogFn({
                        title: '删除人格预设',
                        message: `确定要删除自定义人格预设 "${preset.name}" 吗？`,
                        onConfirm: async () => {
                            try {
                                await api.delete(`/api/personality/custom-presets/${preset.id}`);
                                this.customPersonalityPresets.splice(index, 1);
                                this.showToast('自定义人格预设已删除', 'success');
                            } catch (e) {
                                console.error('删除自定义人格预设失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            }
                        }
                    });
                },

                async loadCustomPersonalityPresets() {
                    try {
                        const res = await api.get('/api/personality/custom-presets');
                        this.customPersonalityPresets = res.data;
                    } catch (e) {
                        console.error('加载自定义人格预设失败:', e);
                    }
                },

                // 用指定角色预设开启新会话（不切换当前角色）
                async startSessionWithPreset(preset) {
                    if (this.isLoading) return;
                    this.isLoading = true;
                    try {
                        // 直接用预设的 systemPrompt 创建会话，不修改当前角色
                        const res = await api.post('/api/sessions', {
                            name: '新会话',
                            type: 'web',
                            user_id: this.username,
                            system_prompt: preset.systemPrompt || preset.prompt || '',
                            first_message: preset.firstMessage || '',
                            sender_name: preset.name || '',
                            sender_avatar: preset.avatar || '',
                            sender_portrait: preset.portrait || ''
                        });
                        const newSession = { ...res.data.session, _isNew: true };
                        this.sessions = [
                            ...this.sessions.filter(session => session.id !== newSession.id),
                            newSession
                        ];
                        this.currentPage = 'chat';
                        this.chatTab = 'web';
                        await this.selectSession(newSession);
                        setTimeout(() => {
                            const session = this.sessions.find(s => s.id === newSession.id);
                            if (session) {
                                session._isNew = false;
                            }
                        }, 1500);
                        this.showToast(`已用「${preset.name}」开启新对话`, 'success');
                    } catch (e) {
                        console.error('用预设开启会话失败:', e);
                        this.showToast('操作失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // AI 生成角色卡
                async aiGenerateCharacter() {
                    if (!this.aiCreateDescription.trim()) {
                        this.showToast('请输入角色描述', 'error');
                        return;
                    }
                    this.isLoading = true;
                    this.aiGeneratedCharacter = null;
                    try {
                        const res = await api.post('/api/personality/ai-generate', {
                            description: this.aiCreateDescription
                        });
                        if (res.data.success) {
                            this.aiGeneratedCharacter = res.data.character;
                            this.showToast('角色卡生成成功！', 'success');
                        } else {
                            this.showToast(res.data.error || '生成失败', 'error');
                        }
                    } catch (e) {
                        console.error('AI生成角色卡失败:', e);
                        this.showToast('生成失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // 应用AI生成的角色卡到编辑器
                applyAiGeneratedCharacter() {
                    if (!this.aiGeneratedCharacter) return;
                    this.personality = { ...this.aiGeneratedCharacter };
                    this.personalityTagsInput = (this.personality.tags || []).join(' ');
                    this.personalityHasUnsavedChanges = true;
                    this.showAiCreateModal = false;
                    this.aiCreateDescription = '';
                    this.aiGeneratedCharacter = null;
                    this.showToast('角色卡已加载到编辑器，请点击"应用"保存', 'success');
                },

                // 取消AI创建
                cancelAiCreate() {
                    this.showAiCreateModal = false;
                    this.aiCreateDescription = '';
                    this.aiGeneratedCharacter = null;
                },

                // 导出当前角色卡（ZIP 格式，包含 JSON 和立绘图片）
                async exportPersonality() {
                    if (!this.personality.name) {
                        this.showToast('请先创建角色卡', 'error');
                        return;
                    }

                    this.isLoading = true;
                    try {
                        // 调用后端 API 导出 ZIP
                        const res = await api.post('/api/personality/export', {
                            character: this.personality
                        }, {
                            responseType: 'blob'
                        });

                        // 创建下载链接
                        const blob = new Blob([res.data], { type: 'application/zip' });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `${this.personality.name}_角色卡.zip`;
                        a.click();
                        URL.revokeObjectURL(url);

                        this.showToast('角色卡已导出（包含立绘）', 'success');
                    } catch (e) {
                        console.error('导出角色卡失败:', e);
                        this.showToast('导出失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // 处理立绘上传
                async handlePortraitUpload(event) {
                    const file = event.target.files[0];
                    if (!file) return;

                    // 验证文件类型
                    if (!file.type.startsWith('image/')) {
                        this.showToast('请上传图片文件', 'error');
                        return;
                    }

                    // 验证文件大小（最大 5MB）
                    if (file.size > 5 * 1024 * 1024) {
                        this.showToast('图片大小不能超过 5MB', 'error');
                        return;
                    }

                    this.isLoading = true;
                    try {
                        // 使用 FormData 上传文件到服务器
                        const formData = new FormData();
                        formData.append('file', file);

                        const res = await api.post('/api/personality/portrait', formData, {
                            headers: { 'Content-Type': 'multipart/form-data' }
                        });

                        if (res.data.success) {
                            // 保存返回的图片 URL
                            this.personality.portrait = res.data.url;
                            this.personalityHasUnsavedChanges = true;
                            this.showToast('立绘上传成功，请点击"应用"保存', 'success');
                        } else {
                            this.showToast(res.data.error || '上传失败', 'error');
                        }
                    } catch (e) {
                        console.error('上传立绘失败:', e);
                        this.showToast('上传失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                        // 清空 input 以便可以重复选择同一文件
                        event.target.value = '';
                    }
                },

                // 删除立绘
                async removePortrait() {
                    if (!this.personality.portrait) return;

                    try {
                        // 调用后端删除接口
                        await api.delete('/api/personality/portrait', {
                            data: { url: this.personality.portrait }
                        });
                    } catch (e) {
                        console.error('删除服务器立绘文件失败:', e);
                    }

                    this.personality.portrait = '';
                    this.personalityHasUnsavedChanges = true;
                    this.showToast('立绘已删除，请点击"应用"保存', 'info');
                },

                // 触发导入文件选择
                triggerImportPersonality() {
                    this.$refs.importPersonalityFile.click();
                },

                // 导入角色卡
                async importPersonality(event) {
                    const file = event.target.files[0];
                    if (!file) return;

                    this.isLoading = true;
                    try {
                        const formData = new FormData();
                        formData.append('file', file);
                        const res = await api.post('/api/personality/import', formData, {
                            headers: { 'Content-Type': 'multipart/form-data' }
                        });
                        if (res.data.success) {
                            this.personality = { ...res.data.character };
                            this.personalityTagsInput = (this.personality.tags || []).join(' ');
                            this.personalityHasUnsavedChanges = true;
                            this.showToast('角色卡已导入，请点击"应用"保存', 'success');
                        } else {
                            this.showToast(res.data.error || '导入失败', 'error');
                        }
                    } catch (e) {
                        console.error('导入角色卡失败:', e);
                        this.showToast('导入失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                        // 清除文件选择以便重新选择同一文件
                        event.target.value = '';
                    }
                },

                // AI随机生成开场白
                async aiGenerateFirstMessage() {
                    if (!this.personality.name) {
                        this.showToast('请先填写角色名称', 'error');
                        return;
                    }
                    this.isLoading = true;
                    try {
                        const res = await api.post('/api/personality/ai-generate-first-message', {
                            name: this.personality.name,
                            basicInfo: this.personality.basicInfo || '',
                            personality: this.personality.personality || '',
                            scenario: this.personality.scenario || '',
                        });
                        if (res.data.success) {
                            this.personality.firstMessage = res.data.firstMessage;
                            this.personalityHasUnsavedChanges = true;
                            this.showToast('开场白已生成', 'success');
                        } else {
                            this.showToast(res.data.error || '生成失败', 'error');
                        }
                    } catch (e) {
                        console.error('生成开场白失败:', e);
                        this.showToast('生成失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                // Memory Functions
                async exportMemory() {
                    try {
                        const res = await api.get('/api/memory/export');
                        const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `memory_export_${new Date().toISOString().split('T')[0]}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                        this.showToast('记忆已导出', 'success');
                    } catch (e) {
                        this.showToast('导出失败', 'error');
                    }
                },
                
                async clearAllMemory() {
                    this.showConfirm({
                        title: '清空所有记忆',
                        message: '确定要清空所有记忆吗？此操作不可恢复。',
                        confirmText: '清空',
                        icon: 'fa-trash-alt',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete('/api/memory');
                                await this.loadMemory();
                                this.showToast('记忆已清空', 'success');
                            } catch (e) {
                                console.error('清空记忆失败:', e);
                                this.showToast('清空失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },
                
                async deleteMemory(id) {
                    this.showConfirm({
                        title: '删除记忆',
                        message: '确定要删除这条记忆吗？此操作不可恢复。',
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/memory/${id}`);
                                await this.loadMemory();
                                this.showToast('记忆已删除', 'success');
                            } catch (e) {
                                console.error('删除记忆失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                toggleMemoryExpand(id) {
                    this.expandedMemory = this.expandedMemory === id ? null : id;
                },

                getPriorityLabel(priority) {
                    const labels = {
                        high: '高优先级',
                        normal: '普通',
                        low: '低优先级'
                    };
                    return labels[priority] || '普通';
                },

                getSessionNameById(targetId) {
                    const session = this.sessions.find(s => s.qq_id === targetId || s.id === targetId);
                    return session ? session.name : targetId;
                },

                isMemoryExpired(mem) {
                    if (!mem.created_at || !mem.expire_days) return false;
                    const created = new Date(mem.created_at);
                    const now = new Date();
                    const diffDays = (now - created) / (1000 * 60 * 60 * 24);
                    return diffDays > mem.expire_days;
                },

                editMemory(mem) {
                    this.editingMemory = {
                        id: mem.id,
                        type: mem.type || 'long',
                        title: mem.title || mem.key || '',
                        summary: mem.summary || '',
                        content: mem.content || mem.value || '',
                        priority: mem.priority || 'normal',
                        expire_days: mem.expire_days || 7,
                        target_id: mem.target_id || ''
                    };
                    this.showAddMemoryModal = true;
                },

                openAddMemoryModal() {
                    this.resetEditingMemory();
                    // 优先使用当前登录用户名，其次使用会话ID
                    if (this.username) {
                        this.editingMemory.target_id = this.username;
                    } else if (this.currentSession) {
                        this.editingMemory.target_id = this.currentSession.qq_id || this.currentSession.id;
                    }
                    this.showAddMemoryModal = true;
                },

                async saveMemory() {
                    if (!this.editingMemory.title || !this.editingMemory.content) {
                        this.showToast('请填写标题和内容', 'warning');
                        return;
                    }

                    this.isLoading = true;
                    try {
                        const data = {
                            ...this.editingMemory,
                            updated_at: new Date().toISOString()
                        };

                        if (this.editingMemory.id) {
                            await api.put(`/api/memory/${this.editingMemory.id}`, data);
                            this.showToast('记忆已更新', 'success');
                        } else {
                            data.created_at = new Date().toISOString();
                            await api.post('/api/memory', data);
                            this.showToast('记忆已添加', 'success');
                        }

                        this.showAddMemoryModal = false;
                        this.resetEditingMemory();
                        await this.loadMemory();
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                resetEditingMemory() {
                    this.editingMemory = {
                        id: null,
                        type: 'long',
                        title: '',
                        summary: '',
                        content: '',
                        priority: 'normal',
                        expire_days: 7,
                        target_id: ''
                    };
                },

                async promoteToLongTerm(mem) {
                    this.showConfirm({
                        title: '转换为长期记忆',
                        message: '确定要将这条短期记忆转为长期记忆吗？',
                        confirmText: '转换',
                        icon: 'fa-arrow-up',
                        iconColor: 'var(--accent)',
                        danger: false,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.put(`/api/memory/${mem.id}`, {
                                    ...mem,
                                    type: 'long',
                                    priority: 'normal',
                                    updated_at: new Date().toISOString()
                                });
                                await this.loadMemory();
                                this.showToast('已转为长期记忆', 'success');
                            } catch (e) {
                                console.error('转换失败:', e);
                                this.showToast('转换失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },
                
                // Knowledge Functions
                toggleDocDropdown(docId) {
                    this.activeDocDropdown = this.activeDocDropdown === docId ? null : docId;
                },

                openKnowledgeModal(doc = null) {
                    if (doc) {
                        this.editingKnowledge = doc;
                        this.knowledgeForm = {
                            id: doc.id,
                            name: doc.name,
                            type: doc.type,
                            content: doc.content || '',
                            description: doc.description || ''
                        };
                    } else {
                        this.editingKnowledge = null;
                        this.knowledgeForm = {
                            id: null,
                            name: '',
                            type: 'txt',
                            content: '',
                            description: ''
                        };
                    }
                    this.showKnowledgeModal = true;
                },

                async saveKnowledge() {
                    this.isLoading = true;
                    try {
                        const data = {
                            name: this.knowledgeForm.name,
                            type: this.knowledgeForm.type,
                            size: this.knowledgeForm.content.length,
                            content: this.knowledgeForm.content,
                            description: this.knowledgeForm.description,
                            created_at: new Date().toISOString()
                        };

                        if (this.editingKnowledge) {
                            await api.put(`/api/knowledge/${this.editingKnowledge.id}`, data);
                            this.showToast('文档已更新', 'success');
                        } else {
                            await api.post('/api/knowledge', data);
                            this.showToast('文档已添加', 'success');
                        }

                        this.showKnowledgeModal = false;
                        await this.loadKnowledge();
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                viewKnowledgeDetail(doc) {
                    this.viewingKnowledge = doc;
                    this.showKnowledgeDetailModal = true;
                },

                async indexKnowledge(doc) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/knowledge/${doc.id}/index`);
                        await this.loadKnowledge();
                        this.showToast('索引建立成功', 'success');
                    } catch (e) {
                        this.showToast('索引建立失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async batchIndexKnowledge() {
                    const unindexed = this.knowledgeDocs.filter(d => !d.indexed);
                    if (unindexed.length === 0) return;

                    this.isLoading = true;
                    try {
                        const res = await api.post('/api/knowledge/rebuild');
                        await this.loadKnowledge();
                        this.showToast(`批量索引完成: ${res.data.rebuilt_documents || 0} 个文档`, 'success');
                    } catch (e) {
                        // Fallback: 逐个索引
                        for (const doc of unindexed) {
                            await api.post(`/api/knowledge/${doc.id}/index`);
                        }
                        await this.loadKnowledge();
                        this.showToast(`已建立 ${unindexed.length} 个文档的索引`, 'success');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async importKnowledge() {
                    this.isLoading = true;
                    this.importResult = null;
                    try {
                        let documents = [];
                        if (this.importMode === 'json') {
                            // 如果有文件内容但没有 text，尝试从文件读取
                            let text = this.importForm.text;
                            if (!text && this.importForm.fileName) {
                                this.showToast('请等待文件读取完成', 'warning');
                                this.isLoading = false;
                                return;
                            }
                            const parsed = JSON.parse(text);
                            // 导出格式: { version, exported_at, total, documents: [...] }
                            // 直接数组格式: [...]
                            // 单文档格式: { name, content, ... }
                            if (parsed.documents && Array.isArray(parsed.documents)) {
                                documents = parsed.documents;
                            } else if (Array.isArray(parsed)) {
                                documents = parsed;
                            } else {
                                documents = [parsed];
                            }
                        } else {
                            // 文本模式：每篇文档以空行分隔
                            const blocks = this.importForm.text.split(/\n\s*\n/);
                            for (const block of blocks) {
                                const lines = block.trim().split('\n');
                                if (lines.length === 0) continue;
                                const name = lines[0].trim();
                                const content = lines.slice(1).join('\n').trim();
                                if (name && content) {
                                    documents.push({ name, content });
                                }
                            }
                        }

                        const res = await api.post('/api/knowledge/batch', {
                            documents: documents.map(item => ({
                                title: item.name || item.title,
                                content: item.content,
                                source: item.source || '',
                                tags: item.tags || []
                            }))
                        });

                        this.importResult = res.data;
                        if (res.data.imported > 0) {
                            this.showToast(`成功导入 ${res.data.imported} 篇文档`, 'success');
                            await this.loadKnowledge();
                            this.importForm.text = '';
                            this.importForm.fileName = '';
                            this.importForm.fileSize = '';
                        }
                        if (res.data.failed > 0) {
                            this.showToast(`导入完成，但有 ${res.data.failed} 篇失败`, 'warning');
                        }
                    } catch (e) {
                        if (e.response && e.response.data && e.response.data.error) {
                            this.showToast('导入失败: ' + e.response.data.error, 'error');
                        } else {
                            this.showToast('导入失败: ' + (e.message || '未知错误'), 'error');
                        }
                    } finally {
                        this.isLoading = false;
                    }
                },

                testImport() {
                    try {
                        let docs = [];
                        if (this.importMode === 'json') {
                            if (!this.importForm.text && this.importForm.fileName) {
                                this.showToast('请等待文件读取完成', 'warning');
                                return;
                            }
                            const parsed = JSON.parse(this.importForm.text);
                            if (parsed.documents && Array.isArray(parsed.documents)) {
                                docs = parsed.documents;
                            } else if (Array.isArray(parsed)) {
                                docs = parsed;
                            } else {
                                docs = [parsed];
                            }
                        } else {
                            const blocks = this.importForm.text.split(/\n\s*\n/);
                            for (const block of blocks) {
                                const lines = block.trim().split('\n');
                                if (lines.length === 0) continue;
                                docs.push({ name: lines[0].trim(), content: lines.slice(1).join('\n').trim() });
                            }
                        }
                        const valid = docs.filter(d => d.name || d.title || d.content);
                        this.importResult = { imported: valid.length, failed: docs.length - valid.length, errors: [] };
                        this.showToast(`预览: 将导入 ${valid.length} 篇文档`, 'success');
                    } catch (e) {
                        this.showToast('预览失败: ' + e.message, 'error');
                    }
                },

                handleImportFileSelect(event) {
                    const file = event.target.files[0];
                    if (!file) return;
                    this.loadImportFile(file);
                },

                handleImportFileDrop(event) {
                    const file = event.dataTransfer.files[0];
                    if (!file) return;
                    this.loadImportFile(file);
                },

                loadImportFile(file) {
                    const reader = new FileReader();
                    reader.onload = (e) => {
                        this.importForm.text = e.target.result;
                        this.importForm.fileName = file.name;
                        this.importForm.fileSize = this.formatFileSize(file.size);
                        // 自动切换到对应模式
                        if (file.name.endsWith('.json')) {
                            this.importMode = 'json';
                        } else {
                            this.importMode = 'text';
                        }
                    };
                    reader.onerror = () => {
                        this.showToast('文件读取失败', 'error');
                    };
                    reader.readAsText(file);
                },

                clearImportFile() {
                    this.importForm.text = '';
                    this.importForm.fileName = '';
                    this.importForm.fileSize = '';
                },

                exportKnowledge(doc) {
                    const blob = new Blob([doc.content || ''], { type: 'text/plain' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `${doc.name}.${doc.type}`;
                    a.click();
                    URL.revokeObjectURL(url);
                    this.showToast('文档已导出', 'success');
                },

                async exportAllKnowledge() {
                    try {
                        const res = await api.get('/api/knowledge/export');
                        if (!res.data.success) {
                            this.showToast('导出失败: ' + res.data.error, 'error');
                            return;
                        }
                        const exportData = {
                            version: res.data.version || '1.0',
                            exported_at: res.data.exported_at,
                            total: res.data.total,
                            documents: res.data.documents
                        };
                        const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `knowledge_backup_${new Date().toISOString().slice(0, 10)}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                        this.showToast(`已导出 ${res.data.total} 篇文档`, 'success');
                    } catch (e) {
                        this.showToast('导出失败', 'error');
                    }
                },

                async deleteKnowledge(doc) {
                    this.showConfirm({
                        title: '删除文档',
                        message: `确定要删除文档"${doc.name}"吗？此操作不可恢复。`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/knowledge/${doc.id}`);
                                this.activeDocDropdown = null;
                                await this.loadKnowledge();
                                this.showToast('文档已删除', 'success');
                            } catch (e) {
                                console.error('删除文档失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                formatTimeAgo(timestamp) {
                    if (!timestamp) return '未知';
                    const date = new Date(timestamp);
                    const now = new Date();
                    const diff = now - date;
                    const minutes = Math.floor(diff / 60000);
                    const hours = Math.floor(diff / 3600000);
                    const days = Math.floor(diff / 86400000);

                    if (minutes < 1) return '刚刚';
                    if (minutes < 60) return `${minutes} 分钟前`;
                    if (hours < 24) return `${hours} 小时前`;
                    if (days < 30) return `${days} 天前`;
                    return date.toLocaleDateString();
                },
                
                getFileIcon(type) {
                    const icons = {
                        pdf: 'fas fa-file-pdf',
                        md: 'fas fa-file-alt',
                        txt: 'fas fa-file-text',
                        doc: 'fas fa-file-word',
                        docx: 'fas fa-file-word'
                    };
                    return icons[type] || 'fas fa-file';
                },
                
                formatFileSize(bytes) {
                    if (bytes < 1024) return bytes + ' B';
                    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
                    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
                },
                
                formatJson(obj) {
                    if (!obj) return '';
                    try {
                        return JSON.stringify(obj, null, 2);
                    } catch (e) {
                        return String(obj);
                    }
                },
                
                normalizeDisplayText(text) {
                    if (typeof text !== 'string' || !text) return text || '';

                    const replacements = {
                        '\xf0\x9f\x94\x8d \xe6\x90\x9c\xe7\xb4\xa2\xe6\x96\xb0\xe9\x97\xbb': '\uD83D\uDD0D \u641C\u7D22\u65B0\u95FB',
                        '\u9983\u6533 \u93bc\u6ec5\u50a8\u93c2\u4f34\u6908': '\uD83D\uDD0D \u641C\u7D22\u65B0\u95FB',
                        '\xf0\x9f\x8c\xa4\xef\xb8\x8f \xe6\x9f\xa5\xe8\xaf\xa2\xe5\xa4\xa9\xe6\xb0\x94': '\uD83C\uDF24\uFE0F \u67E5\u8BE2\u5929\u6C14',
                        '\u9983\u5c0b\u9514 \u93cc\u30e8\u3220\u3049\u59d8': '\uD83C\uDF24\uFE0F \u67E5\u8BE2\u5929\u6C14',
                        '\xf0\x9f\x8c\x90 \xe7\xbd\x91\xe9\xa1\xb5\xe6\x90\x9c\xe7\xb4\xa2': '\uD83C\uDF10 \u7F51\u9875\u641C\u7D22',
                        '\u9983\u5bea \u7f03\u6226\u3009\u93bc\u6ec5\u50a8': '\uD83C\uDF10 \u7F51\u9875\u641C\u7D22',
                        '\xf0\x9f\x95\x90 \xe8\x8e\xb7\xe5\x8f\x96\xe6\x97\xb6\xe9\x97\xb4': '\uD83D\uDD50 \u83B7\u53D6\u65F6\u95F4',
                        '\u9983\u6672 \u947e\u5cf0\u5f47\u93c3\u5815\u68ff': '\uD83D\uDD50 \u83B7\u53D6\u65F6\u95F4',
                        '\xf0\x9f\x93\xa1 \xe8\x8e\xb7\xe5\x8f\x96\xe7\xbd\x91\xe9\xa1\xb5': '\uD83D\uDCE1 \u83B7\u53D6\u7F51\u9875',
                        '\u9983\u6457 \u947e\u5cf0\u5f47\u7f03\u6226\u3009': '\uD83D\uDCE1 \u83B7\u53D6\u7F51\u9875',
                        '\xf0\x9f\x96\xbc\xef\xb8\x8f \xe7\x90\x86\xe8\xa7\xa3\xe5\x9b\xbe\xe7\x89\x87': '\uD83D\uDDBC\uFE0F \u7406\u89E3\u56FE\u7247',
                        '\u9983\u67e4\u9514 \u941e\u55da\u0412\u9365\u5267\u5896': '\uD83D\uDDBC\uFE0F \u7406\u89E3\u56FE\u7247',
                        '\xf0\x9f\x93\x9d \xe5\x88\x9b\xe5\xbb\xba\xe6\x96\x87\xe4\xbb\xb6': '\uD83D\uDCDD \u521B\u5EFA\u6587\u4EF6',
                        '\u9983\u6451 \u9352\u6d98\u7f13\u93c2\u56e6\u6b22': '\uD83D\uDCDD \u521B\u5EFA\u6587\u4EF6',
                        '\xf0\x9f\x93\x96 \xe8\xaf\xbb\xe5\x8f\x96\xe6\x96\x87\xe4\xbb\xb6': '\uD83D\uDCD6 \u8BFB\u53D6\u6587\u4EF6',
                        '\u9983\u6449 \u7487\u8bf2\u5f47\u93c2\u56e6\u6b22': '\uD83D\uDCD6 \u8BFB\u53D6\u6587\u4EF6',
                        '\xe2\x9c\x8f\xef\xb8\x8f \xe7\xbc\x96\xe8\xbe\x91\xe6\x96\x87\xe4\xbb\xb6': '\u270F\uFE0F \u7F16\u8F91\u6587\u4EF6',
                        '\u9241\u5fe5\u7b0d \u7f02\u682c\u7deb\u93c2\u56e6\u6b22': '\u270F\uFE0F \u7F16\u8F91\u6587\u4EF6',
                        '\xf0\x9f\x97\x91\xef\xb8\x8f \xe5\x88\xa0\xe9\x99\xa4\xe6\x96\x87\xe4\xbb\xb6': '\uD83D\uDDD1\uFE0F \u5220\u9664\u6587\u4EF6',
                        '\u9983\u68cf\u9514 \u9352\u72bb\u6ace\u93c2\u56e6\u6b22': '\uD83D\uDDD1\uFE0F \u5220\u9664\u6587\u4EF6',
                        '\xf0\x9f\x93\x81 \xe5\x88\x97\xe5\x87\xba\xe6\x96\x87\xe4\xbb\xb6': '\uD83D\uDCC1 \u5217\u51FA\u6587\u4EF6',
                        '\u9983\u6427 \u9352\u6940\u56ad\u93c2\u56e6\u6b22': '\uD83D\uDCC1 \u5217\u51FA\u6587\u4EF6',
                        '\xf0\x9f\x8c\xb3 \xe6\x98\xbe\xe7\xa4\xba\xe7\x9b\xae\xe5\xbd\x95\xe6\xa0\x91': '\uD83C\uDF33 \u663E\u793A\u76EE\u5F55\u6811',
                        '\u9983\u5c26 \u93c4\u5267\u305a\u9429\u8930\u66df\u7232': '\uD83C\uDF33 \u663E\u793A\u76EE\u5F55\u6811',
                        '\xf0\x9f\x93\xa4 \xe5\x8f\x91\xe9\x80\x81\xe6\x96\x87\xe4\xbb\xb6': '\uD83D\uDCE4 \u53D1\u9001\u6587\u4EF6',
                        '\u9983\u645b \u9359\u6226\u4f79\u6783\u6d60': '\uD83D\uDCE4 \u53D1\u9001\u6587\u4EF6',
                        '\xe2\x9c\x85 \xe6\xb7\xbb\xe5\x8a\xa0\xe5\xbe\x85\xe5\x8a\x9e': '\u2705 \u6DFB\u52A0\u5F85\u529E',
                        '\u9241 \u5a23\u8bf2\u59de\u5bf0\u546d\u59d9': '\u2705 \u6DFB\u52A0\u5F85\u529E',
                        '\xf0\x9f\x93\x8b \xe5\x88\x97\xe5\x87\xba\xe5\xbe\x85\xe5\x8a\x9e': '\uD83D\uDCCB \u5217\u51FA\u5F85\u529E',
                        '\u9983\u6435 \u9352\u6940\u56ad\u5bf0\u546d\u59d9': '\uD83D\uDCCB \u5217\u51FA\u5F85\u529E',
                        '\xe2\x9c\x93 \xe5\xae\x8c\xe6\x88\x90\xe5\xbe\x85\xe5\x8a\x9e': '\u2713 \u5B8C\u6210\u5F85\u529E',
                        '\u9241 \u7039\u5c7e\u579a\u5bf0\u546d\u59d9': '\u2713 \u5B8C\u6210\u5F85\u529E',
                        '\xf0\x9f\x97\x91\xef\xb8\x8f \xe5\x88\xa0\xe9\x99\xa4\xe5\xbe\x85\xe5\x8a\x9e': '\uD83D\uDDD1\uFE0F \u5220\u9664\u5F85\u529E',
                        '\u9983\u68cf\u9514 \u9352\u72bb\u6ace\u5bf0\u546d\u59d9': '\uD83D\uDDD1\uFE0F \u5220\u9664\u5F85\u529E',
                        '\xf0\x9f\xa7\xb9 \xe6\xb8\x85\xe7\xa9\xba\xe5\xbe\x85\xe5\x8a\x9e': '\uD83E\uDDF9 \u6E05\u7A7A\u5F85\u529E',
                        '\u9983\u0427 \u5a13\u546f\u2516\u5bf0\u546d\u59d9': '\uD83E\uDDF9 \u6E05\u7A7A\u5F85\u529E'
                    };

                    let normalized = text;
                    Object.entries(replacements).forEach(([bad, good]) => {
                        if (normalized.includes(bad)) {
                            normalized = normalized.replaceAll(bad, good);
                        }
                    });
                    return normalized;
                },
                
                viewStepDetail(step) {
                    const fullResult = step.full_result || null;
                    this.stepDetailData = {
                        name: this.normalizeDisplayText(step.name || ''),
                        detail: this.normalizeDisplayText(step.detail || ''),
                        arguments: step.arguments || null,
                        full_result: fullResult,
                        thinking_content: step.thinking_content || null,
                        file_changes: Array.isArray(fullResult?.file_changes) ? fullResult.file_changes : []
                    };
                    this.showStepDetailModal = true;
                },

                viewFileChangeDetail(change) {
                    this.stepDetailData = {
                        name: this.normalizeDisplayText(change.path || '文件变更'),
                        detail: this.normalizeDisplayText(this.getFileChangeLabel(change.action)),
                        arguments: null,
                        full_result: null,
                        thinking_content: null,
                        file_changes: [change]
                    };
                    this.showStepDetailModal = true;
                },

                hasRenderableFileChange(change) {
                    if (!change || change.preview_too_large) return false;
                    return !!(change.diff_preview || change.before_preview || change.after_preview);
                },

                getFileChangeLabel(action) {
                    const labels = {
                        created: '新增文件',
                        modified: '修改文件',
                        deleted: '删除文件'
                    };
                    return labels[action] || action || '文件变更';
                },

                getFileChangeColor(action) {
                    const colors = {
                        created: 'var(--success)',
                        modified: 'var(--warning)',
                        deleted: 'var(--error)'
                    };
                    return colors[action] || 'var(--text-secondary)';
                },

                getDiffLineStyle(line) {
                    if (!line) return {};
                    if (line.startsWith('+') && !line.startsWith('+++')) {
                        return { color: 'var(--success)', background: 'rgba(34, 197, 94, 0.08)' };
                    }
                    if (line.startsWith('-') && !line.startsWith('---')) {
                        return { color: 'var(--error)', background: 'rgba(239, 68, 68, 0.08)' };
                    }
                    if (line.startsWith('@@')) {
                        return { color: 'var(--accent-primary)', background: 'rgba(59, 130, 246, 0.08)' };
                    }
                    return {};
                },
                
                // AI Config Functions
                getBaseUrlPlaceholder() {
                    const placeholders = {
                        openai: 'https://api.openai.com/v1',
                        anthropic: 'https://api.anthropic.com',
                        google: 'https://generativelanguage.googleapis.com',
                        azure: 'https://{your-resource}.openai.azure.com',
                        siliconflow: 'https://api.siliconflow.cn',
                        deepseek: 'https://api.deepseek.com',
                        custom: 'https://api.example.com/v1'
                    };
                    return placeholders[this.aiConfig.provider] || placeholders.custom;
                },

                onProviderChange() {
                    // 自动填充默认模型
                    const defaultModels = {
                        openai: 'gpt-4',
                        anthropic: 'claude-3-sonnet-20240229',
                        google: 'gemini-pro',
                        azure: 'gpt-4',
                        siliconflow: 'Qwen/Qwen2.5-72B-Instruct',
                        deepseek: 'deepseek-chat',
                        custom: 'custom'
                    };
                    this.aiConfig.model = defaultModels[this.aiConfig.provider] || 'custom';
                    this.aiConfig.custom_model = '';
                    this.aiConfig.provider_type = this.getProviderTypeByProvider(this.aiConfig.provider);
                    this.applyProviderCapabilities(this.aiConfig);
                    this.currentPreset = '';
                },

                onModelProviderChange() {
                    const defaultModels = {
                        openai: 'gpt-4',
                        anthropic: 'claude-3-sonnet-20240229',
                        google: 'gemini-pro',
                        azure: 'gpt-4',
                        siliconflow: 'Qwen/Qwen2.5-72B-Instruct',
                        deepseek: 'deepseek-chat',
                        custom: 'custom'
                    };
                    this.modelForm.model = defaultModels[this.modelForm.provider] || 'custom';
                    this.modelForm.provider_type = this.getProviderTypeByProvider(this.modelForm.provider);
                    this.applyProviderCapabilities(this.modelForm);
                },

                async onApiKeySelectChange() {
                    if (this.modelForm.selectedApiKeyId) {
                        const keyValue = await this.getApiKeyValue(this.modelForm.selectedApiKeyId);
                        if (keyValue) {
                            this.modelForm.api_key = keyValue;
                        }
                    }
                },

                onModelPurposeChange() {
                    // 根据用途应用默认配置
                    const purposeDefaults = {
                        chat: {
                            temperature: 0.7,
                            max_tokens: 2000,
                            supports_tools: true,
                            supports_reasoning: true,
                            supports_stream: true,
                            system_prompt: ''
                        },
                        vision: {
                            temperature: 0.5,
                            max_tokens: 1000,
                            supports_tools: false,
                            supports_reasoning: false,
                            supports_stream: true,
                            system_prompt: '请详细描述这张图片的内容。'
                        },
                        video: {
                            temperature: 0.5,
                            max_tokens: 1500,
                            supports_tools: false,
                            supports_reasoning: false,
                            supports_stream: true,
                            system_prompt: '请分析这个视频的内容。'
                        },
                        tts: {
                            supports_tools: false,
                            supports_reasoning: false,
                            supports_stream: false,
                            voice: 'default',
                            speed: 1.0,
                            pitch: 1.0,
                            volume: 1.0
                        },
                        stt: {
                            supports_tools: false,
                            supports_reasoning: false,
                            supports_stream: false,
                            language: 'zh'
                        },
                        embedding: {
                            supports_tools: false,
                            supports_reasoning: false,
                            supports_stream: false,
                            dimensions: 1536
                        }
                    };
                    
                    const defaults = purposeDefaults[this.modelForm.purpose];
                    if (defaults) {
                        Object.assign(this.modelForm, defaults);
                    }
                    
                    // 更新配置名称
                    if (!this.editingModel) {
                        const purposeNames = {
                            chat: '对话模型',
                            vision: '图片理解模型',
                            video: '视频理解模型',
                            tts: 'TTS语音合成',
                            stt: 'STT语音识别',
                            embedding: '向量嵌入模型'
                        };
                        this.modelForm.name = `新${purposeNames[this.modelForm.purpose]}配置`;
                    }
                },

                // 打开用途配置
                openPurposeConfig(purpose) {
                    this.editingPurpose = purpose;
                    this.showPurposeConfigModal = true;
                },

                // 关闭用途配置弹窗
                closePurposeConfigModal() {
                    this.showPurposeConfigModal = false;
                    this.editingPurpose = null;
                },

                // 获取指定用途的模型列表
                getModelsByPurpose(purpose) {
                    return this.aiModels.filter(m => (m.purpose || 'chat') === purpose);
                },

                // 应用指定用途的模型
                async applyPurposeModel(model) {
                    try {
                        const purpose = model.purpose || 'chat';
                        const res = await api.post(`/api/ai-models/${model.id}/apply`, {
                            purpose: purpose
                        });
                        if (res.data.success) {
                            // 只有对话模型才更新全局activeModelId
                            if (purpose === 'chat') {
                                this.activeModelId = model.id;
                            }
                            await this.loadActiveModelsByPurpose();
                            this.showToast(res.data.message || `已应用 ${model.name}`, 'success');
                        }
                    } catch (e) {
                        this.showToast('应用模型失败: ' + (e.response?.data?.message || e.message), 'error');
                    }
                },

                // 加载各用途的活跃模型
                async loadActiveModelsByPurpose() {
                    try {
                        const res = await api.get('/api/ai-models/active-by-purpose');
                        if (res.data.success) {
                            this.activeModelsByPurpose = res.data.active_models;
                        }
                    } catch (e) {
                        console.error('加载活跃模型失败:', e);
                    }
                },

                getProviderTypeByProvider(provider) {
                    const mapping = {
                        openai: 'openai_compatible',
                        azure: 'openai_compatible',
                        deepseek: 'openai_compatible',
                        custom: 'openai_compatible',
                        siliconflow: 'siliconflow',
                        anthropic: 'anthropic',
                        google: 'google'
                    };
                    return mapping[provider] || 'openai_compatible';
                },

                getResolvedModelValue(target) {
                    const customModel = typeof target?.custom_model === 'string'
                        ? target.custom_model.trim()
                        : '';
                    return customModel || target?.model || '';
                },

                syncProviderMetadata(target) {
                    target.provider_type = target.provider_type || this.getProviderTypeByProvider(target.provider);
                    if (!target.supports_stream) {
                        target.stream = false;
                    }
                },

                applyProviderCapabilities(target) {
                    const providerType = target.provider_type || this.getProviderTypeByProvider(target.provider);
                    const capabilityMap = {
                        openai_compatible: { supports_tools: true, supports_reasoning: true, supports_stream: true },
                        siliconflow: { supports_tools: true, supports_reasoning: true, supports_stream: true },
                        minimax: { supports_tools: true, supports_reasoning: true, supports_stream: true },
                        anthropic: { supports_tools: false, supports_reasoning: true, supports_stream: true },
                        google: { supports_tools: false, supports_reasoning: true, supports_stream: true }
                    };
                    const defaults = capabilityMap[providerType] || capabilityMap.openai_compatible;
                    target.provider_type = providerType;
                    target.supports_tools = defaults.supports_tools;
                    target.supports_reasoning = defaults.supports_reasoning;
                    target.supports_stream = defaults.supports_stream;
                    if (!target.supports_stream) {
                        target.stream = false;
                    }
                },

                applyAIPreset(preset) {
                    this.aiConfig.provider = preset.provider;
                    this.aiConfig.model = preset.model;
                    this.aiConfig.custom_model = '';
                    this.aiConfig.base_url = preset.base_url;
                    this.aiConfig.provider_type = this.getProviderTypeByProvider(preset.provider);
                    this.applyProviderCapabilities(this.aiConfig);
                    this.currentPreset = preset.name;
                    this.showToast(`已应用 ${preset.name} 配置`, 'success');
                },

                applyModelPresetToForm(preset) {
                    this.modelForm.provider = preset.provider;
                    this.modelForm.model = preset.model;
                    this.modelForm.base_url = preset.base_url;
                    this.modelForm.provider_type = this.getProviderTypeByProvider(preset.provider);
                    this.applyProviderCapabilities(this.modelForm);
                    if (!this.modelForm.name || this.modelForm.name === '新配置') {
                        this.modelForm.name = `${preset.name} 配置`;
                    }
                },

                resetAIParams() {
                    this.aiConfig.temperature = 0.7;
                    this.aiConfig.max_tokens = 2000;
                    this.aiConfig.top_p = 0.9;
                    this.aiConfig.frequency_penalty = 0;
                    this.aiConfig.presence_penalty = 0;
                    this.showToast('参数已重置为默认值', 'success');
                },

                async testAIConnection() {
                    if (!this.aiConfig.api_key) {
                        this.showToast('请先输入 API Key', 'warning');
                        return;
                    }
                    const modelToTest = this.getResolvedModelValue(this.aiConfig);
                    if (!modelToTest) {
                        this.showToast('请先选择或输入模型', 'warning');
                        return;
                    }
                    this.isTesting = true;
                    try {
                        const res = await api.post('/api/ai-config/test', {
                            provider: this.aiConfig.provider,
                            provider_type: this.aiConfig.provider_type,
                            api_key: this.aiConfig.api_key,
                            base_url: this.aiConfig.base_url,
                            model: modelToTest
                        });
                        if (res.data.success) {
                            this.aiStatus = { text: '连接正常', class: 'badge-success' };
                            this.showToast('连接测试成功', 'success');
                        } else {
                            this.aiStatus = { text: '连接失败', class: 'badge-danger' };
                            this.showToast(res.data.message || '连接测试失败', 'error');
                        }
                    } catch (e) {
                        this.aiStatus = { text: '连接失败', class: 'badge-danger' };
                        this.showToast('连接测试失败: ' + (e.response?.data?.message || e.message), 'error');
                    } finally {
                        this.isTesting = false;
                    }
                },

                async saveAIConfig() {
                    this.isLoading = true;
                    try {
                        const payload = {
                            ...this.aiConfig,
                            model: this.getResolvedModelValue(this.aiConfig)
                        };
                        this.syncProviderMetadata(payload);
                        await api.put('/api/ai-config', payload);
                        this.aiConfig = { ...this.aiConfig, ...payload };
                        this.aiStatus = { text: '已配置', class: 'badge-success' };
                        this.showToast('AI 配置已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // 多模型配置管理方法
                openModelManager() {
                    this.showModelManager = true;
                    this.loadAIModels();
                },

                closeModelManager() {
                    this.showModelManager = false;
                },

                openModelEditModal(model = null) {
                    // 加载API Keys列表
                    this.loadApiKeys();

                    if (model) {
                        this.editingModel = model;
                        this.modelForm = {
                            ...model,
                            // 确保purpose字段存在
                            purpose: model.purpose || 'chat',
                            // 确保特有配置字段存在
                            voice: model.voice || 'default',
                            speed: model.speed || 1.0,
                            pitch: model.pitch || 1.0,
                            volume: model.volume || 1.0,
                            language: model.language || 'zh',
                            dimensions: model.dimensions || 1536,
                            // API Key选择
                            selectedApiKeyId: ''
                        };
                        this.modelForm.provider_type = this.modelForm.provider_type || this.getProviderTypeByProvider(this.modelForm.provider);
                        if (typeof this.modelForm.supports_tools !== 'boolean' ||
                            typeof this.modelForm.supports_reasoning !== 'boolean' ||
                            typeof this.modelForm.supports_stream !== 'boolean') {
                            this.applyProviderCapabilities(this.modelForm);
                        }
                    } else {
                        this.editingModel = null;
                        // 如果当前正在编辑某个用途，则默认使用该用途
                        const defaultPurpose = this.editingPurpose || 'chat';
                        const purposeNames = {
                            chat: '对话模型',
                            vision: '图片理解模型',
                            video: '视频理解模型',
                            tts: 'TTS语音合成',
                            stt: 'STT语音识别',
                            embedding: '向量嵌入模型'
                        };
                        this.modelForm = {
                            id: null,
                            name: `新${purposeNames[defaultPurpose]}配置`,
                            purpose: defaultPurpose,
                            provider: 'openai',
                            provider_type: 'openai_compatible',
                            api_key: '',
                            selectedApiKeyId: '',
                            base_url: '',
                            model: 'gpt-4',
                            enabled: true,
                            supports_tools: true,
                            supports_reasoning: true,
                            supports_stream: true,
                            temperature: 0.7,
                            max_tokens: 2000,
                            top_p: 0.9,
                            frequency_penalty: 0,
                            presence_penalty: 0,
                            system_prompt: '',
                            timeout: 60,
                            retry_count: 3,
                            stream: true,
                            enable_memory: true,
                            image_model: '',
                            search_api_key: '',
                            embedding_model: '',
                            max_context_length: 100000,
                            // TTS/STT/Embedding特有配置
                            voice: 'default',
                            speed: 1.0,
                            pitch: 1.0,
                            volume: 1.0,
                            language: 'zh',
                            dimensions: 1536
                        };
                        // 应用该用途的默认配置
                        this.onModelPurposeChange();
                    }
                    this.showModelEditModal = true;
                },

                closeModelEditModal() {
                    this.showModelEditModal = false;
                    this.editingModel = null;
                },

                async saveModel() {
                    this.isLoading = true;
                    try {
                        this.applyProviderCapabilities(this.modelForm);
                        if (this.editingModel) {
                            // 更新现有配置
                            await api.put(`/api/ai-models/${this.modelForm.id}`, this.modelForm);
                            this.showToast('模型配置已更新', 'success');
                        } else {
                            // 创建新配置
                            await api.post('/api/ai-models', this.modelForm);
                            this.showToast('模型配置已创建', 'success');
                        }
                        await this.loadAIModels();
                        this.closeModelEditModal();
                    } catch (e) {
                        this.showToast('保存失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async deleteModel(model) {
                    this.showConfirm({
                        title: '删除模型配置',
                        message: `确定要删除模型配置"${model.name}"吗？此操作不可恢复。`,
                        confirmText: '删除',
                        icon: 'fa-trash',
                        iconColor: 'var(--error)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.delete(`/api/ai-models/${model.id}`);
                                await this.loadAIModels();
                                this.showToast('模型配置已删除', 'success');
                            } catch (e) {
                                console.error('删除模型配置失败:', e);
                                this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async cloneModel(model) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/ai-models/${model.id}/clone`);
                        await this.loadAIModels();
                        this.showToast('模型配置已复制', 'success');
                    } catch (e) {
                        this.showToast('复制失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async toggleModel(model) {
                    try {
                        await api.post(`/api/ai-models/${model.id}/toggle`);
                        model.enabled = !model.enabled;
                        this.showToast(`模型配置已${model.enabled ? '启用' : '禁用'}`, 'success');
                    } catch (e) {
                        this.showToast('操作失败', 'error');
                    }
                },

                async applyModel(model) {
                    this.isLoading = true;
                    try {
                        await api.post(`/api/ai-models/${model.id}/apply`);
                        this.activeModelId = model.id;
                        // 更新当前AI配置（以模型数据为准）
                        this.aiConfig = { ...this.aiConfig, ...model };
                        this.syncProviderMetadata(this.aiConfig);
                        this.updateContextStats();
                        this.showToast(`已应用模型配置: ${model.name}`, 'success');
                    } catch (e) {
                        this.showToast('应用失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async testModel(model) {
                    this.isTesting = true;
                    try {
                        const res = await api.post(`/api/ai-models/${model.id}/test`);
                        if (res.data.success) {
                            this.showToast('连接测试成功', 'success');
                        } else {
                            this.showToast(res.data.message || '连接测试失败', 'error');
                        }
                    } catch (e) {
                        this.showToast('测试失败: ' + (e.response?.data?.message || e.message), 'error');
                    } finally {
                        this.isTesting = false;
                    }
                },

                async deleteModel(model) {
                    // 确认对话框
                    if (!confirm(`确定要删除配置 "${model.name}" 吗？此操作不可恢复。`)) {
                        return;
                    }
                    
                    this.isDeleting = true;
                    try {
                        const res = await api.delete(`/api/ai-models/${model.id}`);
                        if (res.data.success) {
                            // 从列表中移除
                            this.aiModels = this.aiModels.filter(m => m.id !== model.id);
                            this.showToast('配置已删除', 'success');
                            // 刷新各用途的活跃模型状态
                            await this.loadActiveModelsByPurpose();
                        } else {
                            this.showToast(res.data.error || '删除失败', 'error');
                        }
                    } catch (e) {
                        this.showToast('删除失败: ' + (e.response?.data?.error || e.message), 'error');
                    } finally {
                        this.isDeleting = false;
                    }
                },

                getProviderIcon(provider) {
                    const icons = {
                        openai: 'fas fa-robot',
                        anthropic: 'fas fa-cloud',
                        google: 'fas fa-gem',
                        azure: 'fas fa-windows',
                        siliconflow: 'fas fa-microchip',
                        deepseek: 'fas fa-water',
                        custom: 'fas fa-cog'
                    };
                    return icons[provider] || 'fas fa-robot';
                },

                getProviderGlyph(provider) {
                    const glyphs = {
                        openai: '◎',
                        anthropic: '◈',
                        google: '✦',
                        azure: '⬢',
                        siliconflow: '◉',
                        deepseek: '≈',
                        custom: '◌'
                    };
                    return glyphs[provider] || '◌';
                },

                getProviderLabel(provider) {
                    const labels = {
                        openai: 'OpenAI',
                        anthropic: 'Anthropic',
                        google: 'Google',
                        azure: 'Azure',
                        siliconflow: 'SiliconFlow',
                        deepseek: 'DeepSeek',
                        custom: '自定义'
                    };
                    return labels[provider] || provider;
                },

                // Token Functions
                async refreshLogs() {
                    await this.loadLogs();
                    this.showToast('日志已刷新', 'success');
                },
                
                async clearLogs() {
                    this.isLoading = true;
                    try {
                        await api.delete('/api/logs');
                        await this.loadLogs();
                        this.showToast('日志已清空', 'success');
                    } catch (e) {
                        this.showToast('清空失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },
                
                getLogColor(level) {
                    const colors = {
                        info: 'var(--text-secondary)',
                        warning: 'var(--warning)',
                        error: 'var(--danger)'
                    };
                    return colors[level] || 'var(--text-primary)';
                },
                
                // Settings Functions
                async saveSettings() {
                    this.isLoading = true;
                    try {
                        await api.put('/api/settings', this.settings);
                        this.showToast('设置已保存', 'success');
                    } catch (e) {
                        this.showToast('保存失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                // Debug Console Functions
                async sendDebugRequest() {
                    this.isLoading = true;
                    this.debugResponse = null;
                    try {
                        let body = null;
                        if (this.debugForm.body.trim()) {
                            body = JSON.parse(this.debugForm.body);
                        }

                        const res = await api({
                            method: this.debugForm.method,
                            url: this.debugForm.path,
                            data: body
                        });

                        this.debugResponse = {
                            ok: true,
                            status: res.status,
                            data: res.data
                        };
                    } catch (e) {
                        this.debugResponse = {
                            ok: false,
                            status: e.response?.status || 'Error',
                            data: e.response?.data || { error: e.message }
                        };
                    } finally {
                        this.isLoading = false;
                    }
                },

                sendDebugWsEvent() {
                    try {
                        const eventName = this.debugForm.wsEvent;
                        const eventData = JSON.parse(this.debugForm.wsData);

                        socket.emit(eventName, eventData);

                        this.debugWsLogs.unshift({
                            time: new Date().toLocaleTimeString(),
                            type: 'sent',
                            message: `${eventName}: ${JSON.stringify(eventData)}`
                        });

                        // 限制日志数量
                        if (this.debugWsLogs.length > 50) {
                            this.debugWsLogs.pop();
                        }

                        this.showToast('WebSocket 事件已发送', 'success');
                    } catch (e) {
                        this.showToast('发送失败: ' + e.message, 'error');
                    }
                },

                async refreshSystemInfo() {
                    this.isLoading = true;
                    try {
                        const res = await api.get('/api/system/info');
                        this.systemInfo = res.data;
                    } catch (e) {
                        this.systemInfo = {
                            'Error': 'Failed to load system info'
                        };
                    } finally {
                        this.isLoading = false;
                    }
                },

                async testAIConnection() {
                    this.isLoading = true;
                    try {
                        const res = await api.post('/api/sessions/ai/chat', {
                            messages: [{ role: 'user', content: 'Hello' }]
                        });
                        this.showToast('AI 连接正常', 'success');
                    } catch (e) {
                        this.showToast('AI 连接失败: ' + e.message, 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async testQQConnection() {
                    this.showToast('QQ 连接测试功能开发中', 'info');
                },

                async clearAllCache() {
                    this.showConfirm({
                        title: '清除缓存',
                        message: '确定要清除所有缓存吗？',
                        confirmText: '清除',
                        icon: 'fa-broom',
                        iconColor: 'var(--warning)',
                        danger: true,
                        action: async () => {
                            this.isLoading = true;
                            try {
                                await api.post('/api/system/clear-cache');
                                this.showToast('缓存已清除', 'success');
                            } catch (e) {
                                console.error('清除缓存失败:', e);
                                this.showToast('清除失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async reloadConfig() {
                    this.isLoading = true;
                    try {
                        await api.post('/api/system/reload-config');
                        this.showToast('配置已重载', 'success');
                        await this.loadAllData();
                    } catch (e) {
                        this.showToast('重载失败', 'error');
                    } finally {
                        this.isLoading = false;
                    }
                },

                async reloadCoreModules() {
                    this.showConfirm({
                        title: '重载核心代码',
                        message: '确定要重载所有核心代码模块吗？\n\n✅ 可以热重载：业务逻辑、AI服务、工具函数等\n❌ 需要重启：路由配置、API端点修改\n\n注：路由配置的修改需要重启服务才能生效。',
                        confirmText: '重载',
                        icon: 'fa-code',
                        iconColor: 'var(--warning)',
                        action: async () => {
                            this.isLoading = true;
                            try {
                                const res = await api.post('/api/system/reload-core');
                                if (res.data.success) {
                                    const { reloaded_count, failed_count, failed } = res.data;
                                    if (failed_count > 0) {
                                        console.error('重载失败的模块:', failed);
                                        this.showToast(`核心代码重载完成: ${reloaded_count} 个成功, ${failed_count} 个失败`, 'warning');
                                    } else {
                                        this.showToast(`核心代码重载完成: ${reloaded_count} 个模块`, 'success');
                                    }
                                    // 刷新数据以应用新代码
                                    await this.loadAllData();
                                } else {
                                    this.showToast(res.data.message || '重载失败', 'error');
                                }
                            } catch (e) {
                                console.error('重载核心代码失败:', e);
                                this.showToast('重载核心代码失败: ' + (e.response?.data?.error || e.message), 'error');
                            } finally {
                                this.isLoading = false;
                            }
                        }
                    });
                },

                async testWebSocket() {
                    this.showConfirm({
                        title: 'WebSocket 连接测试',
                        message: '确定要测试 WebSocket 连接吗？这将发送一个测试事件。',
                        confirmText: '测试',
                        icon: 'fa-plug',
                        iconColor: 'var(--info)',
                        action: async () => {
                            try {
                                // 发送一个 ping 事件测试连接
                                socket.emit('ping', { timestamp: Date.now() });
                                this.showToast('WebSocket 测试事件已发送，请查看控制台', 'success');
                            } catch (e) {
                                this.showToast('WebSocket 测试失败', 'error');
                            }
                        }
                    });
                },

                appendStreamText(messageId, text = '', isStreaming = true) {
                    const msgIdx = this.currentMessages.findIndex(m => m.id === messageId);
                    if (msgIdx === -1) return;

                    const currentMessage = this.currentMessages[msgIdx];
                    currentMessage.content = (currentMessage.content || '') + text;
                    currentMessage.is_streaming = isStreaming;
                    if (!isStreaming) {
                        currentMessage.stream_complete = true;
                    }
                },

                normalizeStreamChunk(messageId, text = '') {
                    const incoming = String(text || '');
                    if (!incoming) return '';

                    const msg = this.currentMessages.find(m => m.id === messageId);
                    const queued = (this.streamTypeQueues[messageId] || []).join('');
                    const existing = `${msg?.content || ''}${queued}`;
                    if (!existing) return incoming;

                    if (incoming.startsWith(existing)) {
                        return incoming.slice(existing.length);
                    }
                    if (existing.endsWith(incoming)) {
                        return '';
                    }

                    const maxOverlap = Math.min(existing.length, incoming.length, 32);
                    for (let overlap = maxOverlap; overlap >= 3; overlap--) {
                        if (existing.endsWith(incoming.slice(0, overlap))) {
                            return incoming.slice(overlap);
                        }
                    }
                    return incoming;
                },

                enqueueStreamText(messageId, text = '') {
                    const normalizedText = this.normalizeStreamChunk(messageId, text);
                    const chars = Array.from(normalizedText || '');
                    if (!chars.length) return;
                    if (!this.streamTypeQueues[messageId]) {
                        this.streamTypeQueues[messageId] = [];
                    }
                    this.streamTypeQueues[messageId].push(...chars);
                    this.scheduleStreamType(messageId);
                },

                scheduleStreamType(messageId) {
                    if (this.streamTypeTimers[messageId]) return;
                    this.streamTypeTimers[messageId] = setTimeout(() => {
                        delete this.streamTypeTimers[messageId];
                        const queue = this.streamTypeQueues[messageId] || [];
                        if (queue.length) {
                            let takeCount = 1;
                            if (queue.length > 160) takeCount = 4;
                            else if (queue.length > 80) takeCount = 3;
                            else if (queue.length > 40) takeCount = 2;

                            const nextText = queue.splice(0, takeCount).join('');
                            this.appendStreamText(messageId, nextText, true);
                            this.scheduleStreamScroll();
                            this.scheduleStreamType(messageId);
                            return;
                        }

                        if (this.streamEndPending[messageId]) {
                            this.finishStreamMessage(messageId);
                        }
                    }, 18);
                },

                scheduleStreamScroll(force = false) {
                    if (!force && this.isUserScrolling) return;
                    if (this.streamScrollTimer) return;
                    this.streamScrollTimer = setTimeout(() => {
                        this.streamScrollTimer = null;
                        this.$nextTick(() => this.scrollToBottom(force, true));
                    }, 100);
                },

                finishStreamMessage(messageId) {
                    const msgIdx = this.currentMessages.findIndex(m => m.id === messageId);
                    const messageSessionId = msgIdx !== -1 ? this.currentMessages[msgIdx].session_id : null;
                    const queue = this.streamTypeQueues[messageId] || [];
                    if (queue.length) {
                        this.appendStreamText(messageId, queue.splice(0).join(''), true);
                    }
                    if (this.streamTypeTimers[messageId]) {
                        clearTimeout(this.streamTypeTimers[messageId]);
                        delete this.streamTypeTimers[messageId];
                    }
                    delete this.streamTypeQueues[messageId];
                    delete this.streamEndPending[messageId];
                    this.appendStreamText(messageId, '', false);
                    if (messageSessionId && this.activeStreamMessages[messageSessionId] === messageId) {
                        delete this.activeStreamMessages[messageSessionId];
                        this.completedStreamMessages[messageSessionId] = messageId;
                        setTimeout(() => {
                            if (this.completedStreamMessages[messageSessionId] === messageId) {
                                delete this.completedStreamMessages[messageSessionId];
                            }
                        }, 15000);
                    }
                    this.scheduleStreamScroll(true);
                    this.isTyping = false;
                    this.isLoading = false;
                    this.loadingSessionId = null;
                    this.loadingStartTime = null;
                    localStorage.removeItem('nbot_loading_session_id');
                    localStorage.removeItem('nbot_loading_start_time');
                },

                // Socket.io
                initSocket() {
                    socket.on('connect', () => {
                        console.log('Socket connected');
                        this.socketConnected = true;
                        this.updateWebVisibility();
                    });
                    
                    socket.on('disconnect', () => {
                        console.log('Socket disconnected');
                        this.socketConnected = false;
                    });
                    
                    // 初始状态检查
                    this.socketConnected = socket.connected;
                    
                    socket.on('joined_session', (data) => {
                        console.log('Successfully joined session:', data);
                    });

                    // 处理会话更新事件（如 heartbeat 追加到会话）
                    socket.on('session_updated', async (data) => {
                        console.log('Session updated:', data);
                        if (data.action === 'heartbeat_completed' && data.session_id) {
                            // 刷新该会话的消息
                            this.refreshSessionMessages(data.session_id);
                            await this.loadSessions();
                        } else if (data.action === 'heartbeat_created' && data.session_id) {
                            await this.loadSessions();
                            const createdSession = this.sessions.find(s => s.id === data.session_id) || data.session;
                            if (createdSession) {
                                if (this.currentPage === 'chat') {
                                    await this.selectSession(createdSession);
                                } else {
                                    this.showToast(`Heartbeat 已创建新会话：${createdSession.name || data.session_id}`, 'success');
                                }
                            }
                        }
                    });

                    socket.on('new_message', (msg) => {
                        console.log('Received new_message:', msg);
                        
                        // 兼容后端进度卡片可能没有附带 session_id 的情况
                        const isCurrentSession = this.currentSession && 
                            (msg.session_id === this.currentSession.id || !msg.session_id);
                        
                        if (isCurrentSession) {
                            // 检查是否有临时ID（用户自己发送的消息）
                            if (msg.tempId) {
                                // 用服务器返回的消息替换本地临时消息
                                const localIdx = this.currentMessages.findIndex(m => m.id === msg.tempId);
                                if (localIdx !== -1) {
                                    // 保存原始 tempId，以便后续的 thinking_card 能找到父消息
                                    const localMsg = this.currentMessages[localIdx];
                                    
                                    // 替换为服务器返回的消息
                                    const newMsg = {
                                        ...msg,
                                        originalTempId: msg.tempId,  // 保存原始 tempId
                                        attachments: msg.attachments?.length ? msg.attachments : localMsg.attachments,
                                        thinking_cards: localMsg.thinking_cards || [],  // 保留进度卡片
                                        change_cards: localMsg.change_cards || []
                                    };
                                    
                                    this.currentMessages.splice(localIdx, 1, newMsg);
                                    
                                    // 将之前暂存的孤儿卡片关联到新消息
                                    if (this.orphanCards[msg.tempId]) {
                                        const orphanList = this.orphanCards[msg.tempId];
                                        orphanList.forEach(orphan => {
                                            if (!newMsg.thinking_cards.find(c => c.id === orphan.id)) {
                                                newMsg.thinking_cards.push(orphan);
                                            }
                                        });
                                        // 更新内存中的引用（splice 替换后需要重新获取）
                                        const updatedMsg = this.currentMessages[localIdx];
                                        if (updatedMsg) {
                                            updatedMsg.thinking_cards = newMsg.thinking_cards;
                                        }
                                        delete this.orphanCards[msg.tempId];
                                    }
                                    
                                    return;
                                }
                            }

                            // 处理进度卡片（优先处理，避免被添加到消息列表）
                            if (msg.type === 'thinking_card') {
                                // 如果进度卡片被关闭，仅保持打字状态，让龙骨加载动画显示
                                if (!this.showThinkingCard) {
                                    this.isTyping = this.isLoading && !msg.is_complete;
                                    return; // 不处理卡片，但保持加载动画
                                }
                                msg = {
                                    ...msg,
                                    content: this.normalizeDisplayText(msg.content || ''),
                                    steps: (msg.steps || []).map(step => ({
                                        ...step,
                                        name: this.normalizeDisplayText(step.name || ''),
                                        detail: this.normalizeDisplayText(step.detail || '')
                                    }))
                                };
                                // 如果后端没发 id，前端自己生成一个，防止 Vue v-for 的 key 冲突失效
                                if (!msg.id) {
                                    msg.id = 'tc_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);
                                }
                                
                                // 找到关联的用户消息，将卡片存储在该消息中
                                const parentMsgId = msg.parent_message_id;
                                let parentMsg = null;
                                let msgIdx = -1;
                                
                                // 首先尝试通过 id 查找
                                if (parentMsgId) {
                                    msgIdx = this.currentMessages.findIndex(m => m.id === parentMsgId || m.originalTempId === parentMsgId);
                                    if (msgIdx !== -1) {
                                        parentMsg = this.currentMessages[msgIdx];
                                    }
                                }
                                
                                // 如果没找到，尝试在所有消息的 thinking_cards 中查找是否有该卡片（兼容消息已替换的情况）
                                if (!parentMsg) {
                                    for (let i = 0; i < this.currentMessages.length; i++) {
                                        const m = this.currentMessages[i];
                                        if (m.thinking_cards && m.thinking_cards.find(c => c.id === msg.id)) {
                                            parentMsg = m;
                                            msgIdx = i;
                                            break;
                                        }
                                    }
                                }
                                
                                if (parentMsg && msgIdx !== -1) {
                                    // 确保 thinking_cards 数组存在
                                    const oldCards = parentMsg.thinking_cards || [];
                                    const existingIdx = oldCards.findIndex(c => c.id === msg.id);
                                    
                                    // 创建新的卡片数组
                                    let newCards;
                                    if (existingIdx !== -1) {
                                        // 替换整个卡片对象：旧数据作为基础，新数据覆盖旧数据
                                        const oldCard = oldCards[existingIdx];
                                        const updatedCard = { ...oldCard, ...msg };
                                        newCards = [...oldCards];
                                        newCards[existingIdx] = updatedCard;
                                    } else {
                                        newCards = [...oldCards, {...msg}];
                                    }
                                    
                                    // 如果之前有暂存的孤儿卡片，合并过来
                                    if (this.orphanCards[parentMsgId]) {
                                        const orphanList = this.orphanCards[parentMsgId];
                                        orphanList.forEach(orphan => {
                                            if (!newCards.find(c => c.id === orphan.id)) {
                                                newCards.push({...orphan});
                                            }
                                        });
                                        delete this.orphanCards[parentMsgId];
                                    }
                                    
                                    // 替换整个消息对象以触发 Vue 响应式
                                    const updatedMsg = { ...parentMsg, thinking_cards: newCards };
                                    this.currentMessages.splice(msgIdx, 1, updatedMsg);
                                } else {
                                    // 找不到父消息，暂存到 orphanCards（按 parentMsgId 分组）
                                    if (parentMsgId) {
                                        if (!this.orphanCards[parentMsgId]) {
                                            this.orphanCards[parentMsgId] = [];
                                        }
                                        const existingIdx = this.orphanCards[parentMsgId].findIndex(c => c.id === msg.id);
                                        if (existingIdx !== -1) {
                                            // 替换整个卡片对象：旧数据作为基础，新数据覆盖旧数据
                                            const oldCard = this.orphanCards[parentMsgId][existingIdx];
                                            const updatedCard = { ...oldCard, ...msg };
                                            const newList = [...this.orphanCards[parentMsgId]];
                                            newList[existingIdx] = updatedCard;
                                            this.orphanCards[parentMsgId] = newList;
                                        } else {
                                            this.orphanCards[parentMsgId] = [...this.orphanCards[parentMsgId], {...msg}];
                                        }
                                    }
                                }
                                // 强制 Vue 更新以确保进度变化能正确渲染
                                this.$forceUpdate();
                                // 只在用户没有手动滚动时才滚动
                                this.scheduleStreamScroll();
                                return;  // 不添加到消息列表
                            }

                            // 处理 Todo 卡片（优先处理，避免被添加到消息列表）
                            if (msg.type === 'todo_card') {
                                // 如果后端没发 id，前端自己生成一个
                                if (!msg.id) {
                                    msg.id = 'td_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);
                                }

                                const parentMsgId = msg.parent_message_id;

                                if (parentMsgId) {
                                    const parentMsg = this.currentMessages.find(m => m.id === parentMsgId);

                                    if (parentMsg) {
                                        // Vue 3 中直接赋值即可触发响应式更新
                                        if (!parentMsg.todo_cards) {
                                            parentMsg.todo_cards = [];
                                        }
                                        const existingIdx = parentMsg.todo_cards.findIndex(c => c.id === msg.id);

                                        if (existingIdx !== -1) {
                                            parentMsg.todo_cards[existingIdx] = msg;
                                        } else {
                                            parentMsg.todo_cards.push(msg);
                                        }
                                    }
                                }
                                // 只在用户没有手动滚动时才滚动
                                return;  // 不添加到消息列表
                            }

                            if (msg.type === 'change_card') {
                                if (!msg.id) {
                                    msg.id = 'cc_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);
                                }

                                const parentMsgId = msg.parent_message_id;

                                if (parentMsgId) {
                                    const parentMsg = this.currentMessages.find(m => m.id === parentMsgId || m.originalTempId === parentMsgId);

                                    if (parentMsg) {
                                        if (!parentMsg.change_cards) {
                                            parentMsg.change_cards = [];
                                        }
                                        const existingIdx = parentMsg.change_cards.findIndex(c => c.id === msg.id);

                                        if (existingIdx !== -1) {
                                            parentMsg.change_cards[existingIdx] = msg;
                                        } else {
                                            parentMsg.change_cards.push(msg);
                                        }
                                    }
                                }
                                // 只在用户没有手动滚动时才滚动
                                this.scheduleStreamScroll();
                                return;
                            }

                            // 检查是否已存在（排除 thinking_card 类型）
                            const exists = this.currentMessages.find(m => m.id === msg.id);
                            if (!exists) {
                                this.currentMessages.push(msg);
                                // 只在用户没有手动滚动时才滚动
                                this.$nextTick(() => this.scrollToBottom(false));
                            }

                            // 如果是助手消息且不是进度消息且不是文件消息，取消正在思考动画
                            if (msg.role === 'assistant' && !msg.is_progress_message && !msg.file) {
                                this.isTyping = false;
                                this.isLoading = false;
                                this.loadingSessionId = null;
                                localStorage.removeItem('nbot_loading_session_id');
                                localStorage.removeItem('nbot_loading_start_time');
                                // 如果开启了TTS，播放语音
                                if (this.ttsEnabled && msg.content) {
                                    this.speakText(msg.content);
                                }
                            }
                        }
                    });
                    
                    // 流式响应事件处理
                    socket.on('ai_stream_start', (data) => {
                        console.log('[Stream] AI stream start:', data);
                        console.log('[Stream] currentSession.id:', this.currentSession?.id);
                        this.isTyping = false;
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            const existingIdx = this.currentMessages.findIndex(m => m.id === data.message.id);
                            const msg = { ...data.message, content: '', is_streaming: true };
                            this.activeStreamMessages[data.session_id] = data.message.id;
                            delete this.completedStreamMessages[data.session_id];
                            this.streamTypeQueues[data.message.id] = [];
                            this.streamEndPending[data.message.id] = false;
                            if (this.streamTypeTimers[data.message.id]) {
                                clearTimeout(this.streamTypeTimers[data.message.id]);
                                delete this.streamTypeTimers[data.message.id];
                            }
                            if (existingIdx !== -1) {
                                Object.assign(this.currentMessages[existingIdx], msg, { content: '' });
                            } else {
                                this.currentMessages.push(msg);
                            }
                            console.log('[Stream] 消息已添加，当前消息数:', this.currentMessages.length);
                            // 只在用户没有手动滚动时才滚动
                            this.scheduleStreamScroll(true);
                        } else {
                            console.log('[Stream] 会话不匹配，忽略事件');
                        }
                    });

                    socket.on('ai_stream_chunk', (data) => {
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            const activeMessageId = this.activeStreamMessages[data.session_id];
                            const hasEventMessage = this.currentMessages.some(m => m.id === data.message_id);
                            const targetMessageId = hasEventMessage ? data.message_id : activeMessageId;
                            const msgIdx = this.currentMessages.findIndex(m => m.id === targetMessageId);
                            if (msgIdx !== -1) {
                                this.enqueueStreamText(targetMessageId, data.chunk || '');
                            } else {
                                console.log('[Stream] 未找到消息，创建占位消息:', data.message_id);
                                const fallbackMessageId = data.message_id || `stream-${Date.now()}`;
                                this.activeStreamMessages[data.session_id] = fallbackMessageId;
                                this.currentMessages.push({
                                    id: fallbackMessageId,
                                    role: 'assistant',
                                    sender: 'AI',
                                    content: '',
                                    is_streaming: true,
                                    timestamp: new Date().toISOString(),
                                    session_id: data.session_id
                                });
                                this.streamTypeQueues[fallbackMessageId] = [];
                                this.streamEndPending[fallbackMessageId] = false;
                                this.enqueueStreamText(fallbackMessageId, data.chunk || '');
                            }
                        }
                    });
                    
                    socket.on('ai_stream_end', (data) => {
                        console.log('[Stream] AI stream end:', data);
                        const finishedSessionId = data?.session_id || this.loadingSessionId || this.currentSession?.id;
                        const finishedMessageId = data?.message_id && this.currentMessages.some(m => m.id === data.message_id)
                            ? data.message_id
                            : this.activeStreamMessages[finishedSessionId];
                        if (finishedMessageId) {
                            this.streamEndPending[finishedMessageId] = true;
                            this.isTyping = false;
                            this.isLoading = false;
                            this.loadingSessionId = null;
                            this.loadingStartTime = null;
                            localStorage.removeItem('nbot_loading_session_id');
                            localStorage.removeItem('nbot_loading_start_time');
                            this.scheduleStreamType(finishedMessageId);
                        }
                        if (this.currentSession && finishedSessionId === this.currentSession.id && window.__nbotLive2dComment) {
                            // Collect last 5 rounds (up to 10 messages) for Live2D commentary
                            const allMsgs = this.currentMessages.filter(m => m.role === 'user' || m.role === 'assistant');
                            const recent = allMsgs.slice(-10).map(m => ({ role: m.role, content: m.content || '' }));
                            if (recent.length) {
                                window.__nbotLive2dComment(recent);
                            }
                        }
                        if (finishedMessageId) {
                            const queue = this.streamTypeQueues[finishedMessageId] || [];
                            if (!queue.length && !this.streamTypeTimers[finishedMessageId]) {
                                this.finishStreamMessage(finishedMessageId);
                            }
                        } else {
                            this.isTyping = false;
                            this.isLoading = false;
                            this.loadingSessionId = null;
                            this.loadingStartTime = null;
                            localStorage.removeItem('nbot_loading_session_id');
                            localStorage.removeItem('nbot_loading_start_time');
                        }
                        this.processPendingQueue(finishedSessionId);
                    });

                    socket.on('ai_response', (data) => {
                        console.log('Received ai_response:', data);
                        this.isTyping = false;
                        const finishedSessionId = data?.session_id || this.loadingSessionId || this.currentSession?.id;
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        this.loadingStartTime = null;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            if (data.message?.content && window.__nbotLive2dComment) {
                                const allMsgs = this.currentMessages.filter(m => m.role === 'user' || m.role === 'assistant');
                                const recent = allMsgs.slice(-10).map(m => ({ role: m.role, content: m.content || '' }));
                                if (recent.length) {
                                    window.__nbotLive2dComment(recent);
                                }
                            }
                            const existingIdx = this.currentMessages.findIndex(m => m.id === data.message.id);
                            if (existingIdx !== -1) {
                                const existingMessage = this.currentMessages[existingIdx];
                                const isStreamOwned = existingMessage.is_streaming || existingMessage.stream_complete || this.streamEndPending[data.message.id];
                                if (isStreamOwned) {
                                    existingMessage.thinking_cards = data.message.thinking_cards || existingMessage.thinking_cards || [];
                                    existingMessage.change_cards = data.message.change_cards || existingMessage.change_cards || [];
                                    existingMessage.attachments = data.message.attachments || existingMessage.attachments || [];
                                } else {
                                    Object.assign(existingMessage, data.message, {
                                        thinking_cards: data.message.thinking_cards || existingMessage.thinking_cards || [],
                                        change_cards: data.message.change_cards || existingMessage.change_cards || []
                                    });
                                }
                                // 只在用户没有手动滚动时才滚动
                                this.$nextTick(() => this.scrollToBottom(false));
                            } else {
                                const streamMessageId = this.activeStreamMessages[data.session_id] || this.completedStreamMessages[data.session_id];
                                const activeIdx = streamMessageId
                                    ? this.currentMessages.findIndex(m => m.id === streamMessageId)
                                    : -1;
                                if (activeIdx !== -1) {
                                    const activeMessage = this.currentMessages[activeIdx];
                                    const queue = this.streamTypeQueues[streamMessageId] || [];
                                    if (!activeMessage.content && !queue.length && data.message?.content) {
                                        this.enqueueStreamText(streamMessageId, data.message.content);
                                    }
                                    activeMessage.thinking_cards = data.message.thinking_cards || activeMessage.thinking_cards || [];
                                    activeMessage.change_cards = data.message.change_cards || activeMessage.change_cards || [];
                                    activeMessage.attachments = data.message.attachments || activeMessage.attachments || [];
                                    this.streamEndPending[streamMessageId] = true;
                                    this.scheduleStreamType(streamMessageId);
                                } else {
                                    this.currentMessages.push(data.message);
                                }
                                // 只在用户没有手动滚动时才滚动
                                this.$nextTick(() => this.scrollToBottom(false));
                            }
                        } else {
                            console.log('AI response ignored: session mismatch', this.currentSession?.id, data.session_id);
                        }
                        this.processPendingQueue(finishedSessionId);
                    });
                    
                    socket.on('error', (err) => {
                        console.error('Socket error:', err);
                        this.isTyping = false;
                        const failedSessionId = err?.session_id || this.loadingSessionId || this.currentSession?.id;
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        this.loadingStartTime = null;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');
                        if (this.currentSession && failedSessionId === this.currentSession.id && window.__nbotLive2dSay) {
                            window.__nbotLive2dSay('\u8fd9\u6b21\u8bf7\u6c42\u51fa\u9519\u4e86\uff0c\u53ef\u4ee5\u770b\u4e00\u4e0b\u9519\u8bef\u63d0\u793a\u3002', 4200, 6);
                        }
                        this.showToast(err.message || '发生错误', 'error');
                        this.processPendingQueue(failedSessionId);
                    });
                    
                    // 监听会话重命名事件
                    socket.on('session_renamed', (data) => {
                        console.log('Session renamed:', data);
                        // 更新当前会话的名称
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            this.currentSession.name = data.name;
                        }
                        // 更新会话列表中的名称
                        const session = this.sessions.find(s => s.id === data.session_id);
                        if (session) {
                            session.name = data.name;
                        }
                    });
                    
                    // 监听进度消息事件（AI思考过程中发送的消息）
                    socket.on('progress_message', (data) => {
                        console.log('[DEBUG] Progress message received:', data);
                        console.log('[DEBUG] Current session:', this.currentSession?.id);
                        console.log('[DEBUG] Message data:', data.message);
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            // 添加进度消息到当前消息列表（使用 Vue.set 确保响应式）
                            const newMessage = {...data.message, is_progress: true};
                            this.currentMessages = [...this.currentMessages, newMessage];
                            console.log('[DEBUG] Message added, total messages:', this.currentMessages.length);
                            this.$nextTick(() => {
                                // 只在用户没有手动滚动时才滚动
                                this.scrollToBottom(false);
                                console.log('[DEBUG] Scrolled to bottom');
                            });
                        } else {
                            console.log('[DEBUG] Session mismatch or no current session');
                        }
                    });

                    // 监听 exec_command 确认请求事件
                    socket.on('exec_confirm_request', (data) => {
                        console.log('[DEBUG] Exec confirm request received:', data);
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        this.isTyping = false;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');
                        if (this.currentSession && data.session_id === this.currentSession.id) {
                            this.execConfirmData = {
                                requestId: data.request_id || '',
                                command: data.command || '',
                                message: data.message || '',
                                sessionId: data.session_id || ''
                            };
                            this.showExecConfirmModal = true;
                            this.$forceUpdate();
                            console.log('[DEBUG] Showing exec confirm modal');
                        }
                    });

                    socket.on('exec_confirm_result', (data) => {
                        console.log('[DEBUG] Exec confirm result received:', data);
                        this.isTyping = false;
                        const finishedSessionId = data?.session_id || this.loadingSessionId || this.currentSession?.id;
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');

                        if (
                            this.currentSession &&
                            data?.message &&
                            data.session_id === this.currentSession.id
                        ) {
                            const existingIdx = this.currentMessages.findIndex(m => m.id === data.message.id);
                            if (existingIdx !== -1) {
                                Object.assign(this.currentMessages[existingIdx], data.message);
                            } else {
                                this.currentMessages.push(data.message);
                            }
                            // 只在用户没有手动滚动时才滚动
                            this.$nextTick(() => this.scrollToBottom(false));
                        }
                        this.processPendingQueue(finishedSessionId);
                    });
                },
                
                confirmExecCommand() {
                    console.log('[DEBUG] User confirmed exec command:', this.execConfirmData.requestId);
                    if (!(socket && socket.connected)) {
                        this.showToast('Socket未连接，无法确认命令执行', 'error');
                        return;
                    }
                    socket.emit('confirm_exec', {
                        request_id: this.execConfirmData.requestId,
                        approved: true,
                        session_id: this.execConfirmData.sessionId
                    });
                    this.showExecConfirmModal = false;
                    this.isLoading = true;
                    this.loadingSessionId = this.execConfirmData.sessionId;
                    localStorage.setItem('nbot_loading_session_id', this.execConfirmData.sessionId);
                    localStorage.setItem('nbot_loading_start_time', Date.now().toString());
                    this.showToast('命令已确认，正在执行...', 'info');
                },

                rejectExecCommand() {
                    console.log('[DEBUG] User rejected exec command:', this.execConfirmData.requestId);
                    if (!(socket && socket.connected)) {
                        this.showToast('Socket未连接，无法提交拒绝操作', 'error');
                        return;
                    }
                    socket.emit('confirm_exec', {
                        request_id: this.execConfirmData.requestId,
                        approved: false,
                        session_id: this.execConfirmData.sessionId
                    });
                    this.showExecConfirmModal = false;
                    this.showToast('已拒绝执行命令', 'warning');
                },

                scrollToBottom(force = false, instant = true) {
                    const container = this.$refs.messagesContainer;
                    if (container) {
                        // 强制滚动或用户没有手动滚动时才滚动
                        if (force || !this.isUserScrolling) {
                            // 加载消息时立即滚动，按钮点击使用平滑滚动
                            container.scrollTo({
                                top: container.scrollHeight,
                                behavior: instant ? 'instant' : 'smooth'
                            });
                        }
                    }
                },
                
                handleMessagesScroll() {
                    const container = this.$refs.messagesContainer;
                    if (!container) return;

                    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
                    // 当距离底部超过 30px 时，认为用户在查看历史消息
                    if (distanceFromBottom > 30) {
                        this.isUserScrolling = true;
                        this.showScrollToBottom = true;
                    } else {
                        // 只有滚动到底部附近才重置状态
                        this.isUserScrolling = false;
                        this.showScrollToBottom = false;
                    }
                },
                
                formatTime(timestamp) {
                    if (!timestamp) return '';
                    const date = new Date(timestamp);
                    return date.toLocaleDateString('zh-CN');
                },
                
                formatFullTime(timestamp) {
                    if (!timestamp) return '';
                    const date = new Date(timestamp);
                    return date.toLocaleString('zh-CN');
                },

                parseMessageContent(content, msg) {
                    if (!content) return '';

                    // 辅助函数：尝试解析JSON并提取msg字段
                    const tryParseJson = (str) => {
                        if (!str || typeof str !== 'string') return null;
                        
                        try {
                            const parsed = JSON.parse(str);
                            // 如果是双重编码，继续解析
                            if (typeof parsed === 'string') {
                                try {
                                    const inner = JSON.parse(parsed);
                                    return inner;
                                } catch {
                                    return parsed;
                                }
                            }
                            return parsed;
                        } catch (e) {
                            return null;
                        }
                    };

                    // 方法1：处理双重编码的JSON（如 "content": "{\"msg\":\"...\"}"）
                    const extractFromDoubleEncoded = (str) => {
                        // 匹配类似 {"msg":"..."} 的模式，即使被转义
                        const doubleEncodedMatch = str.match(/\\?"{\\?"msg\\?"[:][\s\S]*?\\?}"?/);
                        if (doubleEncodedMatch) {
                            const jsonStr = doubleEncodedMatch[0]
                                .replace(/\\"/g, '"')
                                .replace(/^"|"$/g, '');
                            return tryParseJson(jsonStr);
                        }
                        return null;
                    };

                    // 方法2：处理 markdown JSON 代码块（只处理明确标记为json的代码块）
                    const jsonCodeBlockMatch = content.match(/```json\s*([\s\S]*?)\s*```/);
                    if (jsonCodeBlockMatch) {
                        const codeContent = jsonCodeBlockMatch[1].trim();
                        const parsed = tryParseJson(codeContent);
                        if (parsed && parsed.msg) {
                            return parsed.msg;
                        }
                    }

                    // 方法3：尝试直接解析整个内容
                    let parsed = tryParseJson(content);
                    if (parsed && parsed.msg) {
                        return parsed.msg;
                    }

                    // 方法4：从双重编码中提取
                    parsed = extractFromDoubleEncoded(content);
                    if (parsed && parsed.msg) {
                        return parsed.msg;
                    }

                    // 方法5：查找所有JSON对象并尝试解析
                    const jsonMatch = content.match(/\{[\s\S]*?"msg"[\s\S]*?\}/);
                    if (jsonMatch) {
                        parsed = tryParseJson(jsonMatch[0]);
                        if (parsed && parsed.msg) {
                            return parsed.msg;
                        }
                    }

                    // 方法6：查找原始msg字段（处理转义的情况）
                    const rawMsgMatch = content.match(/"msg"\s*:\s*"([\s\S]*?)"(?:\s*,|\s*\})/);
                    if (rawMsgMatch) {
                        // 尝试解码转义字符
                        try {
                            const decoded = rawMsgMatch[1]
                                .replace(/\\n/g, '\n')
                                .replace(/\\t/g, '\t')
                                .replace(/\\r/g, '\r')
                                .replace(/\\"/g, '"')
                                .replace(/\\\\/g, '\\');
                            return decoded;
                        } catch {
                            return rawMsgMatch[1];
                        }
                    }

                    // 如果都不是JSON，返回原始内容
                    return content;
                },

                renderMessageBody(msg) {
                    const content = this.parseMessageContent(msg?.content || '', msg);
                    if (msg?.is_streaming) {
                        return this.renderStreamingHtml(content);
                    }
                    return this.renderMarkdown(content);
                },

                isStreamAwaiting(msg) {
                    return !!(msg && msg.is_streaming && !(msg.content || '').length);
                },

                renderStreamingHtml(content) {
                    const normalized = String(content || '').replace(/\r\n/g, '\n');
                    if (!normalized) return '';
                    return normalized
                        .split(/\n{2,}/)
                        .map(part => `<p>${this.escapeHtml(part).replace(/\n/g, '<br>')}</p>`)
                        .join('');
                },

                // 判断是否为 Markdown 文件
                isMarkdownFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'md' || ext === 'markdown';
                },
                
                // 判断是否为 Word 文档
                isDocxFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'docx' || ext === 'doc';
                },
                
                // 判断是否为 Excel 文件
                isExcelFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'xlsx' || ext === 'xls';
                },
                
                // 判断是否为 PDF 文件
                isPdfFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'pdf';
                },
                
                // 判断是否为 PPTX 文件
                isPptxFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'pptx';
                },
                
                // 判断是否为 HTML 文件
                isHtmlFile(filename) {
                    if (!filename) return false;
                    const ext = filename.toLowerCase().split('.').pop();
                    return ext === 'html' || ext === 'htm';
                },

                sanitizeRenderedHtml(html) {
                    const template = document.createElement('template');
                    template.innerHTML = html || '';

                    const blockedTags = new Set(['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'form']);
                    const allowedAttrs = new Set(['class', 'id', 'href', 'src', 'alt', 'title', 'target', 'rel', 'colspan', 'rowspan']);
                    const safeUrl = (value) => {
                        if (!value) return true;
                        const trimmed = String(value).trim().toLowerCase();
                        return !trimmed.startsWith('javascript:') && !trimmed.startsWith('data:text/html');
                    };

                    template.content.querySelectorAll('*').forEach((node) => {
                        if (blockedTags.has(node.tagName.toLowerCase())) {
                            node.remove();
                            return;
                        }

                        [...node.attributes].forEach((attr) => {
                            const name = attr.name.toLowerCase();
                            if (name.startsWith('on') || !allowedAttrs.has(name) || !safeUrl(attr.value)) {
                                node.removeAttribute(attr.name);
                            }
                        });

                        if (node.tagName.toLowerCase() === 'a') {
                            node.setAttribute('rel', 'noopener noreferrer');
                        }
                    });

                    return template.innerHTML;
                },

                // 渲染 Markdown 内容
                renderMarkdown(content) {
                    if (!content) return '';
                    
                    // 配置 marked 选项
                    marked.setOptions({
                        breaks: true,  // 支持换行
                        gfm: true,     // 支持 GitHub Flavored Markdown
                        headerIds: false,  // 不生成 header id
                        mangle: false,  // 不转义邮件地址
                        highlight: function(code, lang) {
                            // 代码高亮
                            if (lang && hljs.getLanguage(lang)) {
                                try {
                                    return hljs.highlight(code, { language: lang }).value;
                                } catch (e) {
                                    return code;
                                }
                            }
                            return hljs.highlightAuto(code).value;
                        }
                    });
                    
                    try {
                        let html = marked.parse(content);
                        
                        // 给每个代码块包裹 header + 复制按钮
                        let blockIndex = 0;
                        html = html.replace(
                            /<pre><code class="language-(\w+)">([\s\S]*?)<\/code><\/pre>/g,
                            (match, lang, code) => {
                                const id = `cb_${Date.now()}_${blockIndex++}`;
                                return `
                                  <div class="code-wrap">
                                    <div class="code-header">
                                      <span class="code-lang">${lang}</span>
                                      <button class="code-copy-btn" onclick="copyCodeBlock('${id}')">
                                        <i class="fas fa-copy"></i> 复制
                                      </button>
                                    </div>
                                    <pre id="${id}"><code class="language-${lang}">${code}</code></pre>
                                  </div>`;
                            }
                        );
                        
                        return this.sanitizeRenderedHtml(html);
                    } catch (e) {
                        console.error('Markdown parse error:', e);
                        return this.escapeHtml(content);
                    }
                },
                
                getAttachmentIcon(type) {
                    if (!type) return 'fas fa-file';
                    if (type.startsWith('image/')) return 'fas fa-image';
                    if (type.startsWith('video/')) return 'fas fa-video';
                    if (type.startsWith('audio/')) return 'fas fa-music';
                    if (type.includes('pdf')) return 'fas fa-file-pdf';
                    if (type.includes('word') || type.includes('document')) return 'fas fa-file-word';
                    if (type.includes('excel') || type.includes('spreadsheet')) return 'fas fa-file-excel';
                    if (type.includes('powerpoint') || type.includes('presentation')) return 'fas fa-file-powerpoint';
                    if (type.includes('zip') || type.includes('rar') || type.includes('archive')) return 'fas fa-file-archive';
                    if (type.includes('text/')) return 'fas fa-file-alt';
                    return 'fas fa-file';
                },
                
                getAttachmentIconClass(type) {
                    if (!type) return 'default';
                    if (type.startsWith('image/')) return 'image';
                    if (type.startsWith('video/')) return 'video';
                    if (type.startsWith('audio/')) return 'audio';
                    if (type.includes('pdf') || type.includes('word') || type.includes('excel')) return 'document';
                    return 'default';
                },
                
                handleImageError(event) {
                    // 当图片加载失败时（通常是 Blob URL 失效），显示占位图并隐藏图片
                    event.target.style.display = 'none';
                    const parent = event.target.closest('.attachment-card') || event.target.closest('.uploaded-file');
                    if (parent) {
                        const icon = parent.querySelector('.attachment-icon, .file-icon');
                        if (icon) {
                            icon.style.display = 'flex';
                        }
                    }
                },
                
                formatFileSize(size) {
                    if (!size) return '';
                    if (size < 1024) return size + ' B';
                    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
                    if (size < 1024 * 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + ' MB';
                    return (size / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
                },

                isTextFile(fileName) {
                    const textExtensions = [
                        'py', 'js', 'ts', 'jsx', 'tsx', 'sh', 'bash', 'zsh',
                        'json', 'yaml', 'yml', 'toml', 'xml', 'html', 'css',
                        'md', 'txt', 'csv', 'log', 'ini', 'conf', 'cfg',
                        'sql', 'java', 'kt', 'swift', 'c', 'cpp', 'h', 'hpp',
                        'go', 'rs', 'rb', 'php', 'pl', 'r', 'lua', 'vim',
                        'gitignore', 'dockerfile', 'makefile', 'env'
                    ];
                    const ext = fileName.split('.').pop()?.toLowerCase();
                    return ext && textExtensions.includes(ext);
                },

                getFileIcon(fileName) {
                    const ext = fileName.split('.').pop()?.toLowerCase();
                    const iconMap = {
                        'py': 'fas fa-file-code',
                        'js': 'fas fa-file-code',
                        'ts': 'fas fa-file-code',
                        'jsx': 'fas fa-file-code',
                        'tsx': 'fas fa-file-code',
                        'sh': 'fas fa-terminal',
                        'bash': 'fas fa-terminal',
                        'zsh': 'fas fa-terminal',
                        'json': 'fas fa-file-code',
                        'yaml': 'fas fa-file-code',
                        'yml': 'fas fa-file-code',
                        'xml': 'fas fa-file-code',
                        'html': 'fas fa-file-code',
                        'css': 'fas fa-file-code',
                        'md': 'fas fa-file-alt',
                        'txt': 'fas fa-file-alt',
                        'csv': 'fas fa-file-csv',
                        'sql': 'fas fa-database',
                        'png': 'fas fa-file-image',
                        'jpg': 'fas fa-file-image',
                        'jpeg': 'fas fa-file-image',
                        'gif': 'fas fa-file-image',
                        'svg': 'fas fa-file-image',
                        'pdf': 'fas fa-file-pdf',
                        'pptx': 'fas fa-file-powerpoint',
                        'zip': 'fas fa-file-archive',
                        'rar': 'fas fa-file-archive',
                        '7z': 'fas fa-file-archive',
                        'xlsx': 'fas fa-file-excel',
                        'xls': 'fas fa-file-excel',
                        'doc': 'fas fa-file-word',
                        'docx': 'fas fa-file-word',
                        'exe': 'fas fa-file-alt',
                        'dll': 'fas fa-file-alt'
                    };
                    return iconMap[ext] || 'fas fa-file';
                },
                
                previewAttachment(att) {
                    const imageUrl = att.url || att.data || att.preview;
                    
                    // 图片文件直接在新窗口打开
                    if (imageUrl && att.type && att.type.startsWith('image/')) {
                        const win = window.open('', '_blank');
                        win.document.write('<img src="' + imageUrl + '" style="max-width:100%;max-height:100vh;margin:auto;display:block;">');
                        return;
                    }
                    
                    // 其他文件类型使用预览模态框
                    // 所有文件都尝试用 previewUserFile 处理
                    this.previewUserFile(att);
                },
                
                async previewUserFile(att) {
                    // 文件卡片预览统一复用工作区/静态文件的预览链，避免和工作区行为分叉
                    const staticFileUrl = att.download_url || att.url || '';
                    let safeName = att.safe_name;
                    if (!safeName && typeof staticFileUrl === 'string') {
                        const staticMatch = staticFileUrl.match(/\/static\/files\/([^?#]+)/);
                        if (staticMatch && staticMatch[1]) {
                            try {
                                safeName = decodeURIComponent(staticMatch[1]);
                            } catch (e) {
                                safeName = staticMatch[1];
                            }
                        }
                    }

                    if (safeName) {
                        return this.previewStaticFile(safeName, att.name || '文件预览');
                    }

                    const sessionId = att.session_id || this.currentSession?.id || '';
                    if (sessionId && att.name) {
                        return this.previewFile(sessionId, att.name);
                    }

                    this.showFilePreview = true;
                    this.filePreviewMaximized = false;
                    this.filePreviewData = {
                        sessionId: sessionId,
                        filename: att.name || '文件预览',
                        type: att.type || '',
                        content: '',
                        url: att.url || '',
                        loading: true,
                        error: '',
                        truncated: false,
                        extracted_length: 0,
                        original_length: 0
                    };

                    // 兜底：没有工作区路径也没有 static/files 路径时，尽量按原始附件渲染
                    if (att.type && att.type.startsWith('image/')) {
                        this.filePreviewData.type = 'image';
                        this.filePreviewData.url = att.url || att.data || att.preview;
                        this.filePreviewData.loading = false;
                        return;
                    }

                    if (att.content) {
                        this.filePreviewData.content = att.content;
                        this.filePreviewData.loading = false;
                        return;
                    }

                    if (att.url) {
                        try {
                            const response = await fetch(att.url);
                            if (response.ok && response.status !== 206) {
                                const text = await response.text();
                                this.filePreviewData.content = text;
                                this.filePreviewData.loading = false;
                                return;
                            }
                        } catch (e) {
                            console.log('无法直接 fetch 文件:', e);
                        }
                    }

                    if (!this.filePreviewData.content) {
                        this.filePreviewData.loading = false;
                        this.filePreviewData.error = '无法预览此文件，请下载后查看';
                    }
                },

                formatNumber(num) {
                    if (num === undefined || num === null) return '0';
                    return num.toLocaleString('zh-CN');
                },
                
                // 复制消息内容
                copyMessage(msg) {
                    const text = msg.content || '';
                    this.copyToClipboard(text);
                },

                async regenerateMessage(msg) {
                    if (!this.currentSession || !msg?.id) {
                        this.showToast('无法定位要重新生成的回复', 'error');
                        return;
                    }
                    if (this.isLoading && this.loadingSessionId === this.currentSession.id) {
                        this.showToast('当前会话正在生成中', 'warning');
                        return;
                    }

                    const sessionId = this.currentSession.id;
                    const msgIndex = this.currentMessages.findIndex(m => m.id === msg.id);
                    let promptMessageId = null;
                    if (msgIndex !== -1) {
                        for (let i = msgIndex - 1; i >= 0; i--) {
                            if (this.currentMessages[i].role === 'user') {
                                promptMessageId = this.currentMessages[i].id;
                                break;
                            }
                        }
                        this.currentMessages = this.currentMessages.slice(0, msgIndex).map(currentMsg => {
                            if (promptMessageId && currentMsg.id !== promptMessageId) {
                                return currentMsg;
                            }
                            const cleanedMsg = { ...currentMsg };
                            delete cleanedMsg.thinking_cards;
                            delete cleanedMsg.todo_cards;
                            delete cleanedMsg.change_cards;
                            return cleanedMsg;
                        });
                        if (promptMessageId) {
                            delete this.orphanCards[promptMessageId];
                        }
                    }
                    this.isTyping = true;
                    this.isLoading = true;
                    this.loadingSessionId = sessionId;
                    localStorage.setItem('nbot_loading_session_id', sessionId);
                    localStorage.setItem('nbot_loading_start_time', Date.now().toString());

                    try {
                        const res = await api.post(`/api/sessions/${sessionId}/regenerate`, {
                            message_id: msg.id
                        });
                        if (msgIndex !== -1) {
                            if (res.data?.prompt_message_id) {
                                delete this.orphanCards[res.data.prompt_message_id];
                            }
                        } else {
                            await this.loadMessages(true);
                        }
                        this.showToast('已开始重新生成', 'success');
                        // 重新生成时强制滚动到底部
                        this.$nextTick(() => this.scrollToBottom(true));
                    } catch (e) {
                        this.isTyping = false;
                        this.isLoading = false;
                        this.loadingSessionId = null;
                        localStorage.removeItem('nbot_loading_session_id');
                        localStorage.removeItem('nbot_loading_start_time');
                        await this.loadMessages(true);
                        this.showToast(e.response?.data?.error || '重新生成失败', 'error');
                    }
                },

                async forkSessionFromMessage(msg) {
                    if (!this.currentSession || !msg?.id) {
                        this.showToast('无法定位要分支的回复', 'error');
                        return;
                    }

                    try {
                        const res = await api.post(`/api/sessions/${this.currentSession.id}/fork`, {
                            message_id: msg.id
                        });
                        const forkedSession = res.data.session;
                        if (!forkedSession) {
                            throw new Error('Fork session missing');
                        }
                        this.sessions = [
                            forkedSession,
                            ...this.sessions.filter(s => s.id !== forkedSession.id)
                        ];
                        this.currentPage = 'chat';
                        this.chatTab = forkedSession.type === 'cli' ? 'cli' : 'web';
                        await this.selectSession(forkedSession);
                        this.showToast('已创建会话分支', 'success');
                    } catch (e) {
                        console.error('Fork session failed:', e);
                        this.showToast(e.response?.data?.error || '创建会话分支失败', 'error');
                    }
                },

                // 继续生成（从中断点恢复）
                continueGeneration(msg) {
                    if (!this.currentSession) {
                        this.showToast('请先选择一个会话', 'error');
                        return;
                    }
                    const tempId = 'local_' + Date.now();
                    socket.emit('send_message', {
                        session_id: this.currentSession.id,
                        content: '继续',
                        sender: 'web_user',
                        tempId: tempId
                    });
                },
                
                copyToClipboard(text) {
                    // 优先使用现代 API，失败则降级
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(text).then(() => {
                            this.showToast('已复制', 'success');
                        }).catch(() => {
                            // 降级方案：使用 textarea
                            this.fallbackCopy(text);
                        });
                    } else {
                        // 降级方案：使用 textarea
                        this.fallbackCopy(text);
                    }
                },
                
                fallbackCopy(text) {
                    // 降级复制方案，兼容 Docker 等无剪贴板环境
                    const textarea = document.createElement('textarea');
                    textarea.value = text;
                    textarea.style.position = 'fixed';
                    textarea.style.left = '-9999px';
                    textarea.style.top = '-9999px';
                    document.body.appendChild(textarea);
                    textarea.focus();
                    textarea.select();
                    
                    try {
                        const successful = document.execCommand('copy');
                        if (successful) {
                            this.showToast('已复制', 'success');
                        } else {
                            this.showToast('复制失败', 'error');
                        }
                    } catch (err) {
                        console.error('复制失败:', err);
                        this.showToast('复制失败', 'error');
                    }
                    
                    document.body.removeChild(textarea);
                },
                
                showToast(message, type = 'info') {
                    const icons = {
                        success: 'fas fa-check-circle',
                        error: 'fas fa-times-circle',
                        info: 'fas fa-info-circle'
                    };
                    
                    const toast = {
                        id: Date.now(),
                        message,
                        type,
                        icon: icons[type]
                    };
                    
                    this.toasts.push(toast);
                    setTimeout(() => {
                        this.toasts = this.toasts.filter(t => t.id !== toast.id);
                    }, 3000);
                }
};
