/**
 * Novel2Script — 前端交互逻辑
 * =============================
 * 负责所有用户交互、API 通信和 DOM 渲染。
 *
 * 通信架构：
 *   - WebSocket（主通道）：聊天流式输出、任务进度推送、心跳检测
 *   - SSE（降级方案）：WS 不可用时的聊天流式输出
 *   - HTTP 轮询（降级方案）：WS 不可用时的任务进度查询
 *   - HTTP POST：文件上传（WS 不支持 multipart）
 *
 * 状态管理：
 *   - conversationHistory: 对话消息数组，发送给 LLM
 *   - scriptContext: 当前剧本上下文 JSON，注入到 LLM 系统提示
 *   - pendingChatRequests: WS 流式聊天的进行中请求
 *   - taskResolvers: 异步转换任务的 Promise 解析器
 *
 * 降级策略：
 *   WS 可用 → 全走 WS → WS 断开重连（指数退避，最多 5 次）
 *   → 重连失败 → 聊天降级 SSE，进度降级 HTTP 轮询
 */

// ====== DOM 引用 ======
const chatArea      = document.getElementById('chatArea');
const welcome       = document.getElementById('welcome');
const fileInput     = document.getElementById('fileInput');
const fileBtn       = document.getElementById('fileBtn');
const fileChip      = document.getElementById('fileChip');
const chipName      = fileChip.querySelector('.file-chip-name');
const chipRemove    = fileChip.querySelector('.file-chip-remove');
const textInput     = document.getElementById('textInput');
const sendBtn       = document.getElementById('sendBtn');
const showThinking  = document.getElementById('showThinking');
const charCount     = document.getElementById('charCount');
const toastContainer = document.getElementById('toastContainer');

// 状态指示器
const statusIndicator = document.getElementById('statusIndicator');
const statusDot     = statusIndicator.querySelector('.status-dot');
const statusText    = statusIndicator.querySelector('.status-text');

// 对话管理
const clearBtn      = document.getElementById('clearBtn');
const exportBtn     = document.getElementById('exportBtn');

// 剧本窗口
const openViewerBtn = document.getElementById('openViewerBtn');

// 设置弹窗
const settingsBtn   = document.getElementById('settingsBtn');
const settingsModal = document.getElementById('settingsModal');
const modalClose    = document.getElementById('modalClose');
const modalCancel   = document.getElementById('modalCancel');
const modalSave     = document.getElementById('modalSave');
const setBaseUrl    = document.getElementById('setBaseUrl');
const setApiKey     = document.getElementById('setApiKey');
const setModel      = document.getElementById('setModel');

// ====== 状态 ======
let selectedFile = null;
let conversationHistory = [];
let scriptContext = '';
let isConnected = false;

// BroadcastChannel 用于与独立剧本窗口通信
const scriptChannel = new BroadcastChannel('novel2script-stream');
let viewerWindow = null;
let sentChunks = [];  // 缓存已发送的 chunk，供新窗口同步

// API 设置（从 localStorage 加载，运行时覆盖 config.py 的默认值）
let apiSettings = {
    base_url: '',
    api_key: '',
    model: '',
};

// ====== WebSocket 管理 ======
// WebSocket 用于替代 SSE + HTTP 轮询，实现双向实时通信
let ws = null;                    // WebSocket 实例
let wsReady = false;              // 连接是否就绪
let wsReconnectAttempts = 0;      // 当前重连尝试次数
const WS_MAX_RECONNECT = 5;       // 最大重连次数
const WS_RECONNECT_BASE_DELAY = 1000;  // 基础重连延迟（ms），指数退避

const pendingChatRequests = new Map();  // request_id -> {streamingId, fullContent, fullReasoning}
const activeTasks = new Set();          // 当前订阅的异步任务 ID
const taskResolvers = new Map();        // task_id -> {resolve, reject}，将 WS 推送转为 Promise

