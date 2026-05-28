# Skill: python-env-enforcer

## Purpose
Guarantee every Python invocation in this repo uses the correct interpreter,
encoding, and (where needed) OneDrive-safe paths. Many subtle bugs in this
project trace back to the wrong interpreter or to PowerShell mojibake.

## Triggers
- Any time you are about to run a Python script via `run_in_terminal`.
- Any time you are about to *write* a script that another tool will invoke.
- Any time the user shares an error involving `ModuleNotFoundError`,
  `UnicodeEncodeError`, or `PermissionError` on `.xlsx`.

## Rules (non-negotiable)

1. **Interpreter** — always `C:\Users\tbhbr\miniconda3\python.exe`. Never:
   - `python` (PATH lookup is unreliable here)
   - `python3`
   - `.venv\Scripts\python.exe` (the user disabled venv for this repo)
2. **Encoding** — always prepend `$env:PYTHONIOENCODING="utf-8"` in the same
   PowerShell command. Multiline runs must set it once at top.
3. **OneDrive xlsx** — if a script writes to a `*.xlsx` under the OneDrive
   project folder, expect intermittent `PermissionError`. Mitigation:
   - Copy source xlsx to `$env:TEMP`.
   - Run the script against the TEMP path.
   - Copy result back, retrying on PermissionError with exponential backoff.
4. **Scrape commands** — `scrape_hkjc.py` MUST be called with:
   `--horse-cache "$env:TEMP\horse_cache_hkjc.json" --no-cache`.

## Reference command template

```pwsh
$env:PYTHONIOENCODING="utf-8"; & 'C:\Users\tbhbr\miniconda3\python.exe' <script.py> <args> 2>&1 | Select-Object -Last 60
```

## Runtime helper
`agent_skills.build_python_cmd(script, args, *, tail=60)` returns the exact
PowerShell command string. The dashboard's **Agent Skills** page uses this
helper for every script-launch button.

## Failure modes to watch
- Streamlit child processes inherit the wrong env — pass `env=os.environ | {"PYTHONIOENCODING":"utf-8"}` to `subprocess.run`.
- Conda activate scripts are slow; never embed them in fast-path commands.
- Antivirus on OneDrive holds file handles for ~200 ms after write — that is
  the root cause of the xlsx PermissionError, not Streamlit itself.
