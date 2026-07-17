# BugHound Mini Model Card (Reflection)

---

## 1) What is this system?

**Name:** BugHound
**Purpose:** Analyze a Python snippet, propose a fix, and run reliability checks before suggesting whether the fix should be auto-applied.

**Intended users:** Students learning agentic workflows and AI reliability concepts.

---

## 2) How does it work?

BugHound runs a five-stage loop for every snippet submitted (`bughound_agent.py`, `run()`):

1. **PLAN** — logs that it's about to run a scan-and-fix pass. In the current implementation this stage doesn't actually make a decision; it just records intent.
2. **ANALYZE** — decides *what problems exist*. In heuristic mode this is pure pattern matching: substring/regex checks for `print(`, bare `except:`, and `TODO`, run in `_heuristic_analyze()`. In Gemini mode, the code is sent to the model with a system prompt (`prompts/analyzer_system.txt`) demanding a JSON array of `{type, severity, msg}` objects, which the agent then parses and validates.
3. **ACT** — decides *how to change the code*. Heuristic mode does targeted regex substitution (e.g. `print(` → `logging.info(`). Gemini mode sends the issues + original code to the model (`prompts/fixer_*.txt`) and gets back a full rewritten file.
4. **TEST** — `assess_risk()` in `reliability/risk_assessor.py` scores the proposed change from 0–100, starting at 100 and deducting for issue severity and structural red flags (code got much shorter, a `return` disappeared, a bare `except:` was touched, or — after my change — a `return` value silently changed).
5. **REFLECT** — checks `risk["should_autofix"]` (true only when risk level is "low") and logs a plain-English verdict: safe to auto-apply, or defer to a human.

**Heuristic vs. Gemini, concretely:** heuristic mode only recognizes three hardcoded patterns and can never explain *why* something is risky beyond that pattern. Gemini mode reasons about the code semantically — e.g. it caught "returning 0 on division hides real errors downstream," something no regex could express. But Gemini mode isn't free: the analyzer's output is untrusted input that must be parsed, validated, and — as I found in this activity — normalized before the rest of the pipeline can safely rely on it.

---

## 3) Inputs and outputs

**Inputs tested:**

| File | Shape | Mode |
|---|---|---|
| `sample_code/print_spam.py` | function with 3 `print()` calls, hardcoded `return True` | Gemini |
| `sample_code/flaky_try_except.py` | file I/O wrapped in a bare `except:` | Gemini |
| `sample_code/mixed_issues.py` | `TODO` comment + `print()` + bare `except:` + `return 0` on failure | Gemini |
| `sample_code/cleanish.py` | already using `logging`, no obvious issues | Heuristic |
| A hand-written "weird" case: a file containing only comments (including a commented-out `print(...)` and a `TODO`) | Edge case — no executable code at all | Heuristic |

**Outputs observed:**

