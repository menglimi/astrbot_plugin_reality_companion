const API = "/astrbot_plugin_reality_companion/page";
const $ = (selector) => document.querySelector(selector);
let snapshot = null;

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.message || `HTTP ${response.status}`);
  return payload;
}

function render(data, integration) {
  snapshot = data;
  const counts = data.counts || {};
  $("#global-status").textContent = data.global_enabled ? "已启用" : "已关闭";
  $("#link-status").textContent = integration.private_companion_linked ? "已连接" : "独立运行";
  $("#audio-count").textContent = `${counts.consented || 0} 人`;
  $("#camera-count").textContent = `${counts.camera_consented || 0} 人`;

  const audio = data.audio_output || {};
  $("#audio-backend").textContent = audio.backend_available ? "设备后端可用" : "仅系统默认";
  $("#audio-detail").textContent = audio.error || `当前路由：${audio.label || "跟随系统默认输出"}`;
  const select = $("#audio-device");
  select.innerHTML = (audio.devices || []).map((item) =>
    `<option value="${escapeHtml(item.id)}" ${item.id === audio.selected_device_id ? "selected" : ""}>${escapeHtml(item.name)}</option>`
  ).join("");
  $("#volume").value = audio.playback_volume ?? 35;
  $("#volume-output").textContent = `${audio.playback_volume ?? 35}%`;

  const camera = data.camera || {};
  $("#camera-backend").textContent = camera.global_enabled ? "已启用" : "已关闭";
  $("#camera-detail").textContent = camera.backend?.error || `当前索引：${camera.camera_index ?? 0}`;
  renderCameraDevices(camera.devices || []);
  renderUsers(data.users || []);
}

function renderCameraDevices(devices) {
  $("#camera-devices").innerHTML = devices.length
    ? devices.map((item) => `<div>${escapeHtml(item.name || `摄像头 ${item.index}`)} · 索引 ${Number(item.index)}</div>`).join("")
    : "<div>尚未扫描摄像头</div>";
}

function renderUsers(users) {
  $("#users").innerHTML = users.length ? users.map((user) => {
    const consent = user.consent || {};
    const camera = user.camera || {};
    const alarm = user.alarm || {};
    return `<article class="user-row">
      <div class="user-name"><strong>${escapeHtml(user.label || user.user_id)}</strong><span>${escapeHtml(user.user_id)}</span></div>
      <div class="user-cell"><span>本机音频</span><strong>${consent.local_audio ? "已授权" : "未授权"}</strong></div>
      <div class="user-cell"><span>摄像头单帧</span><strong>${camera.consented ? (camera.enabled ? "已授权并开放" : "已授权，策略关闭") : "未授权"}</strong></div>
      <div class="user-cell"><span>下次提醒</span><strong>${escapeHtml(alarm.next_trigger_text || "未设置")}</strong></div>
    </article>`;
  }).join("") : '<div class="empty">暂无用户记录。请让用户先在私聊中发送“/现实触及”。</div>';
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}

async function load() {
  const payload = await api("/status");
  render(payload.data || {}, payload.integration || {});
}

async function action(payload) {
  const result = await api("/action", { method: "POST", body: JSON.stringify(payload) });
  if (result.data) render(result.data, { private_companion_linked: $("#link-status").textContent === "已连接" });
  return result;
}

$("#refresh").addEventListener("click", () => load().catch((error) => toast(error.message)));
$("#volume").addEventListener("input", (event) => { $("#volume-output").textContent = `${event.target.value}%`; });
$("#save-audio").addEventListener("click", async () => {
  try {
    await action({ action: "select_audio", device_id: $("#audio-device").value, playback_volume: Number($("#volume").value) });
    toast("音频输出已保存");
  } catch (error) { toast(error.message); }
});
$("#test-audio").addEventListener("click", async () => {
  try {
    const payload = await action({ action: "test_audio", playback_volume: Number($("#volume").value) });
    toast(payload.result?.played ? "测试音已播放" : "测试音播放失败");
  } catch (error) { toast(error.message); }
});
$("#scan-camera").addEventListener("click", async () => {
  try {
    const payload = await action({ action: "scan_camera" });
    renderCameraDevices(payload.result?.devices || []);
    toast((payload.result?.devices || []).length ? "摄像头扫描完成" : (payload.result?.error || "没有发现摄像头"));
  } catch (error) { toast(error.message); }
});

load().catch((error) => toast(`读取失败：${error.message}`));
