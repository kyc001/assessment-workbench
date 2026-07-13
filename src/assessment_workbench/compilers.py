import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CompileResult:
    pdf: bytes
    log: str
    elapsed_seconds: float


class LatexCompiler(Protocol):
    def compile(self, source: str, *, job_name: str) -> CompileResult: ...


class LatexCompileError(RuntimeError):
    pass


_BLOCKING_LATEX_DIAGNOSTICS = (
    "fontconfig **error:**",
    "missing character:",
    "could not represent character",
    r"overfull \hbox",
    r"overfull \vbox",
    r"underfull \hbox",
    r"underfull \vbox",
)


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
            environment = _tectonic_environment(root)
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
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise LatexCompileError(
                    f"LaTeX compilation timed out after {self.timeout_seconds} seconds"
                ) from exc
            log = _normalize_log(completed.stdout + completed.stderr, root)
            if completed.returncode != 0:
                raise LatexCompileError(f"LaTeX compilation failed:\n{log}")
            validate_latex_log(log)
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


def validate_latex_log(log: str) -> None:
    normalized = log.casefold()
    for diagnostic in _BLOCKING_LATEX_DIAGNOSTICS:
        if diagnostic in normalized:
            raise LatexCompileError(
                f"LaTeX compilation produced blocking diagnostic {diagnostic!r}:\n{log}"
            )


def _normalize_log(log: str, temporary_root: Path) -> str:
    return log.replace(str(temporary_root), "<temp>")[-1_000_000:]


def _tectonic_environment(temporary_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    if os.name != "nt" or environment.get("FONTCONFIG_FILE"):
        return environment

    windows_fonts = Path(environment.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not windows_fonts.is_dir():
        return environment

    cache_dir = temporary_root / "fontconfig-cache"
    cache_dir.mkdir()
    config_path = temporary_root / "fonts.conf"
    config_path.write_text(
        '<?xml version="1.0"?>\n'
        "<fontconfig>\n"
        f"  <dir>{windows_fonts.as_posix()}</dir>\n"
        f"  <cachedir>{cache_dir.as_posix()}</cachedir>\n"
        "</fontconfig>\n",
        encoding="utf-8",
    )
    environment["FONTCONFIG_FILE"] = str(config_path)
    environment["FONTCONFIG_PATH"] = str(temporary_root)
    return environment
