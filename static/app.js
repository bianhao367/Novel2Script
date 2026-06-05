/**
 * Novel2Script — 前端交互逻辑
 *   - 常规 AI 对话（文字输入 → /api/v1/chat）
 *   - 小说转剧本（文件上传 → /api/v1/convert）
 *   - 对话历史 + 剧本上下文在两种模式间共享
 */

// ====== DOM 引用 ======
const chatArea   = document.getElementById('chatArea');
const welcome    = document.getElementById('welcome');
const fileInput  = document.getElementById('fileInput');
const fileBtn    = document.getElementById('fileBtn');
const fileChip   = document.getElementById('fileChip');
const chipName   = fileChip.querySelector('.file-chip-name');
const chipRemove = fileChip.querySelector('.file-chip-remove');
const textInput  = document.getElementById('textInput');
const sendBtn    = document.getElementById('sendBtn');
const showThinking = document.getElementById('showThinking');

// 设置弹窗
const settingsBtn  = document.getElementById('settingsBtn');
const settingsModal = document.getElementById('settingsModal');
const modalClose   = document.getElementById('modalClose');
const modalCancel  = document.getElementById('modalCancel');
const modalSave    = document.getElementById('modalSave');
const setBaseUrl   = document.getElementById('setBaseUrl');
const setApiKey    = document.getElementById('setApiKey');
const setModel     = document.getElementById('setModel');

// ====== 状态 ======
let selectedFile = null;
let conversationHistory = [];
let scriptContext = '';

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
});

function bindEvents() {
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

    // 文本框自动调整高度
    textInput.addEventListener('input', autoResizeTextarea);

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
}

function getApiParams() {
    return {
        model: apiSettings.model,
        base_url: apiSettings.base_url,
        api_key: apiSettings.api_key,
    };
}

// ====== 文件选择 ======
function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) setFile(file);
}

function setFile(file) {
    if (!file.name.toLowerCase().endsWith('.txt')) {
        alert('请选择 .txt 文件');
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
    const userMsgId = addUserMessage(displayText, hasFile);
    scrollToBottom();

    // 禁用输入
    setSending(true);

    // 显示 AI 加载动画
    const aiMsgId = addAiLoading();
    scrollToBottom();

    try {
        if (hasFile) {
            // --- 文件模式：调用 /api/v1/convert ---
            const formData = new FormData();
            formData.append('file', selectedFile);
            const api = getApiParams();
            if (api.model) formData.append('model', api.model);
            if (api.base_url) formData.append('base_url', api.base_url);
            if (api.api_key) formData.append('api_key', api.api_key);

            const res = await fetch('/api/v1/convert', {
                method: 'POST',
                body: formData,
            });

            removeMessage(aiMsgId);

            if (!res.ok) {
                const err = await res.json();
                const detail = err.error || err.detail || `HTTP ${res.status}`;
                addAiError(detail, showThinking.checked ? JSON.stringify(err, null, 2) : null);
            } else {
                const data = await res.json();
                // 存储剧本上下文供后续对话使用
                scriptContext = JSON.stringify({
                    title: data.title,
                    scenes: data.scenes,
                    characters: data.characters,
                }, null, 2);
                // 将文件+结果加入对话历史
                conversationHistory.push({
                    role: 'user',
                    content: displayText,
                });
                conversationHistory.push({
                    role: 'assistant',
                    content: `已生成剧本初稿「${data.title}」：${data.scene_count}场戏，${data.character_count}个角色。`,
                });
                addAiScript(data, showThinking.checked);
            }
        } else {
            // --- 文本模式：调用 /api/v1/chat (SSE 流式) ---
            conversationHistory.push({
                role: 'user',
                content: text,
            });

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
            } else {
                // 流式读取 SSE 事件流
                const streamingId = addAiStreaming();
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let fullContent = '';
                let readerDone = false;

                while (!readerDone) {
                    const { done, value } = await reader.read();
                    readerDone = done;
                    if (value) {
                        const chunk = decoder.decode(value, { stream: !done });
                        const lines = chunk.split('\n');
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                const payload = line.slice(6);
                                if (payload === '[DONE]') continue;
                                try {
                                    const parsed = JSON.parse(payload);
                                    if (parsed.content) {
                                        fullContent += parsed.content;
                                        updateAiStreaming(streamingId, fullContent);
                                    } else if (parsed.error) {
                                        conversationHistory.pop();
                                        addAiError(parsed.error, null);
                                    }
                                } catch (_) { /* 忽略不完整的 JSON 行 */ }
                            }
                        }
                    }
                }

                finalizeAiStreaming(streamingId);
                conversationHistory.push({
                    role: 'assistant',
                    content: fullContent,
                });
            }
        }
    } catch (err) {
        removeMessage(aiMsgId);
        addAiError(`网络错误: ${err.message}`, null);
    }

    scrollToBottom();
    setSending(false);

    // 重置状态
    textInput.value = '';
    autoResizeTextarea();
    clearFile();
    textInput.focus();
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

function addUserMessage(text, isFile) {
    const id = 'msg-' + Date.now();
    const html = `
        <div class="message user" id="${id}">
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

/** 纯文本 AI 回复 */
function addAiText(text, rawContext) {
    const id = 'msg-' + Date.now();
    let rawBlock = '';
    if (rawContext) {
        rawBlock = `<div class="thinking-block">
            <button class="thinking-toggle" onclick="this.nextElementSibling.classList.toggle('visible')">查看剧本上下文</button>
            <div class="thinking-content">${escapeHtml(rawContext)}</div>
           </div>`;
    }
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
                <div class="bubble">${escapeHtml(text)}${rawBlock}</div>
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

    const rawJson = data.script ? JSON.stringify(data.script, null, 2) : '';
    const rawBlock = showRaw && rawJson
        ? `<div class="thinking-block">
            <button class="thinking-toggle" onclick="this.nextElementSibling.classList.toggle('visible')">查看原始数据</button>
            <div class="thinking-content visible">${escapeHtml(rawJson)}</div>
           </div>`
        : '';

    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
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
                    ${rawBlock}
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
    let rawBlock = '';
    if (rawText) {
        rawBlock = `<div class="thinking-block">
            <button class="thinking-toggle" onclick="this.nextElementSibling.classList.toggle('visible')">查看原始响应</button>
            <div class="thinking-content">${escapeHtml(rawText)}</div>
           </div>`;
    }
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
                <div class="bubble">
                    <div class="error-block">${escapeHtml(detail)}</div>
                    ${rawBlock}
                </div>
            </div>
        </div>`;
    chatArea.insertAdjacentHTML('beforeend', html);
    return id;
}

/** 创建流式输出的消息气泡（初始为空，带闪烁光标） */
function addAiStreaming() {
    const id = 'msg-stream-' + Date.now();
    const html = `
        <div class="message ai" id="${id}">
            <div class="message-avatar">AI</div>
            <div class="message-body">
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

/** 流式输出完成，移除闪烁光标 */
function finalizeAiStreaming(msgId) {
    const el = document.getElementById(msgId + '-content');
    if (el) el.classList.remove('streaming');
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
