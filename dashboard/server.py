"""SAD localhost HTTP boundary.

API routes only compose operational services, knowledge repositories and
narrow systemd controls. All mutation routes require the existing dashboard
--control mode, same-origin check and per-session anti-CSRF token.

The compiled React UI lives under dashboard_web/dist. Until the build is
present, / falls back to the original functional dashboard; /legacy always
keeps the previous interface available for migration verification.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json
import mimetypes
import re
import secrets
import sqlite3

from spore_dashboard_data import snapshot, functions, function_detail, agent_detail, local_inference
from spore_dashboard_control import Controller
from . import knowledge, consolidation

HERE = Path(__file__).resolve().parents[1]
DIST = HERE / "dashboard_web/dist"
LEGACY = (HERE / "spore_dashboard_ui.html").read_text(encoding="utf-8")
ASSET = re.compile(r"[a-zA-Z0-9_.-]{1,160}\Z")


def _int(params, field, default):
    raw = params.get(field,[str(default)])[0]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid "+field) from exc


def serve(repo:Path,port:int=8765,control:bool=False):
    if not 1<=port<=65535:raise SystemExit("Port must be 1..65535")
    repo=Path(repo).resolve()
    controller=Controller(repo,control)
    token=secrets.token_urlsafe(32)

    def page(legacy=False):
        if legacy or not (DIST/"index.html").is_file():
            return LEGACY.replace("__SP_DASH_TOKEN__",json.dumps(token)).replace(
                "__SP_DASH_CONTROL__","true" if control else "false")
        html=(DIST/"index.html").read_text(encoding="utf-8")
        bootstrap=("<script>window.__SAD_TOKEN__="+json.dumps(token)+";"
                   "window.__SAD_CONTROL__="+("true" if control else "false")+";</script>")
        if "</head>" not in html:
            raise RuntimeError("Dashboard React build missing HTML head")
        return html.replace("</head>", bootstrap+"</head>",1)

    class Handler(BaseHTTPRequestHandler):
        def respond(self,payload,status=200,media="application/json; charset=utf-8"):
            if isinstance(payload,bytes):
                body=payload
            elif media.startswith("application/json"):
                body=json.dumps(payload).encode("utf-8")
            else:
                body=payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",media)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store, max-age=0")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("X-Frame-Options","DENY")
            self.send_header("Content-Security-Policy",
                "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' https://media.contentapi.ea.com; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def valid_host(self):
            return self.headers.get("Host","") in (
                f"localhost:{port}",f"127.0.0.1:{port}","localhost","127.0.0.1")

        def asset(self,route):
            # Never resolve a browser-supplied path; only hashed Vite artifacts
            # matching a simple base filename are served.
            name=route.removeprefix("/assets/")
            if not ASSET.fullmatch(name):return self.respond({"error":"Not found"},404)
            file=DIST/"assets"/name
            if not file.is_file() or file.stat().st_size>6_000_000:
                return self.respond({"error":"Not found"},404)
            mime=mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self.respond(file.read_bytes(),media=mime)

        def wallpaper(self):
            for name,media in (
                ("sad_background.webp","image/webp"),
                ("sad_background.png","image/png"),("sad_background.jpg","image/jpeg")):
                file=HERE/name
                if file.is_file() and file.stat().st_size<=20_000_000:
                    return self.respond(file.read_bytes(),media=media)
            return self.respond({"error":"No SAD wallpaper configured"},404)

        def do_GET(self):
            if not self.valid_host():return self.respond({"error":"Localhost only"},403)
            parsed=urlparse(self.path)
            route=parsed.path
            params=parse_qs(parsed.query)
            try:
                if route=="/":return self.respond(page(),media="text/html; charset=utf-8")
                if route=="/legacy":return self.respond(page(True),media="text/html; charset=utf-8")
                if route.startswith("/assets/"):return self.asset(route)
                if route=="/wallpaper":return self.wallpaper()
                if route=="/api/local-inference":
                    return self.respond(local_inference())
                if route=="/api/snapshot":
                    data=snapshot(repo)
                    if control:data["controller"]=controller.state()
                    return self.respond(data)
                if route=="/api/functions":
                    return self.respond(functions(repo,
                        status=params.get("status",["ALL"])[0],
                        search=params.get("search",[""])[0],
                        offset=_int(params,"offset",0),
                        limit=_int(params,"limit",75)))
                if route=="/api/function":
                    return self.respond(function_detail(repo,params.get("va",[""])[0]))
                if route=="/api/agent":
                    return self.respond(agent_detail(repo,_int(params,"worker",0)))
                if route=="/api/allowlist":
                    return self.respond(controller.allowlist_document())
                if route=="/api/claimants":
                    return self.respond(controller.claimants())
                if route=="/api/plan":
                    return self.respond(controller.preview(
                        params.get("module",["SPGonzago"])[0],
                        _int(params,"workers",16),
                        params.get("claim_user",[None])[0]))
                if route=="/api/knowledge/search":
                    return self.respond(knowledge.hybrid_search(
                        repo,params.get("text",[""])[0]))
                if route=="/api/knowledge/overview":
                    result=knowledge.overview(repo)
                    result["consolidator"]=consolidation.state(repo)
                    return self.respond(result)
                if route=="/api/knowledge/techniques":
                    return self.respond(knowledge.techniques(repo,
                        search=params.get("search",[""])[0],
                        category=params.get("category",[""])[0],
                        offset=_int(params,"offset",0),
                        limit=_int(params,"limit",40)))
                if route=="/api/knowledge/technique":
                    return self.respond(knowledge.technique(repo,_int(params,"id",0)))
                if route=="/api/knowledge/findings":
                    return self.respond(knowledge.findings(repo,
                        search=params.get("search",[""])[0],
                        status=params.get("status",[""])[0],
                        offset=_int(params,"offset",0),
                        limit=_int(params,"limit",40)))
                if route=="/api/knowledge/finding":
                    return self.respond(knowledge.finding(repo,_int(params,"id",0)))
                if route=="/api/knowledge/graph":
                    return self.respond(knowledge.graph(repo,_int(params,"limit",45)))
                return self.respond({"error":"Not found"},404)
            except (TypeError, ValueError) as exc:
                return self.respond({"error":str(exc)[:240]},400)
            except (FileNotFoundError,sqlite3.Error,OSError,RuntimeError) as exc:
                return self.respond({"error":type(exc).__name__},503)

        def do_POST(self):
            if not self.valid_host():return self.respond({"error":"Localhost only"},403)
            if not control:return self.respond({"error":"Dashboard controls disabled"},403)
            if self.headers.get("X-Spore-Token")!=token:
                return self.respond({"error":"Invalid control token"},403)
            origin=self.headers.get("Origin","")
            if origin and origin not in (
                f"http://localhost:{port}",f"http://127.0.0.1:{port}"):
                return self.respond({"error":"Invalid origin"},403)
            if self.headers.get("Content-Type","").split(";")[0].strip()!="application/json":
                return self.respond({"error":"Expected JSON"},415)
            route=urlparse(self.path).path
            max_size=300000 if route=="/api/allowlist" else 4096
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=max_size:
                    raise ValueError("Payload exceeds limit or is missing")
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):
                    raise ValueError("Expected JSON object")
                if route=="/api/allowlist":
                    return self.respond(controller.save_allowlist(data))
                if route=="/api/control/start":
                    return self.respond(controller.start(data))
                if route=="/api/control/stop":
                    return self.respond(controller.stop())
                if route=="/api/knowledge/annotation":
                    return self.respond(knowledge.annotate(
                        repo,data.get("id"),data.get("note"),data.get("pinned")))
                if route=="/api/knowledge/settings":
                    return self.respond(consolidation.save_settings(repo,data))
                if route=="/api/knowledge/service":
                    return self.respond(consolidation.action(repo,data.get("action")))
                return self.respond({"error":"Unknown control action"},404)
            except (ValueError,TypeError,PermissionError) as exc:
                return self.respond({"error":str(exc)[:300]},400)
            except (sqlite3.Error,OSError,RuntimeError) as exc:
                return self.respond({"error":type(exc).__name__},503)

        def log_message(self,*args):
            pass

    with ThreadingHTTPServer(("127.0.0.1",port),Handler) as server:
        server.daemon_threads=True
        print(f"Dashboard: http://127.0.0.1:{port}",flush=True)
        print("Control: enabled" if control else "Control: read-only",flush=True)
        print("UI: React/TypeScript" if (DIST/"index.html").is_file()
              else "UI: legacy fallback (build dashboard_web first)",flush=True)
        print("Legacy UI: http://127.0.0.1:"+str(port)+"/legacy",flush=True)
        try:server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:pass
    return 0
