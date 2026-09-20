"""Extracted compatibility-preserving domain functions; no startup side effects."""

def resolved_status_info(data, status, app_name):
    try:
        info = dict(data['status_list'][status])
        if status == 0 or app_name == '关机中':
            info['name'] = app_name
        return info
    except (KeyError, IndexError, TypeError):
        return {'status': status, 'name': app_name or '未知'}

def append_status_history(data, status, app_name, timestamp):
    history = data.setdefault('status_history', [])
    item = {
        'status': int(status),
        'app_name': str(app_name or ''),
        'timestamp': int(timestamp),
    }
    if history and history[-1].get('status') == item['status'] and history[-1].get('app_name') == item['app_name']:
        history[-1] = item
    else:
        history.append(item)
    data['status_history'] = history[-5:]
