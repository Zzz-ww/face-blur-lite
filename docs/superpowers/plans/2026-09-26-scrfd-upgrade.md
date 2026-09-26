# SCRFD 升级（det_500m 移植 + top_k 压测）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--detector scrfd` 全链可达（内核/qc/CLI/blur.sh/下载脚本），YuNet 保持默认零行为变化；top_k 绑定性压测 + 同闸门 A/B 定胜负（spec：docs/superpowers/specs/2026-09-26-scrfd-upgrade-design.md）。

**Architecture:** SCRFDDetector 实现 Detector 协议（letterbox 640 + cv2.dnn 推理 + 纯 numpy 解码）；make_detector 工厂 + resolve_model_path(kind) 双模型解析；注入点三处（kernel.py / qc.py / orchestrator.py 穿线）；CLI 与 blur.sh 透传；A/B 同闸门两臂（YuNet 内核+SCRFD 闸门 vs SCRFD 双坐）。

**Tech Stack:** Python 3.10+（opencv-python/numpy/scipy/pytest 白名单）、cv2.dnn（readNetFromONNX/NMSBoxes）、bash 3.2。

## Global Constraints

- 依赖白名单：Python 仅 `opencv-python/numpy/scipy/pytest`——零新依赖（cv2.dnn/NMSBoxes 均在 opencv-python 内）
- 模型资产不入库（models/*.onnx 与 assets/ gitignore；`git add` 严禁点名这些路径；FACEBLUR_MODEL / FACEBLUR_SCRFD_MODEL env 覆盖）
- **outputs/ 与 inputs/ 整体 gitignore 永不入库（硬保证）；本波实验/AB 产物进 `outputs/20260926-scrfd-ab/` 会话目录（须留存供复核）**
- `docs/industry-benchmark.md` 有用户未暂存手笔——**本波任何任务不得修改/暂存/提交该文件**
- bash 3.2 兼容（无关联数组、无 ${var,,}）
- YuNet 默认行为零变化（默认 kind="yunet"；现有 122 测试即回归锁）
- 退出码语义不变（0/1/2/3，优先级 2>3>1）
- commit 尾行 `Co-Authored-By: Claude Code <noreply@anthropic.com>`；分支 feat/scrfd-upgrade，收尾 --no-ff 合并；**push 仅在用户明确要求时**
- 测试命令一律 `./venv/bin/python -m pytest`（下文简写 pytest）
- 解码常数以 spike 已验证值为准（预处理 (x-127.5)/128、pad 114、输出序 [score×3, dist×3, kps×3]——docs/bugs/014 附录）

---

### Task 0: 建分支 + Phase 1 top_k 绑定性压测（测量，不改产品码）

**Files:**
- Create: `outputs/20260926-scrfd-ab/topk/`（产物目录，gitignored）
- Modify: 无产品码（除非压测判定"绑定"——见 Step 4）

**Interfaces:**
- Produces: top_k 绑定性结论（Task 7 A/B 记录引用；若绑定，detector.py 默认值在本任务内改）

- [ ] **Step 0: 建分支**

```bash
cd /Users/zhujiawei/Desktop/zte/backup/workbackup-main/face-blur-lite
git checkout -b feat/scrfd-upgrade
```

- [ ] **Step 1: 压测脚本（直接测检出面，不跑糊/编码——绑定性问题是检测侧截断）**

```bash
mkdir -p outputs/20260926-scrfd-ab/topk
./venv/bin/python - <<'EOF'
"""top_k 绑定性压测（spec §5）：YuNet top_k∈{50,200,5000} 在两个高密度
真实样本上的检出差异。det_every=3 采样，只跑检测不跑管线。"""
from faceblur_lite.detector import YuNetDetector
from faceblur_lite.ffmpeg_io import FFmpegReader

VIDEOS = ["inputs/real-video/real_video.MP4",
          "inputs/big-face/celebv_5min.mp4"]
for vid in VIDEOS:
    for k in (50, 200, 5000):
        det = YuNetDetector(conf_thr=0.3, top_k=k)
        total = per_frame_max = frames = 0
        with FFmpegReader(vid) as rd:
            for i, frame in enumerate(rd):
                if i % 3:            # det_every=3 采样
                    continue
                frames += 1
                n = len(det.detect(frame))
                total += n
                per_frame_max = max(per_frame_max, n)
        print(f"{vid} top_k={k}: detect_calls={frames} "
              f"detections={total} per_frame_max={per_frame_max}")
EOF
```

预期：每行输出一个三元组；6 行齐全（real_video ~707 次检测调用、celebv 按帧数比例）。耗时约 2-5 分钟。

- [ ] **Step 2: 判定**

比较各视频 `top_k=50` 与 `top_k=5000` 的 `detections`：
- 差 ≤5% → **不绑定**：结论记录（Step 3），不改产品码；
- 差 >5% → **绑定**：把 detector.py:50 `top_k: int = 50` 默认值改为压测中最小的非绑定档（如 200/5000），并在 commit message 里记录数字依据。

- [ ] **Step 3: 结论落档（spec §5 归档要求）**

把 6 行原始数字 + 判定结论追加到 `docs/qc-severity-calibration.md` 末尾新节：

```markdown
## 附录：top_k 绑定性压测（2026-09-26，spec SCRFD §5）

YuNet top_k∈{50,200,5000} × {real_video, celebv_5min}，det3 采样直测检出
（不跑管线）：detections 差 X%/Y% → 判定【不绑定/绑定（默认已改 N）】。
A/B 两臂 top_k 同值，公平性成立。
```

- [ ] **Step 4: Commit（若 Step 2 判定绑定，先改 detector.py:50 再提交两处）**

```bash
git add docs/qc-severity-calibration.md
git commit -m "test(calibration): top_k 绑定性压测归档——[不绑定/绑定改档 N]

spec 2026-09-26 §5：50 vs 5000 检出差 X%（real_video）/Y%（celebv）。
[若绑定：YuNetDetector top_k 默认 50→N，高密度截断证据见本节。]

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 1: letterbox 几何 + SCRFD 解码纯函数（TDD，无模型依赖）

**Files:**
- Modify: `src/faceblur_lite/detector.py`（模块级新增两个纯函数）
- Test: `tests/test_scrfd_decode.py`（新建）

**Interfaces:**
- Produces（Task 2 的 SCRFDDetector 依赖）:
  - `letterbox_shape(h: int, w: int, size: int = 640) -> tuple[float, int, int, int, int]`——返回 `(scale, new_h, new_w, pad_top, pad_left)`
  - `_decode_scrfd(outputs, conf_thr: float, top_k: int, scale: float, pad_top: int, pad_left: int) -> list[Det]`——outputs 为 9 个 ndarray（[score×3 (N,1), dist-bbox×3 (N,4), kps×3 (N,10)]，kps 丢弃），返回原图坐标系的 Det 列表

- [ ] **Step 1: 写失败测试（tests/test_scrfd_decode.py 新建）**

```python
"""SCRFD 解码纯函数单元测试（合成张量，无模型依赖）——spec 2026-09-26 §1/§7。
spike 踩明的两坑各有专测：bbox dist ×stride、anchor np.repeat 逐位置交错。"""
import numpy as np

from faceblur_lite.detector import _decode_scrfd, letterbox_shape


def _mk_outputs(size=64):
    """合成 9 张量：strides(8,16,32) 对应 feat 8/4/2，2 锚/cell。"""
    outs = []
    for feat in (8, 4, 2):        # score×3
        outs.append(np.zeros((feat * feat * 2, 1), np.float32))
    for feat in (8, 4, 2):        # dist-bbox×3
        outs.append(np.ones((feat * feat * 2, 4), np.float32) * 99)
    for feat in (8, 4, 2):        # kps×3（解码不读）
        outs.append(np.zeros((feat * feat * 2, 10), np.float32))
    return outs


def test_letterbox_shape_portrait_and_square():
    """720×1406 竖屏 → 内容带 328×640、上下 pad 156；方图零 pad。"""
    scale, nh, nw, pt, pl = letterbox_shape(720, 1406, 640)
    assert abs(scale - 640 / 1406) < 1e-9
    assert (nh, nw) == (328, 640) and (pt, pl) == (156, 0)
    assert letterbox_shape(640, 640, 640) == (1.0, 640, 640, 0, 0)


def test_decode_dist_times_stride():
    """坑 1：dist 预测 ×stride——dist=1@stride16 = 16px，不是 1px。"""
    outs = _mk_outputs()
    outs[4][11 * 2 + 1] = [1, 1, 1, 1]     # stride16 位置 (row2,col3) 第 2 锚
    outs[1][11 * 2 + 1] = 0.9
    dets = _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0)
    assert len(dets) == 1
    # center=((3+.5)*16,(2+.5)*16)=(56,40)；dist16 → 框 (40,24,72,56)
    assert dets[0].xyxy().tolist() == [40.0, 24.0, 72.0, 56.0]
    assert dets[0].conf == 0.9


def test_decode_anchor_interleave_per_position():
    """坑 2：同位置两锚共享中心（np.repeat 逐位置交错，非 tile）。
    锚 22/23 必须都落在 center=(56,40)——tile 语义会把它俩分到不同格点。"""
    outs = _mk_outputs()
    outs[4][22] = [1, 2, 3, 4]             # stride16 位置 11 锚 0
    outs[4][23] = [1, 1, 1, 1]             # 位置 11 锚 1
    outs[1][22] = 0.9
    outs[1][23] = 0.8
    dets = _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0)
    assert len(dets) == 2
    cx = {(d.x1 + d.x2) / 2 for d in dets}
    cy = {(d.y1 + d.y2) / 2 for d in dets}
    assert cx == {56.0} and cy == {40.0}   # 同中心、不同展宽


def test_decode_maps_back_to_original_coords():
    """letterbox 逆映射：canvas → 原图 = (canvas - pad) / scale。"""
    outs = _mk_outputs()
    outs[4][22] = [1, 1, 1, 1]
    outs[1][22] = 0.9
    dets = _decode_scrfd(outs, 0.5, 50, 0.5, 10, 20)
    # canvas 框 (40,24,72,56) → ((40-20)/.5, (24-10)/.5, (72-20)/.5, (56-10)/.5)
    assert dets[0].xyxy().tolist() == [40.0, 28.0, 104.0, 92.0]


def test_decode_nms_and_top_k():
    """同格点重叠两锚 NMS 留一；三个不相交格点 top_k=2 留分数最高两枚。"""
    outs = _mk_outputs()
    # stride32（feat=2，格点中心 (16,16)/(48,16)/(16,48)/(48,48)）
    outs[2][0] = 0.9                       # (16,16) 锚 0
    outs[2][1] = 0.7                       # (16,16) 锚 1——同中心重叠
    outs[2][4] = 0.85                      # (16,48) 锚 0
    outs[2][6] = 0.8                       # (48,48) 锚 0
    outs[5][0] = outs[5][1] = outs[5][4] = outs[5][6] = [1, 1, 1, 1]
    dets = _decode_scrfd(outs, 0.5, 2, 1.0, 0, 0)
    assert len(dets) == 2
    confs = sorted((d.conf for d in dets), reverse=True)
    assert confs == [0.9, 0.85]            # NMS 杀同中心 0.7；top_k=2 再裁 0.8


def test_decode_empty_when_no_score_above_thr():
    outs = _mk_outputs()
    assert _decode_scrfd(outs, 0.5, 50, 1.0, 0, 0) == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_scrfd_decode.py -q
```
预期：FAIL——ImportError（letterbox_shape 不存在）。

- [ ] **Step 3: 实现（detector.py 末尾追加）**

```python
def letterbox_shape(h: int, w: int, size: int = 640
                    ) -> tuple[float, int, int, int, int]:
    """letterbox 几何（SCRFD，spec 2026-09-26 §1）：长边缩到 size、
    短边居中 pad。返回 (scale, new_h, new_w, pad_top, pad_left)。"""
    scale = size / max(h, w)
    nh = max(1, int(round(h * scale)))
    nw = max(1, int(round(w * scale)))
    return scale, nh, nw, (size - nh) // 2, (size - nw) // 2


def _decode_scrfd(outputs, conf_thr: float, top_k: int,
                  scale: float, pad_top: int, pad_left: int) -> list[Det]:
    """SCRFD buffalo 系 9 张量解码（spike 踩明两坑，docs/bugs/014 附录）：
    输出序 [score×3, dist-bbox×3, kps×3] × strides(8,16,32)，2 锚/cell；
    kps 丢弃（Det 不带关键点）。坐标逆映射回原图：(canvas - pad)/scale。"""
    strides = (8, 16, 32)
    scores = [np.asarray(o).reshape(-1) for o in outputs[0:3]]
    dists = [np.asarray(o).reshape(-1, 4) for o in outputs[3:6]]
    boxes_all, scores_all = [], []
    for i, s in enumerate(strides):
        n = scores[i].shape[0]
        feat = int(round((n / 2) ** 0.5))
        ys, xs = np.meshgrid(np.arange(feat), np.arange(feat),
                             indexing="ij")
        centers = np.stack([(xs + 0.5) * s, (ys + 0.5) * s],
                           axis=-1).reshape(-1, 2)
        centers = np.repeat(centers, 2, axis=0)   # 坑 2：逐位置交错
        keep = scores[i] >= conf_thr
        if not keep.any():
            continue
        d = dists[i][keep] * s                    # 坑 1：dist × stride
        c = centers[keep]
        boxes_all.append(np.stack(
            [c[:, 0] - d[:, 0], c[:, 1] - d[:, 1],
             c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], axis=1))
        scores_all.append(scores[i][keep])
    if not boxes_all:
        return []
    boxes = np.concatenate(boxes_all)
    scs = np.concatenate(scores_all)
    xywh = np.stack([boxes[:, 0], boxes[:, 1],
                     boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]],
                    axis=1)
    keep_idx = np.array(cv2.dnn.NMSBoxes(
        xywh.tolist(), scs.tolist(), conf_thr, 0.4)).reshape(-1)
    order = keep_idx[np.argsort(scs[keep_idx])[::-1]][:top_k]
    out = []
    for j in order:
        x1, y1, x2, y2 = boxes[j]
        out.append(Det((x1 - pad_left) / scale, (y1 - pad_top) / scale,
                       (x2 - pad_left) / scale, (y2 - pad_top) / scale,
                       float(scs[j])))
    return out
```

- [ ] **Step 4: 跑测试确认通过**

```bash
./venv/bin/python -m pytest tests/test_scrfd_decode.py -q && ./venv/bin/python -m pytest -q
```
预期：新增 6 项全 PASS；全量不回归（122 仍绿）。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/detector.py tests/test_scrfd_decode.py
git commit -m "feat(detector): letterbox 几何 + SCRFD 9 张量 numpy 解码纯函数

两坑专测钉死（dist×stride、anchor np.repeat 逐位置交错）；NMS 用
cv2.dnn.NMSBoxes（白名单内）；kps 丢弃。纯函数层无模型依赖。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: SCRFDDetector + make_detector 工厂 + resolve_model_path(kind)

**Files:**
- Modify: `src/faceblur_lite/detector.py`（类 + 工厂 + 模型解析分家）
- Modify: `tests/conftest.py`（requires_scrfd marker）
- Test: `tests/test_detector.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `letterbox_shape` / `_decode_scrfd`
- Produces（Task 3 依赖）:
  - `SCRFDDetector(model_path: str | None = None, conf_thr: float = 0.3, top_k: int = 50)`——实现 Detector 协议；**无 upscale 参数**
  - `make_detector(kind: str, model_path: str | None = None, *, conf_thr: float = 0.3, top_k: int = 50, upscale: float = 1.0) -> Detector`——scrfd 忽略 upscale；未知 kind 抛 ValueError
  - `resolve_model_path(kind: str = "yunet") -> str`——yunet→FACEBLUR_MODEL / scrfd→FACEBLUR_SCRFD_MODEL
  - conftest：`requires_scrfd` marker（models/det_500m.onnx 缺失时 skip）

- [ ] **Step 1: conftest.py 追加 marker（requires_assets 定义之后）**

```python
def scrfd_ready() -> bool:
    return assets_ready() and (ROOT / "models" / "det_500m.onnx").exists()


requires_scrfd = pytest.mark.skipif(
    not scrfd_ready(),
    reason="另需 SCRFD 模型 models/det_500m.onnx（scripts/download_assets.sh）")
```

- [ ] **Step 2: 写失败测试（tests/test_detector.py 追加）**

```python
def test_make_detector_dispatch_and_unknown_kind():
    """工厂分派：yunet→YuNetDetector、scrfd→SCRFDDetector；未知 kind 抛错。"""
    from faceblur_lite.detector import (SCRFDDetector, YuNetDetector,
                                        make_detector)
    assert isinstance(make_detector("yunet", conf_thr=0.3), YuNetDetector)
    assert isinstance(make_detector("scrfd", conf_thr=0.3), SCRFDDetector)
    with pytest.raises(ValueError, match="未知检测器"):
        make_detector("retina", conf_thr=0.3)


def test_make_detector_scrfd_ignores_upscale():
    """scrfd 无 upscale 旋钮（小脸能力即升级目的）——传了也不炸不生效。"""
    from faceblur_lite.detector import SCRFDDetector, make_detector
    det = make_detector("scrfd", conf_thr=0.3, upscale=2.0)
    assert isinstance(det, SCRFDDetector)
    assert not hasattr(det, "upscale") or getattr(det, "upscale", 1.0) == 1.0


def test_resolve_model_path_per_kind(monkeypatch):
    """模型解析分家：FACEBLUR_SCRFD_MODEL 只影响 scrfd。"""
    from faceblur_lite import detector as D
    monkeypatch.setenv("FACEBLUR_SCRFD_MODEL", "/tmp/fake-scrfd.onnx")
    assert D.resolve_model_path("scrfd") == "/tmp/fake-scrfd.onnx"
    monkeypatch.setenv("FACEBLUR_MODEL", "/tmp/fake-yunet.onnx")
    assert D.resolve_model_path("yunet") == "/tmp/fake-yunet.onnx"
    monkeypatch.delenv("FACEBLUR_SCRFD_MODEL")
    monkeypatch.delenv("FACEBLUR_MODEL")
    with pytest.raises(ValueError, match="未知检测器"):
        D.resolve_model_path("mega")


@requires_scrfd
def test_scrfd_detector_finds_lena_face(face_img):
    """spike 金丝雀：det_500m 在 lena 出正确位置框（0.811，014 附录）。
    与 YuNet 框 IoU>0.5——同时锁输出张量序没接错。"""
    import cv2
    from faceblur_lite.detector import SCRFDDetector, YuNetDetector
    frame = cv2.cvtColor(face_img, cv2.COLOR_RGB2BGR)
    s = SCRFDDetector(conf_thr=0.3).detect(frame)
    y = YuNetDetector(conf_thr=0.3).detect(frame)
    assert s and y
    sx1, sy1, sx2, sy2 = s[0].xyxy()
    yx1, yy1, yx2, yy2 = y[0].xyxy()
    ix1, iy1 = max(sx1, yx1), max(sy1, yy1)
    ix2, iy2 = min(sx2, yx2), min(sy2, yy2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (sx2 - sx1) * (sy2 - sy1) + (yx2 - yx1) * (yy2 - yy1) - inter
    assert inter / union > 0.5
```

- [ ] **Step 3: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_detector.py -q -k "make_detector or resolve_model_path or scrfd_detector"
```
预期：FAIL——ImportError（SCRFDDetector/make_detector 不存在）。

- [ ] **Step 4: 实现（detector.py）**

`resolve_model_path` 整体替换为分家版（老调用 `resolve_model_path()` 语义不变）：

```python
def resolve_model_path(kind: str = "yunet") -> str:
    """模型路径分家（spec 2026-09-26 §2）：yunet→FACEBLUR_MODEL，
    scrfd→FACEBLUR_SCRFD_MODEL；各自回落 models/ 下默认件。"""
    if kind == "yunet":
        env = os.environ.get("FACEBLUR_MODEL")
        default = DEFAULT_MODEL
    elif kind == "scrfd":
        env = os.environ.get("FACEBLUR_SCRFD_MODEL")
        default = PROJECT_ROOT / "models" / "det_500m.onnx"
    else:
        raise ValueError(f"未知检测器 kind：{kind}（yunet|scrfd）")
    if env:
        return env
    if default.exists():
        return str(default)
    raise FileNotFoundError(
        f"未找到 {kind} 模型 {default}；先运行 scripts/download_assets.sh"
        f" 或设置 "
        f"{'FACEBLUR_SCRFD_MODEL' if kind == 'scrfd' else 'FACEBLUR_MODEL'}")
```

SCRFDDetector + make_detector（文件末尾、_decode_scrfd 之后）：

```python
class SCRFDDetector:
    """insightface buffalo_s det_500m（SCRFD，2.5MB——spike 见 docs/bugs/014
    附录）。letterbox 640：长边缩到 640、pad 到 640×640（720×1406 → 内容带
    328×640）。预处理常数 (x-127.5)/128 与输出张量序均为 spike 验证值。
    无 upscale——SCRFD 小脸能力即升级目的，该旋钮 YuNet 专属。"""

    STRIDES = (8, 16, 32)
    INPUT_SIZE = 640

    def __init__(self, model_path: str | None = None, conf_thr: float = 0.3,
                 top_k: int = 50):
        path = model_path or resolve_model_path("scrfd")
        self.conf_thr, self.top_k = conf_thr, top_k
        self._net = cv2.dnn.readNetFromONNX(str(path))

    def detect(self, frame_bgr: np.ndarray) -> list[Det]:
        h, w = frame_bgr.shape[:2]
        scale, nh, nw, pt, pl = letterbox_shape(h, w, self.INPUT_SIZE)
        canvas = np.full((self.INPUT_SIZE, self.INPUT_SIZE, 3), 114,
                         np.uint8)
        canvas[pt:pt + nh, pl:pl + nw] = cv2.resize(
            frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 128.0, (self.INPUT_SIZE, self.INPUT_SIZE),
            (127.5, 127.5, 127.5), swapRB=False)
        self._net.setInput(blob)
        outs = self._net.forward(self._net.getUnconnectedOutLayersNames())
        return _decode_scrfd(outs, self.conf_thr, self.top_k,
                             scale, pt, pl)


def make_detector(kind: str, model_path: str | None = None, *,
                  conf_thr: float = 0.3, top_k: int = 50,
                  upscale: float = 1.0) -> Detector:
    """检测器工厂（spec 2026-09-26 §2）。scrfd 忽略 upscale（其小脸能力
    即升级目的，放大旋钮 YuNet 专属）；未知 kind 抛 ValueError。"""
    if kind == "yunet":
        return YuNetDetector(model_path, conf_thr=conf_thr, top_k=top_k,
                             upscale=upscale)
    if kind == "scrfd":
        return SCRFDDetector(model_path, conf_thr=conf_thr, top_k=top_k)
    raise ValueError(f"未知检测器 kind：{kind}（yunet|scrfd）")
```

- [ ] **Step 5: 跑测试确认通过 + 全量**

```bash
./venv/bin/python -m pytest tests/test_detector.py -q && ./venv/bin/python -m pytest -q
```
预期：全 PASS（金丝雀 IoU>0.5 通过=张量序正确；无 det_500m 环境金丝雀 skip）。

- [ ] **Step 6: Commit**

```bash
git add src/faceblur_lite/detector.py tests/conftest.py tests/test_detector.py
git commit -m "feat(detector): SCRFDDetector + make_detector 工厂 + 模型解析分家

cv2.dnn 推理（letterbox 640，spike 验证常数）接 Task 1 解码；requires_scrfd
marker 让无模型环境全量仍绿；lena 金丝雀与 YuNet 框 IoU>0.5 锁张量序。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 注入点——KernelParams.detector / qc_video(detector=) / 编排器穿线

**Files:**
- Modify: `src/faceblur_lite/kernel.py:8, 14-24, 45-46`
- Modify: `src/faceblur_lite/qc.py:10, 52-57`
- Modify: `src/faceblur_lite/orchestrator.py:77, 174, 229, 255-262`
- Test: `tests/test_kernel.py`（追加）、`tests/test_qc.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `make_detector`
- Produces（Task 4 依赖）:
  - `KernelParams.detector: str = "yunet"`
  - `qc_video(path, sample_fps=..., pass_conf=..., model_path=None, detector: str = "yunet") -> QCReport`
  - `run_batch(..., qc_detector: str = "yunet")` / `run_pipeline(..., qc_detector: str = "yunet")`（与既有 qc_sample_fps 同款穿线）

- [ ] **Step 1: 写失败测试（tests/test_kernel.py 追加）**

```python
def test_kernel_threads_detector_kind(tmp_path, move_video, monkeypatch):
    """注入点：KernelParams.detector 透传到 make_detector（mock 断言）。"""
    from faceblur_lite import kernel
    from faceblur_lite.kernel import KernelParams

    calls = []

    class FakeDet:
        def detect(self, frame):
            return []

    def fake_make(kind, mp, **kw):
        calls.append((kind, kw.get("conf_thr"), kw.get("upscale")))
        return FakeDet()

    monkeypatch.setattr(kernel, "make_detector", fake_make)
    kernel.process_video(
        move_video[0], str(tmp_path / "o.mp4"),
        params=KernelParams(detector="scrfd", det_every=2, det_conf=0.4,
                            det_upscale=1.5))
    assert calls == [("scrfd", 0.4, 1.5)]
```

- [ ] **Step 2: 写失败测试（tests/test_qc.py 追加）**

```python
def test_qc_video_threads_detector(tmp_path, move_video, monkeypatch):
    """注入点：qc_video(detector=) 透传到 make_detector，conf 用 pass_conf。"""
    from faceblur_lite import qc as qcmod
    from faceblur_lite.qc import qc_video

    calls = []

    class FakeDet:
        def detect(self, frame):
            return []

    def fake_make(kind, mp, **kw):
        calls.append((kind, kw.get("conf_thr")))
        return FakeDet()

    monkeypatch.setattr(qcmod, "make_detector", fake_make)
    rep = qc_video(move_video[0], sample_fps=2.0, pass_conf=0.5,
                   detector="scrfd")
    assert calls == [("scrfd", 0.5)] and rep.passed
```

- [ ] **Step 3: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_kernel.py tests/test_qc.py -q -k threads_detector
```
预期：FAIL——TypeError（KernelParams 无 detector）/ qc_video 无 detector 参数。

- [ ] **Step 4: 实现 kernel.py 三处**

import 行（:8）：

```python
from faceblur_lite.detector import make_detector
```

KernelParams 末字段（:25 之后追加）：

```python
    detector: str = "yunet"    # 检测器 kind：yunet|scrfd（R5 升级位，spec 2026-09-26）
```

构造点（:45-46 替换）：

```python
    detector = make_detector(p.detector, model_path,
                             conf_thr=p.det_conf, upscale=p.det_upscale)
```

- [ ] **Step 5: 实现 qc.py 三处**

import 行（:10）：

```python
from faceblur_lite.detector import make_detector
```

qc_video 签名（:52-54）：

```python
def qc_video(path: str, sample_fps: float | None = DEFAULT_SAMPLE_FPS,
             pass_conf: float = DEFAULT_PASS_CONF,
             model_path: str | None = None,
             detector: str = "yunet") -> QCReport:
```

构造点（:57 替换）：

```python
    det = make_detector(detector, model_path, conf_thr=pass_conf)
```

- [ ] **Step 6: 实现 orchestrator.py 穿线（与 qc_sample_fps 同款四处）**

`_run` 签名（:77）与 qc 调用（:174）：

```python
                 qc_sample_fps: float,
                 qc_detector: str,
```
```python
            rep = qc_video(video["output_path"], sample_fps=qc_sample_fps,
                           detector=qc_detector)
```

run_batch（:229）与 run_pipeline（:258）签名各加 `qc_detector: str = "yunet",`（置于 qc_sample_fps 之后），两处对 `_run(...)` 的调用（:252/:262）同步补传 `qc_detector`。

- [ ] **Step 7: 跑测试确认通过 + 全量**

```bash
./venv/bin/python -m pytest tests/test_kernel.py tests/test_qc.py -q \
  && ./venv/bin/python -m pytest -q
```
预期：全 PASS（默认 kind="yunet" 下 122+ 基线不回归）。

- [ ] **Step 8: Commit**

```bash
git add src/faceblur_lite/kernel.py src/faceblur_lite/qc.py \
        src/faceblur_lite/orchestrator.py tests/test_kernel.py tests/test_qc.py
git commit -m "feat(inject): KernelParams.detector + qc_video(detector=) + 编排器 qc_detector 穿线

三注入点全部改调 make_detector；默认 yunet 行为零变化；
run_batch/run_pipeline 与 qc_sample_fps 同款参数穿线。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: CLI——single/pipeline --detector、pipeline --qc-detector、qc --detector

**Files:**
- Modify: `src/faceblur_lite/cli.py:34-52, 93-100, 102-111, 158-160`
- Test: `tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `KernelParams.detector` / `run_batch(qc_detector=)` / `qc_video(detector=)`
- Produces: `--detector {yunet,scrfd}`（single/pipeline/qc，默认 yunet）；pipeline `--qc-detector`（缺省跟随 --detector）

- [ ] **Step 1: 写失败测试（tests/test_cli.py 追加）**

```python
def _fake_results():
    class R:
        video_id = "v"
        status = "done"
        qc = None
        chunk_count = 1
        wall_s = 0.0
        output_path = "o"
        error = None
    return [R()]


def test_single_passes_detector_to_params(monkeypatch, tmp_path):
    from faceblur_lite import cli

    class FakeRep:                    # cli single 分支只读这些字段进 JSON
        frames = 1
        detections = 0
        predicted_frames = 0
        track_count = 0
        segments = []
        duration_s = 0.0
        wall_s = 0.0

    seen = {}
    monkeypatch.setattr(
        "faceblur_lite.kernel.process_video",
        lambda i, o, params=None, model_path=None:
        seen.update(params=params) or FakeRep())
    rc = cli.main(["single", str(tmp_path / "in.mp4"),
                   str(tmp_path / "out.mp4"), "--detector", "scrfd"])
    assert rc == 0 and seen["params"].detector == "scrfd"


def test_pipeline_qc_detector_defaults_to_detector(monkeypatch, tmp_path):
    """--qc-detector 缺省跟随 --detector；显式给则覆盖（交叉验收位）。"""
    from faceblur_lite import cli
    seen = {}
    monkeypatch.setattr(
        "faceblur_lite.orchestrator.run_batch",
        lambda *a, **kw: seen.update(kw) or _fake_results())
    cli.main(["pipeline", str(tmp_path / "x.mp4"),
              "--outdir", str(tmp_path), "--workdir", str(tmp_path / "wd1"),
              "--detector", "scrfd"])
    assert seen["qc_detector"] == "scrfd" and seen["params"].detector == "scrfd"
    cli.main(["pipeline", str(tmp_path / "x.mp4"),
              "--outdir", str(tmp_path), "--workdir", str(tmp_path / "wd2"),
              "--detector", "scrfd", "--qc-detector", "yunet"])
    assert seen["qc_detector"] == "yunet"


def test_qc_passes_detector(monkeypatch, tmp_path):
    from faceblur_lite import cli
    from faceblur_lite.qc import QCReport
    seen = {}
    monkeypatch.setattr("faceblur_lite.qc.qc_video",
                        lambda path, **kw: seen.update(kw) or QCReport())
    rc = cli.main(["qc", str(tmp_path / "v.mp4"),
                   "--detector", "scrfd"])
    assert rc == 0 and seen["detector"] == "scrfd"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_cli.py -q -k "detector"
```
预期：FAIL——argparse SystemExit 2（未知选项 --detector）。

- [ ] **Step 3: 实现 cli.py 四处**

single 参数（:39 后）：

```python
    s.add_argument("--detector", choices=["yunet", "scrfd"],
                   default="yunet", help="检测器（SCRFD=spec 2026-09-26）")
```

single 分支（:97-98）：

```python
            KernelParams(det_every=args.det_every, det_conf=args.det_conf,
                         margin=args.margin, detector=args.detector))
```

pipeline 参数（:49 后）：

```python
    p.add_argument("--detector", choices=["yunet", "scrfd"], default="yunet")
    p.add_argument("--qc-detector", choices=["yunet", "scrfd"], default=None,
                   help="qc 复检检测器；缺省跟随 --detector（交叉验收位）")
```

pipeline 分支（:107-111）：

```python
            results = run_batch(
                args.inputs, args.outdir, args.workdir,
                workers=args.workers, segment_s=args.segment,
                params=KernelParams(det_every=args.det_every,
                                    detector=args.detector),
                cp_url=args.cp_url,
                qc_detector=args.qc_detector or args.detector)
```

qc 参数（:76 后）：

```python
    q.add_argument("--detector", choices=["yunet", "scrfd"], default="yunet")
```

qc 分支（:160）：

```python
        rep = qc_video(args.video, sample_fps=args.sample_fps,
                       detector=args.detector)
```

- [ ] **Step 4: 跑测试确认通过 + 全量**

```bash
./venv/bin/python -m pytest tests/test_cli.py -q && ./venv/bin/python -m pytest -q
```
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/faceblur_lite/cli.py tests/test_cli.py
git commit -m "feat(cli): --detector 全子命令可达 + pipeline --qc-detector 跟随语义

single/pipeline/qc 三入口；--qc-detector 缺省跟随 --detector——
交叉验收位（--qc-detector yunet）本轮只铺不评（spec §9）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: blur.sh 透传（--detector / --qc-detector）

**Files:**
- Modify: `scripts/blur.sh:14-30`（usage）、`:131`（全局量）、`:229-252`（cmd_run 解析）、`:284-288`（pipeline 调用）、`:322`（single 调用）、`:139`（qc_one 内 qc 调用）
- Test: 活体验证（bash -n + demo 回归 + 非法值 die2）

**Interfaces:**
- Consumes: Task 4 的 CLI flags
- Produces: blur.sh `--detector`/`--qc-detector`；qc_one 用 `$QC_DET` 透传（Python 编排器与 shell 收尾同语义）

- [ ] **Step 1: usage 两行（run 选项 --mode 行后）**

```bash
  --detector D    检测器：yunet（默认）| scrfd（需 models/det_500m.onnx，scripts/download_assets.sh）
  --qc-detector D  qc 复检检测器；缺省跟随 --detector（交叉验收位，spec 2026-09-26）
```

- [ ] **Step 2: 全局量（:131 QC_BAD 行合并）**

```bash
QC_BAD=0 QC_FATAL=0 QC_MARGINAL=0 QC_DET=yunet
```
（全局默认保证 demo/其它路径调 qc_one 时 set -u 不炸；cmd_run 覆盖它。）

- [ ] **Step 3: cmd_run 解析（locals 行 + case 两分支 + 校验 + 缺省跟随）**

locals 行（:229）：

```bash
  local OUTDIR=outputs WORKERS=2 SEGMENT=30 MODE=auto GO="" DET=yunet QC_DET=""
```
（bash 动态作用域：cmd_run 的局部 DET/QC_DET 对其调用的 qc_one 可见；
cmd_demo 等其它路径回落 :131 的全局 `QC_DET=yunet`。）

case 内追加（--go 分支旁）：

```bash
      --detector)    [ $# -ge 2 ] && [ -n "$2" ] || die2 "--detector 需要一个非空值"; DET=$2;    shift 2 ;;
      --qc-detector) [ $# -ge 2 ] && [ -n "$2" ] || die2 "--qc-detector 需要一个非空值"; QC_DET=$2; shift 2 ;;
```

解析循环后、MODE 校验旁：

```bash
  case "$DET" in yunet|scrfd) ;; *) die2 "--detector 仅支持 yunet|scrfd" ;; esac
  QC_DET=${QC_DET:-$DET}      # 缺省跟随 --detector（与 CLI pipeline 同语义）
```

- [ ] **Step 4: 三个调用点透传**

pipeline 两处（:284/:288 末行续接）：

```bash
      "$FACEBLUR" pipeline "${INPUTS[@]}" --outdir "$OUTDIR" \
        --detector "$DET" --qc-detector "$QC_DET" \
```
（两处同款，保留各自既有参数。）

single 调用（:322）：

```bash
      "$FACEBLUR" single "$p" "$out" --detector "$DET" >"$tmp" 2>"$err"
```

qc_one 内 qc 调用（:139 附近，`"$FACEBLUR" qc "$1"` 处）：

```bash
    "$FACEBLUR" qc "$1" --detector "$QC_DET" >"$tmp" 2>/dev/null
```

- [ ] **Step 5: 语法 + 活体验证**

```bash
bash -n scripts/blur.sh && echo OK
tmpd=$(mktemp -d)
./scripts/blur.sh demo --outdir "$tmpd/demo" >/dev/null 2>&1; echo "demo rc=$?"
./scripts/blur.sh run x.mp4 --detector bogus >/dev/null 2>&1; echo "bogus rc=$?"
rm -rf "$tmpd"
```
预期：`OK`；`demo rc=0`（默认 yunet 路径不变）；`bogus rc=2`（die2）。

- [ ] **Step 6: Commit**

```bash
git add scripts/blur.sh
git commit -m "feat(blur.sh): --detector/--qc-detector 透传——shell 侧与 CLI 同语义

qc_one 经全局 QC_DET（缺省 yunet）透传 qc 子命令，Python 编排器与
shell 收尾复检同检测器；非法值 die2（rc 2）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: download_assets.sh——det_500m + 全量 sha256 校验

**Files:**
- Modify: `scripts/download_assets.sh`（整体增强）

**Interfaces:**
- Produces: 三资产 sha256 校验 + `FACEBLUR_SCRFD_URL` 覆盖 + 断点续传

- [ ] **Step 1: 整体替换为校验版**

```bash
#!/usr/bin/env bash
# 下载模型与测试资产（含 sha256 校验）。产物已被 .gitignore 排除。
# det_500m（SCRFD/buffalo_s）默认走 hf-mirror（大陆可达，慢 ~15KB/s——
# curl -C - 断点续传是关键）；原址备选：
#   https://huggingface.co/immich-app/buffalo_s/resolve/main/detection/model.onnx
# 可用 FACEBLUR_SCRFD_URL 覆盖。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/models" "$ROOT/assets"

fetch() {  # $1=目标路径 $2=sha256 $3=URL
  local dst sum
  dst="$1"
  if [ -f "$dst" ] && [ "$(shasum -a 256 "$dst" | cut -d' ' -f1)" = "$2" ]; then
    echo "✓ 已存在且校验通过：$dst"
    return 0
  fi
  curl -L --fail -C - -o "$dst" "$3"
  sum=$(shasum -a 256 "$dst" | cut -d' ' -f1)
  if [ "$sum" != "$2" ]; then
    echo "sha256 校验失败：$dst（$sum ≠ $2）" >&2
    exit 1
  fi
}

fetch "$ROOT/models/face_detection_yunet_2023mar.onnx" \
  "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

fetch "$ROOT/models/det_500m.onnx" \
  "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a" \
  "${FACEBLUR_SCRFD_URL:-https://hf-mirror.com/immich-app/buffalo_s/resolve/main/detection/model.onnx}"

fetch "$ROOT/assets/lena.jpg" \
  "7de7ed51a1594fff247f4cae2301eceacf5313d6011e37b4a4c8733f7bb72c07" \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"

ls -la "$ROOT/models" "$ROOT/assets"
```

- [ ] **Step 2: 活体验证（本地三件全在——应全部走"已存在且校验通过"）**

```bash
bash scripts/download_assets.sh
```
预期：三行 `✓ 已存在且校验通过`，无下载。

- [ ] **Step 3: 语法检查**

```bash
bash -n scripts/download_assets.sh && echo SYNTAX-OK
```
（校验失败负例由 Task 8 终审活体对抗覆盖——破坏性负例不碰真资产。）

- [ ] **Step 4: Commit**

```bash
git add scripts/download_assets.sh
git commit -m "feat(assets): det_500m 入下载脚本 + 全量 sha256 校验

hf-mirror 主 URL + FACEBLUR_SCRFD_URL 覆盖 + curl -C - 断点续传；
YuNet/lena 补 pin（值从本地已验证文件起算）。已存在即校验跳过下载。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: A/B 执行 + 5 样本绿灯回归 + T_FATAL_CONF 复评

**Files:**
- Create: `outputs/20260926-scrfd-ab/`（全部产物；gitignored）
- Modify: `docs/qc-severity-calibration.md`（分布重采 + 复评结论）
- Modify: `docs/bugs/014-tiny-face-capability-envelope.md`（A/B 表回填）

**Interfaces:**
- Consumes: Task 3 的 `KernelParams(detector=)` 与 `qc_video(detector=, sample_fps=None)`
- Produces: A/B 胜负判定 + 校准复评结论（Task 8 文档引用）

- [ ] **Step 1: A/B 驱动（同闸门两臂，spec §6）**

```bash
mkdir -p outputs/20260926-scrfd-ab
./venv/bin/python - <<'EOF'
"""A/B 同闸门（spec 2026-09-26 §6）：两臂 qc 全用 SCRFD 全密度。
A 臂=YuNet 内核重定基线；B 臂=SCRFD 双坐 × det_conf 矩阵。
配置=BUG-014 冻结两档（det3 默认 / det1+px8+并集）。"""
from dataclasses import replace
from faceblur_lite.kernel import KernelParams, process_video
from faceblur_lite.qc import qc_video

IN = "inputs/real-video/real_video.MP4"
CFGS = {"det3": KernelParams(),
        "det1-px8": KernelParams(det_every=1, margin_px=8)}
rows = []
for arm, kind, confs in [("A", "yunet", (0.3,)),
                         ("B", "scrfd", (0.3, 0.4, 0.5))]:
    for conf in confs:
        for name, base in CFGS.items():
            p = replace(base, detector=kind, det_conf=conf)
            out = f"outputs/20260926-scrfd-ab/{arm}-{name}-c{conf}.mp4"
            rep = process_video(IN, out, p)
            qc = qc_video(out, sample_fps=None, detector="scrfd")
            rows.append((arm, name, conf, rep.detections,
                         len(qc.violations), qc.fatal_count,
                         qc.marginal_count, round(rep.wall_s, 1)))
            print(*rows[-1], flush=True)
print("---- 汇总（arm cfg conf dets viol fatal marg wall）----")
for r in rows:
    print(*r)
EOF
```

预期：8 行齐全（A×2 配置 + B×3conf×2 配置）。耗时约 15-30 分钟（real_video 2121 帧 ×8 跑 + 全密度 qc）。

- [ ] **Step 2: 判定（spec §6 判据）**

- 胜出 = B（同配置同 conf 0.3 对 A）违规降 ≥30% 且 wall_s 劣化 ≤10%；
- 记录 B 臂最优 conf；
- A 对历史 492/308 的差单独记录一行（=闸门灵敏度变化，独立信息）。

- [ ] **Step 3: 5 样本绿灯回归（B 臂最优 conf，主判 SCRFD 闸门 + YuNet 交叉参考）**

```bash
mkdir -p outputs/20260926-scrfd-ab/regression
./scripts/blur.sh run inputs/celebv/celebv_sample1.mp4 \
    inputs/celebv/musicfest_5min.mp4 inputs/celebv/street_5min.mp4 \
    inputs/big-face/celebv_5min.mp4 \
    --outdir outputs/20260926-scrfd-ab/regression \
    --detector scrfd 2>&1 | tail -20
# YuNet 闸门交叉参考列（转红不否决、须解释）
for f in outputs/20260926-scrfd-ab/regression/*_blurred.mp4; do
  echo "== $f"; ./venv/bin/faceblur-lite qc "$f" --detector yunet | head -5
done
```
预期：主判 4 已绿样本 ✅ 不转红（real_video 本就红，不在此列）。

- [ ] **Step 4: T_FATAL_CONF 复评（重校准条款，spec §6）**

B 臂 real_video 违规 conf 分带统计（从 Step 1 的输出文件重取）：

```bash
./venv/bin/python - <<'EOF'
from faceblur_lite.qc import qc_video
import collections
for conf in ("0.3", "0.4", "0.5"):
    out = f"outputs/20260926-scrfd-ab/B-det3-c{conf}.mp4"
    qc = qc_video(out, sample_fps=None, detector="scrfd")
    bands = collections.Counter(
        "fatal" if v.severity == "fatal" else "marginal"
        for v in qc.violations)
    hist = collections.Counter(
        min(int(v.conf * 10) / 10, 0.9) for v in qc.violations)
    print(conf, dict(bands), dict(sorted(hist.items())))
EOF
```

结论追加到 `docs/qc-severity-calibration.md`（新节「SCRFD 闸门分布重采（2026-09-26）」）：分带表 + 一句复评结论。**T=0.7 维持**（0.5-0.7 段仍零视觉标签——下调须标签证据，复核留人；如分布整体平移使 fatal 段占比剧变，列为待用户裁决项而非自行改值）。

- [ ] **Step 5: 014 附录回填 A/B 表**

`docs/bugs/014-tiny-face-capability-envelope.md` 附录末尾追加：

```markdown
**A/B 同闸门结果（2026-09-26，spec §6）**：A 臂（YuNet 内核+SCRFD 闸门）
det3=___ / det1-px8=___（历史 YuNet 闸门 492/308，差值=闸门灵敏度变化）；
B 臂（SCRFD 双坐）最优 c___：det3=___ / det1-px8=___，wall ___s。
判定【胜出/未胜出】（判据：降 ≥30% 且 wall 劣化 ≤10%）。16-32px 残余带
【检出/仍盲】（探针复测一句）。绿灯回归：4 样本【全绿/X 转红】。
```

（数字从 Step 1/3 实测填，不留占位符。）

- [ ] **Step 6: 小脸探针复测（16-32px 带验证——升级初衷）**

```bash
./venv/bin/python - <<'EOF'
"""BUG-014 探针复刻：lena 缩放贴图 16/24/32/48px，SCRFD 检出与否。"""
import cv2, numpy as np
from faceblur_lite.detector import SCRFDDetector
from faceblur_lite.testmedia import load_face
face = cv2.cvtColor(load_face("assets/lena.jpg"), cv2.COLOR_RGB2BGR)
det = SCRFDDetector(conf_thr=0.3)
for px in (16, 24, 32, 48):
    small = cv2.resize(face, (px, px))
    canvas = np.full((720, 720, 3), 90, np.uint8)
    canvas[300:300 + px, 300:300 + px] = small
    hits = [d for d in det.detect(canvas)
            if d.x1 > 250 and d.x2 < 450 and d.conf >= 0.3]
    print(f"{px}px: {'检出 conf=%.3f' % hits[0].conf if hits else '盲'}")
EOF
```

结论并入 Step 5 的 014 回填句。

- [ ] **Step 7: Commit（只提交两文档——outputs/ 整体 gitignored，严禁点名 add）**

```bash
git add docs/qc-severity-calibration.md docs/bugs/014-tiny-face-capability-envelope.md
git commit -m "docs(ab): SCRFD 同闸门 A/B 实测回填——[胜出/未胜出] + T_FATAL_CONF 复评

A 臂重定基线 ___/___（对历史 492/308 差=闸门灵敏度变化）；B 臂最优
c___ ___/___（降 X%，wall Y s）；16-32px 带【检出/仍盲】；4 样本绿灯
回归【全绿】；T=0.7 维持（依据见校准文档新节）。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 文档同步 + 全量回归 + 活体验证

**Files:**
- Modify: `README.md`（模型表 + download_assets 用法）
- Modify: `docs/pipeline-guide.md`（§4 检测器节扩 SCRFD）
- Modify: `docs/design.md`（§2 模型选择升级位回收注记）
- Test: 全量 pytest + 活体（--detector scrfd 全链冒烟 + FACEBLUR_SCRFD_MODEL 覆盖）

**Interfaces:**
- Consumes: 前 7 任务全部产出 + Task 7 的 A/B 数字
- Produces: 文档与实现一致；wave 收尾素材

- [ ] **Step 1: README 模型表（download_assets.sh 提及处之后插入）**

```markdown
模型（`scripts/download_assets.sh` 下载，sha256 校验，不入库）：

| 检测器 | 模型 | 体积 | 用法 |
|---|---|---|---|
| YuNet（默认） | face_detection_yunet_2023mar.onnx | 232KB | 默认；`FACEBLUR_MODEL` 覆盖 |
| SCRFD | det_500m.onnx（buffalo_s） | 2.5MB | `--detector scrfd`；`FACEBLUR_SCRFD_MODEL` 覆盖 |

SCRFD 换档见 [BUG-014 附录](docs/bugs/014-tiny-face-capability-envelope.md)
（A/B 实测：___）。qc 交叉验收位：`--qc-detector yunet`。
```

- [ ] **Step 2: pipeline-guide §4 检测器节（YuNet 小节之后追加）**

```markdown
### 4.x SCRFD 档（--detector scrfd，spec 2026-09-26）

letterbox 链路（与 YuNet 的 setInputSize 全分辨率直喂不同）：

原图 720×1406 ──scale 0.455──▶ 内容带 328×640 ──pad 114──▶ 画布 640×640
                              ──(x-127.5)/128──▶ blob ──cv2.dnn──▶ 9 张量
[l,t,r,b]×stride 中心式解码（两坑：dist×stride、anchor 逐位置交错）──
NMS──▶ 框坐标 (canvas-pad)/scale 映射回原图

| | YuNet（默认） | SCRFD |
|---|---|---|
| 输入 | 全分辨率 setInputSize | letterbox 640 |
| 前向@640×640 | 12.7ms | 15.5ms |
| 小脸包络 | 48px（2× 放大救） | 【A/B 后填：16-32px 档】 |
| upscale 旋钮 | 有（默认关） | 无（能力原生） |
| qc 角色 | --qc-detector yunet 交叉位 | 跟随 --detector |
```

（表内数字以 Task 7 实测回填，不留占位符。）

- [ ] **Step 3: design.md §2 升级位回收注记（模型选择节 SCRFD 升级位段末尾一行）**

```markdown
> 2026-09-26 已回收：`--detector scrfd`（det_500m）全链落地，A/B 实测见
> docs/bugs/014 附录；默认仍 YuNet，胜出后翻默认另立 commit。
```

- [ ] **Step 4: 全量回归**

```bash
./venv/bin/python -m pytest -q && bash -n scripts/blur.sh && echo ALL-OK
```
预期：全绿（122 + 新增 ~15-18 项；零 skip 异常）。

- [ ] **Step 5: 活体验证（scrfd 全链 + env 覆盖）**

```bash
tmpd=$(mktemp -d)
./scripts/blur.sh run inputs/celebv/celebv_sample1.mp4 \
  --outdir "$tmpd/scrfd" --detector scrfd >/dev/null 2>&1
echo "scrfd live rc=$?"
FACEBLUR_SCRFD_MODEL=models/det_500m.onnx ./venv/bin/faceblur-lite qc \
  "$tmpd/scrfd/celebv_sample1_blurred.mp4" --detector scrfd | head -3
./scripts/blur.sh demo --outdir "$tmpd/demo" >/dev/null 2>&1; echo "demo rc=$?"
rm -rf "$tmpd"
```
预期：`scrfd live rc=0`（celebv_sample1 绿样本保持绿）；env 覆盖 qc 出 JSON；`demo rc=0`。

- [ ] **Step 6: Commit**

```bash
git add README.md docs/pipeline-guide.md docs/design.md
git commit -m "docs: SCRFD 档全链文档——README 模型表/pipeline-guide letterbox 链路/design 升级位回收

两检测器对照表（数字=A/B 实测）；qc 交叉验收位用法；默认仍 YuNet。

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 7: 收尾（等用户拍板合并方式，不自动合并/推送）**

向用户报告：全量测试计数、A/B 判定与数字、活体结果、待办 = `git merge --no-ff`（仅用户明确要求后执行；推送前敏感路径预检 outputs//inputs//.onnx/design.pdf/.db）。

---

## Self-Review 记录

- **Spec 覆盖**：§1 SCRFDDetector→Task 1（纯函数）+Task 2（类）；§2 工厂/模型解析→Task 2；§3 注入点与 CLI→Task 3（kernel/qc/orchestrator）+Task 4（CLI）+Task 5（blur.sh）；§4 下载脚本→Task 6；§5 top_k 压测→Task 0；§6 A/B→Task 7；§7 测试→各任务 TDD+Task 8 全量；§8 文档→Task 7（calibration/014）+Task 8（README/guide/design）；§9 非目标无违反（默认不翻、无 upscale、交叉位只铺）。
- **占位符**：Task 7/8 的 `___` 为实测回填位（步骤明令"从实测填，不留占位符"），非计划缺失。
- **类型一致性**：`make_detector(kind, model_path, *, conf_thr, top_k, upscale)`（Task 2 定义=Task 3 消费）；`letterbox_shape->(scale,nh,nw,pad_top,pad_left)`（Task 1 定义=Task 2 消费）；`_decode_scrfd(outputs, conf_thr, top_k, scale, pad_top, pad_left)`（同）；`qc_detector: str = "yunet"`（Task 3 定义=Task 4 消费）；blur.sh `QC_DET` 全局与 `QC_DET=${QC_DET:-$DET}` 跟随语义与 CLI `args.qc_detector or args.detector` 同义。
- **风险预置**：SCRFD 输出张量序若与 spike 不符→Task 2 金丝雀（IoU>0.5）首当其冲炸；NMSBoxes 返回形状版本差异→reshape(-1) 兜底；demo/qc_one 的 set -u→QC_DET 全局默认。
