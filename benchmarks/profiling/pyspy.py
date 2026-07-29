"""py-spy adapter (PLAN.md §10): external sampling, wall clock, attaches to a
live PID with zero in-process overhead. Requires `ptrace_scope=0` for a
non-root attach — already the case on the reference machine (PLAN.md §3).
"""

from __future__ import annotations

import asyncio


class PySpyProfiler:
    name = "py-spy"

    async def attach(self, pid: int, duration: float, out_path: str) -> str:
        cmd = [
            "py-spy", "record", "--pid", str(pid), "--output", out_path,
            "--format", "speedscope", "--duration", str(max(1, int(duration))),
            "--rate", "100",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"py-spy failed (pid {pid}): {stderr.decode()[-2000:]}")
        return out_path
