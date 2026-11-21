---
description: Review all completed worker jobs and apply or discard them
---
List the jobs with the jobs tool. For each one in the done state: open its details, review the diff against the task (for large diffs, delegate a first-pass review to the worker-reviewer subagent), then apply it with apply or discard it with discard. After applying, run tests/build. Summarize the decisions you made, naming the worker that ran each job.
