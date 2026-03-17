from __future__ import annotations

from types import SimpleNamespace
import unittest

from logger import _LoggingLLM, monitor, new_trace_id


class _FakeLLM:
    async def ainvoke(self, *args, **kwargs):
        return SimpleNamespace(
            usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            content="ok",
        )


class LoggingLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_ainvoke_reads_total_tokens_from_dict_usage_metadata(self) -> None:
        trace_id = new_trace_id()
        llm = _LoggingLLM(_FakeLLM(), "deep_research", "gemini-3.1-pro-preview-customtools")

        await llm.ainvoke(["hello"])

        events = monitor._requests.get(trace_id, [])
        llm_end = [event for event in events if event.layer == "llm" and event.event == "end"]
        self.assertTrue(llm_end)
        self.assertEqual(llm_end[-1].metadata["tokens"], 30)

        monitor._requests.pop(trace_id, None)
