"""Version of the per-task workspace layout (docs/task-workspace-spec.md §4.1).

Bump when the .sprintbaton/tasks/<task_id>/ directory's shape changes, so
future analytics can correlate task outcomes with workspace-format version the
same way TaskActionEvent already does for METADATA_FORMAT_VERSION.
"""

TASK_WORKSPACE_FORMAT_VERSION = "v1"
