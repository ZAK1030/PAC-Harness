"""Offline regression coverage for CLI failure evidence and credential masking."""
import json
import unittest

from pac_harness.codex_errors import codex_failure_detail


class CodexFailureDetailTests(unittest.TestCase):
    def test_final_401_is_not_hidden_by_stdin_stderr_or_reconnecting(self):
        detail = ("unexpected status 401 Unauthorized: Incorrect API key provided: sk-user-abc***TAIL. "
                  "You can find your API key at https://platform.openai.com/account/api-keys., "
                  "url: https://api.openai.com/v1/responses, auth error code: invalid_api_key")
        records = [
            {"type": "error", "message": "Reconnecting... 5/5 (request timed out)"},
            {"type": "item.completed", "item": {"type": "error", "message": "early timeout"}},
            {"type": "error", "message": detail},
            {"type": "turn.failed", "error": {"message": detail}},
        ]
        result = codex_failure_detail(records, "Reading prompt from stdin...")
        self.assertIn("API 密钥认证失败", result)
        self.assertIn("HTTP 401", result)
        self.assertIn("invalid_api_key", result)
        self.assertIn("请求端点：https://api.openai.com/v1/responses", result)
        for absent in ("stdin", "Reconnecting", "early timeout", "sk-user", "TAIL"):
            self.assertNotIn(absent, result)

    def test_turn_failed_wins_over_later_item_and_error_noise(self):
        records = [{"type": "turn.failed", "error": "final root cause"},
                   {"type": "item.completed", "item": {"type": "error", "message": "item failure"}},
                   {"type": "error", "message": "unrelated cleanup"}]
        self.assertIn("final root cause", codex_failure_detail(records, "cleanup stderr"))

    def test_last_concrete_error_in_each_priority_wins(self):
        for kind in ("turn.failed", "item.completed", "error"):
            with self.subTest(kind=kind):
                def event(text):
                    if kind == "turn.failed":
                        return {"type": kind, "error": {"message": text}}
                    if kind == "item.completed":
                        return {"type": kind, "item": {"type": "error", "message": text}}
                    return {"type": kind, "message": text}
                records = [event("first cause"), event("last cause"), event("Reconnecting... 1/5 (noise)")]
                result = codex_failure_detail(records, "")
                self.assertIn("last cause", result)
                self.assertNotIn("first cause", result)
                self.assertNotIn("noise", result)

    def test_generic_turn_failed_does_not_hide_specific_item_error(self):
        records = [{"type": "item.completed", "item": {"type": "error", "message": "quota exceeded"}},
                   {"type": "turn.failed", "error": {"message": "Turn failed"}}]
        self.assertIn("quota exceeded", codex_failure_detail(records, ""))

    def test_nested_error_metadata_retains_code_and_endpoint(self):
        records = {"type": "turn.failed", "message": "Turn failed", "error": {"message": "invalid credential",
                   "code": "invalid_api_key", "status_code": 401},
                   "endpoint": "https://gateway.example/v1/responses"}
        result = codex_failure_detail(records, None)
        self.assertIn("HTTP 401", result)
        self.assertIn("invalid_api_key", result)
        self.assertIn("请求端点：https://gateway.example/v1/responses", result)

    def test_mixed_json_records_and_malformed_values_do_not_raise(self):
        records = [None, 3, True, [], {"type": ["bad"]},
                   {"type": "turn.failed", "error": {"message": {"bad": 5}}},
                   "{not json", json.dumps({"type": "error", "message": "specific failure"})]
        self.assertIn("specific failure", codex_failure_detail(records, {"bad": "stderr"}))
        cyclic = {"type": "turn.failed"}
        cyclic["error"] = cyclic
        self.assertIn("未提供具体错误详情", codex_failure_detail(cyclic, None))

    def test_json_document_jsonl_and_stdout_fallback(self):
        event = {"type": "turn.failed", "error": "HTTP 403 permission_denied"}
        for records in ([event], event, json.dumps([event]), json.dumps(event),
                        json.dumps(event).encode(), "junk\n" + json.dumps(event)):
            with self.subTest(records=records):
                self.assertIn("API 拒绝访问", codex_failure_detail(records, "Reading prompt from stdin..."))
        result = codex_failure_detail([], "Reading prompt from stdin...", json.dumps(event))
        self.assertIn("API 拒绝访问", result)

    def test_plain_error_fallback_skips_progress_and_agent_messages(self):
        result = codex_failure_detail([], "Reading prompt from stdin...", "fatal: unsupported option --foo")
        self.assertIn("unsupported option --foo", result)
        result = codex_failure_detail([], "", json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "unrelated successful output"}}))
        self.assertIn("未提供具体错误详情", result)
        self.assertNotIn("unrelated", result)

    def test_reconnect_only_still_reports_underlying_failure_without_chatter(self):
        result = codex_failure_detail([{"type": "error", "message": "Reconnecting... 5/5 (request timed out)"}], "")
        self.assertIn("请求超时", result)
        self.assertNotIn("Reconnecting", result)

    def test_redacts_full_partial_bearer_and_named_credentials(self):
        secrets = ("sk-live-ABCD1234", "sk-user-abc***SECRETTAIL", "unprefixed-token", "jwt.fake.token",
                   "custom-api-secret", "quoted secret with spaces")
        text = (f"HTTP 401: {secrets[0]} {secrets[1]}; Authorization: Bearer {secrets[2]}; "
                f"Bearer {secrets[3]}; api_key={secrets[4]}; API key provided: '{secrets[5]}'")
        result = codex_failure_detail([{"type": "error", "message": text}], "")
        for secret in secrets:
            self.assertNotIn(secret, result)
        self.assertNotIn("SECRETTAIL", result)
        self.assertIn("密钥已隐藏", result)
        self.assertNotIn("]]", result)

    def test_already_masked_credentials_remain_readable(self):
        result = codex_failure_detail([{"type": "error", "message":
            "HTTP 401; API key provided: [REDACTED]; Authorization: Bearer [REDACTED]"}], "")
        self.assertNotIn("]]", result)
        self.assertIn("API key provided: [密钥已隐藏]", result)

    def test_endpoint_strips_userinfo_query_and_fragment_credentials(self):
        text = "HTTP 401; url: https://user:pass@gateway.example/v1/responses?api_key=private&x=1#token"
        result = codex_failure_detail([{"type": "error", "message": text}], "")
        self.assertIn("请求端点：https://gateway.example/v1/responses", result)
        for secret in ("user:pass", "private", "#token", "?api_key"):
            self.assertNotIn(secret, result)

    def test_truncation_is_after_redaction_and_preserves_diagnosis(self):
        text = "HTTP 429 rate_limit_exceeded url: https://gateway.example/v1/responses " + "detail " * 900
        text += "sk-user-neverexpose"
        result = codex_failure_detail([{"type": "error", "message": text}], "")
        self.assertLessEqual(len(result), 3000)
        self.assertIn("API 请求受限", result)
        self.assertIn("请求端点", result)
        self.assertNotIn("sk-user", result)


if __name__ == "__main__":
    unittest.main()
