# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A starter kit for the Knight Center MOOC "Advanced Prompt Engineering for Journalists." Students fork this repo and use it as their workspace across four modules. There is no build system, test suite, or package manager — all content is Markdown, shell scripts, and JSON config files.

## Running the scripts

```bash
# Summarize a single article
bash scripts/summarize-article.sh sample-docs/sample-article.html

# Batch-process a directory of articles (outputs to ./summaries/)
bash scripts/batch-process.sh sample-docs/
```

Both scripts call `claude -p` (non-interactive mode) and require the Claude Code CLI to be installed (`npm install -g @anthropic-ai/claude-code`).

## Skills

Skills live in `skills/` as Markdown files. Each skill is invoked with a slash command matching the filename (e.g., `skills/newsroom-style.md` → `/newsroom-style`). To create a new skill, copy `skills/my-first-skill.md` and edit it.

## MCP configuration

`mcp-configs/filesystem-example.json` is a template, not an active config. To activate it: copy to `~/.claude/mcp.json` (macOS/Linux) or `%APPDATA%\claude\mcp.json` (Windows), replace `YOUR_USERNAME` and the directory path, then restart Claude Code.

The `beat-archive/` folder is the intended target directory for MCP filesystem access — students add their own journalism documents there for the Module 4 RAG exercise.

## Repo structure purpose

| Path | Module | Purpose |
|------|--------|---------|
| `CLAUDE.md` | 1 | Context file students customize for their beat |
| `sample-docs/` | 1 | Pre-made journalism documents for exercises |
| `skills/` | 2 | Skill templates students copy and modify |
| `scripts/` | 3 | Shell pipeline starters |
| `mcp-configs/` | 4 | MCP config examples |
| `beat-archive/` | 4 | Student document archive for RAG |

---

## Beat

I cover Greenfield city hall for [your publication].

Key people and institutions:
- [Mayor name], mayor of Greenfield
- [Council member names and relevant context]
- Greenfield Planning Department
- Greenfield Public Works Department
- Greenfield Finance Department

## Style

- AP style for all writing
- No Oxford comma
- Spell out numbers under 10
- Dollar amounts: $2.3 million, not $2,300,000
- Refer to the City of Greenfield as "the city" on second reference

## Source standards

- Attribute all claims to named sources
- Flag unverified claims separately from verified ones
- If a source makes a factual claim (dates, dollar amounts, vote counts), note whether it can be verified independently

## Terminology

- **Resolution vs. ordinance**: A resolution expresses the council's position or intent on a matter but does not have the force of law. An ordinance is a binding local law that amends the municipal code. Use the correct term — do not use them interchangeably.

## Avoid

- "blasted" — use "criticized," "opposed," or quote directly
- AI-isms: "it's worth noting," "it's important to note," "delve," "tapestry," "nuanced," "comprehensive," "robust," "leverage" (as a verb), "utilize," "in conclusion," "certainly," "absolutely"
- Editorializing — do not characterize a vote or decision as "controversial" or "landmark" without direct attribution
