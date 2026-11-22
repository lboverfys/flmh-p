import unittest

from flmh_agent.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_knowledge_namespace_rejects_index_wildcards_and_paths(self):
        for index in ("knowledge*", "other,index", "../index", "Uppercase"):
            with self.subTest(index=index), self.assertRaises(ConfigurationError):
                Settings.from_env({"ELASTICSEARCH_INDEX": index})

    def test_defaults_select_local_rule_and_template_adapters(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.planner_backend, "rules")
        self.assertEqual(settings.checkpointer_backend, "memory")
        self.assertEqual(settings.embedding_backend, "")
        self.assertIsNone(settings.embedding_dimension)

    def test_embedding_dimension_is_explicit_and_empty_template_keeps_rag_disabled(self):
        self.assertIsNone(Settings.from_env({"BGE_M3_DIMENSION": ""}).embedding_dimension)
        for value in (0, -1, True, "not-a-dimension"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                Settings.from_env({"BGE_M3_DIMENSION": value})
        configured = {
            "EMBEDDING_BACKEND": "bge_m3_http", "BGE_M3_URL": "http://127.0.0.1:1/encode",
            "BGE_M3_MODEL": "fixture-encoder", "BGE_RERANKER_URL": "http://127.0.0.1:1/rerank",
            "BGE_RERANKER_MODEL": "fixture-reranker", "ELASTICSEARCH_URL": "http://127.0.0.1:2",
            "ELASTICSEARCH_INDEX": "fixture",
        }
        with self.assertRaisesRegex(ConfigurationError, "BGE_M3_DIMENSION"):
            Settings.from_env(configured)
        self.assertEqual(Settings.from_env({**configured, "BGE_M3_DIMENSION": "2"}).embedding_dimension, 2)

    def test_production_requires_auth_model_gateway_and_persistent_checkpoint(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"APP_ENV": "production"})

    def test_optional_integrations_require_coherent_configuration(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"CHECKPOINTER_BACKEND": "postgres"})
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"EMBEDDING_BACKEND": "bge_m3_http"})

    def test_mcp_bridge_requires_loopback_binding(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"MCP_HTTP_HOST": "0.0.0.0"})

    def test_partial_tracing_credentials_are_rejected(self):
        for field in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                Settings.from_env({field: "configured-value"})

    def test_production_configuration_accepts_explicit_adapter_selection(self):
        settings = Settings.from_env({
            "APP_ENV": "production",
            "AGENT_API_KEY": "service-token",
            "PLANNER_BACKEND": "model_gateway",
            "CHECKPOINTER_BACKEND": "postgres",
            "POSTGRES_CHECKPOINTER_URI": "postgresql://user:pass@localhost/agent",
        })
        self.assertEqual(settings.environment, "production")

    def test_extracted_rag_sql_and_language_settings_are_configurable(self):
        settings = Settings.from_env({
            "AGENT_DEFAULT_REGION": "ID",
            "AGENT_DEFAULT_LANGUAGE": "id-ID",
            "AGENT_REGION_LANGUAGE_MAP_JSON": '{"US":"en-US","ID":"id-ID"}',
            "AGENT_PLANNER_TEMPERATURE": "0.1",
            "AGENT_PLANNER_MAX_TOKENS": "1500",
            "AGENT_MAX_QUERIES_PER_PLAN": "6",
            "RAG_TOP_K": "8",
            "RAG_CANDIDATE_MULTIPLIER": "3",
            "RAG_RRF_K": "40",
            "RAG_PIT_KEEP_ALIVE": "2m",
            "RAG_LOCAL_CORPUS_FALLBACK": "false",
            "SQL_DIALECT": "mysql",
            "SQL_TIMEOUT_MS": "3000",
            "SQL_MAX_RETRIES": "2",
            "SQL_DEFAULT_SCORE_VERSION": "local-v1",
            "BI_DATABASE_URI": "mysql://reader:secret@127.0.0.1:3306/flmh_product",
        })
        self.assertEqual(settings.agent_default_region, "ID")
        self.assertEqual(settings.agent_default_language, "id-ID")
        self.assertEqual(settings.rag_top_k, 8)
        self.assertFalse(settings.rag_local_corpus_fallback)
        self.assertEqual(settings.sql_dialect, "mysql")
        self.assertEqual(settings.sql_timeout_ms, 3000)

        with self.assertRaises(ConfigurationError):
            Settings.from_env({"BI_DATABASE_URI": "http://127.0.0.1/bad"})
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"AGENT_REGION_LANGUAGE_MAP_JSON": '{"USA":"en-US"}'})
