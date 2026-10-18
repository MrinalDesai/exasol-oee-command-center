# Run guide (Windows)

Every command below is for **Windows PowerShell**, in the order we used them on a
clean laptop. Paste one block at a time. macOS and Linux users: see the Quick start
in [README.md](README.md).

Time needed: about 20 minutes the first time, most of it downloads.

## What you need

| Item | Where to get it |
|---|---|
| Windows 10 or 11, 16 GB RAM recommended | |
| Python 3.10 or newer | https://www.python.org/downloads/ (tick **Add python.exe to PATH**) |
| Git | https://git-scm.com/download/win |
| Exasol Personal launcher (`exasol.exe`) | https://downloads.exasol.com/exasol-personal |

Exasol Personal runs in a container on Windows. If the launcher asks for Podman,
follow its prompt to install it.

---

## Part A · Downloads (once)

**A1.** Download `exasol.exe` from https://downloads.exasol.com/exasol-personal
and save it into a new folder `Downloads\exasol`.

**A2.** Open PowerShell and check it is there:

```powershell
Test-Path "$HOME\Downloads\exasol\exasol.exe"
```

It must print `True`.

**A3.** Install Exasol Personal and the Python runtime for UDFs:

```powershell
$env:Path += ";$HOME\Downloads\exasol"
cd "$HOME\Downloads\exasol"
exasol install local
exasol slc install python3
exasol info
```

`exasol info` shows the connection address (DSN), for example `localhost:8563`.
Note it down.

**A4.** Get the project:

```powershell
$env:Path += ";C:\Program Files\Git\cmd"
cd "$HOME\Downloads\exasol"
git clone https://github.com/MrinalDesai/exasol-oee-command-center
cd exasol-oee-command-center
```

**A5.** Create the Python environment and install the libraries:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell says running scripts is disabled, run this once and then repeat
the `Activate.ps1` line:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**A6.** Create the settings file:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `EXA_DSN` to the address from `exasol info`. The defaults (`sys` / `exasol`,
`EXA_TLS_VERIFY=0`) work for a local Exasol Personal.

---

## Part B · Build and run (once, about 2 minutes)

**B1.** Run the offline tests (no database needed). Expect `101 passed`:

```powershell
python -m pytest -q
```

**B2.** Build everything in Exasol: checks, data, load, combine, train, deploy:

```powershell
python -m oee.cli all
```

It starts with `preflight`, which checks Exasol supports every SQL feature used.
If a check fails, it stops and says which one.

**B3.** Optional extras:

```powershell
python -m oee.cli pillars     # evidence numbers for the full console
python -m oee.cli bench       # fleet scale: ~14 M one-minute readings (takes a few minutes)
```

**B4.** Start the web app:

```powershell
python -m oee.cli serve
```

Open **http://127.0.0.1:8000/demo** in a browser. Leave PowerShell open while
you use it; press `Ctrl+C` there to stop.

---

## Part C · Every time after a restart

The data stays in Exasol. You only need to start the database and the web app:

```powershell
$env:Path += ";$HOME\Downloads\exasol"
cd "$HOME\Downloads\exasol"
exasol start
cd exasol-oee-command-center
.venv\Scripts\Activate.ps1
python -m oee.cli serve
```

Then open http://127.0.0.1:8000/demo.

---

## Using the demo page

Nothing is shown until you press a button. Go top to bottom:

| Step | Press | What you see |
|---|---|---|
| 0 | **Combine** | One machine as five systems see it, then lined up minute by minute. Steps 1–7 unlock. |
| 1 | a fault: **Bearing**, **Coolant**, **Overstrain** or **Drive** | |
| 2–3 | (runs by itself) | The fault's pattern across five streams, then a timed search of the whole history and how often the pattern came true |
| 4 | (runs by itself; **Replay** plays it again) | A machine the model never trained on, until the same pattern selects the fault |
| 5 | (runs by itself once the fault is selected) | Plant store first, then all four store systems, with wrong-location flags and time to site |
| 6 | **Run quality check** | Go / Check first / Stop for each machine's next shift |
| 7 | pick a part, **Search suppliers**, then **Deep scan** | Normal search finds nothing; deep scan reads all three supplier formats and builds the order line |

Each panel has a **show SQL** link with the exact queries and their run time.
The full console is at http://127.0.0.1:8000.

## If something goes wrong

| Message | Fix |
|---|---|
| `exasol : The term 'exasol' is not recognized` | Run `$env:Path += ";$HOME\Downloads\exasol"` in this window |
| `git : The term 'git' is not recognized` | Run `$env:Path += ";C:\Program Files\Git\cmd"` |
| Cannot connect to Exasol | Run `exasol start`, then `exasol info`, and check `EXA_DSN` in `.env` |
| A button does nothing | Press `Ctrl+F5` to reload the page |
| UDF or script language error during `deploy` | Run `exasol slc install python3`, then `python -m oee.cli deploy` |
