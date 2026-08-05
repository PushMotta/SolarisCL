# Installing hsl — Solaris Render Launcher

Load a Houdini `.hip`, see what its LOP network is set up to render, and render
it — from a window or the command line.

This guide is for **running** hsl. If you are working on its source, read
`README.md` and `AGENTS.md` instead.

---

## 1. What you need first

| | Required? | Why |
|---|---|---|
| **Houdini** | **Yes** | hsl does not render anything itself. It drives Houdini's `hython` and `husk`. Without a Houdini install there is nothing for it to launch. |
| **Python 3.9 or newer** | Only for the **slim** zip | The `…-win64-full.zip` ships its own Python — nothing to install. The slim zip needs a normal Python from python.org — *not* the one inside Houdini. |
| **PySide6** | Only for the window, slim zip only | Already inside the full zip. The command line has no dependencies at all. |

Houdini's own Python is not used to run the launcher; hsl starts `hython` as a
separate process when it needs to read or render a scene. That separation is
deliberate — it means the window stays responsive while a heavy `.hip` loads.

hsl has been probed against **Houdini 21.0.729 and 22.0.368**. Older builds
generally work — parameter names are looked up from a list of known spellings
rather than assumed — but see "Check it actually works" below.

---

## 2. Install

**Windows, standalone (recommended for artists): `hsl-<version>-win64-full.zip`**

1. Unzip it anywhere you like, e.g. `C:\Tools\hsl`. Keep the folder together.
2. Double-click **`launch_ui.bat`**. That is the whole install.

The full zip carries its own Python and Qt in a `python\` folder inside — it
touches nothing on the machine, needs no admin rights, and cannot conflict
with any other Python that is or is not installed. Only Houdini itself must
already be there. (The bundled runtime always wins; delete the `python\`
folder if you specifically want the launcher to use your own Python.)

**Windows, slim: `hsl-<version>.zip`** — when you already have Python and
would rather share it:

1. Unzip `hsl-<version>.zip` anywhere you like, e.g. `C:\Tools\hsl`.
   Keep the folder together — `launch_ui.bat` expects `hsl\` beside it.
2. Install Qt for the window:

   ```
   python -m pip install PySide6
   ```

   (`python -m pip`, not bare `pip` — Windows often leaves `pip.exe` off
   `PATH` even when `python` is on it.) Skip this if you only want the
   command line.
3. Double-click **`launch_ui.bat`**.

**Linux / macOS**

```bash
unzip hsl-<version>.zip && cd hsl-<version>
python -m pip install PySide6  # window only
./bin/hsl ui                   # or: ./bin/hsl inspect scene.hip
```

There is nothing to compile and nothing is written outside the folder except a
small settings file in your user profile and scratch files under your temp
directory.

---

## 3. Point it at Houdini

hsl looks for `hython` in this order, and stops at the first hit:

1. `--hython <path>`, if you pass it on the command line
2. the install you picked with `hython --set` (see below) — **this outranks the
   environment variable**, so if `$HSL_HYTHON` seems to be ignored, a saved
   choice is why
3. `$HSL_HYTHON`
4. otherwise, the newest of everything it can find: `$HFS/bin`, then `PATH`,
   then the standard install locations for your platform

`husk` is found the same way, via `$HSL_HUSK` and `$HFS/bin`.

Usually that finds it with no configuration. To see what it found:

```
bin\hsl.bat hython                 # lists every install; * marks the one in use
bin\hsl.bat hython --set 2         # remember install #2 as the default
```

The window has the same list as a dropdown with a **Rescan** button.

To force a specific one:

```
set HSL_HYTHON=C:\Program Files\Side Effects Software\Houdini 20.5.487\bin\hython.exe
set HSL_HUSK=C:\Program Files\Side Effects Software\Houdini 20.5.487\bin\husk.exe
```

If `python` is not on your `PATH`, or you want hsl to use a particular
interpreter, set `HSL_PYTHON` to its full path — `bin\hsl.bat` reads it.

---

## 4. Running it

**The window**

```
launch_ui.bat
```

Or `bin\hsl.bat ui`, optionally with a scene to open: `bin\hsl.bat ui shot.hip`.

**The command line**

```
REM What is this scene set up to render?
bin\hsl.bat inspect C:\jobs\shot\shot_v012.hip

REM Show the commands without running anything
bin\hsl.bat render shot.hip --frames 1001-1100 --chunk 10 --dry-run

