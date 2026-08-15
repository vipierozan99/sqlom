"""Profiler adapters: cProfile, pyinstrument (in-process) and py-spy, austin
(external). All output normalises to speedscope JSON + folded stacks
(`render.py`), written to the `--out-dir` the profile commands take.
"""
