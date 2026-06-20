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