REM Render, four processes at a time
bin\hsl.bat render shot.hip --frames 1001-1100 --chunk 10 --parallel 4

REM Single frame at quarter res, to check
bin\hsl.bat render shot.hip --frames 1050 --res 960 540
```

`bin\hsl.bat --help` and `bin\hsl.bat render --help` list everything.

**Caches and simulations**

hsl also cooks the non-rendering parts of a scene without opening Houdini —
File Cache SOPs, Geometry/Alembic/DOP ROPs and the other `/out` contexts.

```
REM What can be cooked? (inspect lists these alongside the render ROPs)
bin\hsl.bat inspect shot.hip

REM Cook every cache and simulation, in dependency order
bin\hsl.bat cook shot.hip

REM Just one, and show the command first
bin\hsl.bat cook shot.hip --task /obj/geo1/filecache1 --dry-run
```

Order is taken from the scene, so a cache that feeds a sim runs first. Anything
that carries state between frames — simulations, and File Cache SOPs with
"Cache Simulation" on — is cooked in a single ordered process; `--chunk` is
deliberately ignored for those, because splitting them would write a wrong
cache without reporting an error.

### Two render engines

`--engine hython` is the **default**: it renders the ROP directly inside
hython and writes no USD to disk. It is the right choice for local renders and
is *required* for volume-heavy shots, where a USD export would bake live SOP
volumes at tens of GB per frame.

`--engine husk` exports USD first, then runs husk on it. You need it for farm
submission and for anything that edits the exported stage — `--aovs` and
`--relink-from`. Those options are rejected rather than ignored if you ask for
them under hython.

---

## 5. Check it actually works

Run these in order. Each one tells you something different is wrong.

```
bin\hsl.bat hython                     REM 1. Is Houdini found?
bin\hsl.bat inspect shot.hip           REM 2. Can it read a scene?
bin\hsl.bat render shot.hip --frames 1 --dry-run   REM 3. Does the command look right?
```

On step 2, check the reported **resolution, camera and AOVs** against what
Houdini shows you for a scene you know well. hsl reads the composed USD stage
rather than trusting the ROP's parameters, and a scene authored in an unusual
way is the most likely thing to surprise it. Anything it could not work out is
reported as a warning rather than guessed at.

---

## 6. When something goes wrong

**`ModuleNotFoundError: No module named 'PySide6'`**
The window needs Qt: `python -m pip install PySide6`. The command line does
not.

**`'pip' is not recognized`**
Windows installs `pip.exe` into a `Scripts\` folder that is often not on
`PATH` even when `python` is. Run it as a module instead — `python -m pip
install PySide6` — or, if `python` is also missing, `py -m pip install
PySide6` (the `py` launcher is registered by the python.org installer
regardless of `PATH`). Houdini's `hython` has no pip and cannot stand in
here.

**`'python' is not recognized`**
Python is not on your `PATH`. Either reinstall it with "Add Python to PATH"
ticked, or set `HSL_PYTHON` to the full path of `python.exe`.

**No Houdini installs listed by `hsl hython`**
Set `HSL_HYTHON` and `HSL_HUSK` explicitly (section 3), or run hsl from a shell
where Houdini's environment has been sourced so `$HFS` is set.

**The window opens but reading a scene fails**
That is hython, not the window — hsl shells out to it. Check the messages
panel: a licensing failure, a missing `$HIP` reference, or an unreadable scene
all surface there. The inspector needs a real Houdini license for as long as it
takes to load and cook.

**Progress bars sit at zero**
Progress is read from Karma's Alfred-style output. Under the husk engine that
needs the `a` flag on `--verbose`, which hsl adds by default — if you have
overridden verbosity without it, the bars have nothing to read.

**Renders are slow or the machine grinds**
`--parallel` runs several renders on *one* box; they contend for RAM and cores
and each takes its own Karma license. Two or three is usually the most a
workstation wants. For real distribution, use `--dry-run` and hand the printed
commands to your scheduler.

---

## 7. Uninstalling

Delete the folder. Two things live outside it:

- the remembered Houdini choice —
  `%APPDATA%\hsl\settings.json` on Windows, `~/.config/hsl/settings.json`
  elsewhere
- scratch files (cached scene manifests, temporary USD overlays) in the `hsl`
  folder inside your system temp directory — `%TEMP%\hsl` on Windows

Both are safe to delete at any time; they are rebuilt on demand.
