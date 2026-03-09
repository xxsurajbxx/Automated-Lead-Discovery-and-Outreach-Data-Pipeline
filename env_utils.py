from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path = ".env", override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.strip()
        i += 1

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        # Support multiline list-style values in .env such as:
        # KEY = [
        #   "a",
        #   "b"
        # ]
        if value == "[":
            collected = ["["]
            while i < len(lines):
                next_line = lines[i]
                i += 1
                stripped = next_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                collected.append(stripped)
                if stripped.endswith("]"):
                    break
            value = "\n".join(collected)

        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value
