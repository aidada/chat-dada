"""
Dependency Graph Scheduler — executes steps respecting depends_on / parallel_with.
Groups steps into waves: steps with no unresolved dependencies run concurrently.
"""
import asyncio
from typing import Any, Callable, Awaitable

from registry import resolve_fn


async def execute_plan(
    steps: list[dict],
    context: dict[str, Any],
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    Execute a plan's steps respecting dependency order.

    Args:
        steps: List of step dicts with id, type, name, input_key, depends_on
        context: Shared context dict (step results stored as context[step_id])
        on_step: Optional progress callback

    Returns:
        Updated context dict with all step results.
    """
    completed: set[int] = set()
    step_map = {s["id"]: s for s in steps}
    all_ids = set(step_map.keys())

    while completed != all_ids:
        # Find ready steps: all dependencies satisfied
        ready = []
        for sid, step in step_map.items():
            if sid in completed:
                continue
            deps = set(step.get("depends_on", []))
            if deps.issubset(completed):
                ready.append(step)

        if not ready:
            raise RuntimeError(
                f"Deadlock: no steps ready. Completed={completed}, "
                f"Remaining={all_ids - completed}"
            )

        # Execute ready steps concurrently
        if on_step:
            names = ", ".join(s["name"] for s in ready)
            await on_step(f"Executing: {names}")

        tasks = [_run_step(step, context, on_step) for step in ready]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for step, result in zip(ready, results):
            sid = step["id"]
            if isinstance(result, Exception):
                if on_step:
                    await on_step(f"⚠️ Step {step['name']} failed: {result}")
                context[f"step_{sid}_error"] = str(result)
            else:
                context[f"step_{sid}"] = result
            completed.add(sid)

    return context


async def _run_step(
    step: dict,
    context: dict[str, Any],
    on_step: Callable[[str], Awaitable[None]] | None,
) -> Any:
    """Run a single step by resolving its capability and calling it."""
    name = step["name"]
    cap_type = step["type"]
    input_data = context.get(step.get("input_key", ""), {})

    if on_step:
        emoji = {"agent": "🤖", "tool": "🔧", "renderer": "📄"}.get(cap_type, "▶️")
        await on_step(f"{emoji} {name}: starting...")

    fn = resolve_fn(name)
    result = await fn(input_data) if asyncio.iscoroutinefunction(fn) else fn(input_data)

    if on_step:
        preview = str(result)[:100]
        await on_step(f"✅ {name}: done ({preview}...)")

    return result
