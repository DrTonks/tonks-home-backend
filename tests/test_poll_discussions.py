import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from flask import Flask, request
from sleepy_app.community.store import CommunityStore, CommunityValidationError, validate_comment_payload
from sleepy_app.community.articles import ArticleCommentStore, register_article_comments
from sleepy_app.community.polls import PollStore

class PollDiscussionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.community=CommunityStore(str(self.root/'db.sqlite3'))
        self.polls=PollStore(self.community,self.root/'polls.json')
        self.quiz=dict(id='quiz',version='a'*64,title='Quiz',answer='b',explanation='Secret',options=[dict(id='a',label='A'),dict(id='b',label='B')])
        self.poll={**self.quiz,'id':'open','answer':None}
        self.polls.sync(dict(schema=1,polls=[self.quiz,self.poll]))
        (self.root/'polls.json').write_text(json.dumps(dict(schema=1,polls=[dict(id=p['id'],version=p['version']) for p in [self.quiz,self.poll]])))
        self.manifest=self.root/'articles.json'
        self.manifest.write_text(json.dumps(dict(schema=1,articles=[dict(id='article',slug='post',version='v',blocks=[dict(id='poll:quiz',text='Quiz'),dict(id='poll:open',text='Open'),dict(id='b_text',text='Text')])])))
        self.store=ArticleCommentStore(self.community,self.manifest);self.store.poll_store=self.polls
        self.payload=dict(nickname='Visitor',email='visitor@example.com',content='Secret answer discussion',version='v')
        self.quiz_id=self.create('poll:quiz');self.reply_id=self.create('poll:quiz',self.quiz_id)
        self.open_id=self.create('poll:open');self.text_id=self.create('b_text')
    def tearDown(self):self.tmp.cleanup()
    def create(self,block,parent=None):
        payload={**self.payload,'block_id':block,'parent_id':parent}
        context,target=self.store.context('article',payload,admin=True)
        return self.store.create(validate_comment_payload('about',payload),context,target,'actor','creator','published','test')
    def test_visibility_counts_replies_and_owner_isolation(self):
        for owner in ['', 'bob']:
            result=self.store.listing('article',owner=owner)
            self.assertEqual({x['id'] for x in result['comments']},{self.open_id,self.text_id})
            self.assertEqual(result['count'],2);self.assertEqual(result['locked_blocks'],['poll:quiz'])
            for kwargs in [dict(block='poll:quiz'),dict(root=self.quiz_id)]:
                with self.assertRaises(CommunityValidationError):self.store.listing('article',owner=owner,**kwargs)
        self.polls.state('quiz','a'*64,'alice','a') # Wrong answers also unlock.
        self.assertEqual(self.store.listing('article',owner='alice')['count'],4)
        self.assertEqual(len(self.store.listing('article',root=self.quiz_id,owner='alice')['comments']),1)
        self.assertEqual(self.store.listing('article',owner='bob')['count'],2)
        self.assertEqual(self.store.listing('article',admin=True)['count'],4)
        self.assertEqual(self.store.get_public_totals_by_slug(['post']),{'post':2})
    def test_locked_posts_and_forged_reply_context_are_rejected(self):
        for payload in [dict(block_id='poll:quiz'),dict(parent_id=self.quiz_id,block_id='b_text'),dict(parent_id=self.reply_id)]:
            with self.assertRaises(CommunityValidationError):self.store.context('article',{**self.payload,**payload},owner='bob')
        self.store.context('article',{**self.payload,'block_id':'poll:open'},owner='bob')
        self.polls.state('quiz','a'*64,'bob','b')
        self.store.context('article',{**self.payload,'parent_id':self.quiz_id},owner='bob')
    def test_filter_runs_before_pagination_and_missing_definitions_fail_closed(self):
        for _ in range(25):self.create('poll:quiz')
        result=self.store.listing('article')
        self.assertEqual(len(result['comments']),2);self.assertIsNone(result['next_before'])
        self.store.poll_store=None
        self.assertEqual(self.store.listing('article')['count'],1)
    def test_api_enforces_permission_before_moderation(self):
        app=Flask(__name__);moderator=Mock()
        register_article_comments(app,dict(community_store=self.community,verify_admin_secret=lambda:request.headers.get('X-Admin-Secret')=='secret',get_community_owner_hash=lambda r:r.headers.get('X-Community-Identity',''),comment_moderator=moderator))
        store=app.extensions['article_comments'];store.manifest_path=self.manifest;store.poll_store=self.polls
        client=app.test_client();url='/blog/community/articles/article/comments'
        for payload in [dict(block_id='poll:quiz'),dict(parent_id=self.quiz_id,block_id='poll:open')]:
            r=client.post(url,json={**self.payload,**payload});self.assertEqual(r.status_code,400);self.assertEqual(r.json['code'],'discussion_locked')
        moderator.moderate.assert_not_called()
        r=client.get(url);self.assertIn('no-store',r.headers['Cache-Control']);self.assertIn('X-Community-Identity',r.headers['Vary']);self.assertEqual(r.json['count'],2)
        self.assertEqual(client.get(url+'?root='+str(self.quiz_id)).status_code,400)
        self.assertEqual(client.get(url,headers={'X-Admin-Secret':'secret'}).json['count'],4)

