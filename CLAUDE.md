# igs - Interactive Git Status

A simple terminal UI for git staging and committing. Single-file Python script using only stdlib (curses), designed to be easily copied/wget'd to any Unix machine.

## Vision

nano-like interface for git staging:
- Display files grouped by status (staged, unstaged, untracked)
- Navigate with cursor keys, stage/unstage with Space
- View diffs, commit with simple editor integration
- Future: chunk/line-level staging (like `git add -p`)

## Instructions for Claude

- Only make changes explicitly requested
- Keep code concise and minimal
- Don't over-engineer error handling
- Don't add features, comments, or refactoring that wasn't asked for
- Don't change existing comments or formatting unless required
