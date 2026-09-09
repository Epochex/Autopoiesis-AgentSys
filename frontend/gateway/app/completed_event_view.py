"""Hide archived event cycles from the active view while retaining source ledgers."""
from __future__ import annotations


def without_archived(snapshot: dict, archived: set[str]) -> dict:
    if not archived:
        return snapshot
    suggestions = [row for row in snapshot.get('suggestions', [])
                   if row.get('id') not in archived and row.get('caseId') not in archived]
    removed_ids = {row.get('id') for row in snapshot.get('suggestions', []) if row not in suggestions}
    feed_ids = {f'feed-suggestion-{identity}' for identity in removed_ids}
    feed = [row for row in snapshot.get('feed', [])
            if row.get('id') not in archived | feed_ids and row.get('caseId') not in archived]
    default = snapshot.get('defaultSuggestionId')
    if default in removed_ids:
        default = suggestions[0]['id'] if suggestions else None
    return {**snapshot, 'suggestions': suggestions, 'feed': feed, 'defaultSuggestionId': default}
