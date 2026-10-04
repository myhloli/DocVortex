"""Independent profiling of the frozen model shared entry only observes hot runs and does not modify the function identity of the PDF extractor."""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import cProfile
import json
import os
from pathlib import Path
import pstats
import sys
import threading
import time

from mineru_model_tape import ModelTape
from mineru_shared_benchmark import main as benchmark_main


def main():
    """There is function identity protection on the native path, just replace the diagnostic timer and observe the real call with cProfile."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--docvortex-source", type=Path, required=True)
    parser.add_argument("--mineru-source", type=Path, required=True)
    parser.add_argument("--backend", default="rust")
    parser.add_argument("--render-backend", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args, _ = parser.parse_known_args()
    if any(flag in sys.argv for flag in ("--record", "--memory", "--verify-inputs")):
        parser.error("Profiler must run separately from recording, memory and input audits")
    os.environ.update(
        DOCVORTEX_COMPUTE_BACKEND=args.backend,
        DOCVORTEX_PDF_RENDER_BACKEND=args.render_backend,
        LOGURU_LEVEL="WARNING",
        ORT_DISABLE_TELEMETRY="1",
    )
    sys.path[:0] = [str(args.docvortex_source.resolve() / "src"), str(args.mineru_source.resolve())]
    # First import according to the specified version to prevent the entrance's own import cost from contaminating hot operation statistics.
    from mineru.backend.analysis.pdf import pipeline

    assert Path(pipeline.__file__).resolve().is_relative_to(args.mineru_source.resolve())
    from mineru.utils import timing

    original_timer = timing.stage_timer
    stages = defaultdict(lambda: {"calls": 0, "inclusive_seconds": 0.0, "exclusive_seconds": 0.0})
    local = threading.local()
    enabled = False
    profile = cProfile.Profile()
    resets = 0
    reset, complete = ModelTape.reset, ModelTape.complete

    @contextmanager
    def stage_timer(stage):
        """Sub-stages are deducted based on nested relationships to prevent inclusion relationships from being repeatedly counted in the optimizeable proportion."""
        if not enabled:
            yield
            return
        if not hasattr(local, "stack"):
            local.stack = []
        frame = [stage, time.perf_counter(), 0.0]
        local.stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - frame[1]
            assert local.stack.pop() is frame
            row = stages[stage]
            row["calls"] += 1
            row["inclusive_seconds"] += elapsed
            row["exclusive_seconds"] += elapsed - frame[2]
            if local.stack:
                local.stack[-1][2] += elapsed

    def begin(tape):
        """The first round is only preheating; sampling is started after that, and does not replace any production extraction or analysis methods."""
        nonlocal resets, enabled
        reset(tape)
        enabled = resets > 0
        resets += 1
        if enabled:
            profile.enable()

    def finish(tape):
        """The actual entry is stopped immediately, and the output serialization and summary do not enter the analysis."""
        nonlocal enabled
        profile.disable()
        enabled = False
        complete(tape)

    replaced = []
    for name, module in list(sys.modules.items()):
        if name.startswith("mineru.") and module is not None:
            for key, value in list(vars(module).items()):
                if value is original_timer:
                    replaced.append((module, key, value))
                    setattr(module, key, stage_timer)
    ModelTape.reset = begin
    ModelTape.complete = finish
    arguments = sys.argv[:]
    sys.argv.extend(["--runs", "1"])
    try:
        benchmark_main()
    finally:
        profile.disable()
        ModelTape.reset = reset
        ModelTape.complete = complete
        sys.argv[:] = arguments
        for module, key, value in replaced:
            setattr(module, key, value)
        if args.output.exists():
            profile.dump_stats(str(args.output / "cpu.prof"))
            with (args.output / "cpu.txt").open("w") as stream:
                pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(90)
            (args.output / "stages.json").write_text(json.dumps(dict(stages), ensure_ascii=False, indent=2))
            report = args.output / "report.json"
            if report.exists():
                data = json.loads(report.read_text())
                data.update(
                    timing_eligible=False,
                    profiled=True,
                    profiler_scope="one warm analyze_pdf call; serialization excluded; no extractor identity replacement",
                )
                report.write_text(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
