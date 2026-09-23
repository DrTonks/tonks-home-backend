# 模块化版本的部署与回退

## 好友 RSS 每日缓存

新增 `GET /blog/friend-feeds`，供博客“每日一读”和友链更新时间使用。博客构建生成 `community/friend-feeds.json`；sleepy 默认读取 `SLEEPY_ARTICLE_MANIFEST` 同目录下的该文件，也可通过 `SLEEPY_FRIEND_FEEDS_MANIFEST` 指定绝对路径。订阅地址统一维护在博客的 `src/data/friends.ts`。

`python server.py` 启动时运行内部检查线程，其他 WSGI 入口在首次请求时启动。每 24 小时抓取一次，清单变化时提前刷新。缓存默认位于 `SLEEPY_DATA_FILE` 同目录的 `friend-feeds-cache.json`，可用 `SLEEPY_FRIEND_FEEDS_CACHE` 指定。缓存目录必须可写；发布代码和每月重启时保留此文件，无需增加 crontab。单站失败时保留旧记录并标记过期。

发布博客清单与 sleepy 新代码后重启 sleepy。现有 `/api` 转发若覆盖全部后端路径则无需修改；使用路径白名单时加入 `/api/blog/friend-feeds`。

本版本已于 2026-09-20 部署，结果见 REFACTOR_VALIDATION.md。当前服务器固定为 `/var/sleepy`，网站和通知 worker 由 PM2 管理。日常发布可在仓库根目录执行 `pnpm ship`。

## 一键发布

1. 本地先准备 Python 3.10+ 虚拟环境并安装 `requirements.txt`，运行 `pnpm install`。`pnpm ship:check` 检查单元测试和独立数据的 Waitress 预检。正式发布和云端预演要求运行代码、测试及部署脚本已提交；README、部署文档、`example.jsonc`、`.env.example` 的未提交修改不会单独拦截发布。命令还会抓取 `origin/main`，本地 HEAD 必须包含它的最新提交，防止云端出现 Git 无法复原的代码版本。
2. `pnpm ship:dry-run` 上传 SHA-256 校验的代码包，在云端运行同一预检并列出将更新的文件数量，不切换生产代码或重启 PM2。
3. `pnpm ship` 完成相同检查后，备份将替换的代码，短暂停止 `sleepy-server` 与 `sleepy-notifications`，更新代码，执行 `pm2 restart --update-env`，检查两个进程在线和本地 HTTP 接口。正常异常会自动恢复旧代码并重启。备份留在 `/var/sleepy/.release-backups/<release-id>`。
4. 默认保留云端 `.env`、数据库、`data.json`、RSS 缓存、媒体等文件。要更新生产配置，用 `pnpm ship --env-file /绝对路径/production.env`；预演可用 `pnpm ship:dry-run --env-file /绝对路径/production.env`。发布前不会读取或输出密钥值；云端旧 `.env` 会一并备份，异常时恢复。这个参数应指向完整的生产配置，不是增量片段。

命令使用 `../serverSSH.txt` 的连接信息，也可设置 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_PASS` 或 `DEPLOY_KEY_FILE`；覆盖 Python 用 `SLEEPY_PYTHON`。当前脚本只接受已核实的 `/var/sleepy`，并保留云端原有 `report_app.py` 和 `upload_agent_stats.py`，防止覆盖与本仓库不同的独立任务。`requirements.txt` 有变化时会拒绝发布；先单独迁移云端虚拟环境。云端 PM2 进程变量优先于 `.env`，如果待同步的键被 PM2 覆盖，发布会拒绝并列出键名。正常发布会重启服务以重新加载云端 `.env`。

第一次使用时，如果云端代码与本地发布包不同，命令会拒绝覆盖；先对照预演列出的文件，把需要保留的云端改动收录进 Git 并通过测试。首次成功发布后，脚本在 `.releases/ship-state.json` 记录托管代码的摘要；以后如果有人直接在云端改动这些文件，下一次发布会报告冲突并停止。这个摘要不包含 `.env` 或运行数据。

SSH 被中断或远端进程被强制终止时，不能假设自动回退已完成；先检查 PM2、HTTP 状态和对应备份，再决定是否重试。脚本不恢复数据库快照，避免覆盖发布后产生的新数据。

## 发布前

1. 执行 `python -m unittest discover -s tests` 和 `python scripts/preflight.py`。预检启动真实 Waitress，只使用临时目录和本机端口。
2. 执行 `python scripts/package_release.py --output .releases/sleepy-version.zip`。输出路径不能已存在；ZIP 内有 SHA-256 清单。
3. 发布包包含 Python 包、客户端入口、模板、依赖清单及预检脚本，不含 `.env`、数据库、`data.json`、媒体与 `local.env.bat`。不能再仅上传 `server.py`，必须包含新包目录。
4. 保留当前依赖版本和解释器（云端现有 `/var/sleepy/venv/bin/python`），在独立代码目录解压并校验清单。执行包内 `scripts/preflight.py`，不要将预检接到真实数据。

## 路径与切换

- 如继续原部署目录，默认文件名和数据位置不变；如采用版本目录，设置绝对 `SLEEPY_DATA_DIR` 指向现有数据目录、`SLEEPY_ENV_FILE` 指向原私密配置，并检查每个单项路径覆盖。已有绝对路径保持优先。
- 保留实际生产 `SLEEPY_ARTICLE_MANIFEST`（当前博客发布目录的 `community/comment-manifest.json`）；不要用旧清单覆盖博客自动同步结果。
- 获取当前 PM2 脚本、解释器、环境、cwd、代理和健康检查配置作为回退记录。路径切换需要同步 PM2 的 script/cwd，不能只更改一个软链接便假定所有配置跟随。
- 排空请求并停止旧进程写入，完成 SQLite 一致性备份和 JSON/配置备份，然后启动新版本。新旧进程不得同时连接生产数据处理请求；普通 GET 也可能更新状态。
- 验证身份、评论树与删除、文章清单、统计、状态、音乐、桌宠和管理流程后再保存 PM2 配置。保留旧代码版本与备份。

## 回退

数据库结构未改，优先切回旧代码及原启动配置，继续使用当前数据，避免丢失新版本期间的写入。本地已用临时 SQLite 演练“旧版写入 → 新版写入 → 旧版读取两者”，并检查备份完整性。

这不是生产零停机证明。单进程替换存在短暂切换窗口；严格零停机需要另行设计共享状态与写入协调。不要恢复过期数据库来回退代码；只有确认数据损坏并制定数据恢复方案后才进行数据级恢复。
