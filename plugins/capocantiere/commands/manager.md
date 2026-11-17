---
description: Turn on manager mode for the rest of the session (automatic delegation to Gemini)
---
From now on, for the whole session, work in manager mode following the gemini-delegation skill:
for every request, actively look for what can be delegated to Gemini (gemini_delegate for writing code, gemini_analyze for exploring) and keep for yourself design, delicate parts, review and verification.
This lasts for this session only and does not override delegation mode "off" (use /capocantiere:mode for a lasting change). Instructions in CLAUDE.md still apply, and you can still use Claude subagents for work that needs more judgment than Gemini gives.
Don't ask me to confirm each delegation; just tell me in one line what you delegated. Check gemini_usage now and tell me the available budget.
