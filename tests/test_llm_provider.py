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
            decision = research_business_websites.validate_agent_response(client, model, {"current_page_url": "https://x.com/"}, must_score=False,
                                                                               system_prompt="SYS")
            self.assertEqual((decision["action"], decision["qualification_score"]), ("SCORE", 72))
            kwargs = client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs["model"], model)
            self.assertTrue(kwargs["response_format"]["json_schema"]["strict"])
            self.assertEqual(kwargs["messages"][0]["content"], "SYS")
            schema = kwargs["response_format"]["json_schema"]["schema"]
            self.assertIn("SCRAPE: read one more page", schema["properties"]["action"]["description"])

    def test_team_size_and_languages_come_with_the_score(self):
        content = ('{"action":"SCORE","url":null,"score":80,"reasons":["Small"],"reasoning":"ok","reason":null,'
                   '"summary":"Clinic","relevant_findings":[],"team_size":6,'
                   '"languages":["English"," Urdu ","english","Sindhi"]}')
        client = mock.Mock()
        client.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(content=content))]
        from pipeline.models import DEFAULT_OUTPUT_FIELDS as fields

        decision = research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                                      must_score=False, system_prompt="SYS",
                                                                      output_fields=fields)
        self.assertEqual(decision["extracted"], {"team_size": 6, "languages": ["English", "Urdu", "Sindhi"]})
        schema = client.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["schema"]
        self.assertIn("team_size", schema["required"])  # strict schemas need every field, null when not stated
        self.assertIn("languages", schema["required"])
        unstated = content.replace('"team_size":6', '"team_size":null').replace(
            '"languages":["English"," Urdu ","english","Sindhi"]', '"languages":[]')
        client.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(content=unstated))]
        decision = research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                                      must_score=False, system_prompt="SYS",
                                                                      output_fields=fields)
        self.assertEqual(decision["extracted"], {"team_size": None, "languages": None})
        older = content.replace(',"team_size":6,"languages":["English"," Urdu ","english","Sindhi"]', "")
        client.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(content=older))]
        decision = research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                                      must_score=False, system_prompt="SYS",
                                                                      output_fields=fields)
        self.assertEqual(decision["extracted"], {"team_size": None, "languages": None})  # left out: null
        without = research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                                     must_score=False, system_prompt="SYS")
        self.assertEqual(without["extracted"], {})  # a prompt with no output fields

    def test_answer_refused_by_groqs_schema_check_is_used_when_only_a_null_field_is_missing(self):
        import httpx
        from openai import BadRequestError

        answer = ('{"action":"SCORE","url":null,"score":88,"reasons":["Books by phone"],"reasoning":"ok",'
                  '"summary":"Dental clinic","relevant_findings":[]}')  # "reason" left out
        body = {"error": {"code": "json_validate_failed", "failed_generation": answer}}
        refused = BadRequestError("Generated JSON does not match the expected schema", body=body,
                                  response=httpx.Response(400, request=httpx.Request("POST", "https://x")))
        client = mock.Mock()
        client.chat.completions.create.side_effect = refused
        decision = research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                                      must_score=False, system_prompt="SYS")
        self.assertEqual((decision["action"], decision["qualification_score"]), ("SCORE", 88))
        other = BadRequestError("bad key", body={"error": {"code": "invalid_api_key"}},
                                response=httpx.Response(400, request=httpx.Request("POST", "https://x")))
        client.chat.completions.create.side_effect = other
        with self.assertRaises(BadRequestError):  # any other error still stops
            research_business_websites.validate_agent_response(client, "m", {"current_page_url": "https://x.com/"},
                                                               must_score=False, system_prompt="SYS")

    def test_agent_that_cannot_pick_a_page_scores_from_what_it_read(self):
        extracted = {"url": "https://x.com/", "content": "Dental clinic", "links": [], "emails": []}
        scored = {"action": "SCORE", "summary": "s", "relevant_findings": [], "qualification_score": 70,
                  "qualification_reasons": ["r"], "agent_reasoning": "based on incomplete information"}
        with mock.patch.object(research_business_websites, "extract_page", return_value=extracted), \
                mock.patch.object(research_business_websites, "validate_agent_response",
                                  side_effect=[research_business_websites.ResearchError("no valid URL"), scored]) as agent:
            result = research_business_websites.research_business(
                mock.Mock(), "m", {"website_url": "https://x.com", "qualification_prompt": "CRITERIA"}, None)
        self.assertEqual((result["action"], result["qualification_score"], result["pages_scraped"]), ("SCORE", 70, 1))
        retry = agent.call_args_list[1]
        self.assertTrue(retry.args[3])  # must score now
        self.assertEqual(retry.kwargs["score_note"], "no further useful page could be chosen")
        self.assertNotIn("qualification_prompt", retry.args[2]["business"])  # the criteria are in the system prompt only
        self.assertEqual(retry.args[4], "CRITERIA")  # the campaign prompt exactly as stored, nothing added


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
        from pipeline.migrations._prompt_texts import QUALIFICATION_PROMPT

        size = len(json.dumps(payload)) + len(QUALIFICATION_PROMPT) + len(research_business_websites.answer_format())
        self.assertLess(size / 3.5, 7000)  # tokens, with margin under Groq's 8,000 per minute


class ModelSettingsTests(unittest.TestCase):
    def test_reasoning_models_get_no_temperature_and_room_to_think(self):
        for model in ("gpt-5-mini", "gpt-5.4-mini", "gpt-5.6-luna", "gpt-6-luna", "o4-mini"):
            self.assertEqual(r.tune(model, 0.2, 2500), {"reasoning_effort": "low", "max_completion_tokens": 8500}, model)
        for model in ("openai/gpt-oss-120b", "gpt-4.1-mini"):
            self.assertEqual(r.tune(model, 0.2, 2500), {"temperature": 0.2, "max_completion_tokens": 2500}, model)

    def test_a_refused_setting_is_dropped_once(self):
        import httpx
        from openai import BadRequestError

        def refuse(**request):
            if "temperature" in request:
                raise BadRequestError("Unsupported value: 'temperature'", body={"param": "temperature"},
                                      response=httpx.Response(400, request=httpx.Request("POST", "http://x")))
            return "ok"

        client = mock.Mock()
        client.chat.completions.create.side_effect = refuse
        self.assertEqual(r.complete(client, model="m", temperature=0.2, messages=[]), "ok")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        client.chat.completions.create.side_effect = BadRequestError(
            "bad schema", body={"param": "response_format"},
            response=httpx.Response(400, request=httpx.Request("POST", "http://x")))
        with self.assertRaises(BadRequestError):  # anything else is not retried
            r.complete(client, model="m", temperature=0.2, messages=[])
