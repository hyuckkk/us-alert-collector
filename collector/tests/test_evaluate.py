import evaluate


def test_recall_accuracy_latency_and_missed():
    truth = [
        {"id": "a", "title": "Kids Clay Workshop", "org": "Butterfly Pavilion", "start_date": "2027-07-10", "cost_text": "$20", "url": "https://butterflies.org/event/clay/", "posted_at": "2027-06-01"},
        {"id": "b", "title": "Fall Youth Basketball League", "org": "Hyland Hills", "start_date": "2027-11-01", "url": "https://www.hylandhills.gov/event/bb/"},
        {"id": "c", "title": "Teen Robotics Club", "org": "Westminster Library", "start_date": "2027-08-01", "url": "https://westminsterco.librarycalendar.com/event/robotics"},
    ]
    found = [
        {"event_key": "e1", "title": "Kids Clay Workshop", "org": "Butterfly Pavilion", "start_date": "2027-07-10", "cost_text": "$25", "url": "https://butterflies.org/event/clay?utm_source=x", "first_seen": "2027-06-03T00:00:00Z"},
        {"event_key": "e2", "title": "Fall youth basketball league registration", "org": "Hyland Hills", "start_date": "2027-11-01", "url": "https://news.example.com/x"},
    ]
    r = evaluate.evaluate(truth, found)
    assert r["recall"] == round(2 / 3, 3)
    assert [m["id"] for m in r["missed"]] == ["c"]
    assert r["accuracy"]["start_date"] == "2/2" and r["accuracy"]["cost_text"] == "0/1"
    assert r["latency_days_median"] == 2
    assert r["by_org"]["Westminster Library"] == {"total": 1, "found": 0}
