"""Presentation layer. Knows about `Itinerary`; the agent knows nothing about it."""

from .terminal import render, to_markdown

__all__ = ["render", "to_markdown"]
