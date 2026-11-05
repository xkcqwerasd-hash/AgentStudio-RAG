import os
import re

PATTERNS = [
    ("Generic API Key", re.compile(r"sk-[a-zA-Z0-9]{24,}")),
    ("Generic Hex Secret", re.compile(r"[0-9a-f]{64}")),
]

EXCLUDE_DIRS = {
    ".venv", "node_modules", ".git", "data", ".gemini", "backups", "dist",
    "__pycache__", ".pytest_cache", ".idea", ".vscode"
}

findings = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for file in files:
        if file.startswith(".env"):
            continue
        if file.endswith((".py", ".ts", ".tsx", ".json", ".md", ".sh", ".ps1", ".yml", ".yaml", ".conf", ".bat")):
            path = os.path.join(root, file)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    for name, pat in PATTERNS:
                        matches = pat.findall(content)
                        if matches:
                            findings.append((path, name, len(matches)))
            except Exception:
                pass

print("=== Codebase Secrets Scan Results ===")
for path, name, count in findings:
    print(f"  [ALERT] {name} found in {path} ({count} occurrences)")

if not findings:
    print("  [PASS] Zero exposed secrets found in source code, scripts, docs, and configs!")
