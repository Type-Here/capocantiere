---
description: Show the models of a worker and choose which ones to use (model groups for agy)
argument-hint: [worker] [what to change, e.g. "use strong for implementation" or "never use Claude models"]
---
Call the models tool (worker: the one I name, otherwise agy) with refresh=true and show me the result compactly.

For agy: the models grouped (fast, standard, strong, frontier, other), which model is used for analysis and which for implementation, and why. If I asked for a change, apply it with the models tool (analyze / implement / exclude) and show me the result. If I didn't ask for anything, briefly explain the options and ask whether I want to change something:
- a group (e.g. "strong") follows new models automatically; an exact slug pins that model; "auto" goes back to the default choice;
- exclude takes slug patterns such as "claude-*" to keep some models from ever being picked automatically.

For the other workers there is no model list: tell me which model is configured and that it can be changed with "model" / "analyze_model" for that worker in the config file (~/.config/capocantiere/workers.json).

$ARGUMENTS
