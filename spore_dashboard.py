"""Localhost dashboard and opt-in controls for Spore Agentic Decompiler.

Telemetry is strictly read-only. Mutations, when explicitly enabled, are made
only via the established CLI, protected by a local anti-CSRF token.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import json
import secrets
import sqlite3

from spore_dashboard_data import snapshot,functions,function_detail,agent_detail
from spore_dashboard_control import Controller

PAGE=(Path(__file__).with_name('spore_dashboard_ui.html')).read_text(encoding='utf-8')


def serve(repo:Path,port:int=8765,control:bool=False):
    if not 1<=port<=65535:raise SystemExit('Port must be 1..65535')
    repo=repo.resolve()
    controller=Controller(repo,control)
    token=secrets.token_urlsafe(32)
    html=PAGE.replace('__SP_DASH_TOKEN__',json.dumps(token)).replace('__SP_DASH_CONTROL__','true' if control else 'false')

    class Handler(BaseHTTPRequestHandler):
        def respond(self,payload,status=200,html_page=False):
            body=payload.encode('utf-8') if html_page else json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','text/html; charset=utf-8' if html_page else 'application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store, max-age=0')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',
                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def valid_host(self):
            host=self.headers.get('Host','')
            return host in (f'127.0.0.1:{port}',f'localhost:{port}','127.0.0.1','localhost')

        def do_GET(self):
            if not self.valid_host():return self.respond({'error':'Localhost only'},403)
            route=urlparse(self.path)
            params=parse_qs(route.query)
            try:
                if route.path=='/':return self.respond(html,html_page=True)
                if route.path=='/api/snapshot':
                    return self.respond(snapshot(repo))
                if route.path=='/api/functions':
                    return self.respond(functions(repo,
                       status=params.get('status',['ALL'])[0],search=params.get('search',[''])[0],
                       offset=int(params.get('offset',['0'])[0]),
                       limit=int(params.get('limit',['75'])[0])))
                if route.path=='/api/function':
                    return self.respond(function_detail(repo,params.get('va',[''])[0]))
                if route.path=='/api/agent':
                    return self.respond(agent_detail(repo,int(params.get('worker',['0'])[0])))
                return self.respond({'error':'Not found'},404)
            except (ValueError,TypeError) as exc:
                return self.respond({'error':str(exc)[:200]},400)
            except (sqlite3.Error,OSError) as exc:
                return self.respond({'error':type(exc).__name__},503)

        def do_POST(self):
            if not self.valid_host():return self.respond({'error':'Localhost only'},403)
            if not control:return self.respond({'error':'Dashboard controls disabled'},403)
            if self.headers.get('X-Spore-Token')!=token:
                return self.respond({'error':'Invalid control token'},403)
            origin=self.headers.get('Origin','')
            if origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
                return self.respond({'error':'Invalid origin'},403)
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self.respond({'error':'Expected JSON'},415)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=4096:raise ValueError('Payload exceeds limit or is missing')
                data=json.loads(self.rfile.read(size))
                route=urlparse(self.path).path
                if route=='/api/control/start':result=controller.start(data)
                elif route=='/api/control/stop':result=controller.stop()
                else:return self.respond({'error':'Unknown control action'},404)
                return self.respond(result)
            except (ValueError,TypeError,PermissionError) as exc:
                return self.respond({'error':str(exc)[:800]},400)
            except (sqlite3.Error,OSError,RuntimeError) as exc:
                return self.respond({'error':type(exc).__name__},503)

        def log_message(self,fmt,*args):
            return

    with ThreadingHTTPServer(('127.0.0.1',port),Handler) as server:
        server.daemon_threads=True
        print(f'Dashboard: http://127.0.0.1:{port}',flush=True)
        print('Control: enabled (local token protected)' if control else 'Control: read-only (use --control to allow start/stop)',flush=True)
        print('Press Ctrl+C to close the dashboard. Closing it does not stop decompiler runs.',flush=True)
        try:server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:pass
    return 0
