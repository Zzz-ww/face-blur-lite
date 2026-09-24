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


@dataclass
class Violation:
    t_sec: float
    conf: float
    box: tuple[int, int, int, int]


@dataclass
class QCReport:
    sampled_frames: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def qc_video(path: str, sample_fps: float = DEFAULT_SAMPLE_FPS,
             pass_conf: float = DEFAULT_PASS_CONF,
             model_path: str | None = None) -> QCReport:
    rep = QCReport()
    det = YuNetDetector(model_path, conf_thr=pass_conf)
    with FFmpegReader(path, sample_fps=sample_fps) as rd:
        for i, frame in enumerate(rd):
            rep.sampled_frames += 1
            h, w = frame.shape[:2]
            for d in det.detect(frame):
                area = (d.x2 - d.x1) * (d.y2 - d.y1)
                if area < 8 * 8 or area > 0.4 * w * h:
                    continue    # 微框/巨框多为糊纹理噪声，不算违规
                rep.violations.append(Violation(
                    round(i / sample_fps, 2), d.conf,
                    (int(d.x1), int(d.y1), int(d.x2), int(d.y2))))
    return rep
