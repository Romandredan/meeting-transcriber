from app.progress import ProgressBroker


def test_publish_reaches_subscriber():
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.publish(1, "transcribe", 0.5, "processing")
    evt = q.get_nowait()
    assert evt["job_id"] == 1
    assert evt["stage"] == "transcribe"
    assert evt["progress"] == 0.5


def test_unsubscribe_stops_delivery():
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.unsubscribe(q)
    broker.publish(1, "x", 1.0, "done")
    assert q.empty()


def test_event_carries_analysis_id_none_for_jobs():
    """Фронт различает события по analysis_id — у транскрибации он None."""
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.publish(1, "transcribe", 0.5, "processing")
    assert q.get_nowait()["analysis_id"] is None


def test_event_carries_analysis_id_for_analyses():
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.publish(1, "map 2/7", 0.3, "processing", analysis_id=42)
    evt = q.get_nowait()
    assert evt["analysis_id"] == 42
    assert evt["job_id"] == 1
    assert evt["stage"] == "map 2/7"
