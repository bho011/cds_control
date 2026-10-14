import csv

from services.process_run_logger import ProcessRunLogger


def test_evaluation_status_and_details_are_logged(tmp_path) -> None:
    logger = ProcessRunLogger(process_name="fill_and_measure", log_dir=str(tmp_path))
    logger.write_step(
        state="FINISHED",
        error=None,
        snapshot={},
        actuator_status={},
        extra={"evaluation_status": "NOT_EVALUATED", "evaluation_details": '{"EC": {"status": "NOT_EVALUATED"}}'},
    )
    logger.close()
    with logger.file_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["evaluation_status"] == "NOT_EVALUATED"
    assert "NOT_EVALUATED" in row["evaluation_details"]
