const API = "/api/plug/astrbot_plugin_reality_companion/page";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

let snapshot = null;
let integration = {};
let selectedUserId = "";
let bridgePromise = null;

const THEME_STORAGE_KEY = "reality-companion-ui-v1";
const DEFAULT_THEME = Object.freeze({
  mode: "system",
  accent: "teal",
  glassOpacity: 86,
  glassBlur: 18,
  motion: true,
});
const DEFAULT_AUDIO_DEVICE = Object.freeze({
  id: "system_default",
  name: "跟随系统默认输出",
  host_api: "系统",
});

function readThemePreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) || "{}");
    const savedOpacity = Number(saved.glassOpacity);
    const savedBlur = Number(saved.glassBlur);
    return {
      ...DEFAULT_THEME,
      mode: ["light", "dark", "system"].includes(saved.mode) ? saved.mode : DEFAULT_THEME.mode,
      accent: ["teal", "blue", "rose", "amber"].includes(saved.accent) ? saved.accent : DEFAULT_THEME.accent,
      glassOpacity: Math.min(98, Math.max(58, Number.isFinite(savedOpacity) ? savedOpacity : DEFAULT_THEME.glassOpacity)),
      glassBlur: Math.min(30, Math.max(0, Number.isFinite(savedBlur) ? savedBlur : DEFAULT_THEME.glassBlur)),
      motion: saved.motion !== false,
    };
  } catch (_) {
    return { ...DEFAULT_THEME };
  }
}

let themePreferences = readThemePreferences();

function resolvedThemeMode(preferences) {
  return preferences.mode === "system"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : preferences.mode;
}

function applyThemePreferences(preferences, persist = false) {
  themePreferences = { ...DEFAULT_THEME, ...preferences };
  const root = document.documentElement;
  root.dataset.theme = resolvedThemeMode(themePreferences);
  root.dataset.accent = themePreferences.accent;
  root.dataset.motion = themePreferences.motion ? "on" : "off";
  root.style.setProperty("--glass-alpha", String(themePreferences.glassOpacity / 100));
  root.style.setProperty("--glass-blur", `${themePreferences.glassBlur}px`);

  $$('[data-theme-mode]').forEach((button) => button.classList.toggle("active", button.dataset.themeMode === themePreferences.mode));
  $$('[data-accent]').forEach((button) => button.classList.toggle("active", button.dataset.accent === themePreferences.accent));
  $("#glass-opacity").value = themePreferences.glassOpacity;
  $("#glass-opacity-output").textContent = `${themePreferences.glassOpacity}%`;
  $("#glass-blur").value = themePreferences.glassBlur;
  $("#glass-blur-output").textContent = `${themePreferences.glassBlur}px`;
  $("#motion-enabled").checked = themePreferences.motion;

  if (persist) {
    try { localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(themePreferences)); } catch (_) { /* Local preferences are optional. */ }
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function numberValue(selector, fallback = 0) {
  const parsed = Number($(selector)?.value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizedAudioDevices(audio) {
  const rows = Array.isArray(audio?.devices) ? audio.devices : [];
  const devices = rows.filter((item) => item && typeof item === "object" && String(item.id || "").trim());
  if (!devices.some((item) => item.id === DEFAULT_AUDIO_DEVICE.id)) devices.unshift({ ...DEFAULT_AUDIO_DEVICE });
  return devices;
}

function toast(message, type = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = type ? `show ${type}` : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = ""; }, 2800);
}

function showManagedPage(managed) {
  $("#managed-page").hidden = !managed;
  $("#app-shell").hidden = managed;
}

function openPluginManager() {
  try { window.top.location.assign("/#/plugins"); }
  catch (_) { window.location.assign("/#/plugins"); }
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = Boolean(busy);
  button.classList.toggle("busy", Boolean(busy));
}

async function withBusy(button, task) {
  setBusy(button, true);
  try {
    return await task();
  } finally {
    setBusy(button, false);
  }
}

async function pageBridge(timeoutMs = 3000) {
  if (!bridgePromise) {
    bridgePromise = (async () => {
      const startedAt = Date.now();
      while (Date.now() - startedAt < timeoutMs) {
        const bridge = window.AstrBotPluginPage;
        if (bridge?.apiGet && bridge?.apiPost) {
          if (bridge.ready) await bridge.ready();
          return bridge;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 80));
      }
      return null;
    })();
  }
  return bridgePromise;
}

