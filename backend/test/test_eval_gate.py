"""Deterministic evaluation quality gate tests."""

from types import SimpleNamespace

from eval.evaluator import E2EResult, EvalReport, evaluate_quality_gate


def test_gate_passes_good_e2e_report():
    report = EvalReport(
        timestamp="now",
        e2e_results=[
            E2EResult(topic="a", score=SimpleNamespace(overall_score=4.2)),
            E2EResult(topic="b", score=SimpleNamespace(overall_score=3.8)),
        ],
    )
    passed, reasons = evaluate_quality_gate(report, min_e2e_score=3.5)
    assert passed is True
    assert reasons == []


def test_gate_fails_low_score_and_errors():
    report = EvalReport(
        timestamp="now",
        e2e_results=[
            E2EResult(topic="a", score=SimpleNamespace(overall_score=2.0)),
            E2EResult(topic="b", error="provider unavailable"),
        ],
    )
    passed, reasons = evaluate_quality_gate(
        report, min_e2e_score=3.5, max_errors=0
    )
    assert passed is False
    assert any("平均分" in reason for reason in reasons)
    assert any("错误数" in reason for reason in reasons)
