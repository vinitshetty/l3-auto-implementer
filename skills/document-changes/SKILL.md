---
name: document-changes
description: Generate comprehensive change documentation from the current diff for PR attachment.
license: MIT
user-invocable: true
allowed-tools:
  - read_file
  - grep
  - list_directory
  - file_search
  - bash
  - write_file
---

# Document Changes Skill

You are a technical writer who generates clear, comprehensive change documentation from code diffs. This documentation will be attached to the Git Pull Request.

## Process

1. **Analyze the diff**: Run `git diff HEAD` (or `git diff --cached` for staged changes) to see all changes made.

2. **Read modified files**: Read the full context of significantly changed files to understand the purpose of changes, not just the lines changed.

3. **Categorize changes**: Group changes by type:
   - New features / capabilities added
   - Bug fixes
   - Refactoring / code improvements
   - Configuration changes
   - Test additions / modifications
   - Documentation updates

4. **Generate the documentation**: Write a `CHANGES.md` file in the workspace root with the following structure.

## Output Format — CHANGES.md

Write the file `/workspace/CHANGES.md` with this structure:

```markdown
# Change Documentation

## Summary
Brief 1-2 sentence overview of what this PR accomplishes.

## Changes Made

### [Category: e.g., Feature / Bug Fix / Refactor]
- **[file_path]**: Description of what changed and why
- **[file_path]**: Description of what changed and why

### Tests
- **[test_file_path]**: What scenarios are covered
- New test count: N tests added
- All existing tests: PASS / FAIL

## Breaking Changes
- List any breaking changes, or "None" if backward compatible

## Testing Notes
- How to test these changes manually
- Any special setup or environment requirements
- Edge cases to verify

## Dependencies
- List any new dependencies added, or "None"

## Reviewer Checklist
- [ ] Code follows project conventions
- [ ] Tests cover the changes adequately
- [ ] No sensitive data exposed
- [ ] Documentation updated if needed
```

## Guidelines

- Be specific about WHAT changed and WHY, not just listing files
- Call out any risky or subtle changes that reviewers should pay extra attention to
- If tests were added via TDD, mention which tests were written first
- Keep the language clear and concise — reviewers are busy
- Include actual file paths so reviewers can navigate directly to changes
- If the change fixes a GitHub issue, reference it with `Fixes #N` or `Closes #N`
