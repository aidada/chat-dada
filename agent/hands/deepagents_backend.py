from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (
    BackendFactory,
    BackendProtocol,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GrepMatch,
    SandboxBackendProtocol,
    WriteResult,
)
from deepagents.backends.state import StateBackend
from deepagents.backends.utils import format_content_with_line_numbers

from agent.hands.desktop_manager import DesktopHandsManager
from agent.hands.gateway import ToolGateway
from agent.hands.protocol import ToolCall, ToolContext

_DESKTOP_TOOL_ERROR_PREFIX = "desktop backend error"
_DESKTOP_PATH_KEYS = ("home", "downloads", "documents", "desktop")
_FILESYSTEM_DESKTOP_TOOL_NAMES = {
    "list_dir",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "grep",
    "shell",
}
_GREP_LINE_RE = re.compile(r"^(?P<line>\d+):(.*)$")
_SHELL_EXIT_RE = re.compile(r"^\[exit (?P<code>\d+)\]\n?")


def build_deepagents_backend_factory(
    *,
    user_id: str,
    task_id: str,
    tool_gateway: ToolGateway | None,
    desktop_manager: DesktopHandsManager | None,
    workspace_root: Path | None = None,
    allow_shell: bool = True,
) -> BackendFactory:
    root_dir = (workspace_root or Path.cwd()).resolve()

    def factory(runtime: Any) -> BackendProtocol:
        workspace_backend = FilesystemBackend(root_dir=root_dir, virtual_mode=True)
        scratch_backend = StateBackend(runtime)
        routes: dict[str, BackendProtocol] = {
            "/workspace/": workspace_backend,
            "/scratch/": scratch_backend,
        }

        default_backend: BackendProtocol = workspace_backend
        if (
            tool_gateway is not None
            and desktop_manager is not None
            and user_id
            and desktop_manager.is_connected(user_id)
        ):
            default_backend = DesktopToolBackend(
                user_id=user_id,
                task_id=task_id,
                tool_gateway=tool_gateway,
                path_aliases=_default_desktop_path_aliases(desktop_manager.get_path_aliases(user_id)),
                allow_shell=allow_shell,
            )

        return CompositeBackend(default=default_backend, routes=routes)

    return factory


def resolve_deepagents_runtime(
    *,
    domain: str,
    task_id: str,
    fallback_tools: list[Any],
    configurable: dict[str, Any],
    workspace_root: Path | None = None,
) -> tuple[list[Any], BackendFactory]:
    user_id = str(configurable.get("request_user_id", "") or "")
    tool_gateway = configurable.get("tool_gateway")
    desktop_manager = configurable.get("desktop_manager")

    tools = list(fallback_tools)
    if tool_gateway is not None:
        ctx = ToolContext(user_id=user_id, task_id=task_id, trace_id=task_id)
        tools = tool_gateway.bind_deepagents_tools(domain, task_id, ctx)

    backend = build_deepagents_backend_factory(
        user_id=user_id,
        task_id=task_id,
        tool_gateway=tool_gateway,
        desktop_manager=desktop_manager,
        workspace_root=workspace_root,
        allow_shell=domain not in {"office", "ppt"},
    )
    return tools, backend


