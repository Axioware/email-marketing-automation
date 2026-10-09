import os
import unittest
from unittest import mock

from pipeline.services import llm as r
from pipeline.services import research as research_business_websites

KEYS = ("GROQ_API_KEY", "GROK_API_KEY", "GROQ_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL")


def env(**values):
    clean = {k: v for k, v in os.environ.items() if k not in KEYS}
    return mock.patch.dict(os.environ, {**clean, **values}, clear=True)


class ProviderSelectionTests(unittest.TestCase):
    def test_groq_wins_when_both_are_set(self):
        with env(GROQ_API_KEY="g-key", OPENAI_API_KEY="o-key"):
            client, model, provider = r.make_llm_client()
        self.assertEqual((provider, model), ("groq", r.DEFAULT_GROQ_MODEL))
        self.assertEqual(client.api_key, "g-key")
        self.assertEqual(str(client.base_url).rstrip("/"), r.GROQ_BASE_URL)

    def test_grok_spelling_is_accepted(self):
        with env(GROK_API_KEY="g-key", OPENAI_API_KEY="o-key"):
            client, _, provider = r.make_llm_client()
        self.assertEqual((provider, client.api_key), ("groq", "g-key"))

    def test_groq_spelling_takes_precedence_over_alias(self):
        with env(GROQ_API_KEY="real", GROK_API_KEY="alias"):
            self.assertEqual(r.make_llm_client()[0].api_key, "real")

    def test_falls_back_to_openai_without_groq(self):
        with env(OPENAI_API_KEY="o-key"):
            client, model, provider = r.make_llm_client()
        self.assertEqual((provider, model), ("openai", r.DEFAULT_OPENAI_MODEL))
        self.assertEqual(client.api_key, "o-key")
        self.assertIn("api.openai.com", str(client.base_url))

    def test_empty_or_blank_groq_key_counts_as_absent(self):
        for blank in ("", "   "):
            with env(GROQ_API_KEY=blank, GROK_API_KEY=blank, OPENAI_API_KEY="o-key"):
                self.assertEqual(r.make_llm_client()[2], "openai")

    def test_model_overrides(self):
        with env(GROQ_API_KEY="g", GROQ_MODEL="openai/gpt-oss-20b"):
            self.assertEqual(r.make_llm_client()[1], "openai/gpt-oss-20b")
        with env(OPENAI_API_KEY="o", OPENAI_MODEL="gpt-x"):
            self.assertEqual(r.make_llm_client()[1], "gpt-x")
        with env(GROQ_API_KEY="g", OPENAI_MODEL="gpt-x"):  # OpenAI's model setting never leaks into Groq
            self.assertEqual(r.make_llm_client()[1], r.DEFAULT_GROQ_MODEL)

    def test_neither_key(self):
        with env():
            self.assertIsNone(r.make_llm_client())


class AgentCallTests(unittest.TestCase):
    """The agent sends the same structured request whichever provider is selected."""

    def test_score_decision_is_parsed_from_either_provider(self):
        content = ('{"action":"SCORE","url":null,"score":72,"reasons":["Books by phone"],"reasoning":"ok",'
                   '"reason":null,"summary":"Dental clinic","relevant_findings":["Phone booking"]}')
        for model in (r.DEFAULT_GROQ_MODEL, r.DEFAULT_OPENAI_MODEL):
            client = mock.Mock()
            client.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(content=content))]
            decision = research_business_websites.validate_agent_response(client, model, {"current_page_url": "https://x.com/"}, must_score=False)
            self.assertEqual((decision["action"], decision["qualification_score"]), ("SCORE", 72))
            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs["model"], model)
            self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])


if __name__ == "__main__":
    unittest.main()


class PromptSizeTests(unittest.TestCase):
    def test_agent_request_fits_the_groq_free_tier(self):
        import json

        links = [{"url": f"https://x.pk/page-{i}", "text": "Long link text " * 20, "same_site": True} for i in range(200)]
        context = {"business": {"name": "Clinic"}, "current_page_url": "https://x.pk/", "cleaned_page_content": "word " * 4000,
                   "current_page_links": links[:100], "previously_scraped_pages": [], "previously_discovered_emails": [],
                   "previously_discovered_links": links, "available_links": links, "pages_scraped": 1,
                   "pages_remaining": 4, "visited_urls": ["https://x.pk/"], "visited_identities": [["x.pk", "/", ""]]}
        payload = research_business_websites.prompt_context(context)
        for private in ("visited_identities", "current_page_links", "previously_discovered_links"):
            self.assertNotIn(private, payload)
        self.assertEqual(len(payload["available_links"]), research_business_websites.MAX_PROMPT_LINKS)
        self.assertEqual(set(payload["available_links"][0]), {"url", "text"})
        size = len(json.dumps(payload)) + len(research_business_websites.SYSTEM_PROMPT)
        self.assertLess(size / 3.5, 7000)  # tokens, with margin under Groq's 8,000 per minute
