"""The OpenAPI text says what a 402 is.

Up to 1.0.5 the API description told an agent to "wait for reset" on a 402. A
project's budget is its total spend and nothing resets it (the 1.0.5 dashboard
says so), so an agent that waited never got another answer. The response's own
hint, "do not retry until it changes", was right; the description now agrees.
"""
import re

from main import app


def _error_line(code):
    description = app.openapi()["info"]["description"]
    lines = [l for l in description.splitlines() if re.match(r"- `%s`:" % code, l)]
    assert len(lines) == 1, "the error-code list names %s once" % code
    return lines[0]


def test_a_402_is_not_something_to_wait_out():
    line = _error_line(402)
    assert "wait for" not in line.lower()
    assert "raise" in line.lower() and "do not retry" in line.lower()


def test_the_api_description_never_promises_a_budget_reset():
    description = app.openapi()["info"]["description"].lower()
    assert "wait for reset" not in description
    assert "budget resets" not in description and "resets monthly" not in description
