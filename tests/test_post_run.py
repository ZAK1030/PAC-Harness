import base64
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pac_harness.experience import Experience, MEMORY_PATH, ROLE_MEMORY_PATHS, compact_rule
from pac_harness.model_response import ModelError
from pac_harness.memory_review import collect_memory, consolidate_memory, validate_consolidation
from pac_harness.post_run import Analyzer, collect_run, main as analyzer_main, maybe_review, validate_review
from pac_harness.storage import RunLock, dumps, loads, write_json
from pac_harness.to_user import ToUser
from pac_harness.tools import Knowledge


def finding(index=1, confidence="supported", quote="bowl stayed on table"):
    return {"id": "bowl-retention-test", "roles": ["planner", "detector"], "task_keywords": ["black bowl"],
            "condition": "a bowl grasp is unverified", "lesson": "Lift to verify retention before transfer.",
            "verification": "Compare frames for bowl following.", "recheck_when": "geometry changes.",
            "confidence": confidence, "evidence": [{"event_index": index, "quote": quote}]}


def review(findings=None):
    return {"summary": "The lift exposed a failed grasp.", "findings": findings or [], "code_suggestions": []}


class Client:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages, *, on_event=None):
        self.calls.append(deepcopy(messages))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return result


class PostRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "logs/session/run-002"
        self.directory.mkdir(parents=True)
        self.scope = {"adapter": "examples.list_sorting:create", "context": "workspace_a", "task": "pick up the black bowl"}
        self.state = {"task": self.scope["task"], "turn": 1, "phase": "completed", "identity": {"adapter": {
            "factory": self.scope["adapter"], "settings": {"context": self.scope["context"]}}}}
        write_json(self.directory / "task_state.json", self.state)
        self.event = {"event": "detector", "turn": 1, "report": {"reason": "bowl stayed on table", "facts": {"held": False}}}
        self.write_events([self.event])
        self.original_state = (self.directory / "task_state.json").read_bytes()

    def write_events(self, events):
        (self.directory / "events.jsonl").write_text("\n".join(dumps(e) for e in events) + "\n", encoding="utf-8")

    def assistant(self):
        return ToUser(self.root, {"post_run": {"chunk_turns": 2}}, output_fn=lambda _: None,
                      input_fn=lambda _: self.fail("Automatic review must never request console input"))

    def test_whole_run_review_then_next_run_loads_scoped_memory(self):
        # The task checkpoint's bounded history is insufficient; the review reads all turns.
        events = [dict(self.event, turn=i) for i in range(1, 7)]
        self.write_events(events)
        client = Client(review([finding()]), review([finding(3)]), review([finding(5)]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(client.calls), 4)
        scanned = []
        for messages in client.calls[:3]:
            data = loads(messages[1]["content"][0]["text"])
            scanned += [e["turn"] for e in data["events"] if e["event"] == "detector"]
        self.assertEqual(scanned, list(range(1, 7)))
        self.assertEqual((self.directory / "task_state.json").read_bytes(), self.original_state)
        self.assertEqual(len(Knowledge(self.root).context("planner", {"task_state": self.state})["experience"]), 1)
        other = deepcopy(self.state)
        other["identity"]["adapter"]["settings"]["context"] = "workspace_b"
        self.assertEqual(Knowledge(self.root).context("planner", {"task_state": other})["experience"], [])
        other = deepcopy(self.state)
        other["task"] = "open the drawer"
        self.assertEqual(Knowledge(self.root).context("planner", {"task_state": other})["experience"], [])

    def test_all_result_images_are_sent_with_turn_labels(self):
        (self.directory / "frames").mkdir()
        raw = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jf5kAAAAASUVORK5CYII=")
        frames = {}
        for turn in (1, 2, 3):
            path = self.directory / f"frames/turn-{turn:03d}.png"
            path.write_bytes(raw)
            frames[str(turn)] = {"turn": turn, "steps": turn * 10, "path": path.relative_to(self.root).as_posix()}
        write_json(self.directory / "frames.json", frames)
        self.write_events([dict(self.event, turn=i) for i in (1, 2, 3)])
        client = Client(review(), review(), review())
        self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        labels = [label for c in client.calls for label in loads(c[1]["content"][0]["text"])["image_order"]]
        self.assertEqual({label["turn"] for label in labels}, {1, 2, 3})
        self.assertTrue(all(label["steps"] == label["turn"] * 10 for label in labels))

    def test_hypothesis_is_saved_but_not_loaded_and_not_downgrades_supported(self):
        memory = Experience(self.root)
        memory.merge([finding(confidence="hypothesis")], self.scope, {"run": "logs/a", "events_sha256": "a"})
        self.assertEqual(memory.context("planner", {"task_state": self.state}), [])
        memory.merge([finding()], self.scope, {"run": "logs/b", "events_sha256": "b"})
        memory.merge([dict(finding(confidence="hypothesis"), lesson="unconfirmed replacement")], self.scope,
                     {"run": "logs/c", "events_sha256": "c"})
        loaded = memory.context("planner", {"task_state": self.state})
        self.assertEqual(len(loaded), 1)
        self.assertNotIn("unconfirmed replacement", loaded[0]["rule"])
        self.assertEqual(len(memory.read()["lessons"][0]["sources"]), 3)

    def test_automatic_review_can_update_curated_advice_from_same_run_and_keep_provenance(self):
        memory = Experience(self.root)
        source = {"run": "logs/a", "events_sha256": "a", "evidence_sha256": "a"}
        memory.merge([finding()], self.scope, {**source, "reviewer": "Codex curated"})
        updated = dict(finding(), lesson="Advice consolidated across the run.")
        automatic = {**source, "reviewer": "ToUser automatic post-run review", "model": "test"}
        self.assertEqual(len(memory.merge([updated], self.scope, automatic)), 1)
        item = memory.read()["lessons"][0]
        self.assertEqual(item["lesson"], updated["lesson"])
        self.assertEqual([s["reviewer"] for s in item["sources"]], ["Codex curated", automatic["reviewer"]])
        self.assertEqual(memory.merge([updated], self.scope, automatic), [])

    def test_unknown_or_invented_evidence_is_rejected(self):
        events = [{"event_index": 1, **self.event}]
        for lesson in (finding(99), finding(quote="bowl was lifted")):
            with self.subTest(lesson=lesson), self.assertRaises(ValueError):
                validate_review(review([lesson]), events, self.scope["task"])
        with self.assertRaises(ValueError):
            validate_review(review([finding()]), [{"event_index": 1, "event": "planner", "reason": "bowl stayed on table"}], self.scope["task"])

    def test_runtime_reads_only_concise_role_rules_without_audit_or_legacy_memory(self):
        long = dict(finding(), lesson="x" * 1000)
        write_json(self.root / 'memories/experience.json', {"schema_version": 1, "lessons": [long]})
        self.assertEqual(Experience(self.root).context('planner', {"task_state": self.state}), [])
        lesson = finding()
        lesson['roles'] = ['planner']
        Experience(self.root).merge([lesson, dict(finding(confidence='hypothesis'), id='unconfirmed')], self.scope,
            {"run": "logs/a", "events_sha256": "a"})
        loaded = Experience(self.root).context('planner', {"task_state": self.state})
        self.assertEqual(set(loaded[0]), {'id', 'rule'})
        self.assertLessEqual(len(loaded[0]['rule']), 300)
        self.assertNotIn('sources', dumps(loaded))
        self.assertEqual(Experience(self.root).context('detector', {"task_state": self.state}), [])
        for role in ('planner', 'detector'):
            self.assertNotIn('evidence', (self.root / ROLE_MEMORY_PATHS[role]).read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            Experience(self.root).merge([long], self.scope, {"run": "logs/b", "events_sha256": "b"})

    def test_critical_transferable_rule_survives_newer_noise_and_ids_are_deduplicated(self):
        memory = Experience(self.root)
        critical = dict(finding(), id="bowl-wall-contact", task_keywords=["bowl"],
                        priority="critical", applicability="adapter")
        memory.merge([critical], self.scope, {"run": "logs/critical", "events_sha256": "a"})
        # The same concept can have several scoped records, but only one slot.
        memory.merge([dict(finding(), id="duplicate", task_keywords=["bowl"])], self.scope,
                     {"run": "logs/b", "events_sha256": "b"})
        memory.merge([dict(finding(), id="duplicate")], self.scope,
                     {"run": "logs/c", "events_sha256": "c"})
        self.assertEqual(len(memory.context('planner', {'task_state': self.state})), 2)
        for index in range(8):
            memory.merge([dict(finding(), id=f"newer-{index}")], self.scope,
                         {"run": f"logs/newer-{index}", "events_sha256": str(index)})
        loaded = memory.context("planner", {"task_state": self.state})
        self.assertEqual(loaded[0]["id"], "bowl-wall-contact")
        self.assertEqual(len(loaded), 6)
        self.assertEqual(len({v['id'] for v in loaded}), len(loaded))
        other = deepcopy(self.state)
        other['identity']['adapter']['settings']['context'] = 'workspace_c'
        other['task'] = 'put the bowl inside the drawer'
        self.assertEqual([v['id'] for v in memory.context('planner', {'task_state': other})], ['bowl-wall-contact'])
        other['task'] = 'open the drawer'
        self.assertEqual(memory.context('planner', {'task_state': other}), [])
        other['task'] = 'pick up the bowl'
        other['identity']['adapter']['factory'] = 'another:create'
        self.assertEqual(memory.context('planner', {'task_state': other}), [])

    def test_superseded_advice_stays_in_audit_and_cannot_be_resurrected_by_old_review(self):
        memory = Experience(self.root)
        memory.merge([finding()], self.scope, {'run': 'logs/a', 'events_sha256': 'a'})
        key = memory.read()['lessons'][0]['key']
        replacement = dict(finding(), id='canonical-bowl-grasp', priority='critical',
                           applicability='adapter', task_keywords=['bowl'], supersedes=[key])
        memory.merge([replacement], self.scope, {'run': 'logs/b', 'events_sha256': 'b'})
        memory.merge([finding()], self.scope, {'run': 'logs/c', 'events_sha256': 'c'})
        self.assertEqual(len(memory.read()['lessons']), 2)
        self.assertTrue(memory.read()['lessons'][-1]['retired_by'])
        self.assertEqual([v['id'] for v in memory.context('planner', {'task_state': self.state})], ['canonical-bowl-grasp'])
        for bad in [dict(replacement, confidence='hypothesis'), dict(replacement, roles=['planner']),
                    dict(replacement, supersedes=['a' * 24])]:
            before = (self.root / MEMORY_PATH).read_bytes()
            with self.assertRaises(ValueError):
                memory.merge([bad], self.scope, {'run': 'logs/d', 'events_sha256': 'd'})
            self.assertEqual((self.root / MEMORY_PATH).read_bytes(), before)

    def test_memory_consolidation_checks_original_events_and_keeps_provenance_across_consolidations(self):
        memory = Experience(self.root)
        source = {'run': self.directory.relative_to(self.root).as_posix(), 'events_sha256': 'a'}
        memory.merge([finding()], self.scope, source)
        old_key = memory.read()['lessons'][0]['key']
        canonical = dict(finding(), id='bowl-wall-contact', task_keywords=['bowl'],
                         priority='critical', applicability='adapter', supersedes=[old_key])
        feedback = 'Prioritize bowl grasp contact over repeated generic reminders.'
        client = Client(review([canonical]))
        assistant = Analyzer(self.root, {}, {'model': 'test'}, client=client, output=lambda _: None)
        result = consolidate_memory(assistant, feedback=feedback)
        self.assertEqual(result['status'], 'applied')
        payload = loads(client.calls[0][1]['content'][0]['text'])
        self.assertEqual(payload['user_feedback'], feedback)
        self.assertEqual(payload['events'][0]['source_event_index'], 1)
        archive, data = collect_memory(self.root)
        self.assertEqual([v['id'] for v in data['lesson_catalog']], ['bowl-wall-contact'])
        self.assertEqual(data['events'][0]['run'], source['run'])
        self.assertEqual(data['events'][0]['source_event_index'], 1)
        self.assertEqual((self.directory / 'task_state.json').read_bytes(), self.original_state)
        # A changed original log cannot be replaced by the archive's assertions.
        self.write_events([dict(self.event, report={'reason': 'a different observation'})])
        with self.assertRaisesRegex(ValueError, 'verifiable original evidence'):
            collect_memory(self.root)

    def test_failed_memory_consolidation_never_overwrites_working_rules(self):
        memory = Experience(self.root)
        memory.merge([finding()], self.scope,
                     {'run': self.directory.relative_to(self.root).as_posix(), 'events_sha256': 'a'})
        before = {p: (self.root / p).read_bytes() for p in [MEMORY_PATH, *ROLE_MEMORY_PATHS.values()]}
        assistant = Analyzer(self.root, {}, {'model': 'test'}, client=Client(ModelError('unavailable')), output=lambda _: None)
        self.assertEqual(consolidate_memory(assistant, feedback='Review bowl contact.')['status'], 'failed')
        for path, raw in before.items():
            self.assertEqual((self.root / path).read_bytes(), raw)

    def test_compaction_preserves_critical_priority_and_transfer_scope(self):
        original = dict(finding(), priority='critical', applicability='adapter',
                        task_keywords=['bowl'], lesson='Preserve the concrete contact check. ' * 20)
        short = dict(original, lesson='Inspect contact geometry before closure.')
        wrong = dict(short, priority='normal', applicability='context')
        client = Client(review([original]), review([original]), review([wrong]), review([short]))
        result = self.assistant().review_run(self.directory, model={'model': 'test'}, client=client)
        self.assertEqual(result['status'], 'applied')
        self.assertIn('scope, roles, confidence', client.calls[-1][-1]['content'])
        saved = loads((self.root / ROLE_MEMORY_PATHS['planner']).read_text(encoding="utf-8"))[0]
        self.assertEqual(saved['priority'], 'critical')
        self.assertEqual(saved['scope']['context'], '*')

    def test_alternative_object_keywords_load_without_requiring_impossible_conjunction(self):
        memory = Experience(self.root)
        rule = dict(finding(), task_keywords=['bowl', 'basket', 'plate'], keyword_match='any',
                    applicability='adapter', priority='critical')
        memory.merge([rule], self.scope, {'run': 'logs/a', 'events_sha256': 'a'})
        other = deepcopy(self.state)
        other['identity']['adapter']['settings']['context'] = 'workspace_b'
        other['task'] = 'pick the salad dressing and place it in the basket'
        self.assertEqual(len(memory.context('planner', {'task_state': other})), 1)
        other['task'] = 'open the drawer'
        self.assertEqual(memory.context('planner', {'task_state': other}), [])
        payload = {'lesson_catalog': [], 'task_instructions': ['pick the bowl', 'place it in the basket']}
        validate_consolidation(review([rule]), payload)
        with self.assertRaisesRegex(ValueError, 'match no source task'):
            validate_consolidation(review([dict(rule, keyword_match='all')]), payload)

    def test_interrupted_contract_repair_reuses_draft_without_publishing_it(self):
        memory = Experience(self.root)
        memory.merge([finding()], self.scope,
                     {'run': self.directory.relative_to(self.root).as_posix(), 'events_sha256': 'a'})
        long = dict(finding(), priority='critical', applicability='adapter', lesson='Inspect the contact. ' * 25)
        client = Client(review([long]), ModelError('unavailable'))
        assistant = Analyzer(self.root, {}, {'model': 'test'}, client=client, output=lambda _: None)
        feedback = 'Focus on bowl contact.'
        result = consolidate_memory(assistant, feedback=feedback)
        self.assertEqual(result['status'], 'failed')
        short = dict(long, lesson='Inspect the contact before closure.')
        assistant.client = Client(review([short]))
        result = consolidate_memory(assistant, feedback=feedback)
        self.assertEqual(result['status'], 'applied')
        payload = loads(assistant.client.calls[0][1]['content'][0]['text'])
        self.assertEqual(payload['draft_review']['findings'][0]['id'], long['id'])
        self.assertEqual(payload['draft_review']['findings'][0]['lesson'].strip(), long['lesson'].strip())

    def test_memory_consolidation_cli_does_not_require_episode_or_start_simulator(self):
        write_json(self.root / 'config.json', {'models': {'planner': {'model': 'test'}}})
        with patch('pac_harness.memory_review.consolidate_memory', return_value={'status': 'applied'}) as run:
            code = analyzer_main(['--root', str(self.root), '--config', 'config.json', '--consolidate-memory',
                                  '--feedback', 'Focus on bowl contact.', '--adapter', self.scope['adapter']])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs, {'feedback': 'Focus on bowl contact.',
                         'adapter': self.scope['adapter'], 'force': False})

    def test_consolidation_rejects_stale_snapshot_before_memory_write(self):
        memory = Experience(self.root)
        memory.merge([finding()], self.scope, {'run': 'logs/a', 'events_sha256': 'a'})
        before = (self.root / MEMORY_PATH).read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed during consolidation'):
            memory.merge([finding()], self.scope, {'run': 'logs/b', 'events_sha256': 'b'},
                         expected_archive_sha256='stale')
        self.assertEqual((self.root / MEMORY_PATH).read_bytes(), before)

    def test_old_completed_report_is_condensed_by_analyzer_without_replaying_chunks(self):
        data = collect_run(self.root, self.directory)[1]
        long = dict(finding(), lesson='Historical narrative. ' * 90)
        write_json(self.directory / 'review/report.json', {
            'status': 'applied', 'evidence_sha256': data['evidence_sha256'], 'review': review([long])})
        # First model answer is too long; analyzer repairs before publishing.
        client = Client(review([long]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={'model': 'test'}, client=client)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['memory_format'], 'role_rules_en_v2')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(loads(client.calls[0][1]['content'][0]['text'])['mode'], 'memory_compaction')
        self.assertNotIn('chunk_reviews', client.calls[0][1]['content'][0]['text'])
        self.assertLessEqual(len(compact_rule(result['short_review']['findings'][0])), 300)
        self.assertEqual(set(result['changed_files']), {MEMORY_PATH, *ROLE_MEMORY_PATHS.values()})

    def test_chinese_advice_is_repaired_and_cannot_be_published(self):
        chinese = dict(finding(), lesson='\u62ac\u5347\u786e\u8ba4\u6293\u53d6')
        client = Client(review([chinese]), review([finding()]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={'model': 'test'}, client=client)
        self.assertEqual(result['status'], 'applied')
        self.assertIn('English', client.calls[1][-1]['content'])
        loaded = Experience(self.root).context('planner', {'task_state': self.state})
        self.assertTrue(loaded[0]['rule'].isascii())
        with self.assertRaises(ValueError):
            Experience(self.root).merge([chinese], self.scope, {'run': 'logs/chinese', 'events_sha256': 'x'})

    def test_short_legacy_chinese_rules_are_regenerated_in_english_by_analyzer(self):
        data = collect_run(self.root, self.directory)[1]
        chinese = dict(finding(), lesson='\u62ac\u5347\u786e\u8ba4\u6293\u53d6')
        write_json(self.directory / 'review/report.json', {
            'status': 'applied', 'memory_format': 'role_rules_v1',
            'evidence_sha256': data['evidence_sha256'], 'review': review([chinese])})
        client = Client(review([finding()]))
        result = self.assistant().review_run(self.directory, model={'model': 'test'}, client=client)
        self.assertEqual(result['memory_format'], 'role_rules_en_v2')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(loads(client.calls[0][1]['content'][0]['text'])['mode'], 'memory_compaction')
        archive = Experience(self.root).read()['lessons'][0]
        self.assertEqual(archive['sources'][-1]['language'], 'en')

    def test_verbatim_decoded_quotes_are_not_rejected_for_json_escaping(self):
        quote = 'The stop reason was "arrived".\nThe bowl stayed on table.'
        event = {"event_index": 1, "event": "detector", "report": {"reason": quote}}
        validate_review(review([finding(quote=quote)]), [event], self.scope["task"])

    def test_bad_contract_is_repaired_before_writing_memory(self):
        client = Client(review([finding(99)]), review([finding()]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "applied")
        self.assertIn("Unknown review evidence", client.calls[1][-1]["content"])

    def test_partial_review_failure_is_retryable_and_does_not_change_memory(self):
        self.write_events([dict(self.event, turn=i) for i in (1, 2, 3)])
        client = Client(review([finding()]), ModelError("unavailable"))
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / MEMORY_PATH).exists())
        retry_client = Client(review([finding(3)]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=retry_client)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(retry_client.calls), 2)  # First completed chunk was cached.

    def test_transient_large_request_is_split_and_every_turn_is_still_read(self):
        self.write_events([dict(self.event, turn=i) for i in (1, 2, 3)])
        client = Client(ModelError("HTTP 500", retryable=True), review([finding()]), review([finding(2)]),
                        review([finding(3)]), review([finding()]))
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(result["chunks"]), 3)
        ranges = [sorted({e["turn"] for e in loads(c[1]["content"][0]["text"])["events"]
                           if e["event"] == "detector"}) for c in client.calls[1:-1]]
        self.assertEqual(ranges, [[1], [2], [3]])
        self.assertTrue(loads((self.directory / "review/chunk-001.json").read_text(encoding="utf-8"))["split"])

    def test_repeat_review_is_idempotent_and_new_diagnostics_invalidate_cache(self):
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=Client(review([finding()]), review([finding()])))
        client = Client()
        again = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result, again)
        self.assertEqual(client.calls, [])
        before = collect_run(self.root, self.directory)[1]["evidence_sha256"]
        write_json(self.directory / "new_diagnosis.json", [{"turn": 1, "distance_m": .03}])
        self.assertNotEqual(collect_run(self.root, self.directory)[1]["evidence_sha256"], before)

    def test_feedback_invalidates_completed_review_and_updates_existing_rule(self):
        self.assistant().review_run(self.directory, model={"model": "test"},
                                   client=Client(review([finding()]), review([finding()])))
        feedback = "Check repeated off-center placement in agentview, rather than surface-point agreement."
        updated = dict(finding(), lesson="Reobserve geometry before repeating the same placement.")
        client = Client(review([updated]), review([updated]))
        assistant = Analyzer(self.root, {}, {"model": "test"}, client=client, output=lambda _: None)
        result = assistant.review(self.directory, feedback=feedback)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["status"], "applied")
        for call in client.calls:
            self.assertEqual(loads(call[1]["content"][0]["text"])["user_feedback"], feedback)
        self.assertIn(updated["lesson"], (self.root / ROLE_MEMORY_PATHS["planner"]).read_text(encoding="utf-8"))
        sources = Experience(self.root).read()["lessons"][0]["sources"]
        self.assertEqual(sources[-1]["user_feedback"], feedback)
        self.assertEqual(len(sources), 2)
        assistant.client = Client()
        self.assertEqual(assistant.review(self.directory), result)
        self.assertEqual(assistant.client.calls, [])
        self.assertEqual((self.directory / "task_state.json").read_bytes(), self.original_state)

    def test_feedback_reaches_compaction_and_retries_without_becoming_outcome_evidence(self):
        feedback = "The bowl looks left of the plate center in agentview. Inspect a small rightward correction."
        long = dict(finding(), lesson="Reassess placement geometry. " * 30)
        client = Client(review([long]), review([long]), ModelError("unavailable"))
        assistant = Analyzer(self.root, {}, {"model": "test"}, client=client, output=lambda _: None)
        result = assistant.review(self.directory, feedback=feedback)
        self.assertEqual(result["stage"], "memory_compaction")
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / MEMORY_PATH).exists())
        for call in client.calls:
            payload = loads(call[1]["content"][0]["text"])
            self.assertEqual(payload["user_feedback"], feedback)
            self.assertNotIn(feedback, dumps(payload.get("events", [])))
        assistant.client = Client(review([finding()]))
        result = assistant.review(self.directory)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(assistant.client.calls), 1)
        payload = loads(assistant.client.calls[0][1]["content"][0]["text"])
        self.assertEqual(payload["mode"], "memory_compaction")
        self.assertEqual(payload["user_feedback"], feedback)
        self.assertEqual(result["user_feedback"], feedback)

    def test_feedback_cli_forwards_operator_context(self):
        write_json(self.root / "config.json", {"models": {"planner": {"model": "test"}}})
        with patch("pac_harness.post_run.Analyzer.review", return_value={"status": "no_change"}) as run:
            code = analyzer_main(["--root", str(self.root), "--config", "config.json",
                                  "--run-dir", str(self.directory), "--feedback", "Inspect placement bias."])
        self.assertEqual(code, 0)
        run.assert_called_once_with(self.directory.resolve(), force=False, feedback="Inspect placement bias.")

    def test_completed_review_restores_deleted_role_files_without_model_requests(self):
        result = self.assistant().review_run(self.directory, model={"model": "test"},
                                            client=Client(review([finding()]), review([finding()])))
        self.assertEqual(result['status'], 'applied')
        originals = {path: (self.root / path).read_bytes() for path in ROLE_MEMORY_PATHS.values()}
        archive = (self.root / MEMORY_PATH).read_bytes()
        for path in originals:
            (self.root / path).unlink()
        client = Client()
        restored = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(set(restored['restored_files']), set(originals))
        for path, raw in originals.items():
            self.assertEqual((self.root / path).read_bytes(), raw)
        self.assertEqual((self.root / MEMORY_PATH).read_bytes(), archive)

    def test_restore_uses_latest_archive_and_preserves_existing_role_file(self):
        self.assistant().review_run(self.directory, model={"model": "test"},
                                   client=Client(review([finding()]), review([finding()])))
        new = dict(finding(), lesson='Use the most recent observed outcome.')
        Experience(self.root).merge([new], self.scope, {'run': 'logs/newer', 'events_sha256': 'new'})
        detector = self.root / ROLE_MEMORY_PATHS['detector']
        existing = detector.read_bytes() + b'\n'
        detector.write_bytes(existing)
        (self.root / ROLE_MEMORY_PATHS['planner']).unlink()
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=Client())
        self.assertEqual(result['restored_files'], [ROLE_MEMORY_PATHS['planner']])
        self.assertIn(new['lesson'], (self.root / ROLE_MEMORY_PATHS['planner']).read_text(encoding="utf-8"))
        self.assertEqual(detector.read_bytes(), existing)

    def test_missing_archive_and_role_files_are_rebuilt_from_completed_short_review(self):
        self.assistant().review_run(self.directory, model={"model": "test"},
                                   client=Client(review([finding()]), review([finding()])))
        for path in [MEMORY_PATH, *ROLE_MEMORY_PATHS.values()]:
            (self.root / path).unlink()
        client = Client()
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(result['stage'], 'completed')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(Experience(self.root).context('planner', {'task_state': self.state})), 1)
        self.assertTrue((self.root / ROLE_MEMORY_PATHS['detector']).is_file())

    def test_hierarchical_synthesis_covers_all_chunks_and_resumes_completed_groups(self):
        self.write_events([dict(self.event, turn=i) for i in range(1, 7)])
        assistant = ToUser(self.root, {"post_run": {"chunk_turns": 1, "synthesis_fan_in": 2}},
                          output_fn=lambda _: None)
        # Six chronological reviews, then three first-level summaries. Fail on level two.
        replies = [dict(review(), summary=f"turn {i}") for i in range(1, 7)]
        groups = [dict(review(), summary=f"group {i}") for i in range(1, 4)]
        client = Client(*replies, *groups, ModelError("upstream unavailable", retryable=True))
        result = assistant.review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "final_synthesis")
        for i, call in enumerate(client.calls[6:9]):
            payload = loads(call[1]["content"][0]["text"])
            self.assertEqual(payload["chunk_reviews"], replies[i * 2:i * 2 + 2])
            self.assertEqual(payload["outcome_event_indices"], list(range(1, 8)))
        self.assertFalse((self.root / MEMORY_PATH).exists())
        retry = Client(dict(review(), summary="groups 1 and 2"), review([finding()]))
        result = assistant.review_run(self.directory, model={"model": "test"}, client=retry)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(retry.calls), 2)
        self.assertEqual(loads(retry.calls[1][1]["content"][0]["text"])["chunk_reviews"][1], groups[2])

    def test_failed_synthesis_group_is_split_without_unbounded_recursion(self):
        self.write_events([dict(self.event, turn=i) for i in range(1, 7)])
        assistant = ToUser(self.root, {"post_run": {"chunk_turns": 1, "synthesis_fan_in": 3}},
                          output_fn=lambda _: None)
        client = Client(*[review() for _ in range(6)], ModelError("HTTP 500", retryable=True),
                        review(), review(), review(), review(), review([finding()]))
        result = assistant.review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "applied")
        cache = loads((self.directory / 'review/synthesis-01-001.json').read_text(encoding="utf-8"))
        self.assertTrue(cache['split'])
        self.assertEqual(len(client.calls), 12)

    def test_synthesis_failure_and_cached_splits_are_reported_accurately(self):
        self.write_events([dict(self.event, turn=i) for i in (1, 2)])
        messages = []
        assistant = self.assistant()
        assistant.output_fn = messages.append
        client = Client(ModelError("HTTP 500", retryable=True), review(), review(),
                        ModelError("Model HTTP 500", retryable=True))
        result = assistant.review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "final_synthesis")
        self.assertEqual(len(result["chunks"]), 2)
        self.assertIn("final_synthesis", messages[-1])
        self.assertIn("Model HTTP 500", messages[-1])
        messages.clear()
        retry = Client(review())
        result = assistant.review_run(self.directory, model={"model": "test"}, client=retry)
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(len(retry.calls), 1)
        self.assertTrue(any("Reusing cached split" in message for message in messages))
        self.assertEqual(sum("Reusing completed review cache" in message for message in messages), 2)
        self.assertFalse(any("Service request failed" in message for message in messages))

    def test_missing_images_are_explicit_and_logs_can_still_be_reviewed(self):
        write_json(self.directory / "frames.json", {"1": {"turn": 1, "path": "logs/missing.png"}})
        client = Client(review(), review())
        result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "no_change")
        self.assertTrue(result["omissions"])

    def test_disabled_review_never_calls_model(self):
        self.assertIsNone(maybe_review(self.root, self.directory, {}))

    def test_active_episode_is_not_reviewed_or_its_report_overwritten(self):
        original = {"status": "reviewing", "owner": "another process"}
        write_json(self.directory / "review/report.json", original)
        client = Client()
        with RunLock(self.directory):
            result = self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(client.calls, [])
        self.assertEqual(loads((self.directory / "review/report.json").read_text(encoding="utf-8")), original)

    def test_generic_cli_saves_result_and_closes_adapter_before_review(self):
        from pac_harness.__main__ import main
        from examples.list_sorting import ListEnvironment, DemoPlanner, DemoDetector
        config = {"adapter": {"factory": "examples.list_sorting:create", "settings": {}},
                  "models": {"planner": {"model": "test"}, "detector": {"model": "test"}},
                  "to_user": {"enabled": False}, "analyzer": {"enabled": True}}
        write_json(self.root / "config.json", config)
        output = self.root / "logs/generic-run"
        env = ListEnvironment(output, [3, 1, 2])
        env.closed = False
        def close():
            env.closed = True
        env.close = close
        def assess(root, directory, config, **kwargs):
            self.assertTrue(env.closed)
            self.assertEqual(loads((directory / "result.json").read_text(encoding="utf-8"))["status"], "completed")
            return {"status": "failed", "error": "review unavailable"}
        with patch("examples.list_sorting.create", return_value=env), patch(
                "pac_harness.__main__.ChatClient"), patch(
                "pac_harness.__main__.Agent", side_effect=[DemoPlanner(), DemoDetector()]), patch(
                "pac_harness.post_run.maybe_review", side_effect=assess) as review_call, patch("builtins.print"):
            code = main(["--root", str(self.root), "--config", "config.json",
                         "--task", "Sort the list", "--run-dir", "logs/generic-run"])
        self.assertEqual(code, 0)
        review_call.assert_called_once()

    def test_secrets_are_redacted_in_review_input_and_report(self):
        self.write_events([dict(self.event, error="Bearer private-token", api_key="secret-value")])
        client = Client(review(), review())
        self.assistant().review_run(self.directory, model={"model": "test"}, client=client)
        self.assertNotIn("secret-value", dumps(client.calls))
        self.assertNotIn("private-token", (self.directory / "review/input.json").read_text(encoding="utf-8"))

    def test_cli_discovers_generic_nested_runs(self):
        write_json(self.root / "config.json", {"models": {"planner": {"model": "test"}}})
        with patch("pac_harness.post_run.Analyzer.review", return_value={"status": "no_change"}) as run, patch("builtins.print"):
            code = analyzer_main(["--root", str(self.root), "--run-dir", "logs"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(self.directory.resolve(), force=False, feedback=None)

    def test_analyzer_reviews_generic_runtime_without_preloaded_context(self):
        from examples.list_sorting import ListEnvironment, DemoPlanner, DemoDetector
        from pac_harness.runtime import Harness
        directory = self.root / 'logs/generic-learning'
        env = ListEnvironment(directory, [3, 1, 2])
        identity = {'factory': 'examples.list_sorting:create', 'settings': {}}
        result = Harness(self.root, env, DemoPlanner(), DemoDetector(), identity=identity,
                         output=lambda _: None).run('Sort the list', directory)
        self.assertEqual(result['status'], 'completed')
        write_json(directory / 'result.json', result)
        _, data = collect_run(self.root, directory)
        outcome = next(e for e in data['events'] if e['event'] == 'action_result')
        advice = {**finding(outcome['event_index'], quote='items'), 'id': 'preserve-list-elements',
                  'task_keywords': ['list'], 'condition': 'When sorting a list.',
                  'lesson': 'Preserve its elements.', 'verification': 'Verify order and membership.',
                  'recheck_when': 'Recheck when the task changes.'}
        client = Client(review([advice]), review([advice]))
        assistant = Analyzer(self.root, {'chunk_turns': 8}, {'model': 'test'}, client=client, output=lambda _: None)
        report = assistant.review(directory)
        self.assertEqual(report['status'], 'applied')
        rules = loads((self.root / ROLE_MEMORY_PATHS['planner']).read_text(encoding='utf-8'))
        self.assertEqual(rules[0]['scope']['context'], '')
        self.assertEqual(rules[0]['scope']['adapter'], identity['factory'])


if __name__ == "__main__":
    unittest.main()
