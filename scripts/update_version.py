#!/usr/bin/env python3
"""Update version in pyproject.toml."""
import sys
import re

def main():
    if len(sys.argv) < 2:
        print("Usage: update_version.py <version>", file=sys.stderr)
        sys.exit(1)

    version = sys.argv[1]

    with open("pyproject.toml", "r", encoding="utf-8") as f:
        content = f.read()

    # Replace version line
    new_content = re.sub(
        r'^version = ".*?"',
        f'version = "{version}"',
        content,
        flags=re.MULTILINE
    )

    with open("pyproject.toml", "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Updated pyproject.toml to version={version}")

if __name__ == "__main__":
    main()
