"""PPT 领域工具集合。"""
from __future__ import annotations

from langchain_core.tools import tool


@tool
async def officecli_run(command: str) -> str:
    """执行 officecli CLI 命令（单条）"""
    import subprocess
    result = subprocess.run(
        ["officecli"] + command.split(),
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


@tool
async def officecli_batch(commands: list[str]) -> str:
    """批量执行 officecli CLI 命令"""
    import subprocess
    results = []
    for cmd in commands:
        result = subprocess.run(
            ["officecli"] + cmd.split(),
            capture_output=True,
            text=True,
        )
        results.append(result.stdout + result.stderr)
    return "\n".join(results)


def get_ppt_tools():
    """Return tools available to PPT domain."""
    return [officecli_run, officecli_batch]
