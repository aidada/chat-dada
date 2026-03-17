"""
Code Executor Tool — runs Python code in a subprocess sandbox.
"""
import subprocess
import tempfile
import os

from logger import log_async


@log_async("tool", "code_executor")
async def run(input_data) -> dict:
    """
    Execute Python code in a sandboxed subprocess.

    Args:
        input_data: str (code) or dict with "code" key
    """
    if isinstance(input_data, str):
        code = input_data
    elif isinstance(input_data, dict):
        code = input_data.get("code", str(input_data))
    else:
        code = str(input_data)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["python", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=tempfile.gettempdir(),
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            return {"status": "error", "result": output or f"Exit code: {result.returncode}"}
        return {"status": "ok", "result": output}
    except subprocess.TimeoutExpired:
        return {"status": "error", "result": "Code execution timed out (30s limit)"}
    finally:
        os.unlink(tmp_path)
