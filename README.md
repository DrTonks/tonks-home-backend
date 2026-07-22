# Personal Status Server

一个面向个人使用的 Flask 后端：公开展示在线状态，接收桌面端状态上报，并提供日历、待办、音乐、博客摘要、GitHub 贡献和 Agent 活动热力图等接口。

本仓库是基于早期 `sleepy` 项目深度改造而来，并不是原项目的镜像。它可作为搭建个人状态页或轻量个人仪表盘后端的参考。

## 功能

- 在线状态与当前前台应用上报
- 浏览器访客与移动端在线人数统计
- GitHub 贡献与技术栈数据
- 博客 RSS/Atom、项目和时间线摘要
- 日历事件、待办事项与音乐文件管理
- Agent 活动热力图：合并 Claude Code 与 Codex 的消息、会话和工具调用数量

Agent 统计只上传按日聚合的活动量；不会读取、保存或上传 token 用量、会话原文或工具输出。

## 技术栈

- Python 3.10+
- Flask + Waitress
- 原生 HTML/CSS/JavaScript 前端资源

## 快速开始

1. 克隆仓库并创建虚拟环境。

   ```powershell
   git clone https://github.com/your-account/personal-status-server.git
   cd personal-status-server
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. 复制并填写私有配置。

   ```powershell
   Copy-Item example.jsonc data.json
   ```

   编辑 `data.json`，至少替换 `secret`、`admin_secret`、用户名和仓库链接。`data.json` 已被 Git 忽略，不能提交。

3. 启动服务。

   ```powershell
   python server.py
   ```

   默认监听 `http://127.0.0.1:9010`（由 `data.json` 的 `host`、`port` 控制）。生产环境建议由 Nginx/Caddy 反向代理并启用 HTTPS。

完整接口见 [API文档.md](API文档.md)。

## Windows 本地上报助手

`start_server.bat` 会顺序执行两件事：

1. `upload_agent_stats.py`：扫描本机 Claude Code 和 Codex 会话，上传合并后的活动量。
2. `report_app.py`：持续上报锁屏状态和当前前台应用。

仓库中的 `前台应用状态.macro` 是对应的 MacroDroid 示例；导入前请把其中的示例 URL 和密钥替换为自己的值。

如需运行第二项 Windows 前台应用上报，另安装其可选依赖：

```powershell
pip install -r requirements-windows-client.txt
```

先复制配置模板：

```powershell
Copy-Item local.env.bat.example local.env.bat
```

然后编辑 `local.env.bat`：

- `SLEEPY_PYTHON`：本机 Python 的绝对路径。
- `SLEEPY_SERVER_URL`：部署后的服务根 URL，不要以 `/` 结尾。
- `SLEEPY_STATUS_SECRET`：对应 `data.json` 的 `secret`。
- `SLEEPY_ADMIN_SECRET`：对应 `data.json` 的 `admin_secret`。

`local.env.bat` 已被 Git 忽略。不要把真实地址、密钥或令牌写回脚本、示例文件或文档。

若单独使用活动统计脚本：

```powershell
python upload_agent_stats.py --server https://status.example.com --secret YOUR_ADMIN_SECRET
```

可用 `--dry-run` 验证扫描结果而不发出网络请求。

## 配置与隐私

以下内容默认不会进入 Git：

- `data.json`：运行时状态、密钥、GitHub token、个人日历/待办等。
- `local.env.bat` 与 `.env*`：本地地址和密钥。
- `部署指南.md`、本地诊断日志与上传的 `music/` 文件。

发布前请执行：

```powershell
git init
git status --ignored
git add .
git diff --cached --check
```

重点确认暂存区中没有 `data.json`、`local.env.bat`、IP 地址、域名、访问令牌、密码或个人内容。若文件此前已被 Git 跟踪，`.gitignore` 不会自动停止跟踪；请先使用 `git rm --cached <file>`，再提交。

## 安全建议

- 不要通过 URL 查询参数长期传递高价值密钥；本项目沿用这一兼容接口，公网部署时应使用 HTTPS，并考虑改为请求头鉴权。
- 请为 `secret` 与 `admin_secret` 使用不同的随机值。
- GitHub token 最小化授权，避免写入权限；不需要 GitHub 卡片时留空即可。
- 服务对外开放前，请在反向代理、防火墙和访问日志层面做好限制。

## 许可证与来源

请在发布前补充你希望采用的 `LICENSE`。如果保留或复用了上游项目代码、资源或设计，请在此处补充上游仓库链接、许可证和归属说明。
