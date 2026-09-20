"""Selected provider/model inheritance; no credentials, model or device calls."""
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from pac_harness.codex_connection import CodexConnectionError, model_connection


CONFIG = '''model = "test-model"
model_provider = "test_provider"
model_reasoning_effort = "ultra"
sandbox_mode = "danger-full-access"
[model_providers.test_provider]
name = "test_provider"
base_url = "https://models.example.test/v1"
wire_api = "responses"
requires_openai_auth = true
[mcp_servers.private_service]
command = "must-not-load"
'''


def overrides(arguments):
    result = {}
    for flag, value in zip(arguments[::2], arguments[1::2]):
        assert flag == "-c"
        key, raw = value.split("=", 1)
        result[key] = tomllib.loads("value=" + raw)["value"]
    return result


class CodexConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.toml"
        self.env = {"CODEX_HOME": str(self.root)}

    def load(self, text=CONFIG, settings=None):
        self.path.write_text(text, encoding="utf-8")
        return model_connection(settings or {}, self.env)

    def test_real_failure_configuration_routes_to_selected_gateway_and_model(self):
        arguments, env, audit = self.load()
        values = overrides(arguments)
        self.assertEqual(values["model_provider"], "test_provider")
        self.assertEqual(values["model"], "test-model")
        self.assertEqual(values["model_reasoning_effort"], "ultra")
        self.assertEqual(values["model_providers.test_provider"]["base_url"], "https://models.example.test/v1")
        self.assertTrue(values["model_providers.test_provider"]["requires_openai_auth"])
        self.assertNotIn("api.openai.com", str(arguments))
        self.assertNotIn("mcp", str(arguments))
        self.assertNotIn("danger-full-access", str(arguments))
        self.assertEqual(env, {})
        self.assertEqual(audit["model_provider"], "test_provider")

    def test_missing_config_uses_codex_defaults_without_reading_auth(self):
        (self.root / "auth.json").write_text("not JSON; must not read", encoding="utf-8")
        arguments, env, audit = model_connection({}, self.env)
        self.assertEqual(overrides(arguments), {"model_provider": "openai"})
        self.assertEqual(audit["config_source"], "Codex defaults")
        self.assertEqual(env, {})

    def test_explicit_model_overrides_inherited_model(self):
        arguments, _, audit = self.load(settings={"model": "selected-model", "reasoning_effort": "high"})
        self.assertEqual(overrides(arguments)["model"], "selected-model")
        self.assertEqual(overrides(arguments)["model_reasoning_effort"], "high")
        self.assertEqual(audit["model"], "selected-model")

    def test_inline_profile_model_and_provider_are_honored(self):
        text = CONFIG + '\n[profiles.special]\nmodel="profile-model"\nmodel_reasoning_effort="high"\n'
        arguments, _, audit = self.load(text, {"profile": "special"})
        self.assertEqual(overrides(arguments)["model"], "profile-model")
        self.assertEqual(audit["model_provider"], "test_provider")

    def test_separate_profile_file_overrides_selected_connection(self):
        (self.root / "special.config.toml").write_text('model="profile-model"\n', encoding="utf-8")
        arguments, _, _ = self.load('profile="special"\n' + CONFIG)
        self.assertEqual(overrides(arguments)["model"], "profile-model")
        self.assertEqual(overrides(arguments)["model_provider"], "test_provider")

    def test_profile_partial_provider_override_preserves_address_and_auth(self):
        (self.root / "special.config.toml").write_text(
            '[model_providers.test_provider]\nstream_max_retries=2\n', encoding="utf-8")
        arguments, _, _ = self.load('profile="special"\n' + CONFIG)
        provider = overrides(arguments)["model_providers.test_provider"]
        self.assertEqual(provider["base_url"], "https://models.example.test/v1")
        self.assertTrue(provider["requires_openai_auth"])
        self.assertEqual(provider["stream_max_retries"], 2)

    def test_unsupported_connection_fields_are_not_silently_dropped(self):
        for field in ('query_params={version="v1"}', 'aws={region="somewhere"}'):
            with self.subTest(field=field), self.assertRaises(CodexConnectionError):
                self.load(CONFIG.replace('requires_openai_auth = true', field))

    def test_missing_provider_or_profile_fails_without_default_routing(self):
        for config, settings in [('model_provider="missing"', {}), (CONFIG, {"profile": "absent"}),
                                 (CONFIG, {"profile": "../other"})]:
            with self.subTest(config=config, settings=settings), self.assertRaises(CodexConnectionError):
                self.load(config, settings)

    def test_provider_auth_environment_is_selected_without_application_credentials(self):
        self.env.update(PROVIDER_KEY="private-provider", UNRELATED_KEY="unrelated", ACTION_ARM_TOKEN="robot-secret")
        text = CONFIG.replace("requires_openai_auth = true", 'env_key="PROVIDER_KEY"')
        arguments, env, audit = self.load(text)
        self.assertEqual(env, {"PROVIDER_KEY": "private-provider"})
        self.assertNotIn("private-provider", str(arguments) + json.dumps(audit))

    def test_static_auth_and_headers_stay_out_of_command_arguments_and_audit(self):
        text = CONFIG.replace("requires_openai_auth = true", '''experimental_bearer_token="private-token"
http_headers={Authorization="private-header"}''')
        arguments, env, audit = self.load(text)
        self.assertIn("private-token", env.values())
        self.assertIn("private-header", env.values())
        self.assertNotIn("private-token", str(arguments) + json.dumps(audit))
        self.assertNotIn("private-header", str(arguments) + json.dumps(audit))

    def test_missing_env_key_is_actionable_before_cli_launch(self):
        with self.assertRaisesRegex(CodexConnectionError, "PROVIDER_KEY"):
            self.load(CONFIG.replace("requires_openai_auth = true", 'env_key="PROVIDER_KEY"'))

    def test_builtin_openai_proxy_is_preserved(self):
        arguments, _, audit = self.load('openai_base_url="https://proxy.example/v1"')
        self.assertEqual(overrides(arguments)["openai_base_url"], "https://proxy.example/v1")
        self.assertEqual(audit["base_url"], "https://proxy.example/v1")


if __name__ == "__main__":
    unittest.main()
