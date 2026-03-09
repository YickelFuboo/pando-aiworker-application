---
name: clawhub
description: Search and install agent skills from ClawHub, the public skill registry.
homepage: https://clawhub.ai
metadata: {"pando":{"emoji":"🦞"}}
---

# ClawHub

Public skill registry for AI agents. Search by natural language (vector search).

## When to use

Use this skill when the user asks any of:
- "find a skill for …"
- "search for skills"
- "install a skill"
- "what skills are available?"
- "update my skills"

## Search

```bash
npx --yes clawhub@latest search "web scraping" --limit 5
```

## Install

```bash
npx --yes clawhub@latest install <slug> --workdir <WORKSPACE_PATH>/skills
```

Replace `<slug>` with the skill name from search results. For `--workdir`, use the **workspace path from your runtime context** (the path shown in "Your workspace is at: ..." in your system prompt). This places the skill into `<workspace>/skills/`, where AI Agent loads workspace skills from. Always include `--workdir`.

## Update

```bash
npx --yes clawhub@latest update --all --workdir <WORKSPACE_PATH>/skills
```

Use the workspace path from your runtime context for `--workdir`.

## List installed

```bash
npx --yes clawhub@latest list --workdir <WORKSPACE_PATH>/skills
```

Use the workspace path from your runtime context for `--workdir`.

## Notes

- Requires Node.js (`npx` comes with it).
- No API key needed for search and install.
- Login (`npx --yes clawhub@latest login`) is only required for publishing.
- `--workdir` must be the workspace path (from your context: "Your workspace is at: ..."). Without it, skills install to the current directory instead of the workspace.
- After install, remind the user to start a new session to load the skill.
