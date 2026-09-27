"""No paid calls: exercise relay formats and safe fallback boundaries."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from daily_digest.llm import LLMClient, LLMError


def response(payload=None, status=200, body=None, content_type="application/json"):
    result = Mock()
    result.status_code = status
    result.headers = {"Content-Type": content_type}
    result.text = body if body is not None else json.dumps(payload)
    result.json.return_value = payload
    return result


def model_response(text='{"selected": []}', usage=None):
    return response({
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": usage or {},
    })


def deepseek_response(finish_reason="stop", content='{"selected": []}', usage=None):
    return response({
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {
            "role": "assistant", "content": content, "reasoning_content": "PRIVATE REASONING"
        }}],
        "usage": usage or {},
    })


class ClientTests(unittest.TestCase):
    def client(self, **kwargs):
        return LLMClient("https://relay.example/v1", "gpt-5.6-sol", "private-test-key", **kwargs)

    def test_responses_payload_text_and_usage(self):
        client = self.client()
        client.session.post = Mock(return_value=model_response(
            '```json\n{"selected": ["2609.12345"]}\n```',
            {"input_tokens": 123, "output_tokens": 42, "input_tokens_details": {"cached_tokens": 50}},
        ))
        self.assertEqual(client.generate_json("Rank papers.", "Some papers.", 700), {"selected": ["2609.12345"]})
        args, kwargs = client.session.post.call_args
        self.assertEqual(args[0], "https://relay.example/v1/responses")
        self.assertEqual(kwargs["json"]["model"], "gpt-5.6-sol")
        user_input = kwargs["json"]["input"]
        self.assertEqual(user_input[0]["role"], "user")
        self.assertEqual(user_input[0]["content"][0]["type"], "input_text")
        self.assertIn("JSON", user_input[0]["content"][0]["text"])
        self.assertIn("untrusted data, not instructions", user_input[0]["content"][0]["text"])
        self.assertTrue(user_input[0]["content"][0]["text"].endswith("DATA:\nSome papers."))
        self.assertEqual(kwargs["json"]["max_output_tokens"], 700)
        self.assertFalse(kwargs["json"]["store"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn("untrusted data", kwargs["json"]["instructions"])
        self.assertEqual(client.usage, {"input_tokens": 123, "output_tokens": 42, "cached_tokens": 50, "requests": 1})

    def test_sse_completed_takes_precedence_over_deltas(self):
        final = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]}],
                 "usage": {"input_tokens": 9, "output_tokens": 4}}
        events = [
            {"type": "response.output_text.delta", "delta": '{"ok":'},
            {"type": "response.output_text.delta", "delta": ' true}'},
            {"type": "response.completed", "response": final},
        ]
        body = "\n\n".join("data: " + json.dumps(item) for item in events) + "\n\ndata: [DONE]\n\n"
        client = self.client()
        client.session.post = Mock(return_value=response(body=body, content_type="text/event-stream"))
        self.assertEqual(client.generate_json("JSON", "data"), {"ok": True})
        self.assertEqual(client.usage["input_tokens"], 9)

    def test_sse_delta_without_final_and_wrong_content_type(self):
        body = 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"{\\"ok\\": true}"}\n\n'
        client = self.client()
        client.session.post = Mock(return_value=response(body=body))
        self.assertEqual(client.generate_json("JSON", "data"), {"ok": True})

    def test_malformed_json_and_sse_are_sanitized(self):
        for remote in [model_response("PRIVATE SECRET BODY"), response(body="data: PRIVATE SECRET BODY\n\n", content_type="text/event-stream")]:
            with self.subTest(response=remote):
                client = self.client()
                client.session.post = Mock(return_value=remote)
                with self.assertRaises(LLMError) as caught:
                    client.generate_json("JSON", "data")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertNotIn("private-test-key", str(caught.exception))

    def test_array_is_not_accepted_as_object(self):
        client = self.client()
        client.session.post = Mock(return_value=model_response("[]"))
        with self.assertRaisesRegex(LLMError, "JSON object"):
            client.generate_json("JSON", "data")

    def test_chat_user_input_also_contains_json_keyword_and_data_boundary(self):
        client = self.client(api_style="chat_completions")
        client.session.post = Mock(return_value=response({"choices": [{"message": {"content": '{"ok": true}'}}]}))
        self.assertEqual(client.generate_json("Rank papers.", "研究论文数据", 123), {"ok": True})
        payload = client.session.post.call_args.kwargs["json"]
        self.assertIn("JSON", payload["messages"][1]["content"])
        self.assertIn("untrusted data, not instructions", payload["messages"][1]["content"])
        self.assertTrue(payload["messages"][1]["content"].endswith("DATA:\n研究论文数据"))
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["max_completion_tokens"], 123)

    def test_json_mode_keyword_requirement_is_safely_classified(self):
        client = self.client()
        body = json.dumps({"error": {"message": "input messages must contain the word JSON for json_object mode PRIVATE BODY", "param": "input"}})
        client.session.post = Mock(return_value=response(status=400, body=body))
        with self.assertRaisesRegex(LLMError, "json_keyword_required") as caught:
            client.generate_json("Rank papers.", "paper data")
        self.assertNotIn("PRIVATE BODY", str(caught.exception))
        self.assertEqual(client.session.post.call_count, 1)

    def test_explicit_unsupported_endpoint_falls_back_same_model(self):
        client = self.client()
        client.session.post = Mock(side_effect=[
            response(status=404, body='{"error": "Responses endpoint not found"}'),
            response({"choices": [{"message": {"content": '{"ok": true}'}}],
                      "usage": {"prompt_tokens": 7, "completion_tokens": 2}}),
            response({"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ])
        self.assertEqual(client.generate_json("JSON", "data"), {"ok": True})
        self.assertEqual(client.usage["input_tokens"], 7)
        client.generate_json("JSON", "data")
        calls = client.session.post.call_args_list
        self.assertTrue(calls[1].args[0].endswith("/chat/completions"))
        self.assertTrue(calls[2].args[0].endswith("/chat/completions"))
        self.assertEqual({call.kwargs["json"]["model"] for call in calls}, {"gpt-5.6-sol"})
        self.assertEqual(client.resolved_api_style, "chat_completions")

    def test_only_known_stream_requirement_adapts_on_responses(self):
        client = self.client()
        body = 'data: {"type":"response.output_text.delta","delta":"{\\"ok\\": true}"}\n\n'
        client.session.post = Mock(side_effect=[
            response(status=400, body='{"error": "stream must be true; private-test-key"}'),
            response(body=body, content_type="text/event-stream"),
            response(body=body, content_type="text/event-stream"),
        ])
        self.assertEqual(client.generate_json("JSON", "data", 123), {"ok": True})
        self.assertEqual(client.generate_json("JSON", "data", 123), {"ok": True})
        calls = client.session.post.call_args_list
        self.assertFalse(calls[0].kwargs["json"]["stream"])
        self.assertTrue(calls[1].kwargs["json"]["stream"])
        self.assertTrue(calls[2].kwargs["json"]["stream"])
        self.assertEqual(client.resolved_api_style, "responses")
        self.assertTrue(all(call.args[0].endswith("/responses") for call in calls))
        self.assertTrue(all(call.kwargs["json"]["max_output_tokens"] == 123 for call in calls))

    def test_optional_json_format_adapts_without_removing_token_cap(self):
        client = self.client()
        client.session.post = Mock(side_effect=[
            response(status=400, body='{"error": "Unsupported parameter: text.format"}'),
            model_response('{"ok": true}'),
        ])
        self.assertEqual(client.generate_json("JSON", "data", 123), {"ok": True})
        calls = client.session.post.call_args_list
        self.assertIn("text", calls[0].kwargs["json"])
        self.assertNotIn("text", calls[1].kwargs["json"])
        self.assertEqual(calls[1].kwargs["json"]["max_output_tokens"], 123)
        self.assertTrue(all(call.args[0].endswith("/responses") for call in calls))

    def test_two_capability_adaptations_are_bounded(self):
        client = self.client()
        client.session.post = Mock(side_effect=[
            response(status=400, body="stream must be true"),
            response(status=400, body="Unsupported parameter: text.format"),
            response(status=400, body="stream must be true"),
        ])
        with self.assertRaisesRegex(LLMError, "stream_required"):
            client.generate_json("JSON", "data")
        self.assertEqual(client.session.post.call_count, 3)

    def test_known_stream_synonyms_and_json_format_value_error(self):
        cases = [
            "stream must be enabled", "only streaming requests are supported",
            "must set stream true", "stream=true required", "Must set 'stream' to true",
            "Invalid value json_object; supported values: text, json_schema",
        ]
        for message in cases:
            with self.subTest(message=message):
                client = self.client()
                client.session.post = Mock(side_effect=[response(status=400, body=message), model_response()])
                self.assertEqual(client.generate_json("JSON", "data"), {"selected": []})
                self.assertEqual(client.session.post.call_count, 2)
                self.assertTrue(all(call.args[0].endswith("/responses") for call in client.session.post.call_args_list))

    def test_diagnostics_only_emit_allowlisted_fields_and_parameters(self):
        for param, expected in [("stream", "stream"), ("private-test-key", "none"), ("stream-private-test-key", "none")]:
            with self.subTest(param=param):
                client = self.client()
                remote_body = json.dumps({"error": {"message": "Invalid input with model request PRIVATE BODY", "param": param}})
                client.session.post = Mock(return_value=response(status=400, body=remote_body))
                with self.assertRaises(LLMError) as caught:
                    client.generate_json("JSON", "data")
                diagnostic = str(caught.exception)
                self.assertIn("mentioned_fields=model,input", diagnostic)
                self.assertIn(f"error_param={expected}", diagnostic)
                self.assertIn("error_shape=json_error_object", diagnostic)
                self.assertNotIn("PRIVATE BODY", diagnostic)
                self.assertNotIn("private-test-key", diagnostic)

    def test_input_validation_hints_never_echo_size_values_or_body(self):
        cases = [
            ("input too long: 1234567 tokens PRIVATE BODY", "input_too_large"),
            ("Maximum context length 1234567 tokens PRIVATE BODY", "input_too_large"),
            ("Input list exceeds maximum size 1234567 PRIVATE BODY", "input_too_large"),
            ("input cannot be empty PRIVATE BODY", "input_missing_or_empty"),
            ("Missing required input PRIVATE BODY", "input_missing_or_empty"),
            ("input invalid UTF-8 encoding PRIVATE BODY", "input_invalid_encoding"),
            ("input content must be a string PRIVATE BODY", "input_invalid_content"),
            ("invalid input message PRIVATE BODY", "input_invalid_content"),
        ]
        for message, hint in cases:
            with self.subTest(hint=hint):
                client = self.client()
                remote_body = json.dumps({"error": {"message": message, "param": "input"}})
                client.session.post = Mock(return_value=response(status=400, body=remote_body))
                with self.assertRaises(LLMError) as caught:
                    client.generate_json("JSON", "data")
                diagnostic = str(caught.exception)
                self.assertIn(hint, diagnostic)
                self.assertIn("error_param=input", diagnostic)
                self.assertNotIn("1234567", diagnostic)
                self.assertNotIn("PRIVATE BODY", diagnostic)
                self.assertEqual(client.session.post.call_count, 1)

    def test_error_hints_are_fixed_and_max_output_cap_is_not_removed(self):
        cases = [
            ("input must be an array private-test-key", "input_needs_array"),
            ("Unsupported parameter: max_output_tokens private-test-key", "unsupported_max_output_tokens"),
            ("model gpt-5.6-sol not found private-test-key", "model_unavailable"),
            ("invalid_api_key private-test-key", "auth_or_quota"),
            ("custom upstream error private-test-key", "invalid_request"),
            ("unsupported parameter reasoning private-test-key", "invalid_request"),
        ]
        for body, hint in cases:
            with self.subTest(hint=hint):
                client = self.client()
                client.session.post = Mock(return_value=response(status=400, body=body))
                with self.assertRaises(LLMError) as caught:
                    client.generate_json("JSON", "data", 123)
                self.assertIn(hint, str(caught.exception))
                self.assertNotIn("private-test-key", str(caught.exception))
                self.assertNotIn(body, str(caught.exception))
                self.assertEqual(client.session.post.call_count, 1)
                self.assertEqual(client.session.post.call_args.kwargs["json"]["max_output_tokens"], 123)

    def test_auth_rate_invalid_model_and_bad_request_never_fall_back(self):
        for status, body in [(401, "private-test-key"), (429, "rate limited"), (404, "model_not_found"), (400, "invalid model"), (422, "invalid input")]:
            with self.subTest(status=status):
                client = self.client()
                client.session.post = Mock(return_value=response(status=status, body=body))
                with patch("daily_digest.llm.time.sleep"), self.assertRaises(LLMError) as caught:
                    client.generate_json("JSON", "data")
                self.assertNotIn("private-test-key", str(caught.exception))
                self.assertEqual(client.session.post.call_count, 3 if status == 429 else 1)
                self.assertTrue(all(call.args[0].endswith("/responses") for call in client.session.post.call_args_list))

    def test_explicit_responses_does_not_fall_back(self):
        client = self.client(api_style="responses")
        client.session.post = Mock(return_value=response(status=405, body="method not allowed"))
        with self.assertRaises(LLMError):
            client.generate_json("JSON", "data")
        self.assertEqual(client.session.post.call_count, 1)

    def test_timeout_retries_are_bounded_and_sanitized(self):
        client = self.client()
        client.session.post = Mock(side_effect=requests.Timeout("private-test-key URL"))
        with patch("daily_digest.llm.time.sleep"), self.assertRaises(LLMError) as caught:
            client.generate_json("JSON", "data")
        self.assertEqual(client.session.post.call_count, 3)
        self.assertNotIn("private-test-key", str(caught.exception))

    def test_rejects_url_credentials_and_query(self):
        for url in ["https://secret@relay.example/v1", "https://relay.example/v1?token=secret"]:
            with self.assertRaises(ValueError):
                LLMClient(url, "gpt-5.6-sol", "key")


class DeepSeekTests(unittest.TestCase):
    def client(self, **kwargs):
        return LLMClient("https://api.deepseek.com", "deepseek-flash", "private-test-key",
                         reasoning_effort="low", **kwargs)

    def test_official_chat_contract_and_usage_without_reasoning_disclosure(self):
        client = self.client()
        client.session.post = Mock(return_value=deepseek_response(
            content='{"selected": ["2609.12345"]}',
            usage={"prompt_tokens": 794, "completion_tokens": 255, "prompt_cache_hit_tokens": 400,
                   "prompt_cache_miss_tokens": 394, "completion_tokens_details": {"reasoning_tokens": 100}},
        ))
        self.assertEqual(client.generate_json("Rank papers.", "研究论文数据", 6144),
                         {"selected": ["2609.12345"]})
        args, kwargs = client.session.post.call_args
        self.assertEqual(args[0], "https://api.deepseek.com/chat/completions")
        payload = kwargs["json"]
        self.assertEqual(payload["max_tokens"], 6144)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn("JSON", payload["messages"][1]["content"])
        self.assertFalse(payload["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(client.resolved_api_style, "chat_completions")
        self.assertEqual(client.usage, {"input_tokens": 794, "output_tokens": 255,
                                       "cached_tokens": 400, "requests": 1})

    def test_nested_cache_usage_is_not_double_counted(self):
        client = self.client(api_style="chat_completions")
        client.session.post = Mock(return_value=deepseek_response(usage={
            "prompt_tokens": 20, "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 8}, "prompt_cache_hit_tokens": 8,
        }))
        client.generate_json("JSON", "data")
        self.assertEqual(client.usage["cached_tokens"], 8)

    def test_interrupted_completion_rejected_even_with_parseable_json(self):
        for reason in ["length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted",
                       "PRIVATE SECRET FINISH REASON"]:
            with self.subTest(reason=reason):
                client = self.client()
                client.session.post = Mock(return_value=deepseek_response(finish_reason=reason))
                with self.assertRaisesRegex(LLMError, "interrupted or incomplete") as caught:
                    client.generate_json("JSON", "data")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(client.session.post.call_count, 1)

    def test_missing_finish_marker_and_reasoning_only_rejected(self):
        cases = [
            {"choices": [{"message": {"content": '{"ok": true}'}}]},
            {"choices": [{"finish_reason": None, "message": {"content": '{"ok": true}'}}]},
            {"choices": []},
            {"choices": [{"finish_reason": "stop", "message": {
                "reasoning_content": '{"ok": true}', "content": None}}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                client = self.client()
                client.session.post = Mock(return_value=response(payload))
                with self.assertRaises(LLMError):
                    client.generate_json("JSON", "data")

    def test_chat_sse_requires_stop_and_ignores_reasoning_deltas(self):
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "PRIVATE REASONING"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": '{"ok": true}'}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 12, "prompt_cache_hit_tokens": 3}},
        ]
        body = "\n\n".join("data: " + json.dumps(item) for item in chunks) + "\n\ndata: [DONE]\n\n"
        client = self.client()
        client.session.post = Mock(return_value=response(body=body, content_type="text/event-stream"))
        self.assertEqual(client.generate_json("JSON", "data"), {"ok": True})
        self.assertEqual(client.usage["cached_tokens"], 3)
        for last_reason in [None, "length"]:
            with self.subTest(last_reason=last_reason):
                chunks[-1]["choices"][0]["finish_reason"] = last_reason
                invalid_body = "\n\n".join("data: " + json.dumps(item) for item in chunks)
                client.session.post = Mock(return_value=response(body=invalid_body,
                                                                  content_type="text/event-stream"))
                with self.assertRaises(LLMError):
                    client.generate_json("JSON", "data")

    def test_official_url_only_https_and_supported_path(self):
        for url in ["http://api.deepseek.com", "https://api.deepseek.com:8443",
                    "https://api.deepseek.com/unexpected/path", "https://api.deepseek.com/beta"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                LLMClient(url, "deepseek-flash", "key")
        for url in ["https://api.deepseek.com/", "https://api.deepseek.com/v1/", "https://api.deepseek.com:443"]:
            with self.subTest(url=url):
                client = LLMClient(url, "deepseek-flash", "key", api_style="chat_completions")
                client.session.post = Mock(return_value=deepseek_response())
                self.assertEqual(client.generate_json("JSON", "data"), {"selected": []})

    def test_compatible_reasoning_efforts_and_disabled_thinking(self):
        for effort, expected in [("minimal", "low"), ("medium", "high"), ("xhigh", "high"),
                                 ("none", "none"), ("low", "low"), ("high", "high"), ("max", "max")]:
            with self.subTest(effort=effort):
                client = LLMClient("https://api.deepseek.com", "deepseek-flash", "key",
                                   reasoning_effort=effort)
                client.session.post = Mock(return_value=deepseek_response())
                client.generate_json("JSON", "data", 8192)
                payload = client.session.post.call_args.kwargs["json"]
                self.assertEqual(payload["reasoning_effort"], expected)
                self.assertEqual(payload["thinking"]["type"], "disabled" if expected == "none" else "enabled")
        with self.assertRaises(ValueError):
            LLMClient("https://api.deepseek.com", "deepseek-flash", "key", reasoning_effort="unsupported")

    def test_output_budget_is_bounded_before_paid_call(self):
        client = self.client()
        client.session.post = Mock()
        for limit in [True, 0, -1, 1.5, 393217]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                client.generate_json("JSON", "data", limit)
        client.session.post.assert_not_called()

    def test_scheduled_off_peak_guard_allows_weekday_utc_midnight_and_weekend(self):
        allowed_times = [
            datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc),
        ]
        for now in allowed_times:
            with self.subTest(now=now):
                client = self.client(off_peak_only=True)
                client.session.post = Mock(return_value=deepseek_response())
                with patch("daily_digest.llm.datetime") as clock:
                    clock.now.return_value = now
                    self.assertEqual(client.generate_json("JSON", "data"), {"selected": []})
                    clock.now.assert_called_with(timezone.utc)
                client.session.post.assert_called_once()

    def test_scheduled_off_peak_guard_blocks_peak_without_request(self):
        for hour in [1, 3, 6, 9]:
            with self.subTest(hour=hour):
                client = self.client(off_peak_only=True)
                client.session.post = Mock()
                with patch("daily_digest.llm.datetime") as clock:
                    clock.now.return_value = datetime(2026, 9, 28, hour, 0, tzinfo=timezone.utc)
                    with self.assertRaisesRegex(LLMError, "no request sent"):
                        client.generate_json("JSON", "data")
                client.session.post.assert_not_called()

    def test_retry_checks_off_peak_boundary_before_another_request(self):
        client = self.client(off_peak_only=True)
        client.session.post = Mock(return_value=response(status=429, body="rate limited"))
        with patch("daily_digest.llm.datetime") as clock, patch("daily_digest.llm.time.sleep"):
            clock.now.side_effect = [
                datetime(2026, 9, 28, 0, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 9, 28, 1, 0, 0, tzinfo=timezone.utc),
            ]
            with self.assertRaisesRegex(LLMError, "no request sent"):
                client.generate_json("JSON", "data")
        self.assertEqual(client.session.post.call_count, 1)

    def test_manual_calls_keep_off_peak_guard_disabled(self):
        client = self.client()
        client.session.post = Mock(return_value=deepseek_response())
        with patch("daily_digest.llm.datetime") as clock:
            self.assertEqual(client.generate_json("JSON", "data"), {"selected": []})
            clock.now.assert_not_called()
        with self.assertRaises(ValueError):
            LLMClient("https://relay.example/v1", "other-model", "key", off_peak_only=True)


if __name__ == "__main__":
    unittest.main()
