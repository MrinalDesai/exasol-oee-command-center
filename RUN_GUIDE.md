<div align="center">

<img src="docs/images/logo.svg" alt="OEE Command Center" width="420">

## Run guide · Windows

**From a clean laptop to the demo page, every command in order**

[![README](https://img.shields.io/badge/README-overview-0B5FFF?style=for-the-badge)](README.md)
[![Problem statement](https://img.shields.io/badge/Problem_statement-read-0B5FFF?style=for-the-badge)](PROBLEM_STATEMENT.md)
[![Run guide](https://img.shields.io/badge/Run_guide-Windows-0B5FFF?style=for-the-badge&logo=windows&logoColor=white)](RUN_GUIDE.md)

</div>

---

> [!IMPORTANT]
> Every command here is for **Windows PowerShell**. Paste **one box at a time**,
> press Enter, and check the **You should see** line before going on.
> Time needed the first time: **about 30 minutes**, most of it downloads.
> macOS and Linux: see the [Quick start](README.md#quick-start).

## Before you start: the two folders

Everything happens in two folders. Most mistakes come from running a command in
the wrong one, so every step below says which folder it needs.

| Name in this guide | Full path | What runs there |
|---|---|---|
| **Exasol folder** | `C:\Users\<you>\Downloads\exasol` | `exasol` commands (start, stop, info) |
| **Project folder** | `C:\Users\<you>\Downloads\exasol\exasol-oee-command-center` | `python` and `git` commands |

`<you>` is your Windows user name. You never type it: the commands use `$HOME`,
which PowerShell fills in for you.

**How to tell which folder you are in:** look at the text just before the
cursor. In the project folder it ends in `exasol-oee-command-center>`. In the
Exasol folder it ends in `\exasol>`.

**How to open PowerShell:** press the Windows key, type `powershell`, press Enter.
Every new PowerShell window starts in `C:\Users\<you>`, so always run the `cd`
line given in the step.

---

## Part A · Install (once)

### A1. Install Python and Git

| Program | Download | During install |
|---|---|---|
| Python 3.10 or newer | https://www.python.org/downloads/ | Tick **Add python.exe to PATH** on the first screen |
| Git | https://git-scm.com/download/win | Keep all the default choices |

Close any open PowerShell windows after installing, then open a new one, so it
picks up the new programs. Check both:

```powershell
python --version
git --version
```

**You should see:** `Python 3.1x.x` and `git version 2.x`.
**If `git` is not recognized:** run `$env:Path += ";C:\Program Files\Git\cmd"` in
that window and try again.

### A2. Download the Exasol launcher

1. Open https://downloads.exasol.com/exasol-personal in your browser.
2. Download the **Windows** launcher, `exasol.exe`.
3. In File Explorer, open your **Downloads** folder, create a new folder named
   `exasol`, and move `exasol.exe` into it.

Check it is in the right place:

```powershell
Test-Path "$HOME\Downloads\exasol\exasol.exe"
```

**You should see:** `True`. If it prints `False`, the file is not in
`Downloads\exasol` yet.

### A3. Install Exasol Personal

**Folder:** Exasol folder.

```powershell
$env:Path += ";$HOME\Downloads\exasol"
cd "$HOME\Downloads\exasol"
exasol version
exasol install local
```

This downloads and starts the database. It takes several minutes the first time.
Exasol Personal runs in a container; if the launcher asks to install **Podman**,
say yes and follow its prompts, then run `exasol install local` again.

Then add the Python runtime the models need inside the database, and check:

```powershell
exasol slc install python3
exasol info
```

**You should see:** `exasol info` prints the connection details, including an
address such as `localhost:8563`. Write that address down.

> [!NOTE]
> The first line, `$env:Path += …`, lets this window find `exasol`. It only lasts
> until you close the window, so it is repeated wherever `exasol` is used.

### A4. Download the project

**Folder:** Exasol folder (the project goes inside it).

```powershell
cd "$HOME\Downloads\exasol"
git clone https://github.com/MrinalDesai/exasol-oee-command-center
cd exasol-oee-command-center
```

**You should see:** the prompt now ends in `exasol-oee-command-center>`.
You are in the project folder for every step from here to the end of Part B.

### A5. Create the Python environment

**Folder:** project folder.

```powershell
cd "$HOME\Downloads\exasol\exasol-oee-command-center"
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**You should see:** `(.venv)` at the start of the prompt.

**If it says `running scripts is disabled on this system`:** run this once, then
run the `Activate.ps1` line again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Now install the libraries (2–5 minutes):

```powershell
pip install -r requirements.txt
```

**You should see:** it ends with `Successfully installed …`.

### A6. Create the settings file

**Folder:** project folder.

```powershell
Copy-Item .env.example .env
notepad .env
```

Notepad opens. Set `EXA_DSN` to the address from `exasol info` (usually
`localhost:8563`). Leave `EXA_USER=sys`, `EXA_PASSWORD=exasol` and
`EXA_TLS_VERIFY=0` as they are. Save and close Notepad.

---

## Part B · Build and run (once, about 5 minutes)

All of Part B runs in the **project folder**, with `(.venv)` showing. If you
opened a new window, first run:

```powershell
cd "$HOME\Downloads\exasol\exasol-oee-command-center"
.venv\Scripts\Activate.ps1
```

### B1. Run the tests (no database needed)

```powershell
python -m pytest -q
```

**You should see:** `101 passed` at the end.

### B2. Build everything in Exasol

Exasol must be running (Part A3, or step C1 after a restart). Then:

```powershell
python -m oee.cli all
```

It runs, in order: `preflight` (checks this Exasol supports every feature
used), `generate` (makes the data), `setup` (loads and combines it), `train`,
`deploy` (puts the models into Exasol) and `status`.

**You should see:** headings `### preflight`, `### generate`, `### setup`,
`### train`, `### deploy`, `### status`, and at the end tables of plant OEE and
machines at risk, with no `Traceback`. About 2–5 minutes.

### B3. Optional: fleet scale

```powershell
python -m oee.cli pillars
python -m oee.cli bench --machines 20 --days 30
```

The second line is a quick check (1–2 minutes). If it finishes without a
`Traceback`, run the full size (~14 M one-minute readings, about 1 GB; needs
about 2 GB of free disk; 10–30 minutes):

```powershell
python -m oee.cli bench
```

After the full run, step 3 on the demo page searches the 14 M readings instead
of the demo plant.

### B4. Start the web app

```powershell
python -m oee.cli serve
```

**You should see:** `OEE Command Center on http://127.0.0.1:8000  (Ctrl+C to stop)`.

Open **http://127.0.0.1:8000/demo** in your browser. Keep this PowerShell
window open while you use the app; closing it stops the app. To stop on purpose,
click in the window and press `Ctrl+C`.

---

## Part C · Every time after a restart or shutdown

The data stays in Exasol, so nothing is rebuilt. You only start the database,
then the app. Use a new PowerShell window.

### C1. Start the database

**Folder:** Exasol folder.

```powershell
$env:Path += ";$HOME\Downloads\exasol"
cd "$HOME\Downloads\exasol"
exasol start
exasol status
```

**You should see:** `exasol status` reports the deployment as running. The first
start after a reboot can take 1–2 minutes; run `exasol status` again until it
does. Do not go on before that. (`exasol info` prints the connection details.)

**If `exasol start` fails:** the container engine is not running. Run
`podman machine start`, wait for it to finish, then `exasol start` again.

### C2. Start the web app

**Folder:** project folder.

```powershell
cd "$HOME\Downloads\exasol\exasol-oee-command-center"
.venv\Scripts\Activate.ps1
python -m oee.cli serve
```

**You should see:** the prompt starts with `(.venv)` and ends in
`exasol-oee-command-center>`, then `OEE Command Center on http://127.0.0.1:8000`.
Open http://127.0.0.1:8000/demo.

### C3. When you are done

Press `Ctrl+C` in the app window. To stop the database too:

```powershell
$env:Path += ";$HOME\Downloads\exasol"
cd "$HOME\Downloads\exasol"
exasol stop
```

---

## Using the demo page

Nothing is shown until you press a button. Go from top to bottom:

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

---

## If something goes wrong

| What you see | What it means | Fix |
|---|---|---|
| `exasol : The term 'exasol' is not recognized` | This window cannot find the launcher | Run `$env:Path += ";$HOME\Downloads\exasol"` |
| `git : The term 'git' is not recognized` | Git is not on the path | Run `$env:Path += ";C:\Program Files\Git\cmd"` |
| `No module named 'oee'` | Wrong folder | Run `cd "$HOME\Downloads\exasol\exasol-oee-command-center"` |
| `No module named 'pyexasol'` (or any library) | The environment is not active | Run `.venv\Scripts\Activate.ps1` in the project folder |
| `running scripts is disabled on this system` | Windows blocks the activate script | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once |
| `Could not connect to Exasol` … `actively refused it` | Exasol is not running | Do step C1 and wait until `exasol status` shows it running |
| Still cannot connect | Need more detail | Run `exasol diag local` (Exasol folder): it shows ports and whether the database is ready |
| `exasol start` fails | The container engine is not running | Run `podman machine start`, then `exasol start` |
| Connects, but login fails | Wrong settings | Check `EXA_DSN`, `EXA_USER`, `EXA_PASSWORD` in `.env` against `exasol info` |
| UDF or script language error during `deploy` | The Python runtime is missing in Exasol | Run `exasol slc install python3` (Exasol folder), then `python -m oee.cli deploy` (project folder) |
| A button on the page does nothing | The browser kept an old copy | Press `Ctrl+F5` |
| `Address already in use` on `serve` | The app is already running in another window | Use that window, or close it and run `serve` again |
