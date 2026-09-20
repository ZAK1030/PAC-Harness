from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json

from pac_harness.storage import RunLock, write_json
from pac_harness.workbench import Workbench, make_server


class FakeAssistant:
    def __init__(self, root, config, *, input_fn, output_fn):
        self.read, self.write = input_fn, output_fn

    def run(self, context):
        while True:
            message = self.read("")
            if message == "C":
                return {"status": "no_change"}
            self.write("收到：" + message)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for folder in ("pac_harness", "examples", "tests", "prompts", "templates", "memories"):
            (self.root / folder).mkdir()
        (self.root / "memories/shared.md").write_text("PRIVATE OLD MEMORY")
        write_json(self.root / "config.json", {"secret": "PRIVATE CONFIG"})
        self.app = Workbench(self.root, FakeAssistant)

    def tearDown(self):
        for sid, session in self.app.sessions.items():
            if session["view"]["status"] in {"busy", "waiting"}:
                self.app.close(sid)
                self.until(sid, "finished")
        self.temp.cleanup()

    def until(self, sid, status):
        for _ in range(200):
            if self.app.sessions[sid]["view"]["status"] == status:
                return
            time.sleep(.01)
        self.fail(self.app.sessions[sid]["view"])

    def test_multiturn_persistence_and_project_exclusivity(self):
        sid = self.app.start("hello\nworld")["id"]
        self.until(sid, "waiting")
        with self.assertRaises(ValueError):
            self.app.start("duplicate")
        self.app.send(sid, "next")
        self.until(sid, "waiting")
        history = self.app.history()
        self.assertEqual(len(history[0]["messages"]), 4)
        restarted = Workbench(self.root)
        self.assertEqual(restarted.history()[0]["status"], "interrupted")
        self.app.close(sid)
        self.until(sid, "finished")

    def test_project_lock_blocks_assistance(self):
        with RunLock(self.root / "maintenance/web-active"):
            sid = self.app.start("hello")["id"]
            self.until(sid, "error")
        self.assertIn("already in use", self.app.history()[0]["messages"][-1]["content"])

    def test_real_to_user_pipeline_applies_and_exposes_audit(self):
        from pac_harness.to_user import ToUser

        class Backend:
            def reply(self, stage, prompt, *, audit):
                (stage / "memories/shared.md").write_text("confirmed knowledge", encoding="utf-8")
                return {"status": "edited", "intent": "persistent_update", "message": "完成离线修改",
                        "summary": "updated", "session_guidance": ""}

            def validate(self, stage, *, audit):
                return {"passed": True, "kind": "offline test double"}

        self.app.assistant_factory = lambda root, config, **io: ToUser(root, config, backend=Backend(), **io)
        sid = self.app.start("更新知识")["id"]
        self.until(sid, "finished")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "confirmed knowledge")
        self.assertEqual(self.app.history()[0]["result"]["status"], "applied")
        self.assertEqual(len(self.app.audit()), 2)

    def test_repaired_to_user_update_and_audit_stay_in_current_project(self):
        from pac_harness.to_user import ToUser

        first = self.root

        class Backend:
            attempts = 0

            def reply(self, stage, prompt, *, audit):
                self.attempts += 1
                text = "confirmed after repair" if self.attempts > 1 else "candidate needs correction"
                (stage / "memories/shared.md").write_text(text, encoding="utf-8")
                return {"status": "edited", "intent": "persistent_update", "message": "完成离线修改",
                        "summary": "updated", "session_guidance": ""}

            def validate(self, stage, *, audit):
                passed = self.attempts > 1
                (audit / "offline-tests.txt").write_text("test passed" if passed else "specific failed assertion",
                                                         encoding="utf-8")
                return {"passed": passed, "kind": "offline test double"}

        backend = Backend()
        self.app.assistant_factory = lambda root, config, **io: ToUser(root, config, backend=backend, **io)
        sid = self.app.start("修复并保存长期知识")["id"]
        self.until(sid, "finished")
        result = self.app.history()[0]["result"]
        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["restart_required"])
        self.assertEqual(backend.attempts, 2)
        self.assertEqual((first / "memories/shared.md").read_text(encoding="utf-8"), "confirmed after repair")
        audits = self.app.audit()
        repaired = [item for item in audits if "repair-" in item["path"]]
        self.assertEqual({Path(item["path"]).name for item in repaired},
                         {"changes.diff", "validation.json", "offline-tests.txt"})
        self.assertTrue(any("specific failed assertion" in item["text"] for item in audits))

    def test_audit_excludes_workspace_and_rejects_linked_evidence(self):
        session = self.root / "maintenance/to-user-example"
        write_json(session / "workspace/turn-001/validation.json", {"private": "staged workspace"})
        write_json(session / "turn-001/repair-001/validation.json", {"passed": True})
        audits = self.app.audit()
        self.assertEqual(len(audits), 1)
        self.assertNotIn("workspace", audits[0]["path"])
        outside = self.root / "private.txt"
        outside.write_text("not audit content")
        link = session / "turn-001/changes.diff"
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Windows may not permit creating test symlinks.
        with self.assertRaises(ValueError):
            self.app.audit()

    def test_http_auth_origin_and_invalid_path(self):
        server, token = make_server(self.app, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base) as response:
                page = response.read().decode()
                self.assertIn("ToUser", page)
                self.assertNotIn('id="wizard"', page)
                self.assertNotIn('id="newScene"', page)
            for headers in ({}, {"X-Workbench-Token": token, "Origin": "https://evil.example"},
                            {"X-Workbench-Token": token, "Host": "evil.example"}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + "/api/project", headers=headers))
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            with urlopen(Request(base + "/api/project", headers={"X-Workbench-Token": token})) as response:
                self.assertEqual(json.load(response)["path"], str(self.root.resolve()))
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/scenes", headers={"X-Workbench-Token": token}))
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/create", data=json.dumps({"name": "../x", "description": "x"}).encode(), headers={"X-Workbench-Token": token}))
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
