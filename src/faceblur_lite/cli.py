"""入口：single（单进程内核）/ pipeline（控制面+数据面并行）/
qc（输出域复检）/ demo-video（合成测试视频）/ status（管线库状态）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from faceblur_lite import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="faceblur-lite")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("single", help="单进程：一个视频 → 一个脱敏视频")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--det-every", type=int, default=3)
    s.add_argument("--det-conf", type=float, default=0.3)
    s.add_argument("--margin", type=float, default=0.15)

    p = sub.add_parser(
        "pipeline", help="批量分片并行：控制面 + N 个数据面 Worker（R10）")
    p.add_argument("inputs", nargs="+", help="一个或多个输入视频")
    p.add_argument("--outdir", default="outputs",
                   help="输出目录（<stem>_blurred.mp4）")
    p.add_argument("--workdir", default="work-pipeline")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--segment", type=float, default=30.0)
    p.add_argument("--det-every", type=int, default=3)
    p.add_argument("--cp-url", default=None,
                   help="Go 控制面服务地址（如 http://127.0.0.1:8080）；"
                        "默认 SQLite 内嵌控制面")

    wk = sub.add_parser(
        "worker", help="独立数据面 Worker（常驻；容器/跨机/多终端扩容，R12）")
    wk.add_argument("--cp-url", default=None,
                    help="控制面服务地址（与 --db 二选一）")
    wk.add_argument("--db", default=None,
                    help="SQLite 控制面库路径（与 --cp-url 二选一，WAL 跨进程安全）")
    wk.add_argument("--idle-exit-s", type=float, default=0,
                    help="空闲多久退出；0=常驻（默认，容器部署模式）")
    wk.add_argument("--det-every", type=int, default=3)

    sp = sub.add_parser("split", help="切片算子（独立调用；DAG 节点模板用，R13）")
    sp.add_argument("input")
    sp.add_argument("outdir")
    sp.add_argument("--segment", type=float, default=30.0)

    mg = sub.add_parser("merge", help="合并算子（独立调用；DAG 节点模板用，R13）")
    mg.add_argument("output")
    mg.add_argument("chunks", nargs="+")

    q = sub.add_parser("qc", help="输出域抽帧复检")
    q.add_argument("video")
    q.add_argument("--sample-fps", type=float, default=1.0)

    d = sub.add_parser("demo-video", help="生成合成测试视频")
    d.add_argument("output")
    d.add_argument("--scenario", choices=["move", "occlude"], default="move")
    d.add_argument("--duration", type=float, default=6.0)

    st = sub.add_parser("status", help="查看管线库状态")
    st.add_argument("db")

    stat = sub.add_parser(
        "stats", help="最小观测出口：排队/处理分位、吞吐、失败率（R14）")
    stat.add_argument("db")
    stat.add_argument("--json", action="store_true", help="JSON 输出")

    args = ap.parse_args(argv)

    if args.cmd == "single":
        from faceblur_lite.kernel import KernelParams, process_video
        rep = process_video(
            args.input, args.output,
            KernelParams(det_every=args.det_every, det_conf=args.det_conf,
                         margin=args.margin))
        print(json.dumps(rep.__dict__, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "pipeline":
        from faceblur_lite.kernel import KernelParams
        from faceblur_lite.orchestrator import run_batch
        results = run_batch(
            args.inputs, args.outdir, args.workdir,
            workers=args.workers, segment_s=args.segment,
            params=KernelParams(det_every=args.det_every),
            cp_url=args.cp_url)
        print(json.dumps([{
            "video_id": r.video_id, "status": r.status,
            "qc_passed": r.qc.passed if r.qc else None,
            "violations": ([v.__dict__ for v in r.qc.violations]
                           if r.qc else None),
            "chunk_count": r.chunk_count,
            "wall_s": round(r.wall_s, 2),
            "output": r.output_path,
            "error": r.error} for r in results],
            ensure_ascii=False, indent=2))
        return 0 if all(r.status == "done" for r in results) else 1

    if args.cmd == "worker":
        from faceblur_lite.kernel import KernelParams
        from faceblur_lite.worker import worker_loop
        if not args.cp_url and not args.db:
            print("worker 需要 --cp-url 或 --db 之一", file=sys.stderr)
            return 2
        if args.cp_url:
            from faceblur_lite.remote import HttpControlPlane
            cp = HttpControlPlane(args.cp_url)
        else:
            from faceblur_lite.state import SqliteControlPlane
            cp = SqliteControlPlane(args.db)
        wid = f"w-{os.environ.get('HOSTNAME', os.getpid())}"
        worker_loop(cp, wid, KernelParams(det_every=args.det_every),
                    idle_exit_s=args.idle_exit_s)
        return 0

    if args.cmd == "split":
        from faceblur_lite.splitter import split_video
        for path in split_video(args.input, args.outdir,
                                segment_s=args.segment):
            print(path)
        return 0

    if args.cmd == "merge":
        from faceblur_lite.merger import merge_chunks
        print(merge_chunks(args.chunks, args.output))
        return 0

    if args.cmd == "qc":
        from faceblur_lite.qc import qc_video
        rep = qc_video(args.video, sample_fps=args.sample_fps)
        print(json.dumps({
            "passed": rep.passed, "sampled": rep.sampled_frames,
            "violations": [v.__dict__ for v in rep.violations]},
            ensure_ascii=False, indent=2))
        return 0 if rep.passed else 1

    if args.cmd == "demo-video":
        from faceblur_lite.testmedia import build_video, load_face
        root = Path(__file__).resolve().parents[2]
        face_path = root / "assets" / "lena.jpg"
        if not face_path.exists():
            print("缺少资产：先运行 scripts/download_assets.sh",
                  file=sys.stderr)
            return 1
        build_video(args.output, load_face(str(face_path)),
                    scenario=args.scenario, duration_s=args.duration)
        print(f"已生成 {args.output}")
        return 0

    if args.cmd == "stats":
        from faceblur_lite.stats import collect_stats, format_stats
        s = collect_stats(args.db)
        print(json.dumps(s, ensure_ascii=False, indent=2) if args.json
              else format_stats(s))
        return 0

    if args.cmd == "status":
        from faceblur_lite.state import SqliteControlPlane
        for v in SqliteControlPlane(args.db).video_overview():
            print(json.dumps(v, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
