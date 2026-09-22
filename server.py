#!/usr/bin/env python3
"""Compatibility entry point: python server.py and server:app remain supported."""
from sleepy_app.app import create_app
from sleepy_app.common.proxy import get_waitress_proxy_settings
import os

app = create_app()
# Compatibility for existing startup diagnostics; business code uses app.extensions.
community_store = app.extensions['sleepy_runtime'].community_store

def main():
    from waitress import serve
    runtime = app.extensions['sleepy_runtime']
    runtime.d.load()
    app.extensions['friend_feeds'].start()
    serve(app,
          host=os.environ.get('SLEEPY_HOST', runtime.d.data.get('host', '0.0.0.0')),
          port=int(os.environ.get('SLEEPY_PORT', runtime.d.data.get('port', 9010))),
          threads=16, **get_waitress_proxy_settings())

if __name__ == '__main__':
    main()
