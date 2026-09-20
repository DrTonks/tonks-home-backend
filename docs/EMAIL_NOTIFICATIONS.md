# 管理员邮件通知

通知模块位于 `sleepy_app/notifications/`。网站进程只在业务数据库事务中保存通知事件，独立 worker 通过 Resend API 发送邮件，不依赖 Redis，不在 Flask 导入时启动线程。代码默认关闭；本版已于2026-09-20经用户授权部署，云端自动通知已开启。

## 事件和范围

- 普通 about/friends 评论、文章/段落评论：自动通过、待审核、拒绝均通知；管理员本人发言排除。
- 公开推荐、友链申请、反馈群聊/主题/回复：新增时通知；同一反馈主题首条消息只通知一次。
- about/friends 在两站共享，只产生一条事件，来源标为博客/主页。没有可靠提交来源时不猜具体网站。
- 点赞、浏览统计、管理员修改状态/删除、管理员新增推荐不通知。
- 初次启用不扫描或补发历史记录。友链指向主页管理入口，文章指向实际文章页面；没有携带管理员密钥的免登录链接。
- 拒绝邮件省略留言正文，审核原因去掉URL后摘要展示，避免转发广告。邮件中的访客输入不会作为HTML执行。

## 配置

私密配置放在 `.env` 或 `SLEEPY_ENV_FILE` 指定文件，禁止写入前端或 Git。

| 变量 | 含义 |
|---|---|
| RESEND_API_KEY | Resend 仅发信、限定 mail.tonks.top 的密钥 |
| SLEEPY_NOTIFICATIONS_ENABLED | 1启用入队和worker；默认0 |
| SLEEPY_NOTIFY_FROM | 发件人，默认 Tonks 网站通知 <notify@mail.tonks.top> |
| SLEEPY_NOTIFY_TO | 必填；逗号分隔的1–10个管理员邮箱 |
| SLEEPY_NOTIFY_KINDS | 默认全部：comment,article_comment,friend_application,feedback,recommendation |
| SLEEPY_NOTIFY_REJECTED | 默认1；0时不记录拒绝通知 |
| SLEEPY_NOTIFY_INTERVAL_SECONDS | 默认2，最少2秒；只运行一个worker |

第一版逐事件发送，暂不做定时摘要；拒绝通知可单独关闭。频率限制控制发送速度，积压任务保存在数据库。推荐在 Resend 域名设置中关闭点击与打开追踪。

配置在进程启动时读取。启用/停用入队需重启网站后端，启用/停用发信需重启/停止worker。只停止worker会积压任务，恢复后继续发送；关闭网站入队开关不会事后补发关闭期间事件。

## 运维命令

在 sleepy 根目录及其 Python 环境运行：

```text
python -m sleepy_app.notifications.worker preview
python -m sleepy_app.notifications.worker status
python -m sleepy_app.notifications.worker run --once
python -m sleepy_app.notifications.worker run
python -m sleepy_app.notifications.worker retry --database community --id 123
python -m sleepy_app.notifications.worker test --confirm-send
```

preview/status不发信；run --once 每个业务数据库最多尝试一条；test --confirm-send 会实际发送一封测试邮件，即使自动通知开关为0，也仅执行明确的测试动作。

status只显示任务类型、编号、状态、次数与脱敏错误，不输出邮件正文或密钥。pending待发，sending已领取，sent表示服务商接受，failed需要检查。sent不代表收件箱投递成功；还需查看Resend投递记录、退信以及QQ垃圾箱。

## 可靠性

notification_outbox分别位于community.sqlite3和recommendations.sqlite3，与对应业务写入同事务；业务保存与任务创建一起成功或回滚。只新增表和索引，不改原表结构，旧版代码可忽略新表。通知写入本身遇到数据库故障时，整笔业务事务回滚，不返回假成功。

SQLite租约避免并发领取，失联任务90秒后可恢复；生产仍只运行一个worker以控制总发送速率。发送前保存发件人、收件人和完整邮件快照，重试沿用同一内容和幂等键；之后改收件人不会改变已尝试任务。

