from __future__ import annotations

from ..queue import TaskQueue


class AccountRegistry:
    def __init__(self) -> None:
        self._queues: dict[str, TaskQueue] = {}

    def register_account(self, account_id: str, queue: TaskQueue | None = None) -> TaskQueue:
        account_queue = queue or TaskQueue()
        self._queues[account_id] = account_queue
        return account_queue

    def get_queue(self, account_id: str) -> TaskQueue | None:
        return self._queues.get(account_id)

    def get_or_create_queue(self, account_id: str) -> TaskQueue:
        queue = self.get_queue(account_id)
        if queue is not None:
            return queue
        return self.register_account(account_id)

    def list_accounts(self) -> list[str]:
        return list(self._queues.keys())

    def add_task(self, account_id: str, task: dict) -> dict:
        task["account_id"] = account_id
        return self.get_or_create_queue(account_id).add_task(task)

