# Runtime Information

Information about the runtime environment and workspace to help the agent and tools locate and use memory and history files.

## Runtime
{{ runtime }}

## Workspace
Your workspace is at: {{ workspace_path }}
- Long-term memory: {{ workspace_path }}/MEMORY.md
- History log: {{ workspace_path }}/HISTORY.md (grep-searchable)

## Memory
- Remember important facts: write to {{ workspace_path }}/MEMORY.md
- Recall past events: grep {{ workspace_path }}/HISTORY.md"""