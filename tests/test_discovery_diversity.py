"""Discovery diversity 2026-09-07C: domain classifier stays clean and specific.

Regression: shell-quoting once corrupted \\b boundaries into backspace bytes
(trial matched economy via 't-rial'); the classifier must keep tech/science
apart from conflict/economy slop.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.discover import DOMAINS, TECH_OUTLETS, classify_domain


def test_patterns_have_no_escape_damage():
    for name, pat in DOMAINS:
        assert '\x08' not in pat, name
        assert '\\\\' not in pat, (name, pat)


def test_domains():
    cases = {
        'US missile strikes kill civilians in Sirik': 'conflict',
        'Iran crude oil exports barrels per day sanctions': 'economy',
        'Iranian rial hits record low against dollar': 'economy',
        'internet shutdown filtering in Tehran mobile networks': 'tech',
        'AI startup scene in Tehran': 'tech',
        'clinical trial results Tehran scientists genome study': 'science',
        'Lake Urmia water levels drought wetland dust': 'environment',
        'parliament debates new hijab fines schedule': 'society',
    }
    for text, want in cases.items():
        assert classify_domain(text) == want, (text, classify_domain(text))


def test_tech_outlets_cover_feeds():
    from src.investigation_layer.news import FEEDS
    assert TECH_OUTLETS <= set(FEEDS), TECH_OUTLETS - set(FEEDS)
