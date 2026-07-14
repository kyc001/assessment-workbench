from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from assessment_workbench.domain import ExamDocument, ExamView


class PdfInspectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class InspectedPdfPage:
    page_number: int
    width_points: float
    height_points: float
    text_characters: int
    ink_ratio: float
    edge_ink_ratio: float
    png: bytes


@dataclass(frozen=True)
class PdfInspectionResult:
    page_count: int
    extracted_text: str
    extracted_text_sha256: str
    pages: tuple[InspectedPdfPage, ...]
    blocking_findings: tuple[str, ...]
    warnings: tuple[str, ...]
    manual_checks_required: tuple[str, ...]


class PdfInspector(Protocol):
    name: str
    version: str

    def inspect(
        self,
        pdf: bytes,
        *,
        exam: ExamDocument,
        view: ExamView,
        job_name: str,
    ) -> PdfInspectionResult: ...


class PopplerPdfInspector:
    name = "poppler"
    version = "v1"

    def __init__(
        self,
        *,
        pdfinfo_command: str = "pdfinfo",
        pdftotext_command: str = "pdftotext",
        pdftoppm_command: str = "pdftoppm",
        timeout_seconds: float = 120,
        raster_dpi: int = 144,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("PDF inspection timeout must be positive")
        if raster_dpi < 72:
            raise ValueError("PDF inspection raster DPI must be at least 72")
        self.pdfinfo_command = _resolve_executable(pdfinfo_command)
        self.pdftotext_command = _resolve_executable(pdftotext_command)
        self.pdftoppm_command = _resolve_executable(pdftoppm_command)
        self.timeout_seconds = timeout_seconds
        self.raster_dpi = raster_dpi

    def inspect(
        self,
        pdf: bytes,
        *,
        exam: ExamDocument,
        view: ExamView,
        job_name: str,
    ) -> PdfInspectionResult:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", job_name) is None:
            raise ValueError("invalid PDF inspection job name")
        if not pdf.startswith(b"%PDF-"):
            raise PdfInspectionError("PDF inspector received invalid PDF bytes")

        with tempfile.TemporaryDirectory(prefix="awb-pdf-inspection-") as temporary:
            root = Path(temporary)
            pdf_path = root / f"{job_name}.pdf"
            text_path = root / f"{job_name}.txt"
            png_prefix = root / "page"
            gray_prefix = root / "analysis"
            pdf_path.write_bytes(pdf)

            info = self._run([self.pdfinfo_command, str(pdf_path)], root)
            self._run(
                [self.pdftotext_command, "-layout", str(pdf_path), str(text_path)],
                root,
            )
            self._run(
                [
                    self.pdftoppm_command,
                    "-png",
                    "-r",
                    str(self.raster_dpi),
                    str(pdf_path),
                    str(png_prefix),
                ],
                root,
            )
            self._run(
                [
                    self.pdftoppm_command,
                    "-gray",
                    "-r",
                    "36",
                    str(pdf_path),
                    str(gray_prefix),
                ],
                root,
            )

            extracted_text = text_path.read_text(encoding="utf-8", errors="replace")
            page_count, width, height = _parse_pdf_info(info)
            png_paths = _numbered_paths(root, "page-*.png")
            gray_paths = _numbered_paths(root, "analysis-*.pgm")
            text_pages = _text_pages(extracted_text, page_count)

            blocking: list[str] = []
            warnings: list[str] = []
            if len(png_paths) != page_count:
                blocking.append(
                    f"raster page count {len(png_paths)} does not match PDF page count {page_count}"
                )
            if len(gray_paths) != page_count:
                blocking.append(
                    f"analysis page count {len(gray_paths)} does not match "
                    f"PDF page count {page_count}"
                )
            if abs(width - 595.28) > 2 or abs(height - 841.89) > 2:
                blocking.append(f"PDF page size is not A4: {width:.2f} x {height:.2f} pt")
            if not extracted_text.strip():
                blocking.append("PDF has no extractable text layer")
            blocking.extend(_semantic_findings(exam, view, extracted_text))

            pages: list[InspectedPdfPage] = []
            for index, (png_path, gray_path) in enumerate(
                zip(png_paths, gray_paths, strict=False), start=1
            ):
                ink_ratio, edge_ink_ratio = _pgm_metrics(gray_path.read_bytes())
                text_characters = len(re.sub(r"\s+", "", text_pages[index - 1]))
                if ink_ratio < 0.001:
                    blocking.append(f"page {index} is visually blank")
                elif ink_ratio < 0.005:
                    warnings.append(f"page {index} has unusually little visible content")
                if edge_ink_ratio > 0.02:
                    warnings.append(f"page {index} has visible content close to the page edge")
                pages.append(
                    InspectedPdfPage(
                        page_number=index,
                        width_points=width,
                        height_points=height,
                        text_characters=text_characters,
                        ink_ratio=ink_ratio,
                        edge_ink_ratio=edge_ink_ratio,
                        png=png_path.read_bytes(),
                    )
                )

        return PdfInspectionResult(
            page_count=page_count,
            extracted_text=extracted_text,
            extracted_text_sha256=hashlib.sha256(extracted_text.encode("utf-8")).hexdigest(),
            pages=tuple(pages),
            blocking_findings=tuple(dict.fromkeys(blocking)),
            warnings=tuple(dict.fromkeys(warnings)),
            manual_checks_required=(
                "check every rendered page for overlap, clipping, and repeated labels",
                "check Chinese text and mathematical notation for visual legibility",
                "check all questions, solutions, final answers, and rubric content for correctness",
            ),
        )

    def _run(self, command: list[str], temporary_root: Path) -> str:
        try:
            completed = subprocess.run(
                command,
                cwd=temporary_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfInspectionError(
                f"PDF inspection command timed out after {self.timeout_seconds} seconds"
            ) from exc
        output = (completed.stdout + completed.stderr).replace(str(temporary_root), "<temp>")
        output = output[-1_000_000:]
        if completed.returncode != 0:
            raise PdfInspectionError(
                f"PDF inspection command failed with exit code {completed.returncode}:\n{output}"
            )
        return output


def _resolve_executable(command: str) -> str:
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate)
    if os.name == "nt" and not candidate.suffix and candidate.parent == Path("."):
        native_executable = _find_windows_native_executable(command)
        if native_executable is not None:
            return native_executable
    executable = shutil.which(command)
    if executable is not None:
        return executable
    raise PdfInspectionError(f"PDF inspection executable not found: {command}")


def _find_windows_native_executable(command: str) -> str | None:
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = raw_directory.strip().strip('"')
        if not directory:
            continue
        for suffix in (".exe", ".com"):
            candidate = Path(directory) / f"{command}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def _parse_pdf_info(output: str) -> tuple[int, float, float]:
    pages_match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", output)
    size_match = re.search(
        r"(?m)^Page(?:\s+\d+)? size:\s+([0-9.]+) x ([0-9.]+) pts",
        output,
    )
    encrypted_match = re.search(r"(?m)^Encrypted:\s+(\S+)", output)
    if pages_match is None or size_match is None:
        raise PdfInspectionError("pdfinfo did not report page count and dimensions")
    if encrypted_match is not None and encrypted_match.group(1).casefold() != "no":
        raise PdfInspectionError("encrypted PDFs cannot pass inspection")
    pages = int(pages_match.group(1))
    if pages < 1:
        raise PdfInspectionError("PDF has no pages")
    return pages, float(size_match.group(1)), float(size_match.group(2))


def _semantic_findings(exam: ExamDocument, view: ExamView, text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", text)
    findings: list[str] = []
    if re.sub(r"\s+", "", exam.title) not in normalized:
        findings.append("PDF text layer is missing the exam title")
    for number in range(1, len(exam.questions) + 1):
        chinese_match = re.search(rf"第{number}题", normalized)
        english_match = re.search(rf"Question{number}(?!\d)", normalized)
        if chinese_match is None and english_match is None:
            findings.append(f"PDF text layer is missing question {number}")
    for title in dict.fromkeys(
        bundle.question.section_title for bundle in exam.questions if bundle.question.section_title
    ):
        if re.sub(r"\s+", "", title) not in normalized:
            findings.append(f"PDF text layer is missing section title: {title}")
    if view is ExamView.QUESTIONS:
        for forbidden in ("最终答案", "评分标准", "Finalanswer", "Scoringrubric"):
            if forbidden in normalized:
                findings.append(f"questions view leaks protected label: {forbidden}")
    elif view is ExamView.SOLUTIONS:
        label = "最终答案" if exam.language.startswith("zh") else "Finalanswer"
        if normalized.count(label) < len(exam.questions):
            findings.append("solutions view is missing one or more final-answer labels")
    else:
        label = "评分标准" if exam.language.startswith("zh") else "Scoringrubric"
        if normalized.count(label) < len(exam.questions):
            findings.append("rubric view is missing one or more rubric labels")
    return findings


def _numbered_paths(root: Path, pattern: str) -> list[Path]:
    def page_number(path: Path) -> int:
        match = re.search(r"-(\d+)\.[^.]+$", path.name)
        if match is None:
            raise PdfInspectionError(f"cannot parse rendered page number: {path.name}")
        return int(match.group(1))

    return sorted(root.glob(pattern), key=page_number)


def _text_pages(text: str, page_count: int) -> list[str]:
    pages = text.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    if len(pages) < page_count:
        pages.extend([""] * (page_count - len(pages)))
    return pages[:page_count]


def _pgm_metrics(payload: bytes) -> tuple[float, float]:
    width, height, pixels = _parse_pgm(payload)
    if len(pixels) != width * height:
        raise PdfInspectionError("rendered grayscale page has invalid pixel data")
    dark = [value < 245 for value in pixels]
    ink_ratio = sum(dark) / len(dark)
    margin_x = max(1, round(width * 0.02))
    margin_y = max(1, round(height * 0.02))
    edge_pixels = 0
    edge_dark = 0
    for y in range(height):
        for x in range(width):
            if x < margin_x or x >= width - margin_x or y < margin_y or y >= height - margin_y:
                edge_pixels += 1
                edge_dark += dark[y * width + x]
    return ink_ratio, edge_dark / edge_pixels


def _parse_pgm(payload: bytes) -> tuple[int, int, bytes]:
    if not payload.startswith(b"P5"):
        raise PdfInspectionError("Poppler grayscale output is not a binary PGM")
    tokens: list[bytes] = []
    index = 2
    while len(tokens) < 3:
        while index < len(payload) and payload[index] in b" \t\r\n":
            index += 1
        if index < len(payload) and payload[index] == ord("#"):
            while index < len(payload) and payload[index] not in b"\r\n":
                index += 1
            continue
        start = index
        while index < len(payload) and payload[index] not in b" \t\r\n":
            index += 1
        if start == index:
            raise PdfInspectionError("invalid PGM header")
        tokens.append(payload[start:index])
    if index >= len(payload) or payload[index] not in b" \t\r\n":
        raise PdfInspectionError("invalid PGM header separator")
    if payload[index : index + 2] == b"\r\n":
        index += 2
    else:
        index += 1
    width, height, maximum = (int(token) for token in tokens)
    if width < 1 or height < 1 or maximum != 255:
        raise PdfInspectionError("unsupported PGM dimensions or sample depth")
    return width, height, payload[index:]
