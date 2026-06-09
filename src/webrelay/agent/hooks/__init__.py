"""Standalone Claude Code hook entry points.

Each module in this package is a separate script invoked by Claude
Code's hook system (see ``~/.claude/settings.json``). Keeping them
out of the main agent package lets the hook runner import only the
stdlib, which matches Claude Code's hook environment.
"""
