---
name: work-summary
description: Generate a concise incremental work summary from this project's git commits. Use for requests such as work summary, session summary, daily summary, or a recap of recent changes.
metadata:
  short-description: Generate incremental Chinese work summaries
---

# Work Summary Generator

## Purpose

Generate a concise, outcome-oriented summary of committed work in this project. Track the last summarized commit in project-local state so repeated summaries are incremental.

## When to use

- The user asks for a work summary, session summary, daily summary, or recent-change recap.
- The user asks what was completed since the last summary.
- The user asks for a commit-based development recap.

Do not use this skill for a technical code review, release note, changelog, or a summary of uncommitted changes unless the user explicitly asks for that form.

## Workflow

1. Resolve the repository root with `git rev-parse --show-toplevel` and run all git queries from there.
2. Read `.claude/work-summary-state.json` if it exists.
   - Use `last_summary.commit_hash` as the baseline when it resolves to a commit.
   - On the first run, use the parent of the oldest recent window (up to the last 10 commits) so the first summary is useful. If the repository has fewer commits, use the empty-root boundary.
   - If the stored commit no longer exists after history rewriting, fall back to the same recent window and mention that the baseline was reset.
3. Query the baseline-to-HEAD range:
   - commit hash, subject, author date, and body;
   - earliest and latest commit timestamps;
   - `git diff --shortstat BASE..HEAD` for files changed, insertions, and deletions.
4. Read relevant diffs when commit subjects are insufficient. Group related commits by user-visible outcome rather than by file or implementation detail.
5. Generate the summary in the user's language. Keep it under 1000 Chinese characters unless the user requests more detail.
6. Only after a successful summary, update `.claude/work-summary-state.json` with the latest summarized commit and statistics. Do not change source code, commit, push, or publish anything as part of this skill.

By default, summarize committed changes only. If there are no new commits, say so clearly and do not advance the state. Uncommitted changes may be mentioned separately only when relevant and without presenting them as delivered work.

## Project-aware categorization

Use only categories supported by the commits, selecting from:

- 功能新增：new user-facing capabilities and completed workflows.
- 指标、策略与回测：indicator calculation, screening, strategy discovery, portfolio simulation, and result/reporting improvements.
- 数据与稳定性：data import, synchronization, persistence, correctness, reliability, and performance improvements.
- 界面与交互：user-facing layout, interaction, usability, and visual improvements.
- 文档、配置与工程：documentation, configuration, packaging, testing infrastructure, and developer workflow.

Describe what changed and why it matters. Merge multiple commits that deliver one outcome, and avoid repeating the same change in several categories.

## Required output

Use this structure when there are new commits:

```markdown
## 工作总结（最早提交时间 ~ 最晚提交时间）

**统计数据**：提交 N 次 | 修改 M 个文件 | +A / -D 行（净增 K 行）

### 功能新增
- ...

### 指标、策略与回测
- ...

### 数据与稳定性
- ...

### 界面与交互
- ...

### 文档、配置与工程
- ...

---
**核心交付**：用一句话概括本次工作的主要结果。

**Last Commit**: `abcdef0`
```

Omit empty categories. The time range and statistics are mandatory. End with a `核心交付` statement and include the latest summarized short commit hash.

## Content constraints

- Focus on delivered outcomes, not the commands, file paths, or internal mechanics used to produce them.
- Do not name specific libraries, frameworks, API providers, or infrastructure products in the normal summary. For example, translate a provider-specific implementation into “完善外部数据接入”.
- Do not expose secrets, local machine paths, or unrelated worktree details.
- Preserve uncertainty: do not call an unmerged or uncommitted change delivered.
- Use concrete user value and observable behavior where the commit evidence supports it; do not invent product impact.

## State format

Keep the state file project-local and machine-readable:

```json
{
  "last_summary": {
    "commit_hash": "abcdef0",
    "datetime_range": {
      "start": "2026-09-01T09:00:00+08:00",
      "end": "2026-09-03T18:30:00+08:00"
    },
    "stats": "5 files changed, 120 insertions(+), 20 deletions(-)",
    "updated_at": "2026-09-03T18:30:00+08:00"
  }
}
```

Write the state atomically or via a short Python JSON update so an interrupted run does not leave malformed JSON. The state is tracking metadata, not a user-facing report.
