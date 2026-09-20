"""personal/service business operations and HTTP handlers. State is application-scoped."""
import sleepy_app.common.responses as u
from datetime import datetime, timedelta, timezone
from flask import request
import uuid

class PersonalService:
    def __init__(self, runtime):
        self.runtime = runtime

    def calendar_events(self):
        """GET: 获取日历事件列表；POST: 增/改/删 事件（需管理员密钥）"""
        if request.method == 'POST':
            auth_err = self.runtime.common_security.require_admin()
            if auth_err:
                return auth_err

            try:
                body = request.get_json(force=True)
            except Exception:
                return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body')

            if body is None:
                return self.runtime.common_security.reterr(code='bad request', message='request body is required')

            try:
                if not isinstance(body, dict):
                    return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')
                action = body.get('action', 'add')
            except AttributeError:
                return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')

            if action == 'add':
                event = body.get('event', {})
                if not event.get('date') or not event.get('name'):
                    return self.runtime.common_security.reterr(code='bad request', message="event must have 'date' and 'name'")

                new_event = {
                    'id': str(uuid.uuid4()),
                    'date': str(event['date']),
                    'name': str(event['name']),
                    'type': str(event.get('type', 'personal'))
                }

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    events = list(self.runtime.d.data.get('calendar_events', []))
                    events.append(new_event)
                    self.runtime.d.dset('calendar_events', events)

                u.info(f'Calendar event added: {new_event["date"]} - {new_event["name"]}')
                return u.format_dict({'success': True, 'code': 'OK', 'event': new_event})

            elif action == 'update':
                event = body.get('event', {})
                event_id = event.get('id', '')
                if not event_id:
                    return self.runtime.common_security.reterr(code='bad request', message="event must have 'id' for update")

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    events = list(self.runtime.d.data.get('calendar_events', []))
                    found = False
                    for i, e in enumerate(events):
                        if e.get('id') == event_id:
                            if 'date' in event:
                                events[i]['date'] = str(event['date'])
                            if 'name' in event:
                                events[i]['name'] = str(event['name'])
                            if 'type' in event:
                                events[i]['type'] = str(event['type'])
                            found = True
                            updated = events[i]
                            break

                    if not found:
                        return self.runtime.common_security.reterr(code='not found', message=f'event not found: {event_id}')

                    self.runtime.d.dset('calendar_events', events)

                u.info(f'Calendar event updated: {updated["date"]} - {updated["name"]}')
                return u.format_dict({'success': True, 'code': 'OK', 'event': updated})

            elif action == 'delete':
                event_id = body.get('event', {}).get('id', body.get('id', ''))
                if not event_id:
                    return self.runtime.common_security.reterr(code='bad request', message="'id' is required for delete")

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    events = list(self.runtime.d.data.get('calendar_events', []))
                    new_events = [e for e in events if e.get('id') != event_id]

                    if len(new_events) == len(events):
                        return self.runtime.common_security.reterr(code='not found', message=f'event not found: {event_id}')

                    self.runtime.d.dset('calendar_events', new_events)

                u.info(f'Calendar event deleted: {event_id}')
                return u.format_dict({'success': True, 'code': 'OK', 'deleted': event_id})

            else:
                return self.runtime.common_security.reterr(code='bad request', message=f"unknown action: {action}. Use 'add', 'update', or 'delete'")

        # GET: 返回事件列表
        self.runtime.d.load()
        events = self.runtime.d.data.get('calendar_events', [])

        # 支持按月/日筛选
        date_filter = request.args.get('date', '')  # YYYY-MM-DD 或 YYYY-MM
        if date_filter:
            events = [e for e in events if e.get('date', '').startswith(date_filter)]

        type_filter = request.args.get('type', '')  # personal|holiday|work
        if type_filter:
            events = [e for e in events if e.get('type') == type_filter]

        # 按日期排序
        events = sorted(events, key=lambda e: e.get('date', ''))

        return u.format_dict({'success': True, 'events': events})


    def todos(self):
        """GET: 列出待办（自动清除超过1天的已完成项）；POST: 增/完成/删（需管理员密钥）"""
        if request.method == 'POST':
            auth_err = self.runtime.common_security.require_admin()
            if auth_err:
                return auth_err

            try:
                body = request.get_json(force=True)
            except Exception:
                return self.runtime.common_security.reterr(code='bad request', message='invalid JSON body')

            if body is None or not isinstance(body, dict):
                return self.runtime.common_security.reterr(code='bad request', message='expected JSON object')

            action = body.get('action', 'add')

            if action == 'add':
                text = body.get('text', '').strip()
                if not text:
                    return self.runtime.common_security.reterr(code='bad request', message="'text' is required")

                new_todo = {
                    'id': str(uuid.uuid4()),
                    'text': text,
                    'done': False,
                    'completed_at': None,
                }

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    todos_list = list(self.runtime.d.data.get('todos', []))
                    todos_list.append(new_todo)
                    self.runtime.d.dset('todos', todos_list)

                u.info(f'Todo added: {text}')
                return u.format_dict({'success': True, 'code': 'OK', 'todo': new_todo})

            elif action == 'complete':
                todo_id = body.get('id', '')
                if not todo_id:
                    return self.runtime.common_security.reterr(code='bad request', message="'id' is required")

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    todos_list = list(self.runtime.d.data.get('todos', []))
                    found = None
                    for t in todos_list:
                        if t.get('id') == todo_id:
                            t['done'] = True
                            t['completed_at'] = datetime.now(timezone.utc).isoformat()
                            found = t
                            break

                    if found is None:
                        return self.runtime.common_security.reterr(code='not found', message=f'todo not found: {todo_id}')

                    self.runtime.d.dset('todos', todos_list)

                u.info(f'Todo completed: {found["text"]}')
                return u.format_dict({'success': True, 'code': 'OK', 'todo': found})

            elif action == 'delete':
                todo_id = body.get('id', '')
                if not todo_id:
                    return self.runtime.common_security.reterr(code='bad request', message="'id' is required")

                with self.runtime.write_lock:
                    self.runtime.d.load()
                    todos_list = list(self.runtime.d.data.get('todos', []))
                    new_list = [t for t in todos_list if t.get('id') != todo_id]

                    if len(new_list) == len(todos_list):
                        return self.runtime.common_security.reterr(code='not found', message=f'todo not found: {todo_id}')

                    self.runtime.d.dset('todos', new_list)

                u.info(f'Todo deleted: {todo_id}')
                return u.format_dict({'success': True, 'code': 'OK', 'deleted': todo_id})

            else:
                return self.runtime.common_security.reterr(code='bad request', message=f"unknown action: {action}. Use 'add', 'complete', or 'delete'")

        # GET: 列出待办（自动清除超过 1 天的已完成项）
        self.runtime.d.load()
        todos_list = list(self.runtime.d.data.get('todos', []))

        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        cutoff_str = cutoff.isoformat()
        cleaned = [
            t for t in todos_list
            if not t.get('done') or (t.get('completed_at') and t['completed_at'] > cutoff_str)
        ]

        if len(cleaned) != len(todos_list):
            with self.runtime.write_lock:
                self.runtime.d.data['todos'] = cleaned
                self.runtime.d.save()

        return u.format_dict({'success': True, 'todos': cleaned})

