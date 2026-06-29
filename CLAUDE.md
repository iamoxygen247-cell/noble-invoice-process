# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes for this project.

This project is developed on **Windows 11** using **VS Code**, **PowerShell**, and **Python**.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## Project Context

This project is developed on Windows 11 using VS Code, PowerShell, and Python.

Prefer Windows-compatible commands and paths. Do not assume Linux/macOS shell syntax unless explicitly requested.

Use the project virtual environment when available:

```powershell
.\.venv\Scripts\python.exe
```

Prefer this pattern for Python commands:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip show requests
```

Avoid relying on global `python`, global `pip`, or Microsoft Store Python unless the user specifically asks about global Python behavior.

If the virtual environment does not exist, say so and propose creating it before installing packages globally.

---

## Related Project Memory Files

Use these files for durable project knowledge:

* `docs/ai/troubleshooting.md` — confirmed mistakes, failed commands, debugging lessons, environment issues, and verified fixes.
* `CLAUDE.local.md` — local machine-specific notes. This file should be gitignored.
* `.claude/rules/*.md` — optional focused rule files for Python, testing, security, Windows, or project-specific workflows.

Before starting debugging, environment setup, dependency installation, or repeated error investigation, check `docs/ai/troubleshooting.md` for known issues.

Do not duplicate long troubleshooting history inside `CLAUDE.md`. Keep this file focused on rules and behavior. Put specific confirmed mistakes and fixes in `docs/ai/troubleshooting.md`.

---

## Standard Commands

When changing Python code, prefer these validation commands when applicable.

Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Install one package:

```powershell
.\.venv\Scripts\python.exe -m pip install PACKAGE_NAME
```

Check an installed package:

```powershell
.\.venv\Scripts\python.exe -m pip show PACKAGE_NAME
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run a Python script:

```powershell
.\.venv\Scripts\python.exe path\to\script.py
```

Run a Python module:

```powershell
.\.venv\Scripts\python.exe -m module_name
```

Show Python version:

```powershell
.\.venv\Scripts\python.exe --version
```

Show active Python executable:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
```

If a command fails, do not immediately guess. Read the error, identify the failing layer, and check whether the issue is caused by:

1. Wrong working directory
2. Wrong Python interpreter
3. Missing virtual environment
4. Missing package
5. Incorrect environment variable
6. Windows path or quoting issue
7. Actual code bug

---

## Python Dependency Rules

When adding a Python package:

1. Explain why the package is needed.
2. Prefer the standard library if it solves the problem simply.
3. Install the package into the project virtual environment, not global Python.
4. Update the dependency file if this project uses one.
5. Verify the package is installed.
6. Avoid adding dependencies for simple tasks.

Preferred install command:

```powershell
.\.venv\Scripts\python.exe -m pip install PACKAGE_NAME
```

Preferred verification command:

```powershell
.\.venv\Scripts\python.exe -m pip show PACKAGE_NAME
```

If the project uses `requirements.txt`, add the package there after confirming it is required.

Do not run `pip install` by itself unless specifically asked. Prefer:

```powershell
.\.venv\Scripts\python.exe -m pip install PACKAGE_NAME
```

This avoids installing packages into the wrong Python environment.

---

## Secrets and Configuration

Never hardcode secrets, API keys, tokens, connection strings, SAS URLs, passwords, or private endpoints in source code, tests, documentation, or troubleshooting notes.

Use environment variables, local gitignored files, or the approved secret store for the project.

When showing examples, use placeholders such as:

```text
YOUR_API_KEY
YOUR_FUNCTION_URL
YOUR_CONNECTION_STRING
```

Before suggesting a commit, check that no secret-like values were added.

Do not store secrets in:

* `CLAUDE.md`
* `docs/ai/troubleshooting.md`
* README files
* test files
* committed config files
* screenshots
* sample payloads

Machine-specific paths, personal environment values, or local workflow notes should go in `CLAUDE.local.md`, not in committed project files.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

* State your assumptions explicitly.
* If multiple interpretations exist, present them instead of silently picking one.
* If a simpler approach exists, say so.
* Push back when warranted.
* If something is unclear and blocks safe progress, stop. Name what's confusing. Ask.
* If progress can be made safely with an explicit assumption, state the assumption and continue.

Do not pretend certainty when the code, environment, or requirement is unclear.

---

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

* No features beyond what was asked.
* No abstractions for single-use code.
* No "flexibility" or "configurability" that wasn't requested.
* No error handling for impossible scenarios.
* No new dependency unless it is clearly justified.
* If you write 200 lines and it could be 50, rewrite it.

Ask yourself:

```text
Would a senior engineer say this is overcomplicated?
```

If yes, simplify.

---

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

* Don't "improve" adjacent code, comments, or formatting.
* Don't refactor things that aren't broken.
* Match existing style, even if you'd do it differently.
* If you notice unrelated dead code, mention it but don't delete it.
* Avoid changing public behavior unless the task requires it.

When your changes create orphans:

* Remove imports, variables, functions, or files that your changes made unused.
* Don't remove pre-existing dead code unless asked.

The test:

```text
Every changed line should trace directly to the user's request.
```

Before finishing, review the diff and verify that every change is intentional.

---

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

* "Add validation" → "Write tests for invalid inputs, then make them pass"
* "Fix the bug" → "Write a test that reproduces it, then make it pass"
* "Refactor X" → "Ensure tests pass before and after"
* "Install a package" → "Install it into `.venv`, verify with `pip show`, then run the affected code"
* "Fix environment issue" → "Identify interpreter, working directory, package state, and command output"

For multi-step tasks, state a brief plan:

```text
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria such as "make it work" require clarification or explicit assumptions.

When practical, verify using tests or commands rather than visual inspection alone.

---

## 5. Learn From Mistakes and Self-Correct

**Treat mistakes as feedback. Do not repeat known failures.**

When an implementation, command, test, assumption, or explanation turns out to be wrong:

1. Identify the mistake clearly.
2. Explain why it happened.
3. State the corrected rule or approach.
4. Apply the correction before continuing.
5. If the lesson is reusable, propose where it should be documented.

Use this format:

```text
Mistake:
[What was wrong]

Cause:
[Why the mistake happened]

Correction:
[What should be done instead]

Reusable lesson:
[Should this update CLAUDE.md, docs/ai/troubleshooting.md, .claude/rules/*.md, or stay temporary?]
```

Use `docs/ai/troubleshooting.md` for confirmed project-specific mistakes, recurring setup problems, failed commands, environment issues, debugging lessons, and fixes that should be remembered for future work.

Before starting debugging or environment-related work, check `docs/ai/troubleshooting.md` for known issues.

Before adding a new troubleshooting entry, confirm:

* The issue actually happened.
* The fix was verified.
* The lesson is likely to be useful again.

Do not add:

* Temporary guesses
* Unverified assumptions
* Secrets, tokens, keys, passwords, or SAS URLs
* One-off errors unlikely to happen again
* Personal machine-specific paths unless this is in a gitignored local file

If the same mistake happens twice, propose turning the troubleshooting entry into a stronger project rule in `CLAUDE.md` or `.claude/rules/*.md`.

Do not silently continue after a mistake. Stop, correct the approach, and then continue.

---

## 6. Debugging Discipline

**Find the failing layer before changing code.**

When something fails, do not start editing immediately.

First identify which layer failed:

1. Command syntax
2. Working directory
3. Python interpreter
4. Virtual environment
5. Installed dependency
6. Environment variable
7. File path
8. External service
9. Application code
10. Test expectation

For Python on Windows, check these early:

```powershell
Get-Location
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.\.venv\Scripts\python.exe -m pip list
```

If a package import fails, verify the package is installed in the same Python environment used to run the script.

Do not fix symptoms before confirming the cause.

---

## 7. Communication Style

**Be direct, specific, and verifiable.**

When explaining changes:

* Say what changed.
* Say why it changed.
* Say how to verify it.
* Mention any assumptions.
* Mention any risk or tradeoff.

Prefer exact commands over vague instructions.

For Windows instructions, prefer PowerShell examples.

Bad:

```text
Run the tests.
```

Better:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Bad:

```text
Install the dependency.
```

Better:

```powershell
.\.venv\Scripts\python.exe -m pip install requests
.\.venv\Scripts\python.exe -m pip show requests
```

---

## 8. Completion Checklist

Before considering a task complete, check:

* The request was satisfied.
* The solution is no more complex than necessary.
* Only necessary files were changed.
* No unrelated refactoring was done.
* No secrets were added.
* Python commands used the project virtual environment where applicable.
* Tests or validation commands were run when practical.
* Any failure was explained, not hidden.
* Any reusable mistake was proposed for `docs/ai/troubleshooting.md`.

If validation cannot be run, explain why and provide the exact command the user should run.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, clarifying questions come before implementation rather than after mistakes, Python commands consistently use the correct environment, and repeated mistakes become documented rules instead of recurring failures.
