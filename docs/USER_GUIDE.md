# AppShadow user guide

A complete walkthrough, written for someone using this for the first time. No
prior experience with Python or accessibility tooling is assumed. If you can open
a terminal and copy a command, you can follow this.

## Contents

1. [What AppShadow actually does](#1-what-appshadow-actually-does)
2. [What you need before you start](#2-what-you-need-before-you-start)
3. [Installing it](#3-installing-it)
4. [Your first recording, step by step](#4-your-first-recording-step-by-step)
5. [What you get at the end](#5-what-you-get-at-the-end)
6. [Handing the map to an AI](#6-handing-the-map-to-an-ai)
7. [Every button and hotkey explained](#7-every-button-and-hotkey-explained)
8. [Reading the coverage report](#8-reading-the-coverage-report)
9. [What gets saved and what never does](#9-what-gets-saved-and-what-never-does)
10. [When something goes wrong](#10-when-something-goes-wrong)
11. [Running it without the window](#11-running-it-without-the-window)
12. [Frequently asked questions](#12-frequently-asked-questions)

---

## 1. What AppShadow actually does

Imagine you have an old Windows program and you need to explain its entire
interface to somebody else. Every menu, every dialog box, every button, and how
you get from one screen to the next. Doing that by hand means taking dozens of
screenshots and typing out a description of each one.

AppShadow does that typing for you. You open the program and use it normally.
AppShadow sits quietly in the corner and watches. Every time you open a menu or a
dialog appears, it quietly writes down what is on the screen: the name of every
button, where it sits, whether it is greyed out, and what keyboard shortcut it
has. When you press Stop, it hands you one file that describes the whole program.

The important part is the phrase **you use it normally**. AppShadow never clicks
anything itself. It cannot accidentally delete your work or send an email,
because it never touches your program. It only watches.

### Why not just take screenshots?

Screenshots are pictures. A computer has to guess what is in them. AppShadow asks
Windows directly, through the same system that screen readers use for blind
users, so it gets the real text and the real structure instead of guessing.

It also records **how screens connect**. A pile of screenshots tells you what
screens exist. AppShadow tells you that the Export dialog appears when you click
Save As inside the File menu. That difference is what lets an AI rebuild the
program's navigation, not just its appearance.

---

## 2. What you need before you start

| You need | Why | How to check |
|---|---|---|
| Windows 10 or 11 | AppShadow uses a Windows-only accessibility system | You are on it now |
| Python 3.11 or newer | The tool is written in Python | Open a terminal and run `python --version` |
| The program you want to map | Obviously | Make sure it opens normally first |

If `python --version` says something like `Python 3.11.5` or higher, you are
ready. If it says the command is not recognised, install Python from
[python.org](https://www.python.org/downloads/) and **tick the box that says
"Add Python to PATH"** during setup. That box matters, and it is easy to miss.

---

## 3. Installing it

Open a terminal in the AppShadow folder. On Windows, the easiest way is to open
the folder in File Explorer, click the address bar, type `cmd`, and press Enter.

Run these three commands one at a time, waiting for each to finish.

**First**, create a private workspace for the tool so it does not interfere with
any other Python programs on your machine:

```bash
python -m venv .venv
```

**Second**, install the four small libraries AppShadow depends on:

```bash
.venv\Scripts\pip install -r requirements.txt
```

This downloads a few megabytes and takes a minute or two. When it prints
`Successfully installed`, it worked.

**Third**, check that everything is healthy:

```bash
.venv\Scripts\python tests\test_core.py
```

You should see a list of test names followed by something like `24/24 passed`. If
you see that, the installation is good.

> **What is that `.venv` thing?** It is a private folder holding this tool's
> libraries. It keeps AppShadow's dependencies separate from everything else on
> your computer, so nothing breaks. You never need to look inside it. The only
> thing to remember is that you run the tool with `.venv\Scripts\python` rather
> than plain `python`.

---

## 4. Your first recording, step by step

Let us map Windows Notepad, because it is small and harmless. Once this works,
the same five steps work for any program.

### Step 1: Open the program you want to map

Open Notepad first, before starting AppShadow. AppShadow needs something to
attach itself to.

### Step 2: Start AppShadow

```bash
.venv\Scripts\python main.py
```

A small dark window appears in the top right corner of your screen. It stays on
top of other windows on purpose, so it does not get lost behind the program you
are mapping. You can drag it anywhere by its title bar.

### Step 3: Tell it which program to watch

Find the button labelled **Pick app**. Press and hold your left mouse button on
it, then, still holding, drag the mouse over the Notepad window and let go.

As you drag, the box on the left updates to show whichever program is under your
mouse pointer. When you release over Notepad it should read something like
`notepad.exe (pid 16536)`.

> **What is a "pid"?** Short for process ID. It is just a number Windows uses to
> tell running programs apart. If you have two Notepad windows open, the pid says
> which one AppShadow is watching. You do not need to do anything with it.

### Step 4: Press Start, then use the program

Click **Start**. The dot in AppShadow's title bar turns red and starts blinking.
That means it is watching.

Now go and use Notepad exactly as you normally would. Open the File menu. Look at
Edit. Open Format, then Font, so a dialog appears. Close it again. Wander through
the program the way you would if you were showing it to a colleague.

Each time AppShadow records a screen you will hear a soft tick, a small message
will slide up from the bottom right saying which screen was captured, and the
counter in the middle of the window will go up.

**Take your time.** AppShadow waits for the screen to settle before recording, so
moving slowly gives better results than rushing.

### Step 5: Press Stop

Click **Stop**. AppShadow writes everything to disk and the status line changes
to tell you how many screens it saved and where they went.

That is a complete session. You just mapped Notepad.

---

## 5. What you get at the end

Look inside the `sessions` folder next to `main.py`. You will find a new folder
named after the date and time, like `session_2026-08-24_1432`. Inside it:

```
session_2026-08-24_1432/
   app_map.json      the main file, this is the one you give to an AI
   app_graph.mmd     a diagram of how the program is organised
   graph.mmd         a diagram of the order screens were captured in
   screenshots/      one picture per screen
```

### app_map.json

This is the point of the whole exercise. It is a text file describing every
screen. One screen looks like this:

```json
{
  "state_id": "state_0004",
  "kind": "menu_open",
  "window_title": "Document - WordPad",
  "reached_by": { "action": "menu_open", "on": "MenuItem:File", "from": "state_0003" },
  "screenshot": "screenshots/state_0004.png",
  "window_rect": [120, 80, 1280, 720],
  "controls": [
    { "type": "Group", "name": "Save a copy of the document", "rect": [181, 4, 292, 288] },
    { "type": "MenuItem", "name": "Rich Text document", "rect": [181, 27, 292, 53],
      "depth": 1, "leads_to": null },
    { "type": "MenuItem", "name": "Plain text document", "rect": [181, 186, 292, 53],
      "depth": 1, "leads_to": null }
  ],
  "unexplored": ["MenuItem:Rich Text document"],
  "ts": "2026-08-24T14:32:10+05:00"
}
```

Reading that in plain English:

| Field | Means |
|---|---|
| `state_id` | A name for this screen so other screens can point at it |
| `kind` | What sort of screen it is: a window, an open menu, or a dialog box |
| `window_title` | The text in the program's title bar at the time |
| `reached_by` | How you got here. This one says you clicked File on screen `state_0003` |
| `window_rect` | Where the window itself sat on screen, as `[x, y, width, height]` |
| `controls` | Everything on the screen: buttons, menu items, text boxes |
| `rect` | The control's `[x, y, width, height]`, measured from the window's top left corner |
| `depth` | How deeply nested it is. `0` is top level, `1` is inside the thing above it, and so on |
| `leads_to` | Where clicking that control took you. `null` means you never clicked it |
| `unexplored` | A list of things you saw but never tried |

Some controls carry extra detail when the program provides it: `class_name` (the
underlying widget type), `help_text` (its tooltip), and `checked`, `expanded` or
`selected` for things like checkboxes, expanders and the currently active tab.

### Why depth matters

The controls are a flat list, but `depth` records the real nesting, so the
original structure can be rebuilt exactly. A list like this:

```
ToolBar "Clipboard"     depth 0
  SplitButton "Paste"   depth 1
  Button "Cut"          depth 1
  Button "Copy"         depth 1
ToolBar "Font"          depth 0
```

means Paste, Cut and Copy sit inside the Clipboard toolbar, and Font is a
separate toolbar beside it. That is the information an AI needs to reproduce a
layout in HTML or React, rather than guessing from a screenshot.

### app_graph.mmd and graph.mmd

Two pictures, written in a format called Mermaid. To view either one, open
[mermaid.live](https://mermaid.live) and paste the file contents in. GitHub also
draws them automatically if you paste the contents into a fenced code block
marked `mermaid`.

**`app_graph.mmd` is the one you usually want.** It shows how the *program* is
organised: the application at the left, each screen branching off it, and every
menu item drawn as a labelled arrow into whatever it opens. Items you never
opened still show up with a dashed outline, so you can see the parts of the
program that are still unmapped. A screen with a lot of untouched controls gets
summarised as "+31 more not opened" to keep the picture readable; the full list
is always in `app_map.json`.

**`graph.mmd` is the recording itself**, in the order screens were captured:
state_0001, then state_0002, and so on. It is useful for checking that a session
went the way you expected, and less useful for understanding the software.

### screenshots/

One image per screen, cropped to just the program window. These are your safety
net for anything the automatic reading missed, which mostly means custom-drawn
toolbars that do not report themselves to Windows properly.

---

## 6. Handing the map to an AI

This is what you built the map for. Open `app_map.json`, copy everything in it,
and paste it into Claude or whichever assistant you use, with a question after it.

Things that work well:

> Here is a JSON map of a Windows application. What are all the export options
> and how does a user reach them?

> Using this map, rebuild the File menu as a React component.

> Read this map and list every dialog box, with the menu path to open each one.

The map is designed so the assistant can answer without ever seeing a screenshot.
The `reached_by` field is what lets it work out navigation paths rather than
guessing from screen names.

### Give it the file, not the folder

This one matters and is easy to get wrong. If you point an assistant at the whole
session folder, it will find `screenshots/` and start reading the images, which is
slow and burns an enormous number of tokens for information that is already
sitting in the JSON in a form it can read a hundred times more cheaply.

Hand over `app_map.json` on its own. Add `app_graph.mmd` if you want the
assistant to reason about how screens connect. Keep the screenshots back and
bring them out only for a screen the map flags with `"needs_vision": true`, which
is AppShadow telling you that particular screen was drawn in a way Windows could
not describe and the picture really is the only source.

If your map is very large, you do not have to paste the whole thing. The
`core/exporter.py` module can split a map into one file per menu, so you only hand
over the part that matters for the question you are asking.

---

## 7. Every button and hotkey explained

| Control | What it does |
|---|---|
| **Pick app** | Hold the mouse down on it, drag onto any window, release. That window's program becomes the target |
| **Start** | Begins watching. Start and Pick app grey out so you cannot change targets mid recording |
| **Capture** | Records the current screen immediately, without waiting for AppShadow to notice a change |
| **Stop** | Finishes the session and writes the files |
| The blinking dot | Red and blinking means recording. Grey means idle |
| The counter | Running total of screens, menus and dialogs captured so far |
| **Last event** | What was captured most recently, so you can tell it is keeping up |

Two keyboard shortcuts work even while the program you are mapping has focus, so
you do not have to click back onto the AppShadow window:

| Shortcut | Does |
|---|---|
| `Ctrl+Shift+C` | Same as pressing Capture |
| `Ctrl+Shift+X` | Same as pressing Stop |

### When to use Capture

Most of the time AppShadow notices screen changes by itself. Some programs draw
their own toolbars in a way Windows cannot describe, and those changes go
unnoticed. If you are looking at something on screen and the counter did not go
up, press `Ctrl+Shift+C`. It forces a recording of whatever is in front of you
right now, and saves a screenshot so nothing is lost.

---

## 8. Reading the coverage report

When you press Stop, AppShadow prints something like this:

```
Captured 6 states (1 dialog, 5 window)
Controls seen: 221 · unexplored: 121
Revisit these to finish the map:
  state_0001 (Document - WordPad): 45 unexplored, e.g. Button:Save
  state_0004 (window): 5 unexplored, e.g. MenuItem:Rich Text document
```

**"Unexplored" means you saw a button but never clicked it.** AppShadow knows the
button exists, but it does not know what happens when you press it, so that part
of the map has a hole in it.

This is a to-do list, not an error. It is telling you: if you want a complete map,
go back and visit those screens. The states with the biggest numbers are where a
second pass buys you the most.

Getting to zero means deliberately opening everything. That is normal and
expected for a thorough mapping session. Window controls like minimise and close
are deliberately not counted, because they belong to Windows rather than to the
program you are mapping, and because "finishing" your map by clicking Close would
end the recording.

---

## 9. What gets saved and what never does

This matters, so it is worth being precise.

**AppShadow never records anything you type.** Text boxes, password fields and
document bodies are written down as *existing*, with a marker saying the contents
were deliberately left out. The map will say "there is a filename box here". It
will never say what you typed into it.

This holds for the screenshots too. Since a screenshot is a pixel perfect copy of
your screen, the areas covered by those fields are painted out black before the
image is saved. The menus and buttons around them stay perfectly visible, so the
picture is still useful.

This was tested by typing a distinctive password into a live session and then
searching every output file for it. It appears in none of them.

**Two things to be aware of anyway.** Menu labels are recorded as they appear, so
a Recent Files list will include your document names, because that is part of the
program's interface rather than something you typed during the session. And
AppShadow captures only the window you targeted, so anything else on your screen
is never in the pictures.

---

## 10. When something goes wrong

### "python is not recognised"

Python is either not installed or was installed without being added to PATH.
Reinstall from python.org and tick "Add Python to PATH".

### The Pick app button will not select my program

Remember it is a drag, not a click. Press and hold on the button, move the mouse
over the target window while still holding, and only then release.

### The counter is not going up while I use the program

Some programs draw their interface in a custom way that Windows cannot see into.
Press `Ctrl+Shift+C` to force a capture. Those screens are marked in the map so
you know their screenshots need a closer look.

### Modern Windows apps (Calculator, Settings, Store apps)

These apps run inside a shared Windows frame, so always pick them with the
crosshair (or `--title-re` from the terminal) rather than `--pid`: the frame's
process owns every modern app on the desktop, and only the window you picked
says which one you mean. They also redraw their content without telling Windows,
so AppShadow captures after each mouse click inside them. Changes you make with
the keyboard alone (typing digits into Calculator) are not captured
automatically; press `Ctrl+Shift+C` if you need one.

### The program I want to map shows a splash or licence screen that will not go away

Just click past it yourself before pressing Start. This is exactly why AppShadow
is driven by a person instead of running automatically: splash screens, licence
checks and login prompts stop automated tools cold, and a human gets through them
without thinking about it.

### The hotkeys do nothing

If the program you are mapping runs as administrator, Windows will not deliver
keyboard shortcuts to a normal program like AppShadow. Either run AppShadow as
administrator too, or use the Capture and Stop buttons instead.

### A menu closes before it gets recorded

Menus are recorded the instant they open, and the reading is scoped to just the
menu so it finishes in a few hundredths of a second. If one still slips past, open
it again and hold it open a moment longer.

---

## 11. Running it without the window

If you would rather not use the small window, you can record from the terminal.
This records whatever window has "Notepad" in its title for sixty seconds:

```bash
.venv\Scripts\python recorder.py --title-re ".*Notepad.*" --seconds 60
```

You can also target a program by its process ID with `--pid 16536`, or have
AppShadow launch it for you with `--launch "C:\Path\To\Program.exe"`
(`--launch calc.exe` works too, even though `calc.exe` only starts the real
Calculator and exits). The session is named after the program's executable;
pass `--app "Name"` to call it something else.

There is also a way to look at a single window right now, without recording a
session at all. This prints the structure of the window as JSON and tells you how
long it took to read:

```bash
.venv\Scripts\python -m scrapers.windows_uia --title-re ".*Notepad.*"
```

That is the quickest way to find out how much of a given program AppShadow can
actually see before you commit to a full session.

---

## 12. Frequently asked questions

**Will it click things in my program or change my files?**
No. AppShadow only reads. It has no code that clicks, types or modifies anything
in the program you are mapping.

**Does it work on web pages?**
Not yet. It is built for regular Windows desktop programs. The code is arranged so
that support for browsers, macOS and Android can be added later without changing
how the map itself works.

**Does it record the whole screen?**
No, only the window you picked. Everything else you have open stays private.

**How long should a session be?**
As long as it takes to visit the screens you care about. Ten minutes of unhurried
clicking usually covers a medium sized program. The map is saved automatically
every sixty seconds while you record, so a crash does not lose your work.

**Can I add to a map later?**
Each session produces its own folder. To extend your coverage, record a second
session and give both maps to your assistant, or revisit the screens listed in the
coverage report during a single longer session.

**Something is missing from the map that I can clearly see on screen.**
That control is almost certainly drawn by the program itself rather than through
Windows, which makes it invisible to the accessibility system AppShadow reads. Its
screenshot is still saved, and the screen is flagged in the map so you know to
look at the picture. This is a known limit of every tool that reads interfaces
this way, not a bug in AppShadow.
