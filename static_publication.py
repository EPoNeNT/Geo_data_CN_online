"""Validated publication helpers for ordinary static report JSON files."""
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Tuple


def publish_json_reports(output_dir: str, reports: Iterable[Tuple[str, Callable[[], dict]]]) -> None:
    """Generate, validate, and publish report objects with rollback on replace errors."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    reports = list(reports)
    filenames = [name for name, _ in reports]
    if len(set(filenames)) != len(filenames):
        raise ValueError("Report filenames must be unique")
    if any(Path(name).name != name or not name.endswith(".json") for name in filenames):
        raise ValueError("Report filenames must be plain JSON filenames")

    stage = Path(tempfile.mkdtemp(prefix=".static-report-stage-", dir=str(output_path)))
    preserve_stage = False
    try:
        staged_files = []
        batch_generated_at = None
        for filename, generate in reports:
            data = generate()
            if not isinstance(data, dict):
                raise TypeError(f"{filename} must contain a JSON object")
            generated_at = data.get("generatedAt")
            if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
                raise ValueError(f"{filename} has no valid generatedAt")
            try:
                datetime.fromisoformat(generated_at[:-1] + "+00:00")
            except ValueError as error:
                raise ValueError(f"{filename} has no valid generatedAt") from error
            if batch_generated_at is None:
                batch_generated_at = generated_at
            elif generated_at != batch_generated_at:
                raise ValueError(f"{filename} generatedAt does not match the report batch")
            staged_path = stage / filename
            with staged_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            with staged_path.open("r", encoding="utf-8") as stream:
                validated = json.load(stream)
            if not isinstance(validated, dict) or not isinstance(validated.get("generatedAt"), str):
                raise ValueError(f"{filename} failed JSON object validation")
            staged_files.append((filename, staged_path))

        backups = {}
        published = []
        try:
            for filename, _ in staged_files:
                final_path = output_path / filename
                if final_path.exists():
                    backup_path = stage / (filename + ".backup")
                    shutil.copy2(final_path, backup_path)
                    backups[filename] = backup_path
            for filename, staged_path in staged_files:
                os.replace(staged_path, output_path / filename)
                published.append(filename)
        except Exception:
            rollback_errors = []
            for filename in reversed(published):
                final_path = output_path / filename
                backup_path = backups.get(filename)
                try:
                    if backup_path is not None:
                        os.replace(backup_path, final_path)
                    elif final_path.exists():
                        final_path.unlink()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                preserve_stage = True
                raise RuntimeError(
                    "Report publication failed and rollback was incomplete; "
                    f"recovery files retained at {stage}"
                ) from rollback_errors[0]
            raise
    finally:
        if not preserve_stage:
            if stage.resolve().parent != output_path.resolve() or not stage.name.startswith(".static-report-stage-"):
                raise RuntimeError("Unexpected report staging directory")
            shutil.rmtree(stage)
