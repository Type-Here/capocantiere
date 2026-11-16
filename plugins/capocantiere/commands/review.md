---
description: Review all completed Gemini jobs and apply or discard them
---
List the jobs with gemini_jobs. For each one in the done state: open its details, review the diff against the task (for large diffs, delegate a first-pass review to the gemini-reviewer subagent), then apply it with gemini_apply or discard it with gemini_discard. After applying, run tests/build. Summarize the decisions you made.