/** 建立 WebSocket 连接，连接成功后自动重订阅进行中的任务。 */
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws`;

    ws = new WebSocket(url);

    ws.onopen = () => {
        wsReady = true;
        wsReconnectAttempts = 0;
        // 重连后重新订阅所有进行中的任务
        for (const taskId of activeTasks) {
            wsSend({ action: 'subscribe_task', task_id: taskId });
        }
    };

    ws.onmessage = (event) => {
        handleWsMessage(JSON.parse(event.data));
    };

    ws.onclose = () => {
        wsReady = false;
        setConnectionStatus(false, '连接断开');
        scheduleReconnect();
    };

    ws.onerror = () => {};
}

function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
    }
}

function scheduleReconnect() {
    if (wsReconnectAttempts >= WS_MAX_RECONNECT) {
        setConnectionStatus(false, '无法连接，已降级');
        checkHealth();
        return;
    }
    const delay = WS_RECONNECT_BASE_DELAY * Math.pow(2, wsReconnectAttempts);
    wsReconnectAttempts++;
    setTimeout(connectWebSocket, delay);
}

/**
 * WebSocket 消息路由：根据 type 字段分发到对应处理逻辑。
 * 消息协议（服务端→客户端）：
 * - ping: 心跳，需回 pong
 * - health: 连接建立后的健康信息（model, base_url）
 * - chat_chunk: 流式聊天片段（reasoning 或 content）
 * - chat_done: 流式聊天完成
 * - chat_error: 流式聊天错误
 * - task_progress: 异步任务进度更新
 * - task_done: 异步任务完成
 * - task_failed: 异步任务失败
 * - error: 通用错误
 */
function handleWsMessage(msg) {
    switch (msg.type) {
        case 'ping':
            wsSend({ action: 'pong' });
            break;

        case 'health':
            setConnectionStatus(true, `已连接 · ${msg.model || ''}`);
            break;

        case 'chat_chunk': {
            const pending = pendingChatRequests.get(msg.request_id);
            if (!pending) return;
            if (msg.chunk_type === 'reasoning') {
                pending.fullReasoning += msg.content;
                updateThinkingContent(pending.streamingId, pending.fullReasoning);
            } else if (msg.chunk_type === 'content') {
                pending.fullContent += msg.content;
                updateAiStreaming(pending.streamingId, pending.fullContent);
            }
            break;
        }

        case 'chat_done': {
            const pending = pendingChatRequests.get(msg.request_id);
            if (!pending) return;
            finalizeAiStreaming(pending.streamingId);
            conversationHistory.push({ role: 'assistant', content: pending.fullContent });
            pendingChatRequests.delete(msg.request_id);
            setSending(false);
            resetInput();
            break;
        }

        case 'chat_error': {
            const pending = pendingChatRequests.get(msg.request_id);
            if (pending) {
                removeMessage(pending.streamingId);
                pendingChatRequests.delete(msg.request_id);
            }
            conversationHistory.pop();
            addAiError(msg.error, null);
            showToast('请求失败', 'error');
            setSending(false);
            resetInput();
            break;
        }

        case 'task_progress':
            scriptChannel.postMessage({ type: 'progress', step: msg.step, percent: msg.percent });
            break;

        case 'task_done': {
            activeTasks.delete(msg.task_id);
            if (msg.result) {
                scriptChannel.postMessage({ type: 'done', result: msg.result });
            }
            const resolver = taskResolvers.get(msg.task_id);
            if (resolver) {
                resolver.resolve(msg.result);
                taskResolvers.delete(msg.task_id);
            }
            break;
        }

        case 'task_failed': {
            activeTasks.delete(msg.task_id);
            const resolver = taskResolvers.get(msg.task_id);
            if (resolver) {
                resolver.reject(new Error(msg.error || '转换失败'));
                taskResolvers.delete(msg.task_id);
            }
            break;
        }

        case 'error':
            showToast(msg.message, 'error');
            break;
    }
}

function resetInput() {
    textInput.value = '';
    autoResizeTextarea();
    updateCharCount();
    clearFile();
    textInput.focus();
}

// ====== 初始化 ======
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    bindEvents();
    connectWebSocket();
    // WS 连接失败时降级到 HTTP 健康检查
    setTimeout(() => {
        if (!wsReady) checkHealth();
    }, 3000);
});

function bindEvents() {
    // 对话管理
    clearBtn.addEventListener('click', clearConversation);
    exportBtn.addEventListener('click', exportScript);

    // 设置弹窗
    settingsBtn.addEventListener('click', openSettings);
    modalClose.addEventListener('click', closeSettings);
    modalCancel.addEventListener('click', closeSettings);
    modalSave.addEventListener('click', saveSettings);
    settingsModal.addEventListener('click', (e) => {
        if (e.target === settingsModal) closeSettings();
    });

    // 剧本窗口
    openViewerBtn.addEventListener('click', openScriptViewer);

    // 文件选择
    fileInput.addEventListener('change', handleFileSelect);
    fileBtn.addEventListener('click', () => fileInput.click());
    chipRemove.addEventListener('click', clearFile);

    // 发送
    sendBtn.addEventListener('click', handleSend);
    textInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    });

    // 文本框自动调整高度 + 字数统计
    textInput.addEventListener('input', () => {
        autoResizeTextarea();
        updateCharCount();
    });

    // 拖拽上传
    document.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });
    document.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const files = e.dataTransfer.files;
        if (files.length > 0 && files[0].name.toLowerCase().endsWith('.txt')) {
            setFile(files[0]);
        }
    });
}

// ====== 健康检查 ======

async function checkHealth() {
    try {
        const res = await fetch('/api/v1/health');
        if (res.ok) {
            const data = await res.json();
            setConnectionStatus(true, `已连接 · ${data.model || ''}`);
        } else {
            setConnectionStatus(false, '连接失败');
        }
    } catch (_) {
        setConnectionStatus(false, '无法连接');
    }
}

function setConnectionStatus(connected, text) {
    isConnected = connected;
    statusDot.classList.toggle('connected', connected);
    statusDot.classList.toggle('error', !connected);
    statusText.textContent = text;
}

// ====== Toast 通知系统 ======

function showToast(message, type = 'info', duration = 3000) {
    const id = 'toast-' + Date.now();
    const iconMap = {
        success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
        error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
        info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    };
    const icon = iconMap[type] || iconMap.info;
    const html = `<div class="toast toast-${type}" id="${id}"><div class="toast-icon">${icon}</div><span>${escapeHtml(message)}</span></div>`;
    toastContainer.insertAdjacentHTML('beforeend', html);

    setTimeout(() => {
        const el = document.getElementById(id);
        if (el) {
            el.classList.add('toast-exit');
            setTimeout(() => el.remove(), 300);
        }
    }, duration);
}

// ====== API 设置管理 ======

function loadSettings() {
    try {
        const saved = sessionStorage.getItem('novel2script_settings');
        if (saved) {
            apiSettings = JSON.parse(saved);
        }
    } catch (_) { /* 解析失败则用默认空值 */ }
}

function openSettings() {
    setBaseUrl.value = apiSettings.base_url;
    setApiKey.value = apiSettings.api_key;
    setModel.value = apiSettings.model;
    settingsModal.style.display = 'flex';
}

function closeSettings() {
    settingsModal.style.display = 'none';
}

function saveSettings() {
    apiSettings = {
        base_url: setBaseUrl.value.trim(),
        api_key: setApiKey.value.trim(),
        model: setModel.value.trim(),
    };
    sessionStorage.setItem('novel2script_settings', JSON.stringify(apiSettings));
    closeSettings();
    showToast('设置已保存', 'success');
    checkHealth();
}

function getApiParams() {
    return {
        model: apiSettings.model,
        base_url: apiSettings.base_url,
        api_key: apiSettings.api_key,
    };
}

// ====== 剧本窗口管理 ======

function openScriptViewer() {
    if (viewerWindow && !viewerWindow.closed) {
        viewerWindow.focus();
        return;
    }
    const w = 540;
    const h = Math.min(screen.height, 900);
    const left = screen.width - w - 40;
    const top = (screen.height - h) / 2;
    viewerWindow = window.open(
        '/static/script-viewer.html',
        'scriptViewer',
        `width=${w},height=${h},left=${left},top=${top},resizable=yes,scrollbars=no`
    );
}

function closeScriptViewer() {
    if (viewerWindow && !viewerWindow.closed) {
        viewerWindow.close();
    }
    sentChunks = [];
}

function resetScriptStream() {
    sentChunks = [];
    scriptChannel.postMessage({ type: 'reset' });
}

// 监听剧本窗口的同步请求（窗口关闭后重新打开时）
scriptChannel.onmessage = (event) => {
    const msg = event.data;
    if (msg.type === 'sync_request') {
        // 重放所有已缓存的 chunk
        for (const chunk of sentChunks) {
            scriptChannel.postMessage(chunk);
        }
    }
};

// ====== 对话管理 ======

function clearConversation() {
    if (conversationHistory.length === 0 && !scriptContext) return;
    if (!confirm('确定清空所有对话和剧本上下文？')) return;

    conversationHistory = [];
    scriptContext = '';
    exportBtn.disabled = true;

    // 移除所有消息，保留欢迎页
    chatArea.querySelectorAll('.message').forEach(el => el.remove());
    if (welcome) welcome.style.display = '';
    showToast('对话已清空', 'info');
}

function exportScript() {
    if (!scriptContext) {
        showToast('没有可导出的剧本', 'error');
        return;
    }
    const blob = new Blob([scriptContext], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'script.json';
    a.click();
    URL.revokeObjectURL(url);
    showToast('剧本已导出', 'success');
}

// ====== 字数统计 ======

function updateCharCount() {
    const len = textInput.value.length;
    charCount.textContent = `${len} / 2000`;
    charCount.classList.toggle('warning', len > 1800 && len <= 2000);
    charCount.classList.toggle('danger', len > 2000);
}

// ====== 文件选择 ======

function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) setFile(file);
}

function setFile(file) {
    if (!file.name.toLowerCase().endsWith('.txt')) {
        showToast('请选择 .txt 文件', 'error');
        return;
    }
    selectedFile = file;
    chipName.textContent = file.name;
    fileChip.style.display = 'inline-flex';
    fileBtn.classList.add('has-file');
}

function clearFile() {
    selectedFile = null;
    fileInput.value = '';
    fileChip.style.display = 'none';
    fileBtn.classList.remove('has-file');
}

// ====== 发送 ======

async function handleSend() {
    const text = textInput.value.trim();
    const hasFile = !!selectedFile;
    const hasText = text.length > 0;

    if (!hasFile && !hasText) return;

    if (welcome) welcome.style.display = 'none';

    let displayText = text;
    if (hasFile) {
        const fileLabel = `[上传了文件: ${selectedFile.name} (${formatSize(selectedFile.size)})]`;
        displayText = text ? `${fileLabel}\n\n${text}` : `${fileLabel}\n请将这部小说转换为剧本。`;
    }

    addUserMessage(displayText, hasFile);
    scrollToBottom();
    setSending(true);

    const aiMsgId = addAiLoading();
    scrollToBottom();

    try {
        if (hasFile) {
            // --- 文件模式：SSE 流式 + 独立剧本窗口 ---
            const formData = new FormData();
            formData.append('file', selectedFile);
            const api = getApiParams();
            if (api.model) formData.append('model', api.model);
            if (api.base_url) formData.append('base_url', api.base_url);
            if (api.api_key) formData.append('api_key', api.api_key);

            removeMessage(aiMsgId);

            // 打开剧本窗口，初始化流
            resetScriptStream();
            openScriptViewer();
            openViewerBtn.style.display = '';

            try {
                const data = await convertStream(formData);
                scriptContext = JSON.stringify({
                    title: data.title,
                    scenes: data.scenes,
                    characters: data.characters,
                }, null, 2);
                exportBtn.disabled = false;
                conversationHistory.push({ role: 'user', content: displayText });
                conversationHistory.push({
                    role: 'assistant',
                    content: `已生成剧本初稿「${data.title}」：${data.scene_count}场戏，${data.character_count}个角色。`,
                });
                scriptChannel.postMessage({ type: 'done', result: data });
                showToast('剧本生成成功', 'success');
            } catch (e) {
                scriptChannel.postMessage({ type: 'error', error: e.message });
                addAiError(e.message, null);
                showToast('转换失败', 'error');
            }
            setSending(false);
            resetInput();
        } else {
            // --- 文本模式：WS 优先，SSE 降级 ---
            conversationHistory.push({ role: 'user', content: text });

            if (wsReady) {
                removeMessage(aiMsgId);
                const requestId = crypto.randomUUID();
                const streamingId = addAiStreaming(showThinking.checked ? '思考' : null);

                pendingChatRequests.set(requestId, {
                    streamingId,
                    fullContent: '',
                    fullReasoning: '',
                });

                wsSend({
                    action: 'chat',
                    request_id: requestId,
                    messages: conversationHistory,
                    script_context: scriptContext,
                    ...getApiParams(),
                });
                // handleWsMessage 中的 chat_done/chat_error 会处理后续
            } else {
                await handleSendSSE(aiMsgId);
                setSending(false);
                resetInput();
            }
        }
    } catch (err) {
        removeMessage(aiMsgId);
        addAiError(`网络错误: ${err.message}`, null);
        showToast('网络错误', 'error');
        setSending(false);
        resetInput();
    }

    scrollToBottom();
}

/** SSE 降级方案 */
async function handleSendSSE(aiMsgId) {
    const res = await fetch('/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            messages: conversationHistory,
            script_context: scriptContext,
            stream: true,
            ...getApiParams(),
        }),
    });

    removeMessage(aiMsgId);

    if (!res.ok) {
        const err = await res.json();
        const detail = err.error || err.detail || `HTTP ${res.status}`;
        conversationHistory.pop();
        addAiError(detail, showThinking.checked ? JSON.stringify(err, null, 2) : null);
        showToast('请求失败', 'error');
        return;
    }

    const streamingId = addAiStreaming(showThinking.checked ? '思考' : null);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let fullContent = '';
    let fullReasoning = '';
    let readerDone = false;
    let buffer = '';

    while (!readerDone) {
        const { done, value } = await reader.read();
        readerDone = done;
        if (value) {
            buffer += decoder.decode(value, { stream: !done });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const payload = line.slice(6);
                    if (payload === '[DONE]') continue;
                    try {
                        const parsed = JSON.parse(payload);
                        if (parsed.type === 'reasoning') {
                            fullReasoning += parsed.content;
                            updateThinkingContent(streamingId, fullReasoning);
                        } else if (parsed.type === 'content') {
                            fullContent += parsed.content;
                            updateAiStreaming(streamingId, fullContent);
                        } else if (parsed.error) {
                            conversationHistory.pop();
                            addAiError(parsed.error, null);
                            showToast(parsed.error, 'error');
                        }
                    } catch (_) { /* 非 JSON 行，跳过 */ }
                }
            }
        }
    }

    finalizeAiStreaming(streamingId);
    conversationHistory.push({ role: 'assistant', content: fullContent });
}

// ====== 流式转换（SSE 进度 + 实时场景渲染） ======

async function convertStream(formData) {
    const res = await fetch('/api/v1/convert/stream', { method: 'POST', body: formData });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || err.error || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result = null;
    let streamError = null;

    function processLine(line) {
        if (!line.startsWith('data: ')) return;
        const data = JSON.parse(line.slice(6));

        if (data.type === 'error') {
            streamError = data.error;
            throw new Error(data.error);
        }
        if (data.type === 'progress' && data.step) {
            scriptChannel.postMessage({ type: 'progress', step: data.step, percent: data.percent });
        }
        if (data.type === 'chunk_result' && data.data) {
            scriptChannel.postMessage({ type: 'chunk_result', data: data.data });
            if (sentChunks.length < 200) {
                sentChunks.push({ type: 'chunk_result', data: data.data });
            }
        }
        if (data.type === 'done') {
            result = data.result;
        }
    }

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (value) {
                buffer += decoder.decode(value, { stream: true });
            }
            if (done) {
                // 流结束，处理 buffer 中残留的最后一行
                if (buffer.trim()) {
                    try { processLine(buffer.trim()); } catch (_) { /* 末尾残留不完整行 */ }
                }
                break;
            }

            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (line.trim()) {
                    try { processLine(line); } catch (e) {
                        console.error('SSE 处理错误:', e, line);
                    }
                }
            }
        }
    } catch (e) {
        console.error('convertStream 错误:', e);
        scriptChannel.postMessage({ type: 'error', error: e.message });
        throw e;
    }

    if (!result) {
        throw new Error(streamError || '转换未返回结果');
    }
    return result;
}


function setSending(sending) {
    textInput.disabled = sending;
    sendBtn.disabled = sending;
    if (sending) {
        sendBtn.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
    } else {
        sendBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>';
    }
}

// ====== 文本框自适应高度 ======

function autoResizeTextarea() {
    textInput.style.height = 'auto';
    textInput.style.height = Math.min(textInput.scrollHeight, 120) + 'px';
}

// ====== 消息渲染 ======
// 所有消息通过 insertAdjacentHTML 动态插入 chatArea
// 每种消息类型对应一个 add* 函数，返回 DOM id 供后续更新/删除

/** 格式化当前时间为 HH:MM */
function formatTime() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, '0');
    const m = String(now.getMinutes()).padStart(2, '0');
    return `${h}:${m}`;
}

/** 添加用户消息气泡（右侧蓝色） */
function addUserMessage(text, isFile) {
    const id = 'msg-' + Date.now();
    const html = `
        <div class="message user" id="${id}">
            <div class="message-time">${formatTime()}</div>
            <div style="flex:1;"></div>
            <div class="message-body">
                <div class="bubble">${escapeHtml(text)}</div>
            </div>
            <div class="message-avatar">我</div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    return id;
}

