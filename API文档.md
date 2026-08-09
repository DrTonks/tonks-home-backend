# Sleepy 服务器 API 接口文档 v2

## 基础信息

| 项目 | 值 |
|------|-----|
| 服务器地址 | `<SERVER_URL>` |
| 数据格式 | JSON (Content-Type: application/json) |
| 字符编码 | UTF-8 |

服务启动时自动读取与 `server.py` 同目录的 `.env`，进程环境变量优先于文件值。敏感配置包括 `SLEEPY_STATUS_SECRET`、`SLEEPY_ADMIN_SECRET`、`SLEEPY_GITHUB_TOKEN`、`SLEEPY_AI_API_KEY`；`data.json` 只保留运行时业务数据。若旧 `data.json` 仍有对应密钥，只有在 `.env` 已提供非空替代值时才会自动移除旧副本。

## 认证体系

系统使用两级密钥认证，均通过 URL query parameter 传入：

| 密钥类型 | 参数 | 默认值 | 适用接口 |
|----------|------|--------|----------|
| PC 状态密钥 | `?secret=xxx` | `<STATUS_SECRET>`（`.env` 中 `SLEEPY_STATUS_SECRET`） | `GET /set` |
| 管理员密钥 | `?secret=xxx` | `<ADMIN_SECRET>`（`.env` 中 `SLEEPY_ADMIN_SECRET`） | 所有管理接口 |

**认证失败统一响应** (HTTP 200 + JSON):
```json
{
    "success": false,
    "code": "not authorized",
    "message": "invalid admin secret"
}
```

---

## 接口总览

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/` | 无 | 服务健康信息 |
| GET | `/query` | 无 | PC 当前状态 |
| GET | `/get/status_list` | 无 | 全部状态定义 |
| GET | `/online_count` | 无 | 在线人数统计 |
| GET | `/set` | PC 密钥 | 更新 PC 状态 |
| GET | `/agent-activity` | 无 | 活动热力图数据 |
| POST | `/agent-activity` | 管理员 | 上传活动数据 |
| GET | `/blog-posts` | 无 | 最新博客文章 |
| GET | `/music/list` | 无 | 音乐文件列表 |
| GET | `/music/<filename>` | 无 | 音乐流媒体 |
| GET | `/music/lyrics/<filename>` | 无 | 歌词（LRC）|
| POST | `/music/upload` | 管理员 | 上传音乐（可选一并传歌词）|
| POST | `/music/delete` | 管理员 | 删除音乐（联动删歌词）|
| GET | `/calendar/events` | 无 | 日历事件列表 |
| POST | `/calendar/events` | 管理员 | 增/改/删事件 |
| GET | `/calendar/holidays` | 无 | 公共节假日 |
| GET | `/github/stats` | 无 | GitHub 统计 |
| GET | `/images/<filename>` | 无 | 博客项目/时光机图片 |
| POST | `/pet/reply` | 无（服务端限流） | 桌宠单轮 AI 回复，支持 JSON/SSE |
| POST | `/pet/recommendations` | 无（服务端限流） | 向网站作者提交歌/书/游戏/番剧推荐 |
| GET | `/pet/recommendations` | 管理员 | 查询推荐，可按日期和分类筛选 |
| DELETE | `/pet/recommendations/<id>` | 管理员 | 删除一条推荐 |

---

## 公开接口

### GET `/query` — 获取当前 PC 状态

**请求参数**: 无

**响应 — 正常状态 (status=0)**:
```json
{
    "success": true,
    "status": 0,
    "info": {
        "id": 0,
        "name": "VSCode 编辑器",
        "desc": "目前电脑正在使用的应用，大概率在玩手机摸鱼。",
        "color": "awake"
    },
    "timestamp": 1757551064
}
```

**响应 — 离机状态 (status=1)**:
```json
{
    "success": true,
    "status": 1,
    "info": {
        "id": 1,
        "name": "似了",
        "desc": "睡似了或其他原因不在线，紧急情况请使用电话联系。",
        "color": "sleeping"
    },
    "timestamp": 1757551234
}
```

**响应 — 未知状态 (配置错误时)**:
```json
{
    "success": true,
    "status": 99,
    "info": {
        "status": 99,
        "name": "未知"
    },
    "timestamp": null
}
```

**Vue 调用**:
```javascript
async getStatus() {
  const { data } = await axios.get('/query');
  if (data.success) {
    this.currentStatus = data.status;         // 0 | 1 | ...
    this.statusInfo = data.info;              // { id, name, desc, color }
    this.lastUpdate = data.timestamp;          // unix timestamp (seconds)
  }
}
```

---

### GET `/get/status_list` — 获取全部状态定义

**响应**:
```json
[
    {
        "id": 0,
        "name": "APP",
        "desc": "目前手机使用的应用，大概率在玩手机摸鱼。",
        "color": "awake"
    },
    {
        "id": 1,
        "name": "似了",
        "desc": "睡似了或其他原因不在线，紧急情况请使用电话联系。",
        "color": "sleeping"
    }
]
```

> **注意**: 这个接口直接返回数组（无 `success` 包装），这是历史设计，前端需注意。

---

### GET `/online_count` — 在线人数

基于 5 分钟内活跃 IP 的统计，支持 X-Client-ID / isMobile 请求头区分设备。

**响应**:
```json
{
    "success": true,
    "online_count": 3,
    "mobile_count": 1
}
```

**Vue 调用**:
```javascript
async getOnlineCount() {
  const { data } = await axios.get('/online_count');
  this.onlineCount = data.online_count;       // 总在线
  this.mobileCount = data.mobile_count;        // 手机端
}
```

---

### GET `/` — 服务健康信息

返回用于部署检查的 JSON：

```json
{ "success": true, "service": "personal-status-server" }
```

---

### GET `/set` — 更新 PC 状态

**请求参数 (query string)**:

| 参数 | 必填 | 说明 |
|------|------|------|
| `secret` | 是 | PC 状态密钥 |
| `status` | 是 | 状态码（整数，对应 status_list 索引） |
| `app_name` | 是 | 当前应用名称 |
| `timestamp` | 可选 | Unix 时间戳（秒） |

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "set_to": 0,
    "app_name": "VSCode 编辑器"
}
```

