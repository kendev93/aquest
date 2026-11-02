from aquest.coercion import as_finite_float, as_mapping


def test_as_mapping_passes_mappings_and_rejects_other_values() -> None:
    value = {"a": 1}
    assert as_mapping(value) is value
    assert as_mapping(None) == {}
    assert as_mapping([("a", 1)]) == {}
    assert as_mapping("text") == {}


def test_as_finite_float_rejects_bools_text_and_non_finite_numbers() -> None:
    assert as_finite_float(3) == 3.0
    assert as_finite_float(2.5) == 2.5
    assert as_finite_float(True) is None
    assert as_finite_float("3.0") is None
    assert as_finite_float(float("nan")) is None
    assert as_finite_float(float("inf")) is None
    assert as_finite_float(None) is None
