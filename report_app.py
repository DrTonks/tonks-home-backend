"""Compatibility launcher. Existing scheduled tasks keep this path."""
import runpy
if __name__ == '__main__':
    runpy.run_module('clients.report_app', run_name='__main__')
