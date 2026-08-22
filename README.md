# 我会来到你身边

`astrbot_plugin_reality_companion` 是 AstrBot 陪伴插件系列的现实设备联动插件，承接原“我会永远陪着你”中的现实触及能力。

它独立管理本机音频、摄像头单帧、用户知情授权、现实提醒和设备侧主动语音策略；安装“我会永远陪着你”后，会自动复用其人格上下文、TTS、主要用户权限和官方 Cron 能力。未安装主插件时仍可使用摄像头测试和固定测试音频等基础能力。

## 安装依赖

请使用 AstrBot 自带 Python 安装 `requirements.txt`。Windows 示例：

```powershell
C:\path\to\AstrBot\python\python.exe -m pip install -r requirements.txt
```

OpenCV 只用于任务触发的单帧读取，不会持续录像。AstrBot Desktop 已自带 OpenCV，`requirements.txt` 不会再安装任何额外 `cv2` 包，避免 Windows 便携环境出现二进制扩展递归加载冲突；设备扫描会探测可打开的索引 0 到 7。`sounddevice` 和 `soundfile` 用于将 TTS 音频发送到指定输出设备。

## 授权边界

- 音频与摄像头分别授权、分别撤销。
- 摄像头仅允许 AstrBot 管理员、主插件主要用户或本插件明确配置的用户发起授权。
- 每次只读取一帧，默认不保存原图。
- 工具失败时会返回结构化失败回执，模型不得编造画面内容。

## 移动端网关（供自建客户端对接）

本插件可选地提供独立移动端网关，供你自行维护的客户端接入，不随本插件 Release 分发应用程序或安装包。网关以配对令牌换取短期会话令牌，可提供 Together 房间链接、接收前台位置上下文、维护屏幕共享状态、接收结构化身体/活动数据并支持撤销会话。

从零配对和网络路线请参阅：[手机陪伴终端从零配对说明书](./手机陪伴终端从零配对说明书.md)。

已授权用户可在私聊发送 `现实触及 配对令牌` 查看当前令牌；尚未配置时会自动生成。发送 `现实触及 重置配对令牌` 会生成新令牌、清空已有移动端会话并重启网关。令牌不会在群聊中输出。

设备直达检查同样只接受私聊中的授权用户：`现实触及 摄像头单帧` 输出一次不落盘的摄像头画面，`现实触及 语音试听` 播放固定测试语音，`现实触及 位置检查` 查看手机终端最近一次仍在有效期内的前台位置。

1. 需要 Together 房间时先安装 Together Companion；需要屏幕共享状态时先安装 Screen Companion。
2. 在本插件配置页打开 `mobile.enabled`，把 `mobile.host` 填为 AstrBot 电脑的组网 IP、`mobile.port` 保持 `6322`，设置至少 24 位随机 `pairing_token`，并把 `allowed_user_id` 填为实际使用者的 AstrBot 用户 ID。
3. 保存后重启 AstrBot，使移动网关按新地址重新绑定。可从同一组网设备访问 `http://电脑组网IP:6322/health` 检查网关是否可达。

网关使用独立的 `aiohttp` 服务，不经过需要 Dashboard JWT 的 AstrBot 插件拓展 API。服务默认监听 `0.0.0.0:6322`，但 `0.0.0.0` 只是服务端监听占位地址，客户端应填写电脑实际的组网地址，例如 `http://100.66.1.4:6322`。建议将 `host` 直接设置为电脑的组网 IP；使用 `0.0.0.0` 会同时监听物理局域网等其他网卡。

启用配置示例：

```json
{
  "mobile": {
    "enabled": true,
    "host": "100.66.1.4",
    "port": 6322,
    "public_base_url": "https://companion.example.com",
    "pairing_token": "至少 24 位随机长令牌",
    "allowed_user_id": "主要用户 ID",
    "session_ttl_hours": 168,
    "location_ttl_seconds": 900,
    "amap_reverse_geocode_enabled": false,
    "amap_api_key": "高德 Web 服务 API Key",
    "amap_cache_ttl_seconds": 1800,
    "amap_request_timeout_seconds": 5,
    "telemetry_enabled": false,
    "telemetry_ttl_seconds": 3600,
    "activity_enabled": false,
    "activity_ttl_seconds": 900,
    "screen_upload_enabled": true
  }
}
```

### 高德区域识别

