"""Compatibility launcher. Existing scheduled tasks keep this path."""
import runpy
if __name__ == '__main__':
    runpy.run_module('clients.upload_agent_stats', run_name='__main__')
