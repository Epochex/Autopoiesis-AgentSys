from frontend.gateway.app.completed_event_view import without_archived


def test_archive_hides_only_selected_cycle_and_preserves_new_recurrence():
    old = {'id': 'cycle-old', 'device': 'same-service'}
    new = {'id': 'cycle-new', 'device': 'same-service'}
    snapshot = {'suggestions': [old, new], 'feed': [
        {'id': 'feed-suggestion-cycle-old'}, {'id': 'feed-suggestion-cycle-new'}],
        'defaultSuggestionId': 'cycle-old'}
    result = without_archived(snapshot, {'cycle-old'})
    assert result['suggestions'] == [new]
    assert result['defaultSuggestionId'] == 'cycle-new'
    assert result['feed'] == [{'id': 'feed-suggestion-cycle-new'}]
    assert snapshot['suggestions'] == [old, new]


def test_archive_last_card_clears_default():
    result = without_archived({'suggestions': [{'id': 'cycle-old'}], 'defaultSuggestionId': 'cycle-old'}, {'cycle-old'})
    assert result['suggestions'] == []
    assert result['defaultSuggestionId'] is None