高德接入是可选的位置语义增强：现实触及服务端收到手机位置后，使用高德 Web 服务 API 的逆地理编码得到城市、城区、街道和附近 POI，并按约百米网格做短期内存缓存。高德请求失败、超时或 Key 未配置时，位置上报和原有本地点位识别继续工作。

陪伴插件只接收城市/城区级的 `area_label`，例如“上海市·徐汇区”，用于生成“今天一直在徐汇附近活动”这类模糊背景、区域天气匹配和通勤判断；精确坐标、完整地址和高德 Key 不会注入模型提示，也不会下发到手机。用户主动标记的“家、公司”等地点仍以本地地点认知地图为准，高德结果不会覆盖这些语义。

建议先在高德开放平台创建仅启用“Web 服务 API”的 Key，再在本插件的“Android 网关 → 位置增强”中打开开关并填写。修改开关或 Key 后保存并重启 Reality Companion（或 AstrBot）使配置生效。

Docker + Caddy/Nginx 反向代理时，建议将 `mobile.public_base_url` 设置为手机实际访问的 HTTPS 地址。未设置时，移动网关会按 `X-Forwarded-Host`、`X-Forwarded-Proto`、`Host` 的顺序解析外部地址；只有这些信息都不可用时才回退到本地监听地址。Caddy 至少应转发 `Host`、`X-Forwarded-Host` 和 `X-Forwarded-Proto`。

移动端独立端口的根路径包括 `/health`、`/pair`、`/status`、`/room/create`、`/location`、`/location/heartbeat`、`/location/revoke`、`/device/status`、`/device/activity`、`/telemetry`、`/screen/heartbeat` 和 `/session/close`。设备状态、手机活动摘要、身体数据只保留短期内存态；位置心跳只延长已接收位置的有效期，不修改原始坐标或采集时间。伪窥屏使用 Android `UsageStatsManager`，必须由用户在手机设置中授予“使用情况访问”并在 App 内主动打开；服务端只接受应用类别和脱敏名称，不接收截图、窗口标题、通知或聊天内容。敏感应用默认只保留“私密应用”类别，陪伴插件以低置信度的“最近可能在使用……”语气使用摘要，不把它当成实时窥屏事实。`/telemetry` 仅在 `mobile.telemetry_enabled` 开启后接受最多 32 项结构化数值和受限活动状态，不接受自由文本，不做医疗诊断；陪伴插件只读取经过整理的短期摘要。服务端暂时保留 `/astrbot_plugin_reality_companion/mobile/*` 兼容别名，但新客户端应使用根路径。接口不会返回 CORS 许可头，所有敏感响应均标记为 `no-store`；除配对外均要求会话令牌，配对用户固定为服务端配置的 `allowed_user_id`。修改监听地址、端口或启用开关后请重启 Reality Companion（或重启 AstrBot）使独立服务重新绑定。

外部应用上报示例：

```json
{
  "source": "health_connect",
  "captured_at": 1787132400,
  "measurements": [
    {"type": "heart_rate", "value": 78, "unit": "bpm"},
    {"type": "steps", "value": 6420, "unit": "count"}
  ],
  "activity": {"state": "walking", "duration_minutes": 18}
}
```

自建客户端的屏幕共享可复用 `astrbot_plugin_screen_companion` 的远程 WebSocket：开启 `remote_mode` 并设置 `remote_auth_token`。创建视频通话房间时，网关要求 Together 返回 HTTPS 地址；没有安全地址时会返回 `409`。组网链路应提供端到端加密；若不能保证加密，请在网关前增加 HTTPS 反向代理，避免配对密钥、会话令牌和位置数据以明文传输。

## 统一房间代理（mobile.proxy_rooms，默认开启）

手机陪伴终端创建的一起看 / 游戏 / 工作协同房间，链接统一指向移动网关本身（路径原样保留：`/join`、`/ws`、`/media`、`/avatar` 转发给 Together，`/room/<token>`、`/api/room` 转发给 Game Companion，`/assets` 按 Referer 区分）。Together 与 Game Companion 只需在本机可达，不再要求手机直连其端口或准备公网隧道；绑定 0.0.0.0 的房间服务也会由网关经回环转发。原生 App（client=android_native）的通话房间在代理模式下允许 LAN 明文，浏览器场景仍强制 HTTPS。关闭该开关即恢复旧的直连房间链接行为。
