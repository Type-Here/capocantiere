---
description: Turn on manager mode for the rest of the session (automatic delegation to workers)
---
From now on, for the whole session, work in manager mode following the worker-delegation skill:
for every request, actively look for what can be delegated to a worker (delegate for writing code, analyze for exploring) and keep for yourself design, delicate parts, review and verification.
This lasts for this session only and does not override delegation mode "off" (use /capocantiere:mode for a lasting change). Instructions in CLAUDE.md still apply, and you can still use Claude subagents for work that needs more judgment than a worker gives.
Don't ask me to confirm each delegation; just tell me in one line what you delegated and to which worker. Call workers now and tell me, compactly, which workers are available and how much budget each has left.
