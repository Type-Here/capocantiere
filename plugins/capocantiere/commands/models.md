---
description: Show the Gemini models available on this account and choose which ones to use
argument-hint: [what to change, e.g. "use strong for implementation" or "never use Claude models"]
---
Call gemini_models with refresh=true and show me, compactly: the models grouped (fast, standard, strong, frontier, other), which model is used for analysis and which for implementation, and why.

If I asked for a change, apply it with gemini_models (analyze / implement / exclude) and show me the result:

$ARGUMENTS

If I didn't ask for anything, briefly explain the options and ask whether I want to change something:
- a group (e.g. "strong") follows new models automatically; an exact slug pins that model; "auto" goes back to the default choice;
- exclude takes slug patterns such as "claude-*" to keep some models from ever being picked automatically.
