# -*- coding: utf-8 -*-
"""Regression tests for Scroll's startup-time synchronous I/O.

The historical entry point was a populated legacy ``history.db`` whose date
index had to be rebuilt before startup could proceed: ``_init_schema`` ran in a
worker thread so a slow migration did not block the startup loop. History now
lives in the platform's PostgreSQL store, where the server maintains indexes
itself — there is no startup migration left to pin here. Index health is
covered by ``tests/unit/agents/context/test_history_store.py``.
"""