async function api(path, options = {}) {
  const bridge = await pageBridge();
  const endpoint = `page/${String(path).replace(/^\/+/, "")}`;
  if (bridge) {
    const method = String(options.method || "GET").toUpperCase();
    const body = options.body ? JSON.parse(options.body) : {};
    const payload = method === "GET" ? await bridge.apiGet(endpoint) : await bridge.apiPost(endpoint, body);
    if (payload?.ok === false || payload?.status === "error") throw new Error(payload.message || "请求失败");
    return payload;
  }
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    credentials: "same-origin",
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.message || `HTTP ${response.status}`);
  return payload;
}

async function action(payload) {
  const result = await api("/action", { method: "POST", body: JSON.stringify(payload) });
  if (result.data) render(result.data);
  return result;
}

function setBadge(node, text, state = "") {
  if (!node) return;
  node.textContent = text;
  node.className = `status-badge${state ? ` ${state}` : ""}`;
}

function configPayload() {
  return {
    action: "save_global_config",
    enabled: $("#global-enabled").checked,
    vision_provider_id: $("#vision-provider").value.trim(),
    timezone: $("#timezone").value.trim(),
    audio_default_playback_volume: numberValue("#default-volume", 35),
    authorized_user_ids: $("#authorized-users").value,
    mobile: {
      enabled: $("#mobile-enabled").checked,
      host: $("#mobile-host").value.trim(),
      port: numberValue("#mobile-port", 6322),
      allowed_user_id: $("#mobile-user").value.trim(),
      pairing_token: $("#mobile-token").value.trim(),
      session_ttl_hours: numberValue("#mobile-session-ttl", 168),
      location_ttl_seconds: numberValue("#mobile-location-ttl", 900),
      proxy_rooms: $("#mobile-proxy-rooms").checked,
      screen_upload_enabled: $("#mobile-screen").checked,
    },
  };
}

function renderOverview(data) {
  const counts = data.counts || {};
  const config = data.configuration || {};
  const mobile = config.mobile || {};
  $("#metric-global").textContent = data.global_enabled ? "已启用" : "已关闭";
  $("#metric-global-note").textContent = integration.private_companion_linked ? "已连接主陪伴插件" : "当前独立运行";
  $("#metric-audio").textContent = String(counts.consented || 0);
  $("#metric-camera").textContent = String(counts.camera_consented || 0);
  $("#metric-reminders").textContent = String(Number(counts.scheduled || 0) + Number(counts.custom_scheduled || 0));
  setBadge($("#integration-badge"), integration.private_companion_linked ? "主插件已连接" : "独立运行", integration.private_companion_linked ? "good" : "warn");

  $("#global-enabled").checked = Boolean(config.enabled ?? data.global_enabled);
  $("#vision-provider").value = config.vision_provider_id || "";
  $("#timezone").value = config.timezone || "Asia/Shanghai";
  $("#authorized-users").value = (config.authorized_user_ids || []).join("\n");
  $("#default-volume").value = config.audio_default_playback_volume ?? 35;
  $("#default-volume-output").textContent = `${config.audio_default_playback_volume ?? 35}%`;

  const audio = data.audio_output || {};
  const camera = data.camera || {};
  const cameraBackendError = camera.backend?.error || "OpenCV 摄像头模块未加载";
  const runtimeRows = [
    ["A", "音频输出", audio.label || "跟随系统默认输出", audio.backend_available ? `${(audio.devices || []).length} 个设备入口` : "仅系统默认"],
    ["C", "摄像头单帧", camera.backend?.available ? `索引 ${camera.camera_index ?? 0}` : cameraBackendError, camera.backend?.available ? (camera.global_enabled ? "已启用" : "已关闭") : "OpenCV 异常"],
    ["M", "Android 网关", mobile.running ? `${mobile.host}:${mobile.bound_port || mobile.port}` : "当前未监听", mobile.enabled ? (mobile.running ? "运行中" : "启动失败") : "已关闭"],
    ["U", "用户范围", `${counts.users || 0} 位可配置用户`, `${counts.proactive_voice || 0} 位启用主动语音`],
  ];
  $("#runtime-list").innerHTML = runtimeRows.map(([icon, title, detail, state]) => `
    <div class="runtime-row"><span class="runtime-icon">${icon}</span><div><b>${escapeHtml(title)}</b><small>${escapeHtml(detail)}</small></div><span>${escapeHtml(state)}</span></div>
  `).join("");
}

