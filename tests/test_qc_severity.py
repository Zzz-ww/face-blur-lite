"""qc 严重度分桶单元测试（纯合成，无资产依赖）——spec 2026-09-25 §2。"""
from faceblur_lite.qc import (T_FATAL_CONF, QCReport, Violation,
                              severity_for)


def test_severity_boundary():
    """conf 单轴分桶：>=T_FATAL_CONF 为 fatal；边界值本身落 fatal。"""
    assert T_FATAL_CONF == 0.7          # 临时待校准，见校准文档
    assert severity_for(0.50) == "marginal"
    assert severity_for(0.69) == "marginal"
    assert severity_for(0.70 - 1e-9) == "marginal"
    assert severity_for(T_FATAL_CONF) == "fatal"
    assert severity_for(0.99) == "fatal"


def test_report_counts_and_passed_unchanged():
    rep = QCReport(violations=[
        Violation(0.0, 0.90, (0, 0, 9, 9), "fatal"),
        Violation(1.0, 0.55, (0, 0, 9, 9), "marginal"),
        Violation(2.0, 0.51, (0, 0, 9, 9), "marginal")])
    assert rep.fatal_count == 1 and rep.marginal_count == 2
    assert not rep.passed                      # 分级不改变 passed 二值语义
    assert QCReport().fatal_count == 0
    assert QCReport().passed
