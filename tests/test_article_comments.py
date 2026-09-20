import json
import tempfile
import unittest
from pathlib import Path
from flask import Flask
from sleepy_app.community.store import CommunityStore, CommunityValidationError, validate_comment_payload, CommunityBurstLimiter
from sleepy_app.community.moderation import ModerationResult
from sleepy_app.community.articles import ArticleCommentStore, register_article_comments


class ArticleCommentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.manifest = self.path / 'manifest.json'
        self.article = {'id':'a_one','version':'v1','blocks':[{'id':'b_one','text':'可信原文'}]}
        self.manifest.write_text(json.dumps({'schema':1,'articles':[self.article]}), encoding='utf8')
        self.community = CommunityStore(str(self.path / 'db.sqlite3'))
        self.store = ArticleCommentStore(self.community, self.manifest)
        self.payload = {'nickname':'访客','email':'visitor@example.com','content':'讨论内容', 'version':'v1','block_id':'b_one'}

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **changes):
        payload = {**self.payload, **changes}
        context,parent=self.store.context('a_one',payload)
        return self.store.create(validate_comment_payload('about',payload),context,parent,'actor','owner','published','test')

    def test_avatar_non_qq_is_local_and_qq_keeps_redirect(self):
        app = Flask(__name__)
        register_article_comments(app, {
            'community_store': self.community,
            'community_qq_number': lambda email: '12345678' if email.endswith('@qq.com') else None,
            'community_avatar_svg': lambda email: '<svg xmlns="http://www.w3.org/2000/svg"/>',
            'community_qq_avatar_url': lambda qq: 'https://q1.qlogo.cn/' + qq,
        })
        app.extensions['article_comments'].manifest_path = self.manifest
        client = app.test_client()
        ident = self.create()
        response = client.get(f'/blog/community/articles/a_one/avatar/{ident}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/svg+xml')
        ident = self.create(email='12345678@qq.com')
        self.assertEqual(client.get(f'/blog/community/articles/a_one/avatar/{ident}').status_code, 302)
        self.assertEqual(client.get(f'/blog/community/articles/a_one/avatar/{ident}?fallback=1').mimetype, 'image/svg+xml')

    def test_public_totals_batch_includes_replies_and_excludes_moderated(self):
        self.article['slug'] = 'nested/post'
        self.manifest.write_text(json.dumps({'schema': 1, 'articles': [self.article]}), encoding='utf8')
        root = self.create()
        self.create(parent_id=root)
        pending = self.create()
        self.store.manage('a_one', pending, 'pending')
        self.assertEqual(self.store.get_public_totals_by_slug(['nested/post', 'unknown']),
                         {'nested/post': 2, 'unknown': None})
        self.manifest.unlink()
        with self.assertRaises(CommunityValidationError):
            self.store.get_public_totals_by_slug(['nested/post'])

    def test_trusted_quote_and_room_isolation(self):
        self.create(quote='伪造原文')
        result=self.store.listing('a_one')
        self.assertEqual(result['comments'][0]['quote'],'可信原文')
        self.assertEqual(result['counts'],{'b_one':1})
        self.assertNotIn('email',result['comments'][0])
        self.assertEqual(self.community.list_public_comments('about'),[])

    def test_replies_inherit_context_and_old_quote_survives(self):
        root=self.create()
        self.create(parent_id=root,block_id='forged')
        self.article.update(version='v2',blocks=[])
        self.manifest.write_text(json.dumps({'schema':1,'articles':[self.article]}),encoding='utf8')
        result=self.store.listing('a_one')
        self.assertEqual(result['comments'][0]['quote'],'可信原文')
        self.assertEqual(result['comments'][0]['reply_count'],1)
        replies=self.store.listing('a_one',root=root)
        self.assertEqual(replies['comments'][0]['block_id'],'b_one')
        self.assertEqual(replies['comments'][0]['quote'],'可信原文')
        self.assertEqual(replies['comments'][0]['reply_to_name'],'访客')
        self.assertEqual(result['count'],2)

    def test_stale_forged_unknown_and_draft_rejected(self):
        for patch in ({'version':'old'},{'block_id':'fake'}):
            with self.assertRaises(CommunityValidationError):self.create(**patch)
        with self.assertRaises(CommunityValidationError):self.store.article('not-published')
        self.article['draft']=True
        self.manifest.write_text(json.dumps({'schema':1,'articles':[self.article]}),encoding='utf8')
        with self.assertRaises(CommunityValidationError):self.store.article('a_one')

    def test_root_pagination_and_counts_not_page_length(self):
        for i in range(23):self.create(content=f'留言{i}')
        first=self.store.listing('a_one')
        second=self.store.listing('a_one',before=first['next_before'])
        self.assertEqual(first['count'],23)
        self.assertEqual(len(first['comments']),20)
        self.assertEqual(len(second['comments']),3)
        self.assertFalse(set(c['id'] for c in first['comments']) & set(c['id'] for c in second['comments']))

    def test_moderation_visibility_and_cross_article_reply(self):
        root=self.create()
        self.create(parent_id=root)
        self.store.manage('a_one',root,'rejected')
        self.assertEqual(self.store.listing('a_one')['count'],0)
        self.assertEqual(self.store.listing('a_one')['comments'],[])
        self.assertEqual(len(self.store.listing('a_one',admin=True)['comments']),1)
        with self.assertRaises(CommunityValidationError):self.create(parent_id=root)

    def test_deleted_comments_are_not_public_placeholders(self):
        root = self.create()
        reply = self.create(parent_id=root)
        self.store.manage('a_one', reply, 'deleted')
        self.assertEqual(self.store.listing('a_one', root=root)['comments'], [])
        self.assertEqual(self.store.listing('a_one')['comments'][0]['reply_count'], 0)
        self.store.manage('a_one', root, 'deleted')
        for options in ({}, {'block': 'b_one'}, {'owner': 'owner'}):
            result = self.store.listing('a_one', **options)
            self.assertEqual(result['comments'], [])
            self.assertEqual(result['count'], 0)
            self.assertIsNone(result['next_before'])
        with self.assertRaises(CommunityValidationError):
            self.store.listing('a_one', root=root)
        self.assertEqual(self.store.listing('a_one', admin=True)['comments'], [])
        with self.assertRaises(CommunityValidationError):
            self.store.listing('a_one', root=root, admin=True)

    def test_approval_requires_all_ancestors_published(self):
        root=self.create()
        middle=self.create(parent_id=root)
        leaf=self.create(parent_id=middle)
        self.store.manage('a_one',root,'rejected')
        for ident in (middle,leaf):
            with self.assertRaises(CommunityValidationError):self.store.manage('a_one',ident,'published')
        self.assertEqual(self.store.listing('a_one')['count'],0)
        self.store.manage('a_one',root,'published')
        with self.assertRaises(CommunityValidationError):self.store.manage('a_one',leaf,'published')
        self.store.manage('a_one',middle,'published')
        self.store.manage('a_one',leaf,'published')
        self.assertEqual(self.store.listing('a_one')['count'],3)
        self.store.manage('a_one',middle,'deleted')
        with self.assertRaises(CommunityValidationError):self.store.manage('a_one',leaf,'published')

    def test_history_truncation_keeps_recent_comments(self):
        from sleepy_app.community.moderation import CommentModerationService
        from unittest.mock import patch
        for i in range(23):self.create(content=f'留言{i:02d} '+('内容'*350))
        history=self.store.history('actor')
        self.assertEqual(len(history),20)
        self.assertTrue(history[0]['content'].startswith('留言03'))
        self.assertTrue(history[-1]['content'].startswith('留言22'))
        with patch.dict('os.environ',{'SLEEPY_COMMENT_HISTORY_CHARS':'2000'}):
            result=CommentModerationService._history_payload(page='article:a_one',nickname='访客',content='新评论',reply_to_name='',history=history)
        self.assertTrue(result['same_email_history']['truncated'])
        self.assertTrue(result['same_email_history']['items'][-1]['content'].startswith('留言22'))

    def test_route_uses_existing_identity_moderation_and_quota(self):
        from flask import request
        class Moderator:
            def moderate(self,**kwargs):return ModerationResult('review','test','test')
        app=Flask(__name__)
        services={'community_store':self.community,'verify_admin_secret':lambda:request.headers.get('X-Admin-Secret')=='test-admin',
                  'get_community_owner_hash':lambda r:r.headers.get('X-Community-Identity',''),
                  'get_community_actor_hash':lambda email:'actor','get_community_rate_limit_keys':lambda r:('ip','client'),
                  'community_comment_limiter':CommunityBurstLimiter(30),'comment_moderator':Moderator()}
        register_article_comments(app,services)
        app.extensions['article_comments'].manifest_path=self.manifest
        client=app.test_client()
        url='/blog/community/articles/a_one/comments'
        result=client.post(url,json=self.payload,headers={'X-Community-Identity':'shared-cookie-token'}).get_json()
        self.assertEqual(result['status'],'pending')
        self.assertEqual(client.get(url).get_json()['comments'],[])
        admin={'X-Admin-Secret':'test-admin'}
        self.assertEqual(client.get(url,headers={'X-Admin-Secret':'wrong'}).status_code,401)
        self.assertEqual(client.patch(url+'/'+str(result['id']),json={'status':'published'},headers=admin).status_code,200)
        public=client.get(url,headers={'X-Community-Identity':'shared-cookie-token'}).get_json()
        self.assertTrue(public['comments'][0]['owned'])
        self.assertEqual(client.get(url+'?before=oops').status_code,400)
        self.assertEqual(client.delete(url+'/'+str(result['id'])).status_code,401)
        self.assertEqual(client.post(url,data='x'*8193,content_type='application/json').status_code,413)
        self.assertEqual(client.post(url,json={**self.payload,'parent_id':[]}).status_code,400)
        for parent in (10**100,1.5,True,{},'1.5','-1'):
            with self.subTest(parent=parent):
                self.assertEqual(client.post(url,json={**self.payload,'parent_id':parent}).status_code,400)
        for body in ([1],{'status':[]},{'status':{}},None):
            with self.subTest(body=body):
                self.assertEqual(client.patch(url+'/'+str(result['id']),json=body,headers=admin).status_code,400)
        for method in ('patch','delete'):
            self.assertEqual(getattr(client,method)(url+'/'+str(10**100),json={'status':'published'},headers=admin).status_code,400)
        self.assertEqual(client.get('/blog/community/articles/a_one/avatar/'+str(10**100)).status_code,404)

    def test_moderation_receives_merged_history_in_time_order(self):
        from unittest.mock import patch
        captured=[]
        class Moderator:
            def moderate(self,**kwargs):
                captured.extend(kwargs['history'])
                return ModerationResult('review','test','test')
        self.create(content='文章旧留言')
        app=Flask(__name__)
        services={'community_store':self.community,'verify_admin_secret':lambda:False,
                  'get_community_owner_hash':lambda r:'owner','get_community_actor_hash':lambda email:'actor',
                  'get_community_rate_limit_keys':lambda r:('ip','client'),
                  'community_comment_limiter':CommunityBurstLimiter(30),'comment_moderator':Moderator()}
        register_article_comments(app,services)
        app.extensions['article_comments'].manifest_path=self.manifest
        room_history=[{'content':'未来旧群聊','status':'published','created_at':'2099-01-01T00:00:00+00:00'},
                      {'content':'过去旧群聊','status':'published','created_at':'2000-01-01T00:00:00+00:00'}]
        with patch.object(self.community,'actor_history',return_value=room_history):
            response=app.test_client().post('/blog/community/articles/a_one/comments',json=self.payload)
        self.assertEqual(response.status_code,200)
        self.assertEqual([h['content'] for h in captured],['过去旧群聊','文章旧留言','未来旧群聊'])

if __name__=='__main__':unittest.main()