- **cleanish.py:** 0 issues, code returned unchanged, risk score 100/low/auto-fix.
- **print_spam.py (Gemini):** 2 low-severity issues (print usage, "hardcoded boolean return"). The proposed fix only converted 1 of 3 `print()` calls to logging and silently changed `return True` to `return None`. Risk score **90/low/should_autofix=True** *before* my guardrail change; **60/medium/should_autofix=False** *after*.
- **flaky_try_except.py (Gemini):** 3 issues (missing `with`, bare `except:`, `return None` on failure). The fix replaced the bare except with a specific exception type but also changed the function's contract from "returns `None` on failure" to "raises `RuntimeError` on failure" — correctly scored **30/high/no-autofix**, though not because the risk assessor understood the contract change; it happened to score high purely from severity deductions.
- **mixed_issues.py (Gemini):** 4 issues detected (TODO, print, bare except, "return 0 hides errors"). The fix replaced the try/except entirely with an explicit `if y == 0: raise ValueError(...)`. Correctly scored **25/high/no-autofix**.
- **Comments-only file (heuristic):** *before* my fix, the heuristic analyzer flagged the commented-out `# print(...)` as a real "Code Quality" issue and then rewrote the comment text itself — scored **75/low/should_autofix=True**. *After* my fix, only the genuine `TODO` issue is detected; score **80/low** (still auto-fixable, correctly, since there's no real code to break).

---

## 4) Reliability and safety rules

**Rule 1 — Severity-based score deduction** (`assess_risk`, lines ~36–47): subtracts 40/20/5 points for High/Medium/Low severity issues.
- *Why it matters:* it's the main signal that turns "the analyzer found something bad" into "the system should be cautious."
- *False positive it can cause:* none directly, but it's only as good as the severity label it receives.
- *False negative it can cause:* **this is the bug I found and fixed.** The rule does exact string matching against `"high"/"medium"/"low"` (lowercased). Before my change, an issue labeled `"Critical"` — a very plausible thing for an LLM to output despite the prompt asking for only three values — got **zero deduction**, because it matched none of the three branches. I confirmed this directly: `severity="Critical"` on a "SQL injection risk" issue scored **100/low/should_autofix=True**, meaning a critical security issue could be silently auto-applied. My fix normalizes any severity outside the three recognized values to `"High"` in `_normalize_issues()` (fail cautious, not silent). Verified with `tests/test_agent_workflow.py::test_unknown_severity_from_llm_is_normalized_to_high_risk` — fails on the original code, passes after the fix.

**Rule 2 — "Return statements may have been removed"** (`assess_risk`, lines ~56–58): deducts 30 points if the literal substring `"return"` exists in the original code but not in the fixed code.
- *Why it matters:* removing a return path is a common way an "improved" function silently breaks its contract.
- *False negative it can cause:* **the second bug I found.** This check only looks at whether the word `return` exists at all, not what follows it. On `print_spam.py`, Gemini changed `return True` to `return None` — the word `return` is still there, so this rule fired zero deduction for what is a genuine behavior change (any caller checking the return value truthiness would break). I added a new signal, `_return_values()`, which extracts the actual expression after each `return` keyword and compares the *sets* of returned expressions between original and fixed code, deducting 30 points if they differ. Verified with `tests/test_risk_assessor.py::test_return_value_change_is_flagged_even_though_return_keyword_remains` — fails on the original code (score stays 100), passes after the fix (score drops, reason listed).
- *False positive this new rule could cause:* a genuinely fine refactor that changes a return expression's *form* without changing its *meaning* (e.g. `return a + b` becoming `return sum([a, b])`) would still be flagged, even though behavior is identical. The rule can't tell semantic equivalence from a literal difference — it's a blunt but honest heuristic.

---

## 5) Observed failure modes

1. **Unsafe confidence (over-trusting a low score):** `print_spam.py` in Gemini mode was scored 90/low/should_autofix=True by the original risk assessor, but the actual fix silently changed `return True` to `return None` and only converted 1 of the 3 `print()` calls it supposedly addressed. A team trusting the "low risk, auto-apply" verdict would have shipped a behavior change with no warning.

2. **False positive on non-code text:** feeding the heuristic analyzer a comments-only file (no real executable code) caused it to flag a commented-out `print("...")` string as a real "Code Quality" issue, then "fix" it by rewriting the comment's text — and scored this pointless edit as safe to auto-apply (75/low). The analyzer had no concept of "this text is inside a comment, not running code."

---

## 6) Heuristic vs Gemini comparison

- **What Gemini detected that heuristics did not:** resource-leak risk (file not opened with `with`), the semantic implication of "returning 0 on division error hides real bugs downstream," and "a hardcoded boolean return may be unnecessary." None of these are expressible as a simple substring/regex check — they require understanding *intent*, not just syntax.
- **What heuristics caught consistently:** the three patterns they're built for (`print(`, bare `except:`, `TODO`) — reliably, cheaply, and offline, but only for exactly those patterns and nothing else. They also, before my fix, could not distinguish those patterns appearing in comments from the same patterns appearing in real code.
- **How the fixes differed:** heuristic fixes are narrow, mechanical, and predictable (regex substitution) — easy to reason about but limited in scope. Gemini's fixes were more thorough (e.g. converting an entire try/except into an explicit validation) but also more likely to change more than was strictly asked for — the `print_spam.py` case being the clearest example.
- **Did the risk scorer agree with my intuition?** Partially. It correctly flagged the two multi-issue, high-severity cases as high risk. It initially *disagreed* with my intuition on `print_spam.py` — I considered the return-value change meaningfully risky, but the original scorer rated it 90/low. That gap is exactly what motivated the guardrail I added in Part 3.

---

## 7) Human-in-the-loop decision

**Scenario:** any time a proposed fix changes what a function *returns* — not just whether the word `return` is present, but the actual value/expression — BugHound should refuse to auto-apply and require human review, regardless of how low the rest of the risk score is. A return value is part of a function's contract with every caller; a silent change there can break code far away from the diff itself, and neither the LLM's own self-report nor a purely syntactic check can be trusted to catch it reliably.

- **Trigger:** the `_return_values()` mismatch I implemented in `reliability/risk_assessor.py`.
- **Where implemented:** in the risk assessment logic, not the agent workflow or UI — it needs to run for every fix regardless of source (heuristic or LLM), and the risk assessor is the single place all fixes already funnel through before the auto-apply decision is made.
- **What message the tool should show:** something more specific than the current generic "Human review recommended" — e.g. *"This fix changes what the function returns (was: `True`, now: `None`). Confirm this is intentional before applying."* The current UI doesn't surface which reason contributed most, so a user has to read the whole reasons list to notice this specific one.

---

## 8) Improvement idea

**Validate that the LLM's proposed fix is syntactically valid Python before showing it as a proposed fix**, using `ast.parse(fixed_code)` in a try/except inside `propose_fix()`. Right now, if Gemini returns malformed or truncated Python (e.g. cut off mid-function due to a token limit, or with stray markdown that `_strip_code_fences()` doesn't fully catch), the agent has no way to detect this before displaying it as a "fix" — it would only be exposed when a human actually tried to run the code. This is a small, targeted addition (a few lines in `propose_fix()`, falling back to the heuristic fixer on a `SyntaxError` exactly like the existing empty-output fallback does) that closes a real gap: currently nothing in the pipeline checks that the "fixed" code is even valid Python.
