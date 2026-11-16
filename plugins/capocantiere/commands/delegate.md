---
description: Plan the work, delegate the mechanical parts to Gemini, then review and apply
argument-hint: <description of the work>
---
Use the gemini-delegation skill. Act as the manager for this work:

$ARGUMENTS

1. If you need to understand the code, use gemini_analyze first.
2. Show me a short plan: which tasks you delegate to Gemini (in parallel where possible) and which you keep for yourself.
3. Launch the jobs with gemini_delegate and work on your own part in the meantime.
4. Collect the results, review every diff, apply or discard, and verify everything with tests/build.
5. Finish with a summary: what Gemini did, what you did, what you fixed.