function addAiLoading() {
    const id = 'msg-loading-' + Date.now();
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
                <div class="bubble">
                    <div class="typing-indicator">
                        <span></span><span></span><span></span>
                    </div>
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    return id;
}

/** 剧本生成结果（文件上传后的返回） */
function addAiScript(data, showRaw) {
    const id = 'msg-' + Date.now();
    const statsHtml = `
        <div class="script-stats">
            <div class="stat-item">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/></svg>
                <span class="stat-value">${data.scene_count}</span> 场戏
            </div>
            <div class="stat-item">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
                <span class="stat-value">${data.character_count}</span> 个角色
            </div>
        </div>`;

    const charsHtml = data.characters && data.characters.length > 0
        ? `<div class="character-list">${data.characters.map(c =>
            `<span class="character-tag">${escapeHtml(c.name)}</span>`
          ).join('')}</div>`
        : '';

    const scenesList = data.scenes && data.scenes.length > 0
        ? data.scenes.map(s => `第${s.scene_number}场: ${escapeHtml(s.slugline || '未标场地')} (${s.dialogue_count}句对白, ${s.action_count}条动作)`).join('\n')
        : '';

    const rawJson = showRaw && data.script ? JSON.stringify(data.script, null, 2) : '';
    const thinkingHtml = rawJson ? thinkingBoxHtml(id, '原始数据', rawJson) : '';

    const html = `
        <div class="message ai" id="${id}">
            <div class="message-time">${formatTime()}</div>
            <div class="message-avatar">AI</div>
            <div class="message-body">
                ${thinkingHtml}
                <div class="bubble">
                    <div class="ai-header">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                        <span class="ai-label">已生成剧本初稿 — ${escapeHtml(data.title || 'Untitled')}</span>
                    </div>
                    ${statsHtml}
                    ${charsHtml}
                    <div class="script-card">
                        <div class="card-label">场次概览</div>
                        <pre>${scenesList}</pre>
                    </div>
                    <p style="margin-top:14px;font-size:13px;color:var(--text-muted);">
                        剧本已保存至 output/${escapeHtml(data.novel_name)}/${escapeHtml(data.novel_name)}.yaml。你可以继续在下方输入消息，让我帮你修改剧本。
                    </p>
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    return id;
}

function addAiError(detail, rawText) {
    const id = 'msg-err-' + Date.now();
    const thinkingHtml = rawText ? thinkingBoxHtml(id, '原始响应', rawText) : '';
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-time">${formatTime()}</div>
            <div class="message-avatar">AI</div>
            <div class="message-body">
                ${thinkingHtml}
                <div class="bubble">
                    <div class="error-block">${escapeHtml(detail)}</div>
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    return id;
}

/** 创建流式输出的消息气泡（初始为空，带闪烁光标） */
function addAiStreaming(thinkingLabel) {
    const id = 'msg-stream-' + Date.now();
    const thinkingHtml = thinkingLabel ? thinkingBoxHtml(id, thinkingLabel, '') : '';
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-time">${formatTime()}</div>
            <div class="message-avatar">AI</div>
            <div class="message-body">
                ${thinkingHtml}
                <div class="bubble">
                    <div class="streaming-content streaming" id="${id}-content"></div>
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    scrollToBottom();
    return id;
}

/** 更新流式气泡中的文本 */
function updateAiStreaming(msgId, content) {
    const el = document.getElementById(msgId + '-content');
    if (el) {
        el.textContent = content;
        scrollToBottom();
    }
}

/** 更新流式思考内容框（LLM 推理过程流入此框） */
function updateThinkingContent(msgId, content) {
    const body = document.getElementById(msgId + '-thinking-body');
    if (body) {
        body.textContent = content;
        if (!body.classList.contains('open')) body.classList.add('open');
        scrollToBottom();
    }
}

/** 流式输出完成，移除闪烁光标 */
function finalizeAiStreaming(msgId) {
    const el = document.getElementById(msgId + '-content');
    if (el) el.classList.remove('streaming');
}

/** 思考内容框 HTML 片段 */
function thinkingBoxHtml(id, label, content) {
    const openClass = content ? ' open' : '';
    return `<div class="thinking-box" id="${id}-thinking">
        <div class="thinking-header" onclick="this.nextElementSibling.classList.toggle('open')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
            <span>${escapeHtml(label)}</span>
        </div>
        <div class="thinking-body${openClass}" id="${id}-thinking-body">${content ? escapeHtml(content) : ''}</div>
    </div>`;
}

function removeMessage(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// ====== 工具函数 ======

function scrollToBottom() {
    requestAnimationFrame(() => {
        chatArea.scrollTop = chatArea.scrollHeight;
    });
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
