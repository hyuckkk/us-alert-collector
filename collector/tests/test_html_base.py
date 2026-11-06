from fetch import parse_html

SOURCE = {"id": "northglenn-test", "url": "https://www.northglenn.org/parks_rec/programs/youth_teens/index.php"}

def test_base_href_prevents_nested_paths_and_navigation_collection():
    html = '<base href="https://www.northglenn.org/"><a href="parks_rec/programs/camp.php">Youth camp registration</a><a href="government/boards.php">Government boards</a>'
    rows = parse_html(html, SOURCE)
    assert [r.url for r in rows] == ["https://www.northglenn.org/parks_rec/programs/camp.php"]

def test_no_base_preserves_document_relative_links():
    rows = parse_html('<a href="events/test">Example event</a>', SOURCE)
    assert rows[0].url == "https://www.northglenn.org/parks_rec/programs/youth_teens/events/test"

def test_external_base_does_not_expand_allowed_host():
    assert parse_html('<base href="https://other.example/"><a href="events/test">Example event</a>', SOURCE) == []

def test_first_base_wins_and_relative_base_is_resolved():
    rows = parse_html('<base href="/"><base href="/wrong/"><a href="events/test">Example event</a>', SOURCE)
    assert rows[0].url == "https://www.northglenn.org/events/test"
