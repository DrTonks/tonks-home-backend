# 博客图片单一源

博客原图是唯一需要管理的素材。继续在博客放图，projects.json/timeline.json填写原始/images/...路径，不需要向sleepy/images重复复制。

后端使用现有SLEEPY_BLOG_BASE_URL（默认https://blog.tonks.top）构造图片绝对URL，在/blog-posts返回副本中同时规范化image和images。项目image保持字符串，时光机image保持数组，images始终为数组。原因是现有主页项目卡片读取image，而其他调用方可能读取images；不是让维护者配置两次。

旧/images/projects/...接口302跳转博客，Cache-Control max-age=300，兼容缓存页面与旧客户端。其他本地图片路由和音乐服务不变。旧文件保留作回滚资料，不再需要更新；尚未删除。图片是否存在由博客响应决定，不逐图HEAD检查，不下载或代理图片内容。

当前返回博客原图而不是指纹WebP；先解决双份维护。后续可缓存博客/_images/manifest.json选取展示图，查找失败回退原图。不要手动猜测WebP文件名。

发布前确认SLEEPY_BLOG_BASE_URL正确，博客/images/projects路径可从主页加载，再验证/blog-posts两个字段和旧接口302。不要在博客原图尚不可访问时删除旧备份。本轮32项API回归和4项图片函数测试通过；没有部署或清理文件。
