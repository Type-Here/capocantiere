"""Shared by the fake sandbox tools: append one JSON line to $FAKE_LOG."""

import json
import os


def log(tool, argv, **extra):
    path = os.environ.get("FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict({"bin": tool, "argv": argv, "cwd": os.getcwd(), "env": {}}, **extra)) + "\n")
