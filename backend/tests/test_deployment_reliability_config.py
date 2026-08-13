from pathlib import Path


def test_pubsub_delivery_script_configures_bounded_push_retry() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "configure_pubsub_delivery.sh"
    ).read_text(encoding="utf-8")

    assert "--ack-deadline=120" in script
    assert "--min-retry-delay=10s" in script
    assert "--max-retry-delay=60s" in script
    assert "dead-letter" not in script
