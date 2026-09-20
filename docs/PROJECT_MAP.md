# Sleepy 项目地图

本轮已完成模块拆分，2026-09-20 经用户授权完成云端部署。验证记录见 [验收记录](REFACTOR_VALIDATION.md)，未来发布见 [部署与回退](DEPLOYMENT.md)，接口定位见 [HTTP 路由地图](HTTP_ROUTES.md)。

## 请求如何流动

```text
博客 / tonks-home / 本地上报脚本
  → 现有反向代理 → Waitress（16 线程，单进程）
  → server.py → create_app() → Runtime
  → 业务服务 / 段评注册函数 / pet_ai 蓝图
  → SQLite、JSON 文件、媒体文件或外部服务
```

`server.py` 保留启动入口和 `app`；应用工厂装配依赖及路由。每个应用的存储、缓存、限流器和锁由 `Runtime` 独立持有。业务模块不导入 `server.py`，不使用动态全局注入。

## 代码导航

| 位置 | 职责 |
|---|---|
| `server.py` | 兼容 `python server.py` 与 `server:app`；启动 Waitress |
| `sleepy_app/app.py` | 应用工厂、原有路由注册、请求钩子及段评依赖装配 |
| `sleepy_app/config.py` | 统一绝对数据路径；环境变量覆盖 |
| `sleepy_app/runtime.py` | 创建服务、存储、进程内缓存、锁、限流器 |
| `sleepy_app/notifications/` | 新增管理员邮件事件队列、模板、Resend投递及独立worker；详见 EMAIL_NOTIFICATIONS.md |
| `sleepy_app/common/` | 身份校验、响应、环境、日志、代理和 JSON 存储 |
| `sleepy_app/community/` | 普通评论、段评、反馈、友链申请、审核、头像与互动存储 |
| `sleepy_app/blog/` | 博客文章聚合、浏览统计、Agent 活动存储、图片工具 |
| `sleepy_app/status/` | 状态上报、在线人数、历史与热力图 |
| `sleepy_app/personal/` | 日历、待办、推荐服务和推荐存储 |
| `sleepy_app/media/` | 音乐、封面、图片的 HTTP 服务 |
| `sleepy_app/integrations/` | 天气、GeoIP、GitHub、节假日适配 |
| `pet_ai/` | 保留原有桌宠蓝图、模型与工具调用模块 |
| `clients/` | Windows 状态和 Agent 统计上报实现 |
| 根目录 `report_app.py`、`upload_agent_stats.py` | 原命令的薄入口；参数与当前目录不变 |
| `manage_article_views.py` | 浏览量管理命令；与后端共用数据路径 |
| `scripts/` | 代码发布包、隔离启动预检、基准对照、数据回退演练 |
| `tests/` | API、数据、身份、路径、客户端兼容与打包测试 |

## 维护边界

新增业务逻辑放入对应服务；新路由在装配层注册；数据库操作留在存储层。跨业务协作使用明确的 runtime 服务依赖，公共纯函数放入对应领域模块。不要把 runtime 演变成包含业务逻辑的万能类。

原有内部模块已移动：`analytics.py` → `blog/storage.py`，`community.py` → `community/store.py`，`article_comments.py` → `community/articles.py`，`recommendations.py` → `personal/recommendation_store.py`；以上目标均位于 `sleepy_app/` 下。环境、日志、响应和 JSON 存储归入 `common/`。仓库内导入已同步更新；仓库外若直接导入旧内部模块，需要改为新路径。

## 数据与脚本兼容

默认仍使用项目根目录的原文件名。`SLEEPY_DATA_DIR` 可单独指定持久数据根目录；单项路径变量优先，相对单项路径统一相对数据根目录解析。`.env` 默认仍在项目根目录，独立版本部署须明确 `SLEEPY_ENV_FILE`。

SQLite schema、SQL、缓存 TTL、限流规则和线程数未因拆分改变。WAL/SHM 属于活动数据，备份使用 SQLite backup。进程内状态尚未分布式化，不增加 PM2 实例数。

已检查真实存在的 Windows `Startup/start_server.bat` 和项目根目录 bat：仍调用原有两个 Python 入口，无需修改。用户文字中的 `Startup/start/_server.bat` 路径不存在。没有真实运行上报脚本，避免产生无意义的线上状态与统计。

应用工厂隔离的是业务运行状态；环境变量及日志仍属于进程级设置，测试通过明确路径隔离。生产继续单应用、单进程。
