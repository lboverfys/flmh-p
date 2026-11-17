import unittest

from flmh_agent.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_select_local_rule_and_template_adapters(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.planner_backend, "rules")
        self.assertEqual(settings.checkpointer_backend, "memory")
        self.assertEqual(settings.embedding_backend, "")

    def test_production_requires_auth_model_gateway_and_persistent_checkpoint(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"APP_ENV": "production"})

    def test_optional_integrations_require_coherent_configuration(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"CHECKPOINTER_BACKEND": "postgres"})
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"EMBEDDING_BACKEND": "bge_m3_http"})

    def test_production_configuration_accepts_explicit_adapter_selection(self):
        settings = Settings.from_env({
            "APP_ENV": "production",
            "AGENT_API_KEY": "service-token",
            "PLANNER_BACKEND": "model_gateway",
            "CHECKPOINTER_BACKEND": "postgres",
            "POSTGRES_CHECKPOINTER_URI": "postgresql://user:pass@localhost/agent",
        })
        self.assertEqual(settings.environment, "production")

