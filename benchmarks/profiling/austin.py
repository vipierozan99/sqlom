"""austin adapter: external sampling, **wall + CPU**
(`-c`/`--cpu` samples on-CPU stacks only; the default samples wall time),
thread-aware. Austin 4's `-o` output is its binary "mojo" format, which
`austin2speedscope` cannot read directly — it wants the older text/collapsed
"austin" format, so the pipeline is `austin` (mojo) -> `mojo2austin` (text) ->
`austin2speedscope` (speedscope JSON), all three from the `austin-python`
package (the `austin` sampler binary from `austin-dist` ships none of them).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


class AustinProfiler:
    name = "austin"

    def __init__(self, cpu_only: bool = False) -> None:
        self.cpu_only = cpu_only

    async def attach(self, pid: int, duration: float, out_path: str) -> str:
        with tempfile.TemporaryDirectory(prefix="rowform-bench-austin-") as tmpdir:
            raw_path = str(Path(tmpdir) / "austin.raw")
            cmd = [
                "austin", "-p", str(pid), "-x", str(max(1, int(duration))), "-o", raw_path,
            ]
            if self.cpu_only:
                cmd.insert(1, "-c")
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _stdout, stderr = await proc.communicate()
            # Austin's exit code is not a plain success/failure flag — a
            # fully successful sample (0.00% error rate, full output written)
            # still exits nonzero (observed: 254). Treat "did it write a
            # non-empty raw file" as the actual success signal instead, and
            # only surface stderr when that didn't happen.
            if not Path(raw_path).exists() or Path(raw_path).stat().st_size == 0:
                raise RuntimeError(
                    f"austin produced no output for pid {pid} (exit {proc.returncode}): "
                    f"{stderr.decode()[-2000:]}"
                )

            text_path = str(Path(tmpdir) / "austin.txt")
            to_text = await asyncio.create_subprocess_exec(
                "mojo2austin", raw_path, text_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await to_text.communicate()
            if not Path(text_path).exists() or Path(text_path).stat().st_size == 0:
                raise RuntimeError(f"mojo2austin failed: {stderr.decode()[-2000:]}")

            convert = await asyncio.create_subprocess_exec(
                "austin2speedscope", text_path, out_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await convert.communicate()
            if convert.returncode != 0:
                raise RuntimeError(f"austin2speedscope failed: {stderr.decode()[-2000:]}")
        return out_path
