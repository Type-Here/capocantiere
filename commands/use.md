---
description: Choose the default worker for this session (copilot, vibe, agy, or auto)
argument-hint: [worker|auto]
---
If I gave no argument, call the workers tool and show me compactly which workers can be used right now (enabled, installed, authenticated, budget left) and which one is the session default; then ask which one I want.

Otherwise call the workers tool with use set to what I gave ("auto" goes back to the configured preference order) and confirm in one line, mentioning any warning in the result.

$ARGUMENTS

The choice lasts for this session only and the automatic fallback still applies when that worker runs out of quota or budget. To change it permanently, edit "order" in ~/.config/capocantiere/workers.json.
