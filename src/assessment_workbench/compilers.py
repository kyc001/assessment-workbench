import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompileResult:
    pdf: bytes
    log: str
    elapsed_seconds: float


class LatexCompileError(RuntimeError):
    pass


class TectonicCompiler:
    def __init__(self, executable: str = "tectonic", *, timeout_seconds: float = 120) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def compile(self, source: str, *, job_name: str) -> CompileResult:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", job_name) is None:
            raise ValueError("invalid LaTeX job name")
        executable = shutil.which(self.executable)
        if executable is None:
            candidate = Path(self.executable)
            if not candidate.is_file():
                raise LatexCompileError(f"Tectonic executable not found: {self.executable}")
            executable = str(candidate)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="awb-tectonic-") as temporary:
            root = Path(temporary)
            source_path = root / f"{job_name}.tex"
            output_dir = root / "output"
            output_dir.mkdir()
            source_path.write_text(source, encoding="utf-8")
            command = [
                executable,
                "-X",
                "compile",
                str(source_path),
                "--outdir",
                str(output_dir),
                "--keep-logs",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexCompileError(
                    f"LaTeX compilation timed out after {self.timeout_seconds} seconds"
                ) from exc
            log = _normalize_log(completed.stdout + completed.stderr, root)
            if completed.returncode != 0:
                raise LatexCompileError(f"LaTeX compilation failed:\n{log}")
            pdf_path = output_dir / f"{job_name}.pdf"
            if not pdf_path.is_file():
                raise LatexCompileError(f"LaTeX compiler produced no PDF:\n{log}")
            pdf = pdf_path.read_bytes()
            if not pdf.startswith(b"%PDF-"):
                raise LatexCompileError("LaTeX compiler produced an invalid PDF")
        return CompileResult(
            pdf=pdf,
            log=log,
            elapsed_seconds=time.monotonic() - started,
        )


def _normalize_log(log: str, temporary_root: Path) -> str:
    return log.replace(str(temporary_root), "<temp>")[-1_000_000:]
