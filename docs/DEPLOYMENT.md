# 模块化版本的部署与回退

## 好友 RSS 每日缓存

新增 `GET /blog/friend-feeds`，供博客“每日一读”和友链更新时间使用。博客构建生成 `community/friend-feeds.json`；sleepy 默认读取 `SLEEPY_ARTICLE_MANIFEST` 同目录下的该文件，也可通过 `SLEEPY_FRIEND_FEEDS_MANIFEST` 指定绝对路径。订阅地址统一维护在博客的 `src/data/friends.ts`。

`python server.py` 启动时运行内部检查线程，其他 WSGI 入口在首次请求时启动。每 24 小时抓取一次，清单变化时提前刷新。缓存默认位于 `SLEEPY_DATA_FILE` 同目录的 `friend-feeds-cache.json`，可用 `SLEEPY_FRIEND_FEEDS_CACHE` 指定。缓存目录必须可写；发布代码和每月重启时保留此文件，无需增加 crontab。单站失败时保留旧记录并标记过期。

发布博客清单与 sleepy 新代码后重启 sleepy。现有 `/api` 转发若覆盖全部后端路径则无需修改；使用路径白名单时加入 `/api/blog/friend-feeds`。

本版本已于 2026-09-20 经用户明确授权完成部署，结果见 REFACTOR_VALIDATION.md。以下流程用于后续版本；后续生产上传与 PM2 重启仍需用户批准。

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