**响应 — 密钥错误**:
```json
{
    "success": false,
    "code": "not authorized",
    "message": "invaild secret"
}
```

**响应 — status 不是数字**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "argument 'status' must be a number"
}
```

---

### GET `/agent-activity` — Agent 活动热力图数据

返回 AI 助手使用统计数据，每条包含基于 messageCount 百分位的 `intensity` 字段。

**intensity 等级说明**:

| intensity | 含义 | 判断条件 |
|-----------|------|----------|
| 0 | 无活动 | messageCount = 0 |
| 1 | 低活跃 | messageCount ≤ 25% 分位 |
| 2 | 中等 | 25% < messageCount ≤ 50% 分位 |
| 3 | 高活跃 | 50% < messageCount ≤ 75% 分位 |
| 4 | 极高活跃 | messageCount > 75% 分位 |

**响应 — 有数据**:
```json
{
    "success": true,
    "activities": [
        {
            "date": "2026-06-16",
            "messageCount": 196,
            "sessionCount": 5,
            "toolCallCount": 423,
            "intensity": 1
        },
        {
            "date": "2026-06-28",
            "messageCount": 3636,
            "sessionCount": 8,
            "toolCallCount": 7890,
            "intensity": 4
        },
        {
            "date": "2026-07-04",
            "messageCount": 1,
            "sessionCount": 1,
            "toolCallCount": 2,
            "intensity": 1
        }
    ]
}
```

**响应 — 无数据** (首次部署)：
```json
{
    "success": true,
    "activities": [],
    "note": "No activity data yet. POST to this endpoint with admin secret to upload."
}
```

**Vue 调用** (渲染热力图)：
```javascript
async getAgentActivity() {
  const { data } = await axios.get('/agent-activity');
  if (data.success && data.activities.length > 0) {
    // 按日期建立索引
    this.heatmapData = {};
    data.activities.forEach(a => {
      this.heatmapData[a.date] = {
        count: a.messageCount,
        intensity: a.intensity  // 0-4, 映射为 CSS class: .intensity-0 ~ .intensity-4
      };
    });
  }
}
```

**CSS 颜色方案建议**:
```css
.intensity-0 { background: #ebedf0; }  /* 无活动 — 浅灰 */
.intensity-1 { background: #9be9a8; }  /* 低 — 浅绿 */
.intensity-2 { background: #40c463; }  /* 中 — 绿色 */
.intensity-3 { background: #30a14e; }  /* 高 — 深绿 */
.intensity-4 { background: #216e39; }  /* 极高 — 墨绿 */
```

---

### GET `/blog/views` — 批量读取文章浏览量

重复传入 `slugs` 查询参数，一次读取当前页面所有文章的浏览量，最多 100 篇：

```http
GET /blog/views?slugs=first-post&slugs=folder/second-post
```

```json
{
  "success": true,
  "views": {
    "first-post": 12,
    "folder/second-post": 8
  }
}
```

### POST `/blog/views/<slug>` — 记录文章浏览

进入文章详情页时调用。服务端根据 `X-Client-ID`（缺失时使用 IP 与 User-Agent）
生成不可逆匿名哈希，同一访客、同一文章在 30 分钟时间桶内只计一次。

```json
{
  "success": true,
  "slug": "folder/second-post",
  "views": 9,
  "counted": true
}
```

浏览量存放在独立的 `analytics.sqlite3`，不会对 `data.json` 做高频整文件写入。

---

### GET `/blog-posts` — 最新博客文章 + 精选项目/时间线

从 blog.example.com 获取最新文章 (Atom/RSS)，同时附加最新的项目和时光机条目（按 `startDate` 降序排列，取最新一条）。

**请求参数**:

| 参数 | 默认 | 范围 | 说明 |
|------|------|------|------|
| `count` | 2 | 1-20 | 返回文章数量 |

**响应 — 成功获取全部数据**:
```json
{
    "success": true,
    "count": 2,
    "posts": [
        {
            "title": "贷款平台可视化数据大屏",
            "link": "https://blog.example.com/posts/dashboard/dash/",
            "date": "2025-10-23T00:00:00.000Z",
            "summary": "使用three.js与网上公开建模搭建的数据大屏..."
        },
        {
            "title": "金融学基础 - 通用计算器",
            "link": "https://blog.example.com/posts/calculator/calculator/",
            "date": "2025-10-09T00:00:00.000Z",
            "summary": "覆盖贷款、投资分析与项目评估的教学工具..."
        }
    ],
    "featuredProject": {
        "id": "career-planner-2026",
        "title": "微光职引——职业规划智能体",
        "description": "旨在让大学生快速了解当前就业市场...",
        "image": "/images/projects/fc2026-1.jpg",
        "category": "web",
        "techStack": ["AI", "LLM", "智能体"],
        "status": "in-progress",
        "startDate": "2026-06-01",
        "tags": ["服创", "AI", "职业规划", "智能体"],
        "award": "西部赛区一等奖",
        "featured": true,
        "links": [
            {"name": "预览", "url": "https://career-planner.example.com/", "type": "preview"}
        ],
        "images": ["/images/projects/fc2026-1.jpg"]
    },
    "featuredTimeline": {
        "id": "2026-06-career-planner",
        "title": "第17届中国大学生服务外包创新创业大赛 - 微光职引",
        "description": "参加第17届服创大赛【A13】赛题...",
        "type": "achievement",
        "startDate": "2026-06-01",
        "icon": "material-symbols:emoji-events",
        "location": "四川 成都",
        "organization": "中国大学生服务外包创新创业大赛",
        "achievements": ["西部赛区一等奖 · 晋级国赛"],
        "color": "#EA580C",
        "links": [],
        "images": ["/images/projects/fc2026-1.jpg", "/images/projects/fc2026-2.jpg"]
    }
}
```

**响应 — 博客额外数据不可达时**:
```json
{
    "success": true,
    "count": 2,
    "posts": [ ... ],
    "featuredProject": null,
    "featuredTimeline": null
}
```

> `featuredProject` / `featuredTimeline` 获取失败时为 `null`，不影响文章列表和前端渲染。

**响应 — RSS 服务器不可达**:
```json
{
    "success": true,
    "count": 0,
    "posts": [],
    "featuredProject": null,
    "featuredTimeline": null
}
```

**Vue 调用**:
```javascript
async getBlogPosts(count = 2) {
  const { data } = await axios.get('/blog-posts', { params: { count } });
  if (data.success) {
    this.blogPosts = data.posts.map(p => ({
      ...p,
      displayDate: this.formatDate(p.date)
    }));
    // 精选项目和时光机可能为 null（数据源不可用时）
    this.featuredProject = data.featuredProject;
    this.featuredTimeline = data.featuredTimeline;
  }
}
```

---

### GET `/music/list` — 音乐文件列表

`hasLyrics` 表示该曲是否有配对的 `.lrc` 歌词（后端按文件系统实时判定）；无歌词即纯音乐。

**响应 — 有歌曲**:
```json
{
    "success": true,
    "music": [
        {
            "filename": "song_a1b2c3d4.mp3",
            "title": "晴天",
            "artist": "周杰伦",
            "hasLyrics": true
        },
        {
            "filename": "another_e5f6g7h8.mp3",
            "title": "夜的钢琴曲",
            "artist": "石进",
            "hasLyrics": false
        }
    ]
}
```

**响应 — 无歌曲**:
```json
{
    "success": true,
    "music": []
}
```

**Vue 使用**:
```javascript
async getMusicList() {
  const { data } = await axios.get('/music/list');
  if (data.success) {
    this.songs = data.music.map(s => ({
      ...s,
      // 拼接流媒体 URL
      src: `${this.serverBase}/music/${encodeURIComponent(s.filename)}`
    }));
  }
}
```

---

### GET `/music/<filename>` — 音乐流媒体

直接返回音频二进制流，支持 HTTP Range 请求（断点续传/拖动进度条）。

**响应头**: `Content-Type: audio/mpeg` (根据扩展名自动设置)
**支持格式**: mp3, wav, ogg, flac, m4a, aac

**使用方式**: 直接将 URL 赋给 `<audio>` 的 `src` 属性，无需 JS fetch。

```html
<audio :src="`${serverBase}/music/${song.filename}`" controls />
```

**响应 — 文件不存在**:
```json
{
    "success": false,
    "code": "not found",
    "message": "music file not found: nonexistent.mp3"
}
```

---

### GET `/music/lyrics/<filename>` — 歌词（LRC）

返回该音乐配对的 `.lrc` 歌词纯文本（`Content-Type: text/plain; charset=utf-8`），支持 Range。`<filename>` 传音乐文件名（不含 `.lrc`）。无歌词时返回 `not found`，前端据此按纯音乐处理。

**使用方式**: 先看 `/music/list` 的 `hasLyrics`，为 `true` 再拉歌词。

```javascript
async getLyrics(filename) {
  try {
    const { data } = await axios.get(
      `/music/lyrics/${encodeURIComponent(filename)}`,
      { responseType: 'text' }
    );
    return typeof data === 'string' ? data : '';
  } catch {
    return ''; // 无歌词 / 纯音乐
  }
}
```

**响应 — 无歌词**:
```json
{
    "success": false,
    "code": "not found",
    "message": "lyrics not found: song_a1b2c3d4.mp3"
}
```

---

### GET `/calendar/events` — 日历事件列表

**请求参数**:

| 参数 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `date` | 否 | 按日期筛选 (YYYY-MM-DD 或 YYYY-MM) | `?date=2026-07` |
| `type` | 否 | 按类型筛选 | `?type=work` |

**类型枚举**: `personal` | `work` | `holiday`

**响应 — 全部事件**:
```json
{
    "success": true,
    "events": [
        {
            "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb",
            "date": "2026-07-15",
            "name": "项目截止日",
            "type": "work"
        },
        {
            "id": "ac7e0fa6-bf4b-4018-b335-8da3911bf522",
            "date": "2026-07-20",
            "name": "朋友生日",
            "type": "personal"
        },
        {
            "id": "c8f1ab92-3d41-58e6-9c0f-ef1234567890",
            "date": "2026-08-01",
            "name": "团队 outing",
            "type": "personal"
        }
    ]
}
```

> 事件按 date 升序排列。

**响应 — 按月筛选** (`?date=2026-07`):
```json
{
    "success": true,
    "events": [
        { "id": "79ada2ec-...", "date": "2026-07-15", "name": "项目截止日", "type": "work" },
        { "id": "ac7e0fa6-...", "date": "2026-07-20", "name": "朋友生日", "type": "personal" }
    ]
}
```

**响应 — 按类型筛选** (`?type=work`):
```json
{
    "success": true,
    "events": [
        { "id": "79ada2ec-...", "date": "2026-07-15", "name": "项目截止日", "type": "work" }
    ]
}
```

**响应 — 组合筛选** (`?date=2026-07&type=personal`):
```json
{
    "success": true,
    "events": [
        { "id": "ac7e0fa6-...", "date": "2026-07-20", "name": "朋友生日", "type": "personal" }
    ]
}
```

**响应 — 无匹配事件**:
```json
{
    "success": true,
    "events": []
}
```

**Vue 调用**:
```javascript
async getCalendarEvents(month) {
  const { data } = await axios.get('/calendar/events', {
    params: { date: month }   // e.g. "2026-07"
  });
  if (data.success) {
    // 按日期分组，便于渲染日历格子
    this.eventMap = {};
    data.events.forEach(e => {
      if (!this.eventMap[e.date]) this.eventMap[e.date] = [];
      this.eventMap[e.date].push(e);
    });
  }
}
```

---

### GET `/calendar/holidays` — 公共节假日

**请求参数**:

| 参数 | 默认 | 说明 |
|------|------|------|
| `year` | 当前年份 | 查询年份 |
| `country` | CN | 国家代码 (ISO 3166-1) |

**响应 — 中国 2026 年** (`?year=2026&country=CN`):
```json
{
    "success": true,
    "year": 2026,
    "country": "CN",
    "publicHolidays": [
        { "date": "2026-01-01", "name": "元旦", "countryCode": "CN" },
        { "date": "2026-02-17", "name": "春节", "countryCode": "CN" },
        { "date": "2026-05-01", "name": "劳动节", "countryCode": "CN" },
        { "date": "2026-06-19", "name": "端午节", "countryCode": "CN" },
        { "date": "2026-09-25", "name": "中秋节", "countryCode": "CN" },
        { "date": "2026-10-01", "name": "国庆节", "countryCode": "CN" }
    ],
    "customHolidays": []
}
```

**响应 — 含自定义节日** (用户在日历中添加了 type=holiday 的事件):
```json
{
    "success": true,
    "year": 2026,
    "country": "CN",
    "publicHolidays": [ /* ... 同上 ... */ ],
    "customHolidays": [
        {
            "id": "xxx-xxx-xxx",
            "date": "2026-07-15",
            "name": "个人假期",
            "type": "holiday"
        }
    ]
}
```

**响应 — 外部 API 不可用**:
```json
{
    "success": true,
    "year": 2026,
    "country": "CN",
    "publicHolidays": [],
    "customHolidays": []
}
```

> `publicHolidays` 来自 Nager.Date API，失败时返回空数组。`customHolidays` 来自本地的 calendar_events (type=holiday)，始终可用。

**Vue 调用**:
```javascript
async getHolidays(year) {
  const { data } = await axios.get('/calendar/holidays', {
    params: { year: year || new Date().getFullYear() }
  });
  if (data.success) {
    // 合并公共假日和自定义假日
    this.allHolidays = [
      ...data.publicHolidays,
      ...data.customHolidays
    ];
  }
}
```

---

### GET `/github/stats` — GitHub 贡献统计

获取 GitHub 贡献日历和技术栈数据。返回 365 天的贡献数据，每天有一个 0-4 的 level 值。

**响应 — 成功**:
```json
{
    "success": true,
    "username": "your-github-username",
    "totalContributions": 18,
    "days": [
        { "date": "2025-07-06", "count": 0, "level": 0 },
        { "date": "2025-07-07", "count": 0, "level": 0 },
        { "date": "2025-07-23", "count": 5, "level": 4 },
        { "date": "2025-07-24", "count": 0, "level": 0 }
    ],
    "topLanguages": [
        { "name": "Vue", "color": "#41b883", "stars": 3 },
        { "name": "Python", "color": "#3572A5", "stars": 2 },
        { "name": "TypeScript", "color": "#3178c6", "stars": 1 }
    ]
}
```

**level 等级说明**:
| level | 说明 |
|-------|------|
| 0 | 无贡献 |
| 1 | 低 (≤ 25% 分位) |
| 2 | 中等 |
| 3 | 高 |
| 4 | 极高 (> 75% 分位) |

**响应 — Token 未配置**:
```json
{
    "success": false,
    "error": "GitHub token not configured"
}
```

**响应 — API 限流或网络错误**:
```json
{
    "success": false,
    "error": "GitHub API HTTP 403"
}
```

**Vue 调用** (渲染贡献热力图):
```javascript
async getGitHubStats() {
  try {
    const { data } = await axios.get('/github/stats');
    if (data.success) {
      this.githubUser = data.username;
      this.totalContributions = data.totalContributions;
      this.contributionDays = data.days;   // 52*7 = 364/365 days
      this.topLanguages = data.topLanguages;
    } else {
      // Token 未配置或 API 不可用 — 隐藏 GitHub 卡片
      this.showGitHubCard = false;
    }
  } catch {
    this.showGitHubCard = false;
  }
}
```

> `days` 数组长度为 365 或 364，按日期顺序排列。建议使用 CSS Grid (52列 × 7行) 渲染热力图。

---

### GET `/images/<filename>` — 博客项目/时光机图片

提供博客项目和时间线的图片文件。图片文件需放在服务器的 `images/` 目录下，结构应与博客的 `/images/projects/` 保持一致。

**路径参数**: `<filename>` — 相对于 `images/` 目录的文件路径（如 `projects/fc2026-1.jpg`）

| 状态码 | 含义 |
|--------|------|
| 200 | 图片二进制数据 |
| 404 (JSON) | 文件不存在或路径越界 |

**成功**: 返回图片二进制流，`Content-Type` 根据文件扩展名自动设置。

**错误 — 文件不存在**:
```json
{
    "success": false,
    "code": "not found",
    "message": "image not found: projects/nonexistent.jpg"
}
```

**安全**: 路径遍历攻击已被 `os.path.normpath` 防护，`../data.json` 等请求会被拒绝。

---

## 桌宠 AI 与推荐接口

### POST `/pet/reply` — 桌宠单轮 AI 回复

无登录、无服务端会话历史。后端只接受预先启用的 `question_id`，不接受前端传入系统提示词、模型名、搜索 URL 或任意工具定义。

**请求头**:

| Header | 必填 | 说明 |
|--------|------|------|
| `Content-Type: application/json` | 是 | JSON 请求体 |
| `Accept: text/event-stream` | 否 | 提供时返回 SSE，否则返回普通 JSON |
| `X-Client-ID` | 建议 | 浏览器生成的匿名随机 ID，只用于限流 |

**请求体**:

```json
{
  "pet_id": "static",
  "question_id": "q_recent_music",
  "answer": "《晴天》",
  "context": {
    "previous_answer": "《夜曲》",
    "user_name": "Tonks",
    "city": "福州",
    "weather": { "desc": "多云", "temp": 28 }
  }
}
```

| 字段 | 约束 |
|------|------|
| `pet_id` | `static` 或 `live2d` |
| `question_id` | `pet_ai/questions.json` 中启用的 ID |
| `answer` | 必填字符串，最多 100 字符 |
| `context.previous_answer` | 可选，最多 100 字符；仅允许该字段的问题会保留 |
| `context.user_name` / `context.city` | 可选，各最多 30 字符 |
| `context.weather.temp` | 可选，-80 到 80 |
| 整个请求体 | 最多 4096 字节 |

**普通 JSON 成功响应**:

```json
{
  "success": true,
  "reply": "从《夜曲》换到《晴天》，像是把夜色慢慢听亮了。"
}
```

**SSE 响应事件**:

```text
data: {"type":"status","stage":"thinking"}

data: {"type":"status","stage":"searching"}

data: {"type":"status","stage":"thinking"}

data: {"type":"result","reply":"……"}
```

`searching` 只会在歌曲、书、游戏、番剧问题确实触发搜索时出现。事件不会包含搜索词、URL、工具参数、网页原文或模型思考过程。

对于允许搜索的问题，模型可以主动调用搜索；若模型跳过工具却输出“不知道”“只能按名称猜测”等不确定内容，后端会丢弃该草稿、至多补充一次受限搜索后重新生成。搜索不可用或没有有效结果时才允许保守表达不了解，禁止按作品名称猜测事实。

搜索器不接受模型提供的 URL，只接受最多 120 字符的查询词。服务端仅访问固定的 DuckDuckGo HTML 搜索端点；若无结果或不可用，再访问固定的 Bing RSS 搜索端点。每次响应最多读取 512 KiB、最多保留 3 条各 300 字符的标题/摘要，不向模型提供结果链接，也不会继续抓取结果页面。

系统安全提示词、输出规则和两套人设文件在每次请求开始时从 `pet_ai/prompts/*.md`、`pet_ai/personas/*.md` 读取。部署当前版本后，单独修改这些 Markdown 文件无需重启服务；Python 代码、`.env`、问题白名单或供应商配置发生变化时仍需重启。

普通 JSON 失败示例：

```json
{ "success": false, "code": "rate_limited" }
```

SSE 已开始后发生错误：

```text
data: {"type":"error","code":"provider_unavailable"}
```

常见错误码：`invalid_json`、`body_too_large`、`unknown_pet`、`unknown_question`、`field_too_long`、`rate_limited`、`not_configured`、`provider_rate_limited`、`provider_unavailable`、`reply_failed`。

---

### POST `/pet/recommendations` — 提交推荐

公开接口，用于把用户本次填写的歌曲、书、游戏或番剧推荐给网站作者。该接口只保存推荐内容，不调用大模型。

```json
{
  "category": "music",
  "content": "《晴天》 - 周杰伦",
  "user_name": "Tonks",
  "city": "福州"
}
```

| 字段 | 必填 | 约束 |
|------|------|------|
| `category` | 是 | `music`、`book`、`game`、`anime` 四选一 |
| `content` | 是 | 1–100 字符 |
| `user_name` | 否 | 最多 30 字符；缺失或空值统一存为 `unknown` |
| `city` | 否 | 最多 50 字符；来自浏览器已有的粗粒度城市缓存，缺失或空值统一存为 `unknown` |
| 整个请求体 | 是 | 最多 1024 字节 |

建议同时发送 `X-Client-ID` 请求头。成功响应：

```json
{
  "success": true,
  "code": "OK",
  "recommendation": {
    "id": 17,
    "category": "music",
    "content": "《晴天》 - 周杰伦",
    "user_name": "Tonks",
    "city": "福州",
    "created_at": "2026-08-08T12:30:00+00:00"
  }
}
```

默认限制为同一 IP、同一 `X-Client-ID` 各每分钟 6 次、每天 30 次。`user_name` 只是用户填写的显示名，不是可信身份，`city` 也是客户端提交的显示信息而非可信定位。前端展示 `content`、`user_name` 与 `city` 时必须使用文本节点，不得作为 HTML 插入。推荐接口不接收或保存 IP、经纬度、region、country。

---

### GET `/pet/recommendations` — 管理员查询推荐

管理员接口，需要 `?secret=<ADMIN_SECRET>`。没有筛选参数时返回全部推荐，按 ID 倒序排列；未认证请求不会返回昵称、城市或推荐内容。

| Query 参数 | 必填 | 说明 |
|------------|------|------|
| `category` | 否 | `music`、`book`、`game`、`anime` |
| `date` | 否 | `YYYY-MM-DD`，按记录创建时的服务器本地自然日筛选 |

两个参数可组合：

```http
GET /pet/recommendations?category=book&date=2026-08-08&secret=<ADMIN_SECRET>
```

```json
{
  "success": true,
  "recommendations": [
    {
      "id": 18,
      "category": "book",
      "content": "《献给阿尔吉侬的花束》",
      "user_name": "unknown",
      "city": "unknown",
      "created_at": "2026-08-08T12:40:00+00:00"
    }
  ],
  "count": 1,
  "filters": { "category": "book", "date": "2026-08-08" }
}
```

无结果时 `recommendations` 为 `[]`、`count` 为 `0`。日期无效返回 `invalid_date`，分类无效返回 `unknown_category`。

---

## 管理接口 (需要 `?secret=<ADMIN_SECRET>`)

所有管理接口统一在 URL 上附加 `?secret=<ADMIN_SECRET>` 进行认证。认证失败返回：
```json
{ "success": false, "code": "not authorized", "message": "invalid admin secret" }
```

---

### DELETE `/pet/recommendations/<id>` — 删除推荐

```http
DELETE /pet/recommendations/17?secret=<ADMIN_SECRET>
```

成功响应：

```json
{ "success": true, "code": "OK", "deleted": 17 }
```

记录不存在时返回：

```json
{
  "success": false,
  "code": "not found",
  "message": "recommendation not found"
}
```

---

### POST `/agent-activity` — 上传活动数据

用于本地脚本上传 Claude Code 与 Codex 合并后的活动量。上传内容只包含按日聚合的消息、会话和工具调用计数，不包含 token、提示词或会话原文。

**请求体格式**:

可以直接传数组：
```json
[
    {
        "date": "2026-07-03",
        "messageCount": 457,
        "sessionCount": 1,
        "toolCallCount": 1316
    }
]
```

也可以包装在对象中（兼容 stats-cache.json 格式）：
```json
{
    "dailyActivity": [
        { "date": "2026-07-03", "messageCount": 457, "sessionCount": 1, "toolCallCount": 1316 }
    ]
}
```

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "new": 10,
    "total": 42,
    "removed": 0
}
```

**响应 — 空请求体**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "request body is required"
}
```

**响应 — 非 JSON 格式**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "invalid JSON body"
}
```

**本地脚本示例** (Python):
```python
import json, urllib.request

with open('stats-cache.json', 'r', encoding='utf-8') as f:
    cache = json.load(f)

req = urllib.request.Request(
    '<SERVER_URL>/agent-activity?secret=<ADMIN_SECRET>',
    data=json.dumps(cache['dailyActivity']).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
resp = json.loads(urllib.request.urlopen(req).read())
print(resp)  # {'success': True, 'code': 'OK', 'new': 10, 'total': 42, 'removed': 0}
```

---

### POST `/music/upload` — 上传音乐文件

**请求**: multipart/form-data

| 字段 | 必填 | 说明 |
|------|------|------|
| `file` | 是 | 音频文件 |
| `title` | 否 | 歌曲标题 (默认取文件名) |
| `artist` | 否 | 艺术家 (默认 "Unknown") |
| `lyrics` | 否 | LRC 歌词文件；传了即存为 `<音频文件名>.lrc`，不传则为纯音乐 |

**支持格式**: `.mp3` `.wav` `.ogg` `.flac` `.m4a` `.aac`

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "file": {
        "filename": "song_a1b2c3d4.mp3",
        "title": "晴天",
        "artist": "周杰伦",
        "hasLyrics": true
    }
}
```

**响应 — 无文件**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "no file in request"
}
```

**响应 — 不支持的文件类型**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "unsupported file type: .exe. Allowed: .mp3, .wav, .ogg, .flac, .m4a, .aac"
}
```

**响应 — 保存失败** (磁盘满/权限不足):
```json
{
    "success": false,
    "code": "server error",
    "message": "failed to save file: [Errno 28] No space left on device"
}
```

**curl 示例**:
```bash
curl -X POST "<SERVER_URL>/music/upload?secret=<ADMIN_SECRET>" \
  -F "file=@/path/to/song.mp3" \
  -F "title=晴天" \
  -F "artist=周杰伦" \
  -F "lyrics=@/path/to/song.lrc"
```

**Vue 上传示例**:
```javascript
async uploadMusic(file, title, artist, lyrics) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('title', title || file.name.replace(/\.[^.]+$/, ''));
  formData.append('artist', artist || 'Unknown');
  if (lyrics) formData.append('lyrics', lyrics); // 可选 .lrc

  const { data } = await axios.post(
    '/music/upload?secret=<ADMIN_SECRET>',
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' } }
  );
  // data.file = { filename, title, artist, hasLyrics }
}
```

---

### POST `/music/delete` — 删除音乐文件

删除音频的同时会联动删除其配对的 `.lrc` 歌词（若存在）。

**请求体** (JSON):
```json
{
    "filename": "song_a1b2c3d4.mp3"
}
```

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "deleted": "song_a1b2c3d4.mp3"
}
```

**响应 — 文件不在列表中**:
```json
{
    "success": false,
    "code": "not found",
    "message": "music file not found in list: nonexistent.mp3"
}
```

**响应 — 缺少 filename 参数**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "filename is required"
}
```

**Vue 删除示例**:
```javascript
async deleteMusic(filename) {
  const { data } = await axios.post(
    '/music/delete?secret=<ADMIN_SECRET>',
    { filename }
  );
  if (data.success) {
    this.songs = this.songs.filter(s => s.filename !== filename);
  }
}
```

---

### POST `/calendar/events` — 管理日历事件

统一入口，通过 `action` 字段区分操作。

#### 添加事件 (action=add)

**请求体**:
```json
{
    "action": "add",
    "event": {
        "date": "2026-07-15",
        "name": "项目截止日",
        "type": "work"
    }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `event.date` | 是 | 日期 YYYY-MM-DD |
| `event.name` | 是 | 事件名称 |
| `event.type` | 否 | personal / work / holiday (默认 personal) |

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "event": {
        "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb",
        "date": "2026-07-15",
        "name": "项目截止日",
        "type": "work"
    }
}
```

**响应 — 缺少必填字段**:
```json
{
    "success": false,
    "code": "bad request",
    "message": "event must have 'date' and 'name'"
}
```

#### 更新事件 (action=update)

**请求体**:
```json
{
    "action": "update",
    "event": {
        "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb",
        "name": "新的项目截止日",
        "date": "2026-07-16",
        "type": "work"
    }
}
```

> 只需传要修改的字段，不传的字段保持不变。

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "event": {
        "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb",
        "date": "2026-07-16",
        "name": "新的项目截止日",
        "type": "work"
    }
}
```

**响应 — ID 不存在**:
```json
{
    "success": false,
    "code": "not found",
    "message": "event not found: invalid-id"
}
```

#### 删除事件 (action=delete)

**请求体** (两种格式均可):
```json
{
    "action": "delete",
    "event": {
        "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb"
    }
}
```
```json
{
    "action": "delete",
    "id": "79ada2ec-ea0e-4152-9bfd-54624991dcbb"
}
```

**响应 — 成功**:
```json
{
    "success": true,
    "code": "OK",
    "deleted": "79ada2ec-ea0e-4152-9bfd-54624991dcbb"
}
```

**响应 — ID 不存在**:
```json
{
    "success": false,
    "code": "not found",
    "message": "event not found: invalid-id"
}
```

**Vue 日历 CRUD 示例**:
```javascript
// 添加
async addEvent(date, name, type = 'personal') {
  const { data } = await axios.post('/calendar/events?secret=<ADMIN_SECRET>', {
    action: 'add',
    event: { date, name, type }
  });
  if (data.success) this.events.push(data.event);
}

// 修改
async updateEvent(id, changes) {
  const { data } = await axios.post('/calendar/events?secret=<ADMIN_SECRET>', {
    action: 'update',
    event: { id, ...changes }
  });
  if (data.success) {
    const idx = this.events.findIndex(e => e.id === id);
    if (idx >= 0) Object.assign(this.events[idx], data.event);
  }
}

// 删除
async deleteEvent(id) {
  const { data } = await axios.post('/calendar/events?secret=<ADMIN_SECRET>', {
    action: 'delete',
    event: { id }
  });
  if (data.success) {
    this.events = this.events.filter(e => e.id !== id);
  }
}
```

---

## 错误处理指南

### 统一错误格式

```json
{
    "success": false,
    "code": "ERROR_CODE",
    "message": "Human-readable error message"
}
```

### 常见错误码

| code | HTTP Status | 说明 | 处理建议 |
|------|-------------|------|----------|
| `not authorized` | 200 | 密钥错误或缺失 | 检查 `?secret=` 参数 |
| `bad request` | 200 | 请求参数不合法 | 检查请求体和参数格式 |
| `not found` | 200 | 资源不存在 | 正常业务逻辑，提示用户 |
| `server error` | 200 | 服务器内部错误 | 显示通用错误提示，联系管理员 |

> 所有错误都返回 HTTP 200，通过 JSON 中的 `success: false` 判断。这是项目历史设计。

### 前端统一错误拦截

```javascript
// axios 响应拦截器
axios.interceptors.response.use(
  response => {
    const data = response.data;
    // 处理业务层错误
    if (data && data.success === false) {
      console.warn(`API Error [${data.code}]: ${data.message}`);
      // 不在此处抛异常，让调用方自行判断
    }
    return response;
  },
  error => {
    // 网络层错误 (超时、断网、5xx)
    console.error('Network error:', error.message);
    return Promise.reject(error);
  }
);
```

---

## 外部依赖说明

| 外部服务 | 用途 | 失败影响 |
|----------|------|----------|
| blog.example.com | 博客 RSS | `/blog-posts` 返回空数组 |
| api.github.com | GitHub 统计 | `/github/stats` 返回 error |
| date.nager.at | 公共节假日 | `/calendar/holidays` 中 publicHolidays 为空 |

所有外部依赖失败时均优雅降级，不会影响其他接口的正常工作。

---

## 注意事项

1. **CORS 白名单**: 前后端不同域时，在 `.env` 的 `SLEEPY_CORS_ORIGINS` 中填写逗号分隔的可信来源；同源反向代理无需配置
2. **音乐上传大小限制**: Flask 默认无限制，生产环境建议配置 `MAX_CONTENT_LENGTH`
3. **data.json 并发安全**: 写操作使用线程锁 + 原子写入，多进程部署需注意
4. **时间戳**: `/query` 返回的 timestamp 是 Unix 秒级时间戳
5. **管理员密钥防护**: secret 通过 URL 传参，建议仅在可信网络中使用或通过 HTTPS 加密传输

