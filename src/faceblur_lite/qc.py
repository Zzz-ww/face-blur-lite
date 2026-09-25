"""质检算子（数据面）：输出域抽帧复检——R9"怎么证明没漏"。

阈值语义（必须遵守）：pass_conf 作用在重编码后的输出域，与内核源域阈值
det_conf 不可比较——有损重编码会改变置信度，两套阈值各自声明信号域。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from faceblur_lite.detector import YuNetDetector
from faceblur_lite.ffmpeg_io import FFmpegReader

DEFAULT_SAMPLE_FPS = 1.0
DEFAULT_PASS_CONF = 0.5
T_FATAL_CONF = 0.7   # 致命/边缘分界，临时待校准（2026-09-25 444 处分布
                     # 与依据见 docs/qc-severity-calibration.md；SCRFD 升级
                     # 或新增视觉标签 ≥20 处时须重校准；0.5-0.7 段零标签
                     # =已知局限，分界保守取 0.7，无标签段归边缘）


def severity_for(conf: float) -> str:
    """conf 单轴分桶：致命（高置信真信号）/边缘（低置信残块）。"""
    return "fatal" if conf >= T_FATAL_CONF else "marginal"


@dataclass
class Violation:
    t_sec: float
    conf: float
    box: tuple[int, int, int, int]   # (x1, y1, x2, y2) 左上/右下像素坐标——非 xywh
    severity: str                    # "fatal"/"marginal"（severity_for 分桶）


@dataclass
class QCReport:
    sampled_frames: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def fatal_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "fatal")

    @property
    def marginal_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "marginal")


def qc_video(path: str, sample_fps: float | None = DEFAULT_SAMPLE_FPS,
             pass_conf: float = DEFAULT_PASS_CONF,
             model_path: str | None = None) -> QCReport:
    """sample_fps=None → 全密度逐帧复检（最终验收用）。"""
    rep = QCReport()
    det = YuNetDetector(model_path, conf_thr=pass_conf)
    with FFmpegReader(path, sample_fps=sample_fps) as rd:
        stride, offset, fps = rd.sample_stride, rd.sample_offset, rd.meta.fps
        for i, frame in enumerate(rd):
            rep.sampled_frames += 1
            t = (i * stride + offset) / fps if fps > 0 else float(i)
            h, w = frame.shape[:2]
            for d in det.detect(frame):
                area = (d.x2 - d.x1) * (d.y2 - d.y1)
                if area < 8 * 8 or area > 0.4 * w * h:
                    continue    # 微框/巨框多为糊纹理噪声，不算违规
                rep.violations.append(Violation(
                    round(t, 2), d.conf,
                    (int(d.x1), int(d.y1), int(d.x2), int(d.y2)),
                    severity_for(d.conf)))
    return rep
