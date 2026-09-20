"""status/service business operations and HTTP handlers. State is application-scoped."""
from sleepy_app.status.history import resolved_status_info, append_status_history
from sleepy_app.status.heatmap import calc_heatmap_intensity
from sleepy_app.common.environment import configured_value
import sleepy_app.common.responses as u
from flask import request
import time
from markupsafe import escape

class StatusService:
    def __init__(self, runtime):
        self.runtime = runtime

    def update_online_users(self, key, is_mobile=False):
        """记录 key 的最后活跃时间和是否为手机端。
        key: 字符串（例如 'cid:xxx' 或 'ip:1.2.3.4'）
        is_mobile: 布尔，True 表示来自手机客户端
        """
        now = int(time.time())
        with self.runtime.online_lock:
            self.runtime.online_users[key] = {'t': now, 'mobile': bool(is_mobile)}


    def get_online_count(self):
        now = int(time.time())
        mobile_count = 0
        active_count = 0
        with self.runtime.online_lock:
            # 计算并清理过期条目
            for key in list(self.runtime.online_users.keys()):
                entry = self.runtime.online_users.get(key)
                if not entry:
                    continue
                if now - entry.get('t', 0) > self.runtime.ONLINE_TIMEOUT:
                    del self.runtime.online_users[key]
                    continue
                # 仍然活跃
                active_count += 1
                if entry.get('mobile'):
                    mobile_count += 1
        return active_count, mobile_count


    def track_online(self):
        # 在每次请求前统一统计在线信息，避免在各个路由重复调用
        try:
            # 不统计写入接口 /set
            if request.path == '/set':
                return

            key = self.runtime.common_security.get_request_key(request)
            # 读取 isMobile 自定义 header，支持 'true','1','yes' 等
            is_mobile_hdr = request.headers.get('isMobile') or request.headers.get('IsMobile')
            is_mobile = False
            if is_mobile_hdr is not None:
                try:
                    v = str(is_mobile_hdr).strip().lower()
                    if v in ('1', 'true', 'yes', 'on'):
                        is_mobile = True
                except:
                    is_mobile = False
            self.update_online_users(key, is_mobile=is_mobile)
        except Exception:
            # 不影响主流程，记录异常即可
            u.error('track_online failed')


    def index(self):
        return u.format_dict({'success': True, 'service': 'personal-status-server'})


    def query(self):
        self.runtime.d.load()
        self.runtime.common_security.showip(request, '/query')
        st = self.runtime.d.data['status']
        app_name = self.runtime.d.data.get('app_name', '')
        last_ts = self.runtime.d.data.get('timestamp', 0) or 0
        now_ts = int(time.time())

        # 超时检测：10 分钟未收到上报 → 写入"关机中"到 data.json
        TIMEOUT = 600
        if last_ts and now_ts - last_ts > TIMEOUT and app_name != '关机中':
            with self.runtime.write_lock:
                self.runtime.d.load()
                self.runtime.d.data['status'] = 1
                self.runtime.d.data['app_name'] = '关机中'
                self.runtime.d.data['timestamp'] = now_ts
                append_status_history(self.runtime.d.data, 1, '关机中', now_ts)
                self.runtime.d.save()
            app_name = '关机中'
            st = 1

        try:
            stinfo = dict(self.runtime.d.data['status_list'][st])  # copy，避免修改原列表
            if st == 0 or app_name == '关机中':
                stinfo['name'] = app_name
        except:
            stinfo = {
                'status': st,
                'name': '未知'
            }
        timestamp = self.runtime.d.data.get('timestamp', None)
        ret = {
            'success': True,
            'status': st,
            'info': stinfo,
            'timestamp': timestamp
        }
        return u.format_dict(ret)


    def status_history(self):
        self.runtime.d.load()
        items = []
        for entry in reversed(self.runtime.d.data.get('status_history', [])[-5:]):
            status = int(entry.get('status', 99))
            app_name = str(entry.get('app_name') or '')
            items.append({
                'status': status,
                'app_name': app_name,
                'timestamp': entry.get('timestamp'),
                'info': resolved_status_info(self.runtime.d.data, status, app_name),
            })
        return u.format_dict({'success': True, 'history': items})


    def get_status_list(self):
        self.runtime.common_security.showip(request, '/get/status_list')
        stlst = self.runtime.d.dget('status_list')
        return u.format_dict(stlst)


    def online_count(self):
        active_count, mobile_count = self.get_online_count()
        return u.format_dict({"online_count": active_count, "mobile_count": mobile_count, "success": True})


    def set_normal(self):
        # 不记录为在线请求，保留最小日志
        status = escape(request.args.get("status"))
        app_name = escape(request.args.get("app_name"))
        timestamp = request.args.get("timestamp")
        if timestamp is not None:
            try:
                timestamp = int(timestamp)
            except (TypeError, ValueError):
                timestamp = None
        try:
            status = int(status)
        except:
            return self.runtime.common_security.reterr(
                code='bad request',
                message="argument 'status' must be a number"
            )
        secret = request.args.get("secret", "")
        u.info(f'status update requested: status={status}, app_name={app_name}')
        secret_real = configured_value(self.runtime.d, 'SLEEPY_STATUS_SECRET', 'secret', '')
        if secret == secret_real:
            # 用写锁保护写操作，一次性保存避免多次磁盘写入
            with self.runtime.write_lock:
                self.runtime.d.load()
                self.runtime.d.data['status'] = status
                self.runtime.d.data['app_name'] = app_name
                if timestamp is not None:
                    self.runtime.d.data['timestamp'] = timestamp
                effective_timestamp = timestamp if timestamp is not None else int(time.time())
                self.runtime.d.data['timestamp'] = effective_timestamp
                append_status_history(self.runtime.d.data, status, app_name, effective_timestamp)
                self.runtime.d.save()
            u.info('set success')
            ret = {
                'success': True,
                'code': 'OK',
                'set_to': status,
                'app_name':app_name
            }
            return u.format_dict(ret)
        else:
            return self.runtime.common_security.reterr(
                code='not authorized',
                message='invaild secret'
            )


    def agent_activity(self):
        """GET: 返回带强度等级的活动数据（跨机器聚合）；POST: 上传活动数据（需管理员密钥）"""
        if request.method == 'POST':
            # 管理员认证
            auth_err = self.runtime.common_security.require_admin()
            if auth_err:
                return auth_err

            try:
                body = request.get_json(force=True)
            except Exception:
                return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body')

            if body is None:
                return self.runtime.common_security.reterr(code='bad request', message='request body is required')

            # ---- 解析 machineId + dailyActivity ----
            try:
                if isinstance(body, list):
                    # 旧格式：裸数组，无 machineId
                    machine_id = 'unknown'
                    activities = body
                elif isinstance(body, dict):
                    machine_id = str(body.get('machineId') or 'unknown')
                    activities = body.get('dailyActivity', body.get('activities', []))
                else:
                    return self.runtime.common_security.reterr(code='bad request', message='expected JSON object or array')
            except (AttributeError, TypeError):
                return self.runtime.common_security.reterr(code='bad request', message='expected JSON object or array')

            if not isinstance(activities, list):
                return self.runtime.common_security.reterr(code='bad request', message="expected JSON array of activities")

            # ---- 验证并归一化 ----
            normalized = []
            for a in activities:
                if not isinstance(a, dict):
                    continue
                date = a.get('date', '')
                if not date:
                    continue
                try:
                    normalized.append({
                        'date': str(date),
                        'messageCount': int(a.get('messageCount', 0)),
                        'sessionCount': int(a.get('sessionCount', 0)),
                        'toolCallCount': int(a.get('toolCallCount', 0))
                    })
                except (TypeError, ValueError):
                    return self.runtime.common_security.reterr(code='bad request', message='activity counts must be integers')

            if not normalized:
                return u.format_dict({'success': True, 'code': 'OK', 'new': 0, 'total': 0, 'note': 'no valid activities to upsert'})

            # ---- 写入 SQLite（同一 machine+date 覆盖，不同 machine 共存） ----
            new_count, total = self.runtime.agent_store.upsert_activities(machine_id, normalized)

            u.info(f'Agent activity updated [{machine_id}]: {new_count} upserted, {total} total rows')
            return u.format_dict({
                'success': True, 'code': 'OK',
                'new': new_count, 'total': total, 'machineId': machine_id,
            })

        # GET: 返回热力图数据
        # 首次访问时尝试从 data.json 迁移旧数据（仅当 SQLite 仍为空时）
        self.runtime.d.load()
        legacy = self.runtime.d.data.get('agent_activity', [])
        if legacy:
            self.runtime.agent_store.migrate_from_json(legacy)

        activities = self.runtime.agent_store.get_aggregated_activities()
        if not activities:
            return u.format_dict({'success': True, 'activities': [], 'note': 'No activity data yet. POST to this endpoint with admin secret to upload.'})

        result = calc_heatmap_intensity(activities)
        return u.format_dict({'success': True, 'activities': result})

