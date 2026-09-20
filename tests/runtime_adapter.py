"""Test-only adapter for historical assertions; production does not import this."""
import importlib
import json
from pathlib import Path
from sleepy_app.app import create_app
OWNERS = json.loads((Path(__file__).parent / 'fixtures/legacy_owners.json').read_text())
MODULES = ['sleepy_app.blog.storage','sleepy_app.community.store','sleepy_app.personal.recommendation_store','urllib.request','os','time',
           'sleepy_app.community.avatars','sleepy_app.common.proxy','sleepy_app.common.errors',
           'sleepy_app.status.history','sleepy_app.status.heatmap','sleepy_app.blog.slugs']
class RuntimeAdapter:
    def __init__(self, app):
        object.__setattr__(self,'app',app)
        object.__setattr__(self,'runtime',app.extensions['sleepy_runtime'])
    def __getattr__(self,name):
        if name in OWNERS: return getattr(getattr(self.runtime,OWNERS[name]),name)
        if hasattr(self.runtime,name): return getattr(self.runtime,name)
        if name == 'urllib': return importlib.import_module('urllib')
        if name in ['os','time']: return importlib.import_module(name)
        for module in MODULES:
            value=getattr(importlib.import_module(module),name,None)
            if value is not None:return value
        raise AttributeError(name)
    def __setattr__(self,name,value):
        if name in OWNERS:setattr(getattr(self.runtime,OWNERS[name]),name,value)
        else:setattr(self.runtime,name,value)
    def __delattr__(self,name):
        if name in OWNERS:delattr(getattr(self.runtime,OWNERS[name]),name)
        else:delattr(self.runtime,name)