class DesktopToolBackend(SandboxBackendProtocol):
    def __init__(
        self,
        *,
        user_id: str,
        task_id: str,
        tool_gateway: ToolGateway,
        path_aliases: dict[str, str],
        allow_shell: bool = True,
    ) -> None:
        self._user_id = user_id
        self._task_id = task_id
        self._tool_gateway = tool_gateway
        self._allow_shell = allow_shell
        self._path_aliases = {
            key: value.rstrip("/") or value
            for key, value in path_aliases.items()
            if key in _DESKTOP_PATH_KEYS and value
        }
        self._sandbox_id = f"desktop-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        return self._sandbox_id

    def ls_info(self, path: str) -> list[FileInfo]:
        if path == "/":
            return _run_sync(self._root_infos_async())
        return _run_sync(self.als_info(path))

    async def als_info(self, path: str) -> list[FileInfo]:
        if path == "/":
            return await self._root_infos_async()

        resolved_path, alias_prefix = self._resolve_path(path)
        result = await self._execute_tool("list_dir", {"path": resolved_path})
        if not result.success:
            return []
        return _parse_list_dir_output(result.output, path, alias_prefix)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return _run_sync(self.aread(file_path, offset=offset, limit=limit))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        resolved_path, _alias_prefix = self._resolve_path(file_path)
        result = await self._execute_tool(
            "file_read",
            {"path": resolved_path, "offset": int(offset), "limit": int(limit)},
        )
        if not result.success:
            return f"Error: {result.error or result.output or 'read failed'}"
        if not str(result.output or "").strip():
            return "System reminder: File exists but has empty contents"
        return format_content_with_line_numbers(result.output, start_line=offset + 1)

    def write(self, file_path: str, content: str) -> WriteResult:
        return _run_sync(self.awrite(file_path, content))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        resolved_path, _alias_prefix = self._resolve_path(file_path)
        result = await self._execute_tool("file_write", {"path": resolved_path, "content": content})
        if not result.success:
            return WriteResult(error=result.error or result.output or f"{_DESKTOP_TOOL_ERROR_PREFIX}: write failed")
        return WriteResult(path=file_path, files_update=None)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return _run_sync(
            self.aedit(
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
        )

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if replace_all:
            return EditResult(error="Desktop backend edit does not support replace_all=True")

        resolved_path, _alias_prefix = self._resolve_path(file_path)
        result = await self._execute_tool(
            "file_edit",
            {
                "path": resolved_path,
                "old_text": old_string,
                "new_text": new_string,
            },
        )
        if not result.success:
            return EditResult(error=result.error or result.output or f"{_DESKTOP_TOOL_ERROR_PREFIX}: edit failed")
        return EditResult(path=file_path, files_update=None, occurrences=1)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        return _run_sync(self.agrep_raw(pattern, path=path, glob=glob))

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        search_root = path or "/"
        infos = await self.aglob_info(glob or "**/*", path=search_root)
        matches: list[GrepMatch] = []
        for info in infos:
            candidate_path = str(info.get("path", "") or "")
            if not candidate_path or candidate_path.endswith("/"):
                continue
            resolved_path, _alias_prefix = self._resolve_path(candidate_path)
            result = await self._execute_tool(
                "grep",
                {"path": resolved_path, "pattern": pattern, "case_sensitive": True},
            )
            if not result.success:
                continue
            for line in str(result.output or "").splitlines():
                matched = _GREP_LINE_RE.match(line)
                if not matched:
                    continue
                matches.append(
                    {
                        "path": candidate_path,
                        "line": int(matched.group("line")),
                        "text": line.split(":", 1)[1],
                    }
                )
        return matches

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return _run_sync(self.aglob_info(pattern, path=path))

    async def aglob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        resolved_path, alias_prefix = self._resolve_path(path)
        result = await self._execute_tool(
            "file_search",
            {"path": resolved_path, "pattern": pattern},
        )
        if not result.success:
            return []
        infos: list[FileInfo] = []
        for raw_path in str(result.output or "").splitlines():
            normalized = raw_path.strip()
            if not normalized:
                continue
            display_path = _display_path_from_actual(normalized, alias_prefix)
            infos.append(
                {
                    "path": display_path,
                    "is_dir": normalized.endswith("/"),
                    "size": 0,
                    "modified_at": "",
                }
            )
        return infos

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="invalid_path")
            for path, _content in files
        ]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(path=path, content=None, error="invalid_path")
            for path in paths
        ]

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return _run_sync(self.aexecute(command, timeout=timeout))

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not self._allow_shell:
            return ExecuteResponse(
                output="Shell execution is disabled for this domain. Use structured office/file tools instead.",
                exit_code=1,
                truncated=False,
            )
        params: dict[str, Any] = {"command": command}
        if timeout is not None:
            params["timeout_ms"] = int(timeout) * 1000
        result = await self._execute_tool("shell", params)
        output = result.output or result.error or ""
        exit_code = 0 if result.success else 1
        if not result.success:
            matched = _SHELL_EXIT_RE.match(output)
            if matched:
                exit_code = int(matched.group("code"))
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=False)

    async def _execute_tool(self, tool_name: str, params: dict[str, Any]) -> Any:
        self._tool_gateway.set_route(tool_name, "desktop")
        raw_timeout_ms = params.get("timeout_ms")
        try:
            timeout_ms = int(raw_timeout_ms) if raw_timeout_ms is not None else 30_000
        except (TypeError, ValueError):
            timeout_ms = 30_000
        return await self._tool_gateway.execute(
            ToolCall(tool_name=tool_name, params=params, task_id=self._task_id, timeout_ms=max(timeout_ms, 1)),
            ToolContext(user_id=self._user_id, task_id=self._task_id, trace_id=self._task_id),
        )

    def _resolve_path(self, path: str) -> tuple[str, str | None]:
        normalized = _normalize_posix_path(path)
        for alias, actual in self._path_aliases.items():
            alias_prefix = f"/{alias}"
            if normalized == alias_prefix:
                return actual, alias_prefix
            if normalized.startswith(f"{alias_prefix}/"):
                suffix = normalized[len(alias_prefix):].lstrip("/")
                return str(Path(actual) / suffix), alias_prefix
        return normalized, None

    async def _root_infos_async(self) -> list[FileInfo]:
        infos: list[FileInfo] = [
            {"path": "/workspace/", "is_dir": True, "size": 0, "modified_at": ""},
            {"path": "/scratch/", "is_dir": True, "size": 0, "modified_at": ""},
        ]
        infos.extend(
            {"path": f"/{alias}/", "is_dir": True, "size": 0, "modified_at": ""}
            for alias in sorted(self._path_aliases)
        )
        root_listing = await self._execute_tool("list_dir", {"path": "/"})
        if root_listing.success:
            infos.extend(_parse_list_dir_output(root_listing.output, "/", None))
        deduped: dict[str, FileInfo] = {}
        for info in infos:
            deduped[str(info.get("path", ""))] = info
        return [deduped[key] for key in sorted(deduped)]


