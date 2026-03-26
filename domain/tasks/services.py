from __future__ import annotations

from agent_runtime.task_execution import TaskService


class TaskExecutionService:
    """任务执行领域服务包装。

    当前先作为 `agent_runtime.TaskService` 的语义化入口，
    后续再把业务规则继续从运行时层抽离。
    """

    def __init__(self, task_service: TaskService) -> None:
        self.task_service = task_service

    async def submit(self, **kwargs):
        return await self.task_service.submit_task(**kwargs)

    async def get(self, task_id: str):
        return await self.task_service.get_task(task_id)

    async def get_events_after(self, task_id: str, after_seq: int):
        return await self.task_service.get_events_after(task_id, after_seq)

    async def reply(self, task_id: str, answer: str):
        return await self.task_service.reply_to_task(task_id, answer)

    async def cancel(self, task_id: str):
        return await self.task_service.cancel_running_task(task_id)
