# F6：community 懒清理计数日志修复报告

> 后续状态（2026-09-08）：已补齐路由契约预期，由独立审查者Dewey重跑全量103/103通过。F6代码及启动行为复审通过，已于13:37发布，PM2健康检查及发布后父agent核验通过；线上独立复核结果见主页最新报告。下文102/103、尚未部署等为实施阶段的历史记录。
>
> 当前完整报告：[修复与多轮复审](../../tonks-home/docs/community-fixes-2026-09-08.md)。云端回滚备份：`/var/backups/tonks-fixes-20260908-053751-231150`（目录0700、文件0600）。本轮不触发线上清理验证，不提交Git。

日期：2026-09-08。状态：本地修复完成，等待独立 agent 复审；未部署。

来源：[community-review-2026-09-08.md](../../tonks-home/docs/community-review-2026-09-08.md) 的 F6。范围仅后端日志配置、相关测试与本文档。没有操作服务器、启动网络监听、部署、暂存或提交。

## 改动

- `runtime_logging.py`：新增 `configure_community_logging()`，只接管精确名称为 `community` 的 logger。logger 与专用 stderr handler 均为 INFO，保留 ERROR 及异常 traceback；`propagate=False`，避免根 handler 屏蔽或重复输出。根 logger、根 handler 和其他 logger 配置保持原样。
- `server.py`：在运行时环境加载后、业务模块/应用初始化前调用配置。直接运行 `server.py` 及导入 `server` 两种入口都生效。
- 专用 handler 名称为 `sleepy.community.stderr`，重复初始化、配置模块重载和 server 重载时复用相同实例。清除 `community` 上其他直接 handler 绑定，但不关闭可能由其他 logger 共用的资源。因此自定义该 logger 的输出应以此启动配置为准；单独导入 `community.py` 不会接管宿主日志。
- `tests/test_community_logging.py`：新增 4 项测试，每项分别覆盖 import/`__main__` 入口，共 8 个独立进程场景；不使用 `assertLogs`、`assertNoLogs` 或捕获时临时提升级别。
- `API文档.md`：在既有懒清理说明末尾追加日志出口和重载行为说明。

没有修改 `community.py` 中的清理算法、触发入口、事务、每日门槛或计数日志语句。原 `tests/test_community.py` 保持原样，其中已有 `assertLogs` 用例继续作为业务日志内容检查；真实启动输出由新增测试独立验证。

## 回归证据

环境：Windows，Python 3.12.4，Flask 3.0.3，Waitress 3.0.2。原 Python 环境缺少 Waitress，测试依赖安装在 `%TEMP%/sleepy-f6-test-deps`，通过 `PYTHONPATH` 使用，没有修改全局依赖或 requirements。

| 验证 | 结果 |
| --- | --- |
| 先新增测试，在未修复代码上运行 | 4 项测试的 8 个场景全部失败：应有 2 条成功计数日志，实际为 0 条 |
| 修复后运行同一测试 | 4/4 通过，8/8 启动场景通过，耗时 5.961 秒 |
| 后端全量 unittest，隔离配置/数据库 | 103 项，102 通过、1 项既有失败，耗时 12.084 秒 |
| 全量中的原社区测试 | 34/34 通过；加上新增启动日志测试共 38 项通过 |
| 用任务开始时 server.py 备份复现失败测试 | 同样失败，缺少相同两个路由，确认不是 F6 修复引入 |
| Git 已跟踪差异检查 | `git diff --check` 通过；只有已有 CRLF→LF 提示 |

新增测试在全新进程的默认 WARNING 根级别下，分别使用无根 handler、WARNING 根 handler、INFO 根 handler，以及已有多个且被禁用的 community handler 配置。每个场景均验证：

1. 真实提交触发真实 SQLite 清理，输出五张表的精确计数：评论 1、主题 1、反馈消息 1、来源快照 1、事件 3，并检查数据确实删除。
2. 重载配置模块及启动入口三次后，handler 实例不变且仅有一个；同日提交不多记成功日志，次日零清理输出一条全零计数。
3. 删除后注入异常，确认回滚、保留待清理数据和已接受的新留言，只输出一次失败日志及 traceback。同日失败门槛仍生效。
4. 外层懒清理调用异常仍输出一次错误和 traceback，不误报成功计数；外键检查通过。
5. community WARNING 和无关 logger WARNING 各输出一次；无关 INFO、community DEBUG、留言正文、邮箱和 actor 标识不出现在日志中。
6. 根级别保持 WARNING；已有根 handler 的实例、级别、formatter、filter 保持不变；无关 logger 配置不变。

`__main__` 场景执行真实 `server.py` 和真实 `waitress.serve()`，包括 Waitress 的 `logging.basicConfig()`；只 mock `waitress.create_server`，断言服务器创建入口和 `run()` 各被调用一次，不创建 socket 或工作线程。import 场景同样执行真实应用初始化。各子进程使用独立临时 cwd、data.json、数据库及音乐/缓存路径，并显式设置不存在的 `SLEEPY_ENV_FILE`，不会加载仓库私有 `.env`。

## 全量回归的既有失败

`test_all_apis.AllApiRoutesTest.test_route_inventory_matches_tested_contract` 的预期路由清单缺少：

- `DELETE /blog/community/feedback/<int:topic_id>`
- `PATCH /blog/community/comments/<int:comment_id>/pin`

已在临时配置中用本任务开始前备份的 `server.py` 执行同一测试，得到完全相同的失败。该清单不属于 F6 日志修复，未修改。因此本报告不声明全量回归全部通过。

## 复跑与已有修改保留

安装仓库 requirements 后，在仓库根目录执行专用测试即可；该文件自行隔离数据库和进程：

```powershell
python -B -m unittest discover -s tests -p test_community_logging.py -v
```

本机若使用上述临时依赖目录，运行前设置 `$env:PYTHONPATH = "$env:TEMP\sleepy-f6-test-deps"`。

全量回归采用 `unittest.defaultTestLoader.discover(repo / 'tests')`，在临时 cwd 写入 `test_all_apis.TEST_CONFIG`，显式设置 `SLEEPY_ENV_FILE`、四个 `SLEEPY_*_DB`、`SLEEPY_MUSIC_DIR` 和 `SLEEPY_GITHUB_CACHE_FILE` 后执行；原测试框架自身的 `.test-tmp` 夹具由其清理。复跑全量应保留这一隔离，避免单独导入 server 时读取本地私有配置。

修改前备份位于 `%TEMP%/sleepy-f6-baseline-om6y_qfu`。已逐字节确认 `community.py` 和 `tests/test_community.py` 与任务开始时一致；从 `server.py` 移除本次 4 行初始化插入后，与原文件完全一致；原 `API文档.md` 的全部字节仍作为当前文件前缀保留。未覆盖已有置顶、保留期及其他改动。

本次没有执行线上清理或验证进程管理器实际落盘；stderr 输出与真实启动日志初始化已在本地验证，线上是否生效仍取决于之后获授权的发布。本报告供独立 agent 复审使用。
