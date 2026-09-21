# 中国节假日

`GET /calendar/holidays?year=2026` 保留 publicHolidays/customHolidays，新增 workdays、holidayStatus、holidayStale、holidaySource、holidayPapers。
publicHolidays 为每天休息记录，workdays 为调休上班日；前端按日期连续性合并假期，近期事项每段假期只显示一次。

数据来自 https://github.com/NateScarlet/holiday-cn （MIT，许可随 bundled 数据保存）。随代码携带2025/2026已公布安排及2027空数据基线，不把未公布当无假期，也不跨年套用日期。
读取本年和次年公告以处理12月跨年调休。补齐紧邻已公布休息日的普通周末，但不能越过调休上班日；推导日标记 inferredWeekend。

缓存位于 data.json 同目录的 holiday-cache/。已缓存请求不等网络；超过24小时后首次访问触发后台更新，失败保留旧值并冷却1小时。没有缓存/基线的历史年份首次请求会同步查询本年及次年，分别设置3秒网络操作超时（不是总耗时的严格上限）。仅支持2007年至当前年+1。
这是按访问触发的每日更新，不新增常驻进程或定时任务。外部数据需校验年份、日期、布尔字段与公告来源，原子写盘，拒绝空数据覆盖已公布安排。

部署正常打包 sleepy_app 即包含基线与许可证，保留 holiday-cache/ 运行数据。现有启动bat和PM2入口不变。先部署后端再部署主页前端，无新增依赖；本次未部署。

测试：python -m unittest tests.test_calendar_holidays