超时、429、5xx以及并发幂等冲突可重试，其他4xx终止。最多8次自动尝试，采用退避并处理Retry-After。Resend幂等期限24小时，本系统限制20小时内重试；超过窗口进入failed，人工重试也不盲目重发。须先查服务商记录确认是否曾接受，再决定是否另建人工通知。

完成任务30天后清空正文/投递快照，保留去重标识和状态。未完成任务不会因清理而丢失。备份业务数据库时同时备份通知记录；恢复旧数据库可能重新激活已发送任务，因此不要把数据回滚作为日常代码回退手段。

## 经批准后的云端上线

1. 本地审查、全量测试、模拟故障测试和发布包预检。
2. 只合并新增 RESEND_API_KEY/SLEEPY_NOTIFY_* 配置，不覆盖云端其他.env配置；文件仅管理员可读。不要在Shell命令或日志打印值。
3. 备份并上传新后端包，先同步配置为关闭，验证原有接口。
4. 明确启用后设置开关1，重启后端，并启动独立worker：

```text
pm2 start /var/sleepy/venv/bin/python --interpreter none --name sleepy-notifications --cwd /var/sleepy --kill-timeout 30000 --restart-delay 5000 -- -m sleepy_app.notifications.worker run
```

5. 检查worker、测试邮件真实到达和失败日志，确认后保存PM2配置。已存在同名worker时更新/重启，不重复创建。
6. 回退先停止worker并关闭入队，再回退代码。保留现有数据库和outbox，避免覆盖新业务数据。

该进程只发送通知，不启动第二个Flask后端或状态上报客户端。SMTP/API无法保证恰好一次投递，幂等窗口以外的异常需要人工核对。

官方依据：[Resend发信API](https://resend.com/docs/api-reference/emails/send-email)、[幂等键](https://resend.com/docs/dashboard/emails/idempotency-keys)。

## 本版验证（2026-09-20）

- 全量171项测试通过；其中通知相关33项，涵盖事务回滚、HTTP拒绝结果到队列到模拟投递、并发领取、失联恢复、快照稳定、重试窗口、模板转义和管理入口。
- 三个子代理参与实现后的交叉/独立审查；发现的清理异常退出、缺库重试、共享来源映射和管理入口说明问题已修复。最终无阻塞发现。
- 完整发布包校验值、解包后真实Waitress预检、worker status/preview通过。
- 经用户明确同意发送且仅发送一封测试邮件；Resend接受，用户确认在QQ收件箱收到。该结果不保证后续每封都进入收件箱。
- 本地自动通知开关保持0；云端已合并新增私密配置并启用自动通知，详见下方部署记录。
- 若日额度耗尽，429可能在8次尝试后失败，不会无限等待至次日；应查服务商记录并在允许窗口内人工重试。

## 云端部署记录（2026-09-20）

用户明确批准本版部署后完成：

- 发布包72文件，安装70文件；保留云端独立report_app.py和原upload_agent_stats.py。独立目录真实Waitress预检通过。
- 只合并通知配置，.env权限600，原非通知配置逐行核对未变。临时密钥传输文件已删除，密钥未写入代码包、命令参数或日志。
- 已启用SLEEPY_NOTIFICATIONS_ENABLED=1；sender为notify@mail.tonks.top，recipient为3064517736@qq.com。本地开关保持0，避免开发时发信。
- PM2 sleepy-server与独立sleepy-notifications进程在线，worker使用显式interpreter none运行虚拟环境Python；保存PM2进程列表以便原有恢复流程加载。
- 启用前后各12项本机及两站公开接口检查通过；四个数据库quick_check正常；文件hash通过；没有补发历史事件，验证时队列为空。
- 最后观察后端pid1853240、通知pid1853267稳定，无新增错误日志。没有创建测试评论或额外发送测试邮件。
- 首次部署将计划内restart_count误判为故障，自动回退成功且保留数据库；修正为比较观察期pid/restart_count是否变化后重新部署成功。没有业务代码回归。

发布目录：/var/sleepy/.releases/mail-20260920。
成功切换前备份：/var/sleepy/.release-backups/mail-20260920-r2（代码、env、数据库及PM2配置）。

回退须先停止/移除通知worker，恢复备份代码与env并重启原后端；保留当前数据库。不要把新业务数据回滚，也不要开启历史补发。
