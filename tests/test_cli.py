from typer.testing import CliRunner

from assessment_workbench.cli import app

runner = CliRunner()


def test_exam_generate_requires_preset_files_together() -> None:
    result = runner.invoke(
        app,
        [
            "exams",
            "generate",
            "--subject",
            "高考数学",
            "--target-level",
            "高中毕业年级",
            "--requirements",
            "demo",
            "--subject-profile",
            "examples/subject-profiles/gaokao-mathematics.yaml",
        ],
    )

    assert result.exit_code != 0
    assert "--subject-profile and --blueprint must be provided together" in result.output


def test_exam_generate_help_lists_preset_options() -> None:
    result = runner.invoke(app, ["exams", "generate", "--help"])

    assert result.exit_code == 0
    assert "--subject-profile" in result.output
    assert "--blueprint" in result.output
    assert "--human-gates" in result.output


def test_runs_resume_command_is_registered() -> None:
    result = runner.invoke(app, ["runs", "resume", "--help"])

    assert result.exit_code == 0
    assert "RUN_ID" in result.output
