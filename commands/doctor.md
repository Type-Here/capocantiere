---
description: Check what worker isolation and credentials need on this system
---
Call the workers tool with doctor=true and show me the result compactly:
- the system and, if something is missing, the exact install command it suggests. Do NOT run it: commands that need sudo are for me to run. Offer to run a command only if it needs no sudo, and only after I say yes;
- for each worker: installed, credentials (host/token) and whether they work, isolation and its limits;
- the suggestions (turning isolation on, switching to "token" credentials), with the config lines to change.
