/**
 * Novel2Script — 前端交互逻辑
 *   - 常规 AI 对话（文字输入 → /api/v1/chat，SSE 流式）
 *   - 小说转剧本（文件上传 → /api/v1/convert）
 *   - 对话历史 + 剧本上下文在两种模式间共享
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

// API 设置（从 localStorage 加载，运行时覆盖 config.py 的默认值）
let apiSettings = {
    base_url: '',
    api_key: '',
    model: '',
};

// ====== 初始化 ======
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    bindEvents();
    checkHealth();
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
        const saved = localStorage.getItem('novel2script_settings');
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
    localStorage.setItem('novel2script_settings', JSON.stringify(apiSettings));
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

    // 隐藏欢迎页
    if (welcome) welcome.style.display = 'none';

    // 构建用户消息的显示文本
    let displayText = text;
    if (hasFile) {
        const fileLabel = `[上传了文件: ${selectedFile.name} (${formatSize(selectedFile.size)})]`;
        displayText = text ? `${fileLabel}\n\n${text}` : `${fileLabel}\n请将这部小说转换为剧本。`;
    }

    // 显示用户消息
    addUserMessage(displayText, hasFile);
    scrollToBottom();

    // 禁用输入
    setSending(true);

    // 显示 AI 加载动画
    const aiMsgId = addAiLoading();
    scrollToBottom();

    try {
        if (hasFile) {
            // --- 文件模式：优先异步，Redis 不可用时降级同步 ---
            const formData = new FormData();
            formData.append('file', selectedFile);
            const api = getApiParams();
            if (api.model) formData.append('model', api.model);
            if (api.base_url) formData.append('base_url', api.base_url);
            if (api.api_key) formData.append('api_key', api.api_key);

            removeMessage(aiMsgId);

            try {
                const data = await convertAsync(formData);
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
                addAiScript(data, showThinking.checked);
                showToast('剧本生成成功', 'success');
            } catch (e) {
                addAiError(e.message, null);
                showToast('转换失败', 'error');
            }
        } else {
            // --- 文本模式：调用 /api/v1/chat (SSE 流式) ---
            conversationHistory.push({ role: 'user', content: text });

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
            } else {
                // 流式读取 SSE 事件流
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
                                } catch (_) { /* 忽略不完整的 JSON 行 */ }
                            }
                        }
                    }
                }

                finalizeAiStreaming(streamingId);
                conversationHistory.push({ role: 'assistant', content: fullContent });
            }
        }
    } catch (err) {
        removeMessage(aiMsgId);
        addAiError(`网络错误: ${err.message}`, null);
        showToast('网络错误', 'error');
    }

    scrollToBottom();
    setSending(false);

    // 重置状态
    textInput.value = '';
    autoResizeTextarea();
    updateCharCount();
    clearFile();
    textInput.focus();
}

// ====== 异步转换（提交→轮询） ======

async function convertAsync(formData) {
    // 先尝试异步模式
    const submitRes = await fetch('/api/v1/convert/async', {
        method: 'POST',
        body: formData,
    });

    if (!submitRes.ok) {
        // 503 = Redis 不可用，降级到同步模式
        if (submitRes.status === 503) {
            return await convertSync(formData);
        }
        const err = await submitRes.json();
        throw new Error(err.detail || err.error || `HTTP ${submitRes.status}`);
    }

    const { task_id, novel_name } = await submitRes.json();
    const progressId = addAiProgress(`正在转换: ${novel_name}`);

    while (true) {
        await sleep(2000);
        const pollRes = await fetch(`/api/v1/tasks/${task_id}`);
        if (!pollRes.ok) {
            removeMessage(progressId);
            throw new Error(`轮询失败: HTTP ${pollRes.status}`);
        }
        const task = await pollRes.json();
        updateAiProgress(progressId, task.step, task.percent);

        if (task.status === 'done') {
            removeMessage(progressId);
            return task.result;
        }
        if (task.status === 'failed') {
            removeMessage(progressId);
            throw new Error(task.error || '转换失败');
        }
    }
}

async function convertSync(formData) {
    const res = await fetch('/api/v1/convert', {
        method: 'POST',
        body: formData,
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || err.error || `HTTP ${res.status}`);
    }
    return await res.json();
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
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

function formatTime() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, '0');
    const m = String(now.getMinutes()).padStart(2, '0');
    return `${h}:${m}`;
}

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
                        剧本已保存至 output/${escapeHtml(data.novel_name)}/script.yaml。你可以继续在下方输入消息，让我帮你修改剧本。
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

/** 进度条消息气泡 */
function addAiProgress(label) {
    const id = 'msg-progress-' + Date.now();
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
                <div class="bubble">
                    <div class="progress-container">
                        <div class="progress-label">${escapeHtml(label)}</div>
                        <div class="progress-bar-track">
                            <div class="progress-bar-fill" id="${id}-fill" style="width: 0%"></div>
                        </div>
                        <div class="progress-step" id="${id}-step">准备中...</div>
                    </div>
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    scrollToBottom();
    return id;
}

function updateAiProgress(msgId, step, percent) {
    const fill = document.getElementById(msgId + '-fill');
    const stepEl = document.getElementById(msgId + '-step');
    if (fill) fill.style.width = percent + '%';
    if (stepEl) {
        const stepLabels = {
            queued: '排队中...',
            reading: '读取小说...',
            chunking: '分块处理...',
            prompting: '构建 Prompt...',
            calling_llm: '调用 LLM（可能需要 10-60 秒）...',
            llm_complete: 'LLM 响应完成...',
            parsing: '解析校验...',
            saving: '保存剧本...',
            done: '完成!',
        };
        stepEl.textContent = stepLabels[step] || step;
    }
    scrollToBottom();
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
