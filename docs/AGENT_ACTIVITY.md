# Agent 活动采集与历史补传

活动热力图使用 `messageCount` 决定颜色，`toolCallCount` 只作辅助统计。若工具数量持续增加而消息数为 0，优先核查本机日志格式与采集器版本，不要按工具数量伪造消息记录。

## 采集规则

- Claude Code：保持原有主会话消息、主会话及子代理工具调用的统计方式。
- Codex：兼容旧 `event_msg.user_message/agent_message` 和新 `response_item.message`；忽略系统上下文、推理与自动权限审查会话。
- 同时扫描 `$CODEX_HOME/sessions` 与 `$CODEX_HOME/archived_sessions`；未配置时默认 `~/.codex`。
- 使用 `session_meta.id` 归并续接文件，并去除归档/活动文件的重复记录。新旧消息镜像按会话、角色、内容指纹及相邻时间配对，正常重复发言仍计数。
- 日期按 UTC 聚合，保持既有上报习惯。缓存与服务器的同机同日计数仍只增不减，重复补传不累加，其他机器的数据独立保留。
- 内容指纹只用于内存去重；缓存和请求正文仅包含每日消息数、会话数、工具数及机器标识。管理员认证改为 `X-Admin-Secret` 请求头。

## Windows / macOS 更新与补传

1. 找出实际启动脚本、计划任务或 LaunchAgent 所使用的采集器路径、Python、环境变量与 `SLEEPY_MACHINE_ID`。保留原机器标识，防止同一机器变成新的统计来源。
2. 仓库根目录 `upload_agent_stats.py` 是兼容入口，依赖同仓库的 `clients` 包。若现有任务使用独立复制的单文件采集器，应更新为 `clients/upload_agent_stats.py` 的实现文件，或改为调用完整仓库中的兼容入口。
3. 备份当前采集器及 `~/.claude/stats-cache.json`（或既有自定义缓存），不要删除/修改原始会话日志。
4. 使用既有运行环境执行只读检查：

   ```sh
   python3 upload_agent_stats.py --dry-run --days 90
   ```

   此命令不修改缓存、不发送请求。配置了 `CODEX_HOME`、自定义缓存或机器 ID 的任务，应沿用原配置。

5. 确认读取到本机真实日志后，用既有 `SLEEPY_SERVER_URL`、`SLEEPY_ADMIN_SECRET` 环境变量运行：

   ```sh
   python3 upload_agent_stats.py --days 90
   ```

   不要把真实密钥写进命令行、文档或 Git。实际上传会重新计算并更新缓存，随后提交每日汇总。

6. 核对云端该机器的每日计数、上传时间及公开 `/agent-activity` 返回的 `intensity`。确认定时任务下次仍调用新版采集器。

本次修复只涉及本机采集器，不需要发布网站前端、替换云端服务代码或重启 PM2/Apache。各机器必须分别重算；另一台机器没有原始日志时不能代为补算。

## 回归测试

```sh
python3 -B -m unittest discover -s tests -p 'test_upload_agent_stats.py' -v
python3 -B -m unittest discover -s tests -p 'test_client_entrypoints.py' -v
```
