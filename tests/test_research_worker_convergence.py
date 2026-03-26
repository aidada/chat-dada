from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage

from domain_agents.research.worker import run_worker


class _FakeSearchTool:
    name = "exa_deep_search"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, payload):
        self.calls += 1
        return (
            "## Search Result\n\n"
            "A traceable source discusses the same topic and links to https://example.com/paper ."
        )


class _ConvergingLLM:
    def __init__(self) -> None:
        self.search_calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        prompt = "\n".join(str(getattr(message, "content", "")) for message in messages)
        if "检索规划阶段" in prompt:
            self.search_calls += 1
            if self.search_calls <= 2:
                return AIMessage(
                    content="继续检索",
                    tool_calls=[
                        {
                            "name": "exa_deep_search",
                            "args": {
                                "query": "duplicate topic search",
                                "mode": "summary",
                                "category": "research paper",
                                "summary_query": "duplicate topic search",
                            },
                            "id": f"call_{self.search_calls}",
                        }
                    ],
                )
            return AIMessage(content="可进入起草")
        if "收束校验阶段" in prompt:
            return AIMessage(
                content='{"status":"completed","reason":"ok","missing_requirements":[],"blocker_reason":""}'
            )
        return AIMessage(
            content=(
                "## Problem Definition\n\n"
                "This draft now converges with enough detail to pass validation and cites "
                "https://example.com/paper for traceability."
            )
        )


class _RetryingDraftLLM:
    def __init__(self) -> None:
        self.draft_calls = 0
        self.search_calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        prompt = "\n".join(str(getattr(message, "content", "")) for message in messages)
        if "检索规划阶段" in prompt:
            self.search_calls += 1
            return AIMessage(
                content="先补一条证据",
                tool_calls=[
                    {
                        "name": "exa_deep_search",
                        "args": {
                            "query": "urban vehicular GNSS pseudorange multipath model",
                            "mode": "summary",
                            "category": "research paper",
                            "summary_query": "urban vehicular GNSS pseudorange multipath model",
                        },
                        "id": "call_retry_1",
                    }
                ],
            )
        if "收束校验阶段" in prompt:
            return AIMessage(
                content='{"status":"completed","reason":"ok","missing_requirements":[],"blocker_reason":""}'
            )
        self.draft_calls += 1
        if self.draft_calls == 1:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return AIMessage(
            content=(
                "## Problem Definition\n\n"
                "Retry succeeded with the same evidence pack and cites https://example.com/paper."
            )
        )


class ResearchWorkerConvergenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reuses_duplicate_query_results(self) -> None:
        tool = _FakeSearchTool()
        module = {
            "module_id": "problem_definition",
            "title": "问题定义",
            "owner_role": "argument_worker",
            "objective": "先收束问题定义",
        }

        with patch("domain_agents.research.worker.get_llm", return_value=_ConvergingLLM()):
            result = await run_worker(
                module,
                brief={"clarified_goal": "test"},
                tools=[tool],
                max_rounds=2,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(result["search_stats"]["duplicate_hit_total"], 1)

    async def test_draft_module_retries_without_rerunning_tools(self) -> None:
        tool = _FakeSearchTool()
        llm = _RetryingDraftLLM()
        module = {
            "module_id": "problem_definition",
            "title": "问题定义",
            "owner_role": "argument_worker",
            "objective": "先收束问题定义",
        }

        with patch("domain_agents.research.worker.get_llm", return_value=llm):
            result = await run_worker(
                module,
                brief={"clarified_goal": "test"},
                tools=[tool],
                max_rounds=1,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(llm.search_calls, 1)
        self.assertEqual(llm.draft_calls, 2)
