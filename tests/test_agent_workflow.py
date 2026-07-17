from bughound_agent import BugHoundAgent
from llm_client import MockClient


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


def test_heuristic_analyzer_ignores_commented_out_print_statements():
    # Guardrail: pattern-matching for "print(" must skip full-line comments, or a
    # commented-out print call gets flagged as a real Code Quality issue and then
    # "fixed" by rewriting the comment text itself (observed false positive).
    agent = BugHoundAgent(client=None)
    code = (
        "# This file has no real code\n"
        "# TODO: add actual logic later\n"
        '# print("this is commented out, not real code")\n'
    )
    result = agent.run(code)

    assert not any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # TODO detection should still fire -- TODOs are meant to live in comments.
    assert any(issue.get("type") == "Maintainability" for issue in result["issues"])


class _OutOfSpecSeverityClient:
    """Fake LLM client that returns a severity outside the Low/Medium/High spec,
    to test that the agent normalizes it rather than passing it through unchecked."""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if "Return ONLY valid JSON" in system_prompt:
            return '[{"type": "Security", "severity": "Critical", "msg": "SQL injection risk"}]'
        return "def f():\n    pass\n"


def test_unknown_severity_from_llm_is_normalized_to_high_risk():
    # Guardrail: risk_assessor only recognizes "low"/"medium"/"high" (case-insensitive)
    # and silently applies zero deduction to anything else. Without normalization, a
    # "Critical" issue from the LLM would score 100/low/should_autofix=True.
    agent = BugHoundAgent(client=_OutOfSpecSeverityClient())
    result = agent.run("def f():\n    pass\n")

    assert result["issues"][0]["severity"] == "High"
    assert result["risk"]["should_autofix"] is False