function renderDevices(data) {
  const audio = data.audio_output || {};
  const audioDevices = normalizedAudioDevices(audio);
  const selectedAudioId = audioDevices.some((item) => item.id === audio.selected_device_id)
    ? audio.selected_device_id
    : DEFAULT_AUDIO_DEVICE.id;
  setBadge($("#audio-badge"), audio.backend_available ? `${Math.max(0, audioDevices.length - 1)} 个输出设备` : "仅系统默认", audio.backend_available ? "good" : "warn");
  $("#audio-detail").textContent = audio.error || (Array.isArray(audio.devices) ? `当前路由：${audio.label || "跟随系统默认输出"}` : "设备列表暂不可用，已保留系统默认输出。");
  $("#audio-device").innerHTML = audioDevices.map((item) => `
    <option value="${escapeHtml(item.id)}" ${item.id === selectedAudioId ? "selected" : ""}>${escapeHtml(item.name || DEFAULT_AUDIO_DEVICE.name)}${item.host_api && item.id !== "system_default" ? ` · ${escapeHtml(item.host_api)}` : ""}</option>
  `).join("");
  $("#audio-volume").value = audio.playback_volume ?? 35;
  $("#audio-volume-output").textContent = `${audio.playback_volume ?? 35}%`;

  const camera = data.camera || {};
  const devices = camera.devices || [];
  setBadge($("#camera-badge"), camera.backend?.available ? (camera.global_enabled ? "后端正常 · 已启用" : "后端正常 · 已关闭") : "OpenCV 加载异常", camera.backend?.available ? (camera.global_enabled ? "good" : "warn") : "bad");
  $("#camera-enabled").checked = Boolean(camera.global_enabled);
  const currentIndex = Number(camera.camera_index ?? 0);
  const options = devices.map((item) => ({ value: Number(item.index), label: `${item.name || `摄像头 ${item.index}`} · 索引 ${item.index}${item.virtual ? " · 虚拟" : ""}` }));
  if (!options.some((item) => item.value === currentIndex)) options.unshift({ value: currentIndex, label: `当前索引 ${currentIndex}` });
  $("#camera-index").innerHTML = options.map((item) => `<option value="${item.value}" ${item.value === currentIndex ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
  $("#camera-detail").textContent = camera.devices_error || camera.backend?.error || (devices.length ? `已发现 ${devices.length} 个摄像头入口` : "点击扫描以刷新设备清单。");
  $("#camera-min-interval").value = camera.min_interval_seconds ?? 60;
  $("#camera-capture-timeout").value = camera.capture_timeout_seconds ?? 5;
  $("#camera-analysis-timeout").value = camera.analysis_timeout_seconds ?? 25;
  $("#camera-proactive").checked = Boolean(camera.proactive_curiosity_enabled);
  $("#camera-min-tier").value = camera.proactive_min_tier ?? 4;
  $("#camera-max-daily").value = camera.proactive_max_daily ?? 1;
  $("#camera-cooldown").value = camera.proactive_cooldown_minutes ?? 240;
}

function userStatusBadge(text, good) {
  return `<span class="status-badge ${good ? "good" : "warn"}">${escapeHtml(text)}</span>`;
}

function renderUsers(data) {
  const users = data.users || [];
  if (!users.some((item) => item.user_id === selectedUserId)) selectedUserId = users[0]?.user_id || "";
  $("#user-count").textContent = `${users.length} 位`;
  $("#user-list").innerHTML = users.length ? users.map((user) => `
    <button class="user-item ${user.user_id === selectedUserId ? "active" : ""}" type="button" data-user-id="${escapeHtml(user.user_id)}">
      <span class="user-avatar">${escapeHtml((user.label || user.user_id || "U").slice(0, 1).toUpperCase())}</span>
      <span><b>${escapeHtml(user.label || user.user_id)}</b><small>${escapeHtml(user.user_id)}</small></span>
      <i class="user-state-dot ${user.consent?.confirmed ? "good" : ""}"></i>
    </button>
  `).join("") : '<div class="empty-state"><b>暂无用户</b><span>先在私聊中发送“/现实触及”。</span></div>';
  $$("[data-user-id]").forEach((button) => button.addEventListener("click", () => {
    selectedUserId = button.dataset.userId || "";
    renderUsers(snapshot);
  }));
  renderUserDetail(users.find((item) => item.user_id === selectedUserId));
}

function renderUserDetail(user) {
  const root = $("#user-detail");
  if (!user) {
    root.innerHTML = '<div class="empty-state"><b>暂无可配置用户</b><span>用户完成私聊识别后会出现在这里。</span></div>';
    return;
  }
  const consent = user.consent || {};
  const policy = user.policy || {};
  const camera = user.camera || {};
  const alarm = user.alarm || {};
  const cameraReady = camera.eligible && camera.consented;
  const audioReady = consent.local_audio;
  const days = Array.isArray(alarm.days) ? alarm.days.map(Number) : [0,1,2,3,4,5,6];
  const reminders = user.custom_reminders || [];
  const preview = snapshot.camera_preview?.user_id === user.user_id ? snapshot.camera_preview : null;
  root.innerHTML = `
    <header class="user-header">
      <div><h2>${escapeHtml(user.label || user.user_id)}</h2><p>${escapeHtml(user.user_id)}${user.has_private_route ? " · 私聊路由可用" : " · 缺少私聊路由"}</p></div>
      <div class="badge-row">${userStatusBadge(audioReady ? "音频已授权" : "音频未授权", audioReady)}${userStatusBadge(camera.consented ? "摄像头已授权" : "摄像头未授权", camera.consented)}${userStatusBadge(camera.eligible ? "主机用户" : "无摄像头资格", camera.eligible)}</div>
    </header>

    <section class="user-section">
      <div class="user-section-head"><div><h3>主动语音策略</h3><small>控制主动陪伴语音是否同步到所选本机设备。</small></div></div>
      <form id="user-audio-policy" class="settings-form">
        <label class="switch-row full"><span><b>同步主动语音</b><small>${audioReady ? "仍受免打扰和主动频率约束。" : "需先由用户在私聊中完成音频知情确认。"}</small></span><input name="enabled" type="checkbox" ${policy.proactive_voice_enabled ? "checked" : ""} ${audioReady ? "" : "disabled"} /><i aria-hidden="true"></i></label>
        <label class="field full"><span>主动语音音量 <output>${Number(policy.playback_volume ?? 35)}%</output></span><input name="volume" type="range" min="0" max="100" value="${Number(policy.playback_volume ?? 35)}" ${audioReady ? "" : "disabled"} /></label>
        <div class="form-actions"><button class="button primary" type="submit" ${audioReady ? "" : "disabled"}>保存主动语音策略</button></div>
      </form>
    </section>

    <section class="user-section">
      <div class="user-section-head"><div><h3>摄像头用户策略</h3><small>全局摄像头开启后，仍需逐用户开放。</small></div><button id="test-user-camera" class="button compact secondary" type="button" ${cameraReady && snapshot.camera?.global_enabled && camera.enabled ? "" : "disabled"}>读取测试帧</button></div>
      <form id="user-camera-policy" class="settings-form">
        <label class="switch-row full"><span><b>允许明确任务读取单帧</b><small>${cameraReady ? "不会开放持续录像、身份识别或情绪读脸。" : "该用户未完成独立授权或不具备主机资格。"}</small></span><input name="enabled" type="checkbox" ${camera.enabled ? "checked" : ""} ${cameraReady ? "" : "disabled"} /><i aria-hidden="true"></i></label>
        <div class="field-grid two">
          <label class="field"><span>主动视觉策略</span><select name="mode" ${cameraReady ? "" : "disabled"}><option value="off" ${camera.proactive_mode === "off" ? "selected" : ""}>关闭</option><option value="ask" ${camera.proactive_mode === "ask" ? "selected" : ""}>有价值时先询问</option><option value="auto" ${camera.proactive_mode === "auto" ? "selected" : ""}>达到条件可主动单帧</option></select></label>
          <label class="field"><span>用户每日额度</span><input name="limit" type="number" min="-1" max="10" value="${Number(camera.proactive_max_daily ?? -1)}" ${cameraReady ? "" : "disabled"} /><small>-1 继承全局，0 禁止直接主动读取。</small></label>
        </div>
        <div class="form-actions"><button class="button primary" type="submit" ${cameraReady ? "" : "disabled"}>保存摄像头策略</button></div>
      </form>
      ${preview ? `<div class="preview"><img src="${preview.data_url}" alt="最近一次临时摄像头测试帧" /><div><b>临时测试帧</b><br>刷新或离开页面后不保留原图。</div></div>` : ""}
    </section>

    <section class="user-section">
      <div class="user-section-head"><div><h3>起床提醒计划</h3><small>${audioReady ? "按所选设备播放，可等待用户确认或稍后再叫。" : "完成音频授权后才能启用。"}</small></div></div>
      <form id="alarm-form" class="settings-form">
        <label class="switch-row full"><span><b>启用起床提醒</b><small>总开关关闭时会保留计划但不触发。</small></span><input name="enabled" type="checkbox" ${alarm.enabled ? "checked" : ""} ${audioReady ? "" : "disabled"} /><i aria-hidden="true"></i></label>
        <div class="field-grid three">
          <label class="field"><span>时间</span><input name="time" type="time" value="${escapeHtml(alarm.time || "07:30")}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>最多触达次数</span><input name="repeat_count" type="number" min="1" max="6" value="${Number(alarm.repeat_count || 1)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>确认等待（秒）</span><input name="repeat_interval" type="number" min="5" max="300" step="5" value="${Number(alarm.repeat_interval_seconds || 20)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>稍后再叫（分钟）</span><input name="snooze" type="number" min="1" max="120" value="${Number(alarm.snooze_minutes || 10)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>起始音量</span><input name="volume" type="number" min="0" max="100" value="${Number(alarm.playback_volume ?? 35)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>每轮音量增量</span><input name="volume_step" type="number" min="0" max="30" value="${Number(alarm.volume_step ?? 8)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>最高音量</span><input name="max_volume" type="number" min="0" max="100" value="${Number(alarm.max_volume ?? 70)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>淡入时间（毫秒）</span><input name="fade" type="number" min="0" max="5000" step="100" value="${Number(alarm.fade_in_ms ?? 800)}" ${audioReady ? "" : "disabled"} /></label>
          <label class="field"><span>消息交付</span><select name="delivery" ${audioReady ? "" : "disabled"}><option value="chat_on_failure" ${alarm.delivery_mode === "chat_on_failure" ? "selected" : ""}>音频失败时发消息</option><option value="audio_only" ${alarm.delivery_mode === "audio_only" ? "selected" : ""}>仅本机音频</option><option value="audio_and_chat" ${alarm.delivery_mode === "audio_and_chat" ? "selected" : ""}>音频与消息都发送</option></select></label>
        </div>
        <div class="day-picker full">${["一","二","三","四","五","六","日"].map((label, index) => `<label><input type="checkbox" name="day" value="${index}" ${days.includes(index) ? "checked" : ""} ${audioReady ? "" : "disabled"} /><span>周${label}</span></label>`).join("")}</div>
        <label class="switch-row full"><span><b>等待用户确认醒来</b><small>用户回复“醒了”会停止后续触达。</small></span><input name="ack" type="checkbox" ${alarm.require_acknowledgement !== false ? "checked" : ""} ${audioReady ? "" : "disabled"} /><i aria-hidden="true"></i></label>
        <label class="field full"><span>叫醒偏好</span><textarea name="message" rows="3" maxlength="240" ${audioReady ? "" : "disabled"} placeholder="例如：温柔一点，提醒我上午有课">${escapeHtml(alarm.message || "")}</textarea></label>
        <div class="form-actions"><button class="button primary" type="submit" ${audioReady ? "" : "disabled"}>保存提醒计划</button><button id="test-alarm" class="button secondary" type="button" ${audioReady ? "" : "disabled"}>生成并试听</button>${alarm.enabled ? '<button id="disable-alarm" class="button danger" type="button">关闭提醒</button>' : ""}</div>
      </form>
    </section>

    <section class="user-section">
      <div class="user-section-head"><div><h3>自定义现实提醒</h3><small>通过私聊创建，由 AstrBot 官方任务调度。</small></div></div>
      ${reminders.length ? `<div class="reminder-list">${reminders.map((item) => `<div class="reminder-row"><div><b>${escapeHtml(item.topic || "未命名提醒")}</b><small>${escapeHtml(item.scheduled_text || "-")} · ${escapeHtml(item.status || "未知")}</small></div>${["registering","scheduled","triggered","delivering"].includes(item.status) ? `<button class="button compact danger" type="button" data-cancel-reminder="${escapeHtml(item.id)}">取消</button>` : ""}</div>`).join("")}</div>` : '<div class="empty-state"><b>暂无自定义提醒</b><span>在私聊中说“用现实触及提醒我……”即可创建。</span></div>'}
    </section>
  `;
  bindUserDetail(user);
}

function bindUserDetail(user) {
  const audioForm = $("#user-audio-policy");
  audioForm?.elements.volume?.addEventListener("input", () => { $("output", audioForm).textContent = `${audioForm.elements.volume.value}%`; });
  audioForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type='submit']", audioForm);
    try {
      await withBusy(button, () => action({ action: "save_policy", user_id: user.user_id, proactive_voice_enabled: audioForm.elements.enabled.checked, playback_volume: Number(audioForm.elements.volume.value) }));
      toast("主动语音策略已保存", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  const cameraForm = $("#user-camera-policy");
  cameraForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type='submit']", cameraForm);
    try {
      await withBusy(button, () => action({ action: "save_camera_policy", user_id: user.user_id, camera_enabled: cameraForm.elements.enabled.checked, proactive_mode: cameraForm.elements.mode.value, proactive_max_daily: Number(cameraForm.elements.limit.value) }));
      toast("摄像头用户策略已保存", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  $("#test-user-camera")?.addEventListener("click", async (event) => {
    try {
      const result = await withBusy(event.currentTarget, () => action({ action: "test_camera", user_id: user.user_id, purpose: "管理员从现实触及页面手动测试单帧读取" }));
      if (result.result?.status !== "success") throw new Error(result.result?.message || "摄像头读取失败");
      toast("摄像头单帧读取完成", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  const alarmForm = $("#alarm-form");
  const alarmPayload = () => ({
    user_id: user.user_id,
    enabled: alarmForm.elements.enabled.checked,
    time: alarmForm.elements.time.value,
    days: $$('input[name="day"]', alarmForm).filter((item) => item.checked).map((item) => Number(item.value)),
    repeat_count: Number(alarmForm.elements.repeat_count.value),
    repeat_interval_seconds: Number(alarmForm.elements.repeat_interval.value),
    require_acknowledgement: alarmForm.elements.ack.checked,
    snooze_minutes: Number(alarmForm.elements.snooze.value),
    playback_volume: Number(alarmForm.elements.volume.value),
    volume_step: Number(alarmForm.elements.volume_step.value),
    max_volume: Number(alarmForm.elements.max_volume.value),
    fade_in_ms: Number(alarmForm.elements.fade.value),
    delivery_mode: alarmForm.elements.delivery.value,
    message: alarmForm.elements.message.value.trim(),
  });
  alarmForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type='submit']", alarmForm);
    try { await withBusy(button, () => action({ action: "save", ...alarmPayload() })); toast("提醒计划已保存", "success"); } catch (error) { toast(error.message, "error"); }
  });
  $("#test-alarm")?.addEventListener("click", async (event) => {
    try { await withBusy(event.currentTarget, () => action({ action: "test", ...alarmPayload() })); toast("试听已发送到所选设备", "success"); } catch (error) { toast(error.message, "error"); }
  });
  $("#disable-alarm")?.addEventListener("click", async (event) => {
    try { await withBusy(event.currentTarget, () => action({ action: "disable", user_id: user.user_id })); toast("提醒已关闭", "success"); } catch (error) { toast(error.message, "error"); }
  });
  $$('[data-cancel-reminder]').forEach((button) => button.addEventListener("click", async () => {
    try { await withBusy(button, () => action({ action: "cancel_reminder", user_id: user.user_id, reminder_id: button.dataset.cancelReminder })); toast("提醒已取消", "success"); } catch (error) { toast(error.message, "error"); }
  }));
}

function renderMobile(data) {
  const mobile = data.configuration?.mobile || {};
  $("#mobile-enabled").checked = Boolean(mobile.enabled);
  $("#mobile-host").value = mobile.host || "0.0.0.0";
  $("#mobile-port").value = mobile.port ?? 6322;
  $("#mobile-user").value = mobile.allowed_user_id || "";
  $("#mobile-session-ttl").value = mobile.session_ttl_hours ?? 168;
  $("#mobile-location-ttl").value = mobile.location_ttl_seconds ?? 900;
  $("#mobile-proxy-rooms").checked = mobile.proxy_rooms !== false;
  $("#mobile-screen").checked = mobile.screen_upload_enabled !== false;
  $("#mobile-token-note").textContent = mobile.pairing_token_configured ? "已配置令牌；留空将保持原值。" : "尚未配置；启用前请填写至少 24 位随机字符。";
  setBadge($("#mobile-badge"), mobile.running ? "网关运行中" : (mobile.enabled ? "启动失败" : "当前已关闭"), mobile.running ? "good" : (mobile.enabled ? "bad" : "warn"));
  const clientHost = mobile.host === "0.0.0.0" ? "电脑组网 IP" : (mobile.host || "电脑组网 IP");
  const endpoint = mobile.running ? `http://${clientHost}:${mobile.bound_port || mobile.port}` : "网关未监听";
  const gatewayVersion = mobile.gateway_version || "0.2.7";
  const apiVersion = mobile.api_version || "1.0";
  const port = mobile.bound_port || mobile.port || 6322;
  $("#mobile-runtime").innerHTML = `
    <div class="mobile-runtime-meta">
      <span><b>网关版本</b>v${escapeHtml(gatewayVersion)}</span>
      <span><b>终端兼容 API</b>v${escapeHtml(apiVersion)}</span>
      <span><b>房间代理</b>${mobile.proxy_rooms !== false ? "已开启" : "已关闭"}</span>
    </div>
    <code>${escapeHtml(endpoint)}</code>
    <div class="mobile-connection-guide">
      <section><b>怎么连接</b><ul>
        <li><strong>局域网 / Tailscale、ZeroTier：</strong>手机填写 <code>http://电脑组网 IP:${port}</code>，不要填写 127.0.0.1、localhost 或 0.0.0.0。</li>
        <li><strong>跨网络 / 公网：</strong>使用 HTTPS 反向代理或安全隧道；浏览器房间不要使用公网纯 HTTP。</li>
        <li><strong>本机测试：</strong>电脑可访问 <code>http://127.0.0.1:${port}</code>，手机不能访问这个地址。</li>
      </ul><p>保存后，在与 Bot 的私聊发送“现实触及 配对令牌”，再用令牌完成配对。</p></section>
      <section class="mobile-connection-trouble"><b>一起功能一直连接中</b><ul>
        <li>确认房间代理已开启，手机使用移动网关端口，而不是 Together 直连端口（常见为 6321）。</li>
        <li>确认一起房间服务正在运行，并配置了实时共处对话模型。</li>
        <li>修改端口或代理模式后保存并重启网关，再重新打开房间链接。</li>
      </ul></section>
    </div>`;
}

function render(data) {
  snapshot = data;
  renderOverview(data);
  renderDevices(data);
  renderUsers(data);
  renderMobile(data);
  $("#connection-dot").className = "connection-dot online";
  $("#connection-label").textContent = "服务已连接";
}

async function load() {
  const payload = await api("/status");
  integration = payload.integration || {};
  if (integration.managed_by_private_companion) {
    showManagedPage(true);
    return;
  }
  showManagedPage(false);
  render(payload.data || {});
}

const viewMeta = {
  overview: ["运行概览", "设备状态与近期活动"],
  devices: ["设备与摄像头", "输出路由与单帧视觉参数"],
  users: ["用户与提醒", "逐用户授权、策略与计划"],
  mobile: ["Android 网关", "陪伴终端连接与隐私边界"],
};

$$('[data-view]').forEach((button) => button.addEventListener("click", () => {
  const view = button.dataset.view;
  $$('[data-view]').forEach((item) => item.classList.toggle("active", item === button));
  $$('[data-panel]').forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === view));
  $("#view-kicker").textContent = viewMeta[view][0];
  $("#view-title").textContent = viewMeta[view][1];
  window.scrollTo({ top: 0, behavior: "smooth" });
}));

function setThemePanel(open) {
  const panel = $("#theme-panel");
  panel.hidden = !open;
  $("#theme-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("#theme-close").focus();
}

$("#theme-toggle").addEventListener("click", () => setThemePanel($("#theme-panel").hidden));
$("#theme-close").addEventListener("click", () => setThemePanel(false));
$$('[data-theme-mode]').forEach((button) => button.addEventListener("click", () => {
  applyThemePreferences({ ...themePreferences, mode: button.dataset.themeMode }, true);
}));
$$('[data-accent]').forEach((button) => button.addEventListener("click", () => {
  applyThemePreferences({ ...themePreferences, accent: button.dataset.accent }, true);
}));
$("#glass-opacity").addEventListener("input", (event) => {
  applyThemePreferences({ ...themePreferences, glassOpacity: Number(event.target.value) }, true);
});
$("#glass-blur").addEventListener("input", (event) => {
  applyThemePreferences({ ...themePreferences, glassBlur: Number(event.target.value) }, true);
});
$("#motion-enabled").addEventListener("change", (event) => {
  applyThemePreferences({ ...themePreferences, motion: event.target.checked }, true);
});
$("#theme-reset").addEventListener("click", () => {
  applyThemePreferences({ ...DEFAULT_THEME }, true);
  toast("外观已恢复默认", "success");
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (themePreferences.mode === "system") applyThemePreferences(themePreferences);
});

applyThemePreferences(themePreferences);

$("#default-volume").addEventListener("input", (event) => { $("#default-volume-output").textContent = `${event.target.value}%`; });
$("#audio-volume").addEventListener("input", (event) => { $("#audio-volume-output").textContent = `${event.target.value}%`; });
$("#global-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type='submit']", event.currentTarget);
  try { await withBusy(button, () => action(configPayload())); $("#mobile-token").value = ""; toast("基础设置已保存", "success"); } catch (error) { toast(error.message, "error"); }
});
$("#mobile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type='submit']", event.currentTarget);
  try { await withBusy(button, () => action(configPayload())); $("#mobile-token").value = ""; toast("移动端网关配置已保存", "success"); } catch (error) { toast(error.message, "error"); }
});
$("#audio-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type='submit']", event.currentTarget);
  try { await withBusy(button, () => action({ action: "select_output", device_id: $("#audio-device").value, playback_volume: numberValue("#audio-volume", 35) })); toast("输出设备已保存", "success"); } catch (error) { toast(error.message, "error"); }
});
$("#test-audio").addEventListener("click", async (event) => {
  try {
    const result = await withBusy(event.currentTarget, () => action({ action: "test", test_kind: "device", playback_volume: numberValue("#audio-volume", 35) }));
    if (!result.result?.played) throw new Error("测试音播放失败");
    toast("测试音已播放", "success");
  } catch (error) { toast(error.message, "error"); }
});
$("#scan-camera").addEventListener("click", async (event) => {
  try {
    const result = await withBusy(event.currentTarget, () => action({ action: "scan_cameras" }));
    toast((result.result?.devices || []).length ? `发现 ${(result.result?.devices || []).length} 个摄像头入口` : (result.result?.error || "没有发现摄像头"), (result.result?.devices || []).length ? "success" : "error");
  } catch (error) { toast(error.message, "error"); }
});
$("#camera-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type='submit']", event.currentTarget);
  try {
    await withBusy(button, () => action({
      action: "save_camera_config",
      camera_enabled: $("#camera-enabled").checked,
      camera_index: numberValue("#camera-index", 0),
      min_interval_seconds: numberValue("#camera-min-interval", 60),
      capture_timeout_seconds: numberValue("#camera-capture-timeout", 5),
      analysis_timeout_seconds: numberValue("#camera-analysis-timeout", 25),
      proactive_curiosity_enabled: $("#camera-proactive").checked,
      proactive_min_tier: numberValue("#camera-min-tier", 4),
      proactive_max_daily: numberValue("#camera-max-daily", 1),
      proactive_cooldown_minutes: numberValue("#camera-cooldown", 240),
    }));
    toast("摄像头配置已保存", "success");
  } catch (error) { toast(error.message, "error"); }
});
$("#refresh").addEventListener("click", async (event) => {
  event.currentTarget.classList.add("busy");
  try { await load(); toast("状态已刷新", "success"); } catch (error) { toast(error.message, "error"); } finally { event.currentTarget.classList.remove("busy"); }
});
$("#open-companion").addEventListener("click", openPluginManager);

load().catch((error) => {
  showManagedPage(false);
  $("#connection-dot").className = "connection-dot error";
  $("#connection-label").textContent = "连接失败";
  toast(`读取失败：${error.message}`, "error");
});
