"""Loopback-only single-project ToUser transport (stdlib)."""
from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import queue
import secrets
import threading
import uuid

from .storage import RunLock, confined, loads, write_json
from .redaction import redact
from .to_user import ToUser


class Workbench:
    def __init__(self, root, assistant_factory=ToUser):
        self.root = Path(root).resolve()
        self.assistant_factory = assistant_factory
        self.sessions = {}
        self.lock = threading.RLock()

    def _save(self, session):
        write_json(session["path"], session["view"])

    def start(self, message):
        root = self.root
        if not isinstance(message, str) or not message.strip() or len(message) > 30000:
            raise ValueError("消息不能为空，最多 30000 字符")
        with self.lock:
            if any(s["view"]["status"] in {"busy", "waiting", "closing"} for s in self.sessions.values()):
                raise ValueError("当前项目已有协助会话，请继续或结束该会话")
            sid = uuid.uuid4().hex
            session = {"path": confined(root, f"maintenance/web/{sid}.json"), "queue": queue.Queue(),
                       "view": {"id": sid, "status": "busy", "messages": [], "result": None}}
            self.sessions[sid] = session
            self._enqueue(session, message)
            threading.Thread(target=self._run, args=(session, root), daemon=True).start()
            return deepcopy(session["view"])

    def _enqueue(self, session, message):
        session["view"]["messages"].append({"role": "user", "content": redact(message)})
        session["view"]["status"] = "busy"
        self._save(session)
        session["queue"].put(message)

    def send(self, sid, message):
        with self.lock:
            session = self.sessions.get(sid)
            if not session or session["view"]["status"] != "waiting":
                raise ValueError("会话尚未等待输入或已结束")
            if not isinstance(message, str) or not message.strip() or len(message) > 30000:
                raise ValueError("消息不能为空，最多 30000 字符")
            self._enqueue(session, message)

    def close(self, sid):
        with self.lock:
            session = self.sessions.get(sid)
            if not session or session["view"]["status"] not in {"waiting", "busy"}:
                raise ValueError("会话已结束")
            session["view"]["status"] = "closing"
            self._save(session)
            session["queue"].put("C")

    def _run(self, session, root):
        def read(_prompt):
            with self.lock:
                if session["queue"].empty():
                    session["view"]["status"] = "waiting"
                    self._save(session)
            return session["queue"].get()

        def output(message):
            with self.lock:
                session["view"]["messages"].append({"role": "assistant", "content": redact(str(message))})
                self._save(session)

        try:
            with RunLock(confined(root, "maintenance/web-active")):
                config = loads(confined(root, "config.json").read_text(encoding="utf-8"))
                context = {"task": "当前项目协助。澄清需求，将确认知识写入 memories/skills，实现适配器与离线测试，说明配置步骤。此会话未绑定业务任务。"}
                assistant = self.assistant_factory(root, config.get("to_user"), input_fn=read, output_fn=output)
                result = assistant.run(context)
                with self.lock:
                    session["view"]["result"] = redact(result)
                    session["view"]["status"] = "finished"
        except Exception as exc:
            output(str(exc))
            with self.lock:
                session["view"]["status"] = "error"
        finally:
            with self.lock:
                self._save(session)

    def history(self):
        root = self.root
        with self.lock:
            result = []
            parent = confined(root, "maintenance/web")
            if parent.exists():
                for path in sorted(parent.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    checked = confined(root, path.relative_to(root).as_posix())
                    value = loads(checked.read_text(encoding="utf-8"))
                    if value["id"] not in self.sessions and value["status"] in {"busy", "waiting", "closing"}:
                        value["status"] = "interrupted"
                    result.append(value)
            return result

    def audit(self):
        root = self.root
        parent = confined(root, "maintenance")
        result = []
        if parent.exists():
            # Only visit declared audit levels, never the staged workspace or
            # other directories. Repairs have their own diffs and test results.
            names = {"changes.diff", "validation.json", "offline-tests.txt"}
            for session in sorted(parent.glob("to-user-*"), reverse=True):
                session = confined(root, session.relative_to(root).as_posix())
                if not session.is_dir():
                    continue
                for turn in sorted(session.glob("turn-*"), reverse=True):
                    turn = confined(root, turn.relative_to(root).as_posix())
                    if not turn.is_dir():
                        continue
                    attempts = sorted(turn.glob("repair-*"), reverse=True) + [turn]
                    for attempt in attempts:
                        attempt = confined(root, attempt.relative_to(root).as_posix())
                        if not attempt.is_dir():
                            continue
                        for name in sorted(names):
                            path = confined(root, (attempt / name).relative_to(root).as_posix())
                            if path.is_file():
                                result.append({"path": str(path.relative_to(root)),
                                               "text": redact(path.read_text(encoding="utf-8")[:100000])})
                                if len(result) >= 10:
                                    return result
        return result


def make_server(workbench, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, value, status=200, content_type="application/json; charset=utf-8"):
            raw = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self):
            address = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != address:
                self.reply({"error": "Invalid host"}, 403)
                return
            if self.command == "GET" and self.path == "/":
                from .workbench_ui import PAGE
                self.reply(PAGE, content_type="text/html; charset=utf-8")
                return
            if self.headers.get("X-Workbench-Token") != token or self.headers.get("Origin", "http://" + address) != "http://" + address:
                self.reply({"error": "请使用启动时输出的完整工作台链接"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 150000:
                    raise ValueError("请求过大")
                data = loads(self.rfile.read(length).decode()) if length else {}
                if self.command == "GET" and self.path == "/api/project":
                    value = {"path": str(workbench.root)}
                elif self.command == "POST" and self.path == "/api/start":
                    value = workbench.start(data["message"])
                elif self.command == "POST" and self.path == "/api/send":
                    workbench.send(data["id"], data["message"])
                    value = {"ok": True}
                elif self.command == "POST" and self.path == "/api/close":
                    workbench.close(data["id"])
                    value = {"ok": True}
                elif self.command == "POST" and self.path == "/api/state":
                    value = {"history": workbench.history(), "audit": workbench.audit()}
                else:
                    self.reply({"error": "Not found"}, 404)
                    return
                self.reply(value)
            except (ValueError, KeyError, OSError) as exc:
                self.reply({"error": str(exc)}, 400)

        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, token


def serve(root, port=8765):
    server, token = make_server(Workbench(root), port)
    print(f"工作台：http://127.0.0.1:{server.server_port}/#token={token}", flush=True)
    print("仅本机访问。停止业务任务后再进行项目协助；Ctrl+C 关闭服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
