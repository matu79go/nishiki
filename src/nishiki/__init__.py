"""Nishiki — the KOI optimizer.

A CLI tool that wraps an existing AI agent to measure and maximize the KOI (= achieved KPI / run cost)
of each execution path. The production flow is `nishiki start` (the orchestrator drives an interactive
session up to the KOI-optimal table).

The old generic providers/pipeline framework (`watch`/`attach` etc.) was moved to attic/ on 2026-06-21.
"""
__all__ = []
