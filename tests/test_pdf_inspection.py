import os
import shutil
import sys
from pathlib import Path

import pytest

from assessment_workbench.pdf_inspection import PopplerPdfInspector


@pytest.mark.skipif(os.name != "nt", reason="Windows executable resolution regression")
def test_poppler_prefers_native_executable_over_earlier_command_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim_directory = tmp_path / "shim"
    native_directory = tmp_path / "native"
    shim_directory.mkdir()
    native_directory.mkdir()
    (shim_directory / "fake-poppler.cmd").write_text("@exit /b 3\n", encoding="ascii")
    native_executable = native_directory / "fake-poppler.exe"
    shutil.copy2(sys.executable, native_executable)
    monkeypatch.setenv("PATH", os.pathsep.join((str(shim_directory), str(native_directory))))

    inspector = PopplerPdfInspector(
        pdfinfo_command="fake-poppler",
        pdftotext_command="fake-poppler",
        pdftoppm_command="fake-poppler",
    )

    assert Path(inspector.pdfinfo_command) == native_executable
    assert Path(inspector.pdftotext_command) == native_executable
    assert Path(inspector.pdftoppm_command) == native_executable
