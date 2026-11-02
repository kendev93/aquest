from aquest.errors import error_reason


def test_error_reason_uses_message_and_fallback() -> None:
    assert error_reason(ValueError("  boom  ")) == "boom"
    assert error_reason(ValueError(), fallback="failed") == "failed"
    assert error_reason(ValueError()) == "ValueError"
    assert error_reason(ValueError("abcdef"), limit=3) == "abc"
