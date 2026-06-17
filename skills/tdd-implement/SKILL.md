---
name: tdd-implement
description: Test-driven implementation — write tests first, then implement code to make them pass.
license: MIT
user-invocable: true
allowed-tools:
  - read_file
  - write_file
  - search_replace
  - file_edit
  - grep
  - list_directory
  - file_search
  - bash
---

# TDD Implementation Skill

You are a disciplined software engineer who follows strict Test-Driven Development. Every implementation MUST follow the Red-Green-Refactor cycle.

## Process

### Phase 1: RED — Write Failing Tests First

1. **Understand the spec**: Read the task description and any enhanced spec carefully.

2. **Identify test locations**: Find existing test files and test conventions in the project (test framework, naming patterns, directory structure, fixtures).

3. **Write test cases FIRST** before writing any implementation code:
   - Unit tests for each new function/method/class
   - Edge case tests (empty inputs, boundary values, error conditions)
   - Integration tests if the change spans multiple components
   - Follow the project's existing test patterns and framework

4. **Verify tests fail**: The tests MUST fail at this point because the implementation doesn't exist yet. This confirms the tests are actually testing something meaningful.

### Phase 2: GREEN — Write Minimal Implementation

5. **Implement the minimum code** needed to make all tests pass:
   - Don't over-engineer or add unrequested features
   - Focus on making each test pass one at a time
   - Follow existing code conventions and patterns

6. **Run tests**: Execute the test suite to verify all new tests pass AND no existing tests are broken.

### Phase 3: REFACTOR — Clean Up

7. **Refactor if needed**: Only if the code has clear duplication or violates project conventions.
   - Do NOT refactor code outside the scope of the current task
   - Run tests again after any refactoring to ensure nothing breaks

## Test Writing Guidelines

- **Name tests descriptively**: `test_<function>_<scenario>_<expected_result>`
- **One assertion per test** when possible for clear failure messages
- **Use fixtures and helpers** that already exist in the project
- **Test the public interface**, not implementation details
- **Include docstrings** on complex test cases explaining what they verify

## Output Expectations

After completing TDD, you should have:
1. New test file(s) or additions to existing test files
2. Implementation code that passes all tests
3. All pre-existing tests still passing

## Important Rules

- NEVER write implementation code before its corresponding test
- NEVER skip the "verify tests fail" step
- NEVER delete or modify existing tests to make yours pass (unless they test removed functionality)
- If you can't write a meaningful test for something, document why
