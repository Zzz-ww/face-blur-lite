"""HttpControlPlane——控制面契约的 HTTP 客户端，对接 gocp（Go 服务）。

stdlib urllib，零第三方依赖；实例只含 base_url/timeout（可 pickle，
spawn 传参安全）。JSON 语义见 docs/control-plane-contract.md §6。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpControlPlane:
    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 404 = 资源不存在 → KeyError（与 SqliteControlPlane 的
            # get_video/get_chunk 语义对齐，契约 v1.1）；其余透传 RuntimeError
            if e.code == 404:
                raise KeyError(path) from e
            raise RuntimeError(f"控制面 {method} {path} 失败: "
                               f"{e.code} {e.read().decode()}") from e

    def init_db(self) -> None:
        self._call("POST", "/init")   # schema 由服务启动时创建；幂等

    def submit_video(self, input_path: str, final_output: str,
                     chunk_inputs: list[tuple[int, str, str]]) -> str:
        return self._call("POST", "/videos", {
            "input_path": input_path, "final_output": final_output,
            "chunk_inputs": [list(t) for t in chunk_inputs]})["video_id"]

    def claim_chunk(self, worker_id: str, ttl_s: float) -> dict | None:
        return self._call("POST", "/chunks/claim",
                          {"worker_id": worker_id, "ttl_s": ttl_s})

    def complete_chunk(self, chunk_id: str, metrics: dict) -> bool:
        return self._call("POST", f"/chunks/{chunk_id}/complete",
                          {"metrics": metrics})["merge_claimed"]

    def complete_merge(self, video_id: str, merged_path: str) -> None:
        self._call("POST", f"/videos/{video_id}/merge_complete",
                   {"merged_path": merged_path})

    def fail_chunk(self, chunk_id: str, error: str,
                   max_attempts: int = 3) -> None:
        self._call("POST", f"/chunks/{chunk_id}/fail",
                   {"error": error, "max_attempts": max_attempts})

    def reclaim_expired(self, max_attempts: int = 3) -> int:
        return self._call("POST", "/maintenance/reclaim",
                          {"max_attempts": max_attempts})["reclaimed"]

    def get_video(self, video_id: str) -> dict:
        return self._call("GET", f"/videos/{video_id}")

    def get_chunk(self, chunk_id: str) -> dict:
        return self._call("GET", f"/chunks/{chunk_id}")

    def chunk_outputs(self, video_id: str) -> list[str]:
        return self._call(
            "GET", f"/videos/{video_id}/chunk_outputs")["outputs"]

    def video_overview(self) -> list[dict]:
        return self._call("GET", "/videos")

    def finish_video(self, video_id: str, status: str) -> None:
        self._call("POST", f"/videos/{video_id}/finish", {"status": status})
