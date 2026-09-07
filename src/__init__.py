"""
Package marker for the nexus_think_tank source tree.

The project is organized as:
    src/
        models.py             — Pydantic data models (the schema contract)
        database.py           — SQLite initialization and repository access
        gate1_collection.py   — Gate 1: RSS fetch, trafilatura extraction, dedup
        gate2_extraction.py   — Gate 2: LLM structured extraction
        load_sources.py       — Load source config from sources.yaml
"""
