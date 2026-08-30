"""GigHound stealth-browser worker (AD-4).

Polls the backend for pending StealthTasks, claims them atomically, executes
them in a Playwright Chromium browser with per-(platform, user) persistent
sessions, and posts results back. Never bypasses HITL: it only executes
tasks the backend created from approved queue items.
"""

__version__ = "0.1.0"
