---
description: Show or change how much Claude delegates to workers (off, manual, auto, prefer)
argument-hint: [off|manual|auto|prefer|inherit] [repo|global]
---
If no arguments are given, call the mode tool without arguments and show me compactly: the current mode for this repo and where it comes from, the global default, and what each mode means.

Otherwise call the mode tool with the mode and scope I gave. The scope defaults to this repo; "inherit" removes this repo's setting, or with "global" goes back to the built-in default (auto). Confirm the change in one line.

$ARGUMENTS

The mode applies from now on, in this session too: follow it.
