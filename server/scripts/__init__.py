"""Operational scripts (purge CLI, SQLite→PG cutover, billing reconciliation).

The explicit ``__init__.py`` makes this a regular package so
``python -m scripts.<name>`` resolves against the working directory even if
some future dependency installs a top-level regular ``scripts`` package
into the venv — a namespace package would silently lose that resolution
(session review P3).
"""
