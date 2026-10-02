from sprintbaton.adaptors.base import TaskAdapter
from sprintbaton.adaptors.todoist import TodoistTaskAdapter

__all__ = ["TaskAdapter", "TodoistTaskAdapter", "create_adapter"]


def create_adapter(provider: str, api_token: str) -> TaskAdapter:
    if provider == "todoist":
        return TodoistTaskAdapter(api_token=api_token)
    raise ValueError(f"unsupported todolist provider: {provider}")