def _default_desktop_path_aliases(path_aliases: dict[str, str]) -> dict[str, str]:
    aliases = {key: value for key, value in path_aliases.items() if value}
    home = aliases.get("home") or str(Path.home())
    aliases.setdefault("home", home)
    aliases.setdefault("downloads", str(Path(home) / "Downloads"))
    aliases.setdefault("documents", str(Path(home) / "Documents"))
    aliases.setdefault("desktop", str(Path(home) / "Desktop"))
    return aliases


def _normalize_posix_path(path: str | None) -> str:
    normalized = os.path.normpath(str(path or "/"))
    normalized = normalized.replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _display_path_from_actual(actual_path: str, alias_prefix: str | None) -> str:
    if alias_prefix is None:
        return actual_path
    return actual_path


def _parse_list_dir_output(output: str, requested_path: str, alias_prefix: str | None) -> list[FileInfo]:
    normalized_requested = _normalize_posix_path(requested_path)
    infos: list[FileInfo] = []
    for line in str(output or "").splitlines():
        entry = line.strip()
        if not entry or ":" not in entry:
            continue
        entry_type, raw_name = entry.split(":", 1)
        name = raw_name.strip()
        if not name:
            continue
        if name.startswith("/"):
            display_path = _display_path_from_actual(name, alias_prefix)
        else:
            base = normalized_requested.rstrip("/") or "/"
            display_path = f"{base}/{name}" if base != "/" else f"/{name}"
        is_dir = entry_type.strip() == "dir"
        if is_dir and not display_path.endswith("/"):
            display_path += "/"
        infos.append(
            {
                "path": display_path,
                "is_dir": is_dir,
                "size": 0,
                "modified_at": "",
            }
        )
    return infos


def _run_sync(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
