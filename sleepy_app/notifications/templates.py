"""Small escaped HTML and plain-text administrator notifications."""
import html
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULT_ORIGINS = ('https://tonks.top', 'https://blog.tonks.top')
STATUS_LABELS = {'approved': '自动通过', 'rejected': '自动拒绝', 'pending': '待人工审核',
                 'pending_review': '待人工审核', 'published': '已发布'}
KIND_LABELS = {'comment': '新评论', 'recommendation': '新推荐', 'friend_request': '友链申请',
               'friend_application': '友链申请', 'article_comment': '新评论',
               'feedback': '新反馈', 'test': '通知测试'}


def _summary(value, limit=400):
    text = ' '.join(str(value or '').split())
    return text[:limit] + ('…' if len(text) > limit else '')


def _link(value, allowed_origins):
    try:
        parts = urlsplit(str(value or ''))
        origin = parts.scheme + '://' + parts.netloc
        if origin not in allowed_origins or parts.scheme != 'https' or parts.username or parts.password:
            return None
        if any(ord(c) < 32 for c in str(value)):
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', '', parts.fragment))
    except ValueError:
        return None


def render_notification(event, allowed_origins=DEFAULT_ORIGINS):
    event = dict(event)
    event.setdefault('author', event.get('nickname'))
    source = event.get('site') or event.get('source')
    site = {'home': '主页', 'blog': '博客', '主页': '主页', '博客': '博客',
            '博客/主页（共享）': '博客/主页（共享）', '博客/主页': '博客/主页（共享）'}.get(source, 'Tonks')
    kind = KIND_LABELS.get(event.get('kind'), '网站通知')
    status = str(event.get('status') or '')
    state = STATUS_LABELS.get(status, _summary(status, 30))
    if event.get('kind') in ('comment', 'article_comment', 'feedback') and status == 'published':
        state = '自动通过'
    elif event.get('kind') == 'recommendation':
        state = ''
    subject = f'[{site} · {state or kind}] {kind}'
    rows = [('站点', site)]
    for field, label, limit in [('page', '页面', 140), ('title', '标题', 140),
                                ('created_at', '提交时间', 60), ('entity_id', '记录编号', 60)]:
        if event.get(field):
            rows.append((label, _summary(event[field], limit)))
    if state:
        rows.append(('状态', state))
    if status == 'rejected':
        # Rejected content may contain spam URLs even in author/title/reason fields.
        rows = [(label, re.sub(r'(?:https?://|www\.)\S+', '[链接已省略]', value, flags=re.IGNORECASE)) for label, value in rows]
        if event.get('author'):
            rows.append(('访客', re.sub(r'(?:https?://|www\.)\S+', '[链接已省略]', _summary(event['author'], 80), flags=re.IGNORECASE)))
        rows.append(('内容', '该留言已被拒绝，内容请进入管理页面查看。'))
        if event.get('reason'):
            rows.append(('审核原因', _summary(re.sub(r'(?:https?://|www\.)\S+', '[链接已省略]', str(event['reason']), flags=re.IGNORECASE), 300)))
    else:
        for field, label, limit in [('author', '访客', 80), ('content', '内容摘要', 400),
                                    ('reason', '审核原因', 240), ('quote', '对应段落', 160)]:
            if event.get(field):
                rows.append((label, _summary(event[field], limit)))
    if event.get('kind') == 'friend_application':
        rows.append(('操作说明', '打开主页留言群聊，进入管理模式查看友链申请。'))
    elif event.get('kind') == 'feedback':
        rows.append(('操作说明', '打开主页留言入口，进入反馈群聊；如需查看未通过内容，请先进入管理模式。'))
    link = _link(event.get('url'), allowed_origins)
    text = subject + '\n\n' + '\n\n'.join(f'{label}：{value}' for label, value in rows)
    content = ''.join('<p><strong>' + html.escape(label) + '：</strong>' + html.escape(value) + '</p>'
                      for label, value in rows)
    if link:
        text += '\n\n前往网站查看与处理：\n' + link
        content += '<p><a href="' + html.escape(link, quote=True) + '">前往网站查看与处理</a></p>'
    text += '\n\n此邮件由网站自动发送，请在网站中处理与回复。'
    markup = '<!doctype html><html lang="zh-CN"><body><h2>' + html.escape(subject) + '</h2>' + content
    markup += '<p>此邮件由网站自动发送，请在网站中处理与回复。</p></body></html>'
    return {'subject': subject, 'text': text, 'html': markup}
