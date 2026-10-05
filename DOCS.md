# File Finder

A native Linux desktop app (Qt 6, so it fits Kubuntu and Plasma) that finds files and
folders anywhere on the computer by name and folder, plus the text inside your own files
and, for photos, the place and date they were taken. It forgives typos and learns from you.

## Install (Kubuntu)

    cd filefinder
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python run.py

If the venv step complains, run `sudo apt install python3-venv` first.
Optional, only if you want PDF text searchable: `sudo apt install poppler-utils`
and tick the PDF box in Settings. The olefile package in requirements.txt is only
needed for old .doc, .xls and .ppt files; everything else works without it.

If you used an earlier version, the index is rebuilt once on first start (it takes
the same time as a first scan). What you learned from clicks and searches is kept.

By default the app searches the whole computer. Settings (Ctrl+,) can switch that off and
limit it to folders you choose. The index is one file,
`~/.local/share/filefinder/index.db` (readable only by you). Settings live in
`~/.config/filefinder/config.json`.

Optional launcher entry, saved as `~/.local/share/applications/filefinder.desktop`
(fix the two paths):

    [Desktop Entry]
    Type=Application
    Name=File Finder
    Exec=/home/YOU/filefinder/.venv/bin/python /home/YOU/filefinder/run.py
    Icon=system-search
    Categories=Utility;

## Run it without Python or pip

Build once, then the app starts like any other program. The built copy carries its own
Python and Qt, so nothing needs to be installed on the machine that runs it.

    ./build.sh      (needs python3-venv on this machine, one time)
    ./install.sh    (copies it to ~/.local/opt/filefinder and adds a menu entry)

The built folder is about 150 MB. To use it on another computer, copy dist/filefinder
there and run the file named filefinder inside it. Build on the oldest Linux you want to
support, because a build runs on the same or newer systems.

## Ship it as an AppImage or a .deb

Run ./build.sh first (or let the scripts do it), then either of these:

    ./packaging/build-appimage.sh    makes dist/File_Finder-x86_64.AppImage
    ./packaging/build-deb.sh         makes dist/filefinder_0.0.1_amd64.deb

The AppImage is one file. Make it executable and double click it, or run it from a
terminal. On Ubuntu 24.04 it needs `sudo apt install libfuse2t64` once, or start it with
`--appimage-extract-and-run`. The .deb installs with `sudo apt install ./filefinder_0.0.1_amd64.deb`
and adds a menu entry and the `filefinder` command. Remove it with `sudo apt remove filefinder`.
The version number lives in the file VERSION, and the maintainer line is in packaging/build-deb.sh.

## What "the whole computer" means

Every file and folder you are allowed to see is found by name, system folders included
(for example /etc/hostname or /usr/share/doc). Three levels of care keep it fast and quiet:

* System-wide: found by name and folder.
* Your home folder and external drives (/media, /mnt): also read inside (text, photo date
  and place) and updated live within a second. You can change this list in Settings.
* System folders: refreshed by a quiet background rescan every 15 minutes, since they
  rarely change. The text inside system files is not read.

Never crawled: /proc, /sys, /dev, /run, snap images, caches, network drives (NFS, Samba,
sshfs) and other mounts that are not real disks. Hidden files stay hidden unless you
change that in Settings. Your own files rank above system files with an equal match, and
the box next to the search field switches between "System-wide" and "My folders only".
A machine with a quarter million files has an index of roughly 100 MB. On the test machine
here (84,000 files) the first scan took 5.5 seconds and the index was 51 MB.

## Things to type

    invoice                 words in names, folder names and text inside files
    invoce                  typos are fine
    tax 2025                every word must match somewhere
    photos from Berlin      type image, plus the word berlin (the photo's GPS place counts)
    paris july 2022         photos taken in Paris in July 2022, also with no name hints
    photos last month       uses the date the photo was taken, not the file date
    PDFs in Downloads       type PDF, only inside folders named Downloads
    PDF I downloaded last month
    large videos            video files over 50 MB
    over 10 MB, under 500 KB, small, yesterday, this week, last year, past 30 days

The line under the search box always says how your text was understood.
"last month" means the whole previous calendar month. For photos a date means the
date taken (from the camera), for every other file the last modified time.

## Opening files

Enter or double click opens the file the way the system normally would. That is a list kept
by Linux, not by this app, and it often points at LibreOffice for many file types (plain
text, Markdown, CSV and others) because LibreOffice claims them when it is installed.

You are in control here, inside this app:

* Right click, then "Open with..." (the only open entry in the menu), or press Ctrl+Shift+O: a list of the applications that
  can open that kind of file, best match first, with the system default marked.
* "Show all installed applications" lists everything installed, for the unusual cases.
* "Always use this choice for this file type" remembers your pick, so a plain Enter next time
  uses it. Choosing "Use the system default" with that box ticked removes the memory.
* Settings, "Always ask which application to open files with", shows the list every time.
* Settings, "Forget remembered applications", clears all remembered picks.

The list comes from the installed applications' own description files, so it matches what
is really on the computer. To change the system wide default for all programs instead, use
System Settings, Applications, File Associations.

## Keys

    Ctrl+L or Ctrl+K   search box        Enter / Down   go to results
    Enter              open              Ctrl+Return    open containing folder
    Ctrl+Shift+O       open with...
    Ctrl+Shift+C       copy path         F2             rename
    Shift+F2           move              Delete         move to trash
    Ctrl+N             nearby files      Esc            clear search
    Ctrl+,             settings          Ctrl+R         rescan now

## How the smart parts work

**Typos.** Character trigrams (SQLite FTS5 trigram index) list the words that share
3 letter pieces with what you typed. Damerau Levenshtein edit distance then keeps
words that are 1 or 2 slips away, where swapping two neighbouring letters counts as
one slip. Enough because names are short: trigrams cut 100,000+ words down to a few
hundred, and edit distance picks the right one in microseconds.

**Ranking.** BM25 from SQLite FTS5. A name match counts 10 times a text match and a
folder match 3 times. BM25 is the classic search formula (same family as TF IDF):
rare words matter more, and repeating a word has diminishing returns. Enough
because it is what most search engines used for decades, and it needs no training.

**Related words.** The app counts which words appear together in your own file names,
folder names and texts. It keeps pairs that appear together far more than chance
would give (normalized PMI) and that show up in mostly the same files (Jaccard
overlap). So "tax" finds "return" only if your files really pair them. It learns from
a sample of at most 30,000 files, so it stays light.

**Learning from clicks.** Every open, open folder or copy path adds 1 to a score for
each pair of (search word, file). Scores fade with a 60 day half life. This is a tiny
online learner: a few clicks already lift your usual file, and old habits fade by
themselves.

**Did you mean.** Words not found in your file names are replaced by the closest word
that is. Suggestions also come from your past searches and word completions, and
"Show nearby files" lists the other files in the same folder. Your own vocabulary
gives better spelling help than a generic dictionary.

**Natural language.** A short list of rules (regular expressions) for sizes, dates,
file types and "in Folder". These phrases are a small closed set, so rules are
predictable and easy to extend (see queryparse.py).

**Photo date and place.** The app reads the small EXIF header of each photo (JPEG, PNG,
WebP, HEIC and raw formats such as DNG) with a few dozen lines of its own code. It
never decodes the picture. The date taken goes into its own column and is used by date
filters. The GPS position is turned into place names by finding the nearest of about
60,000 towns and cities (population 5,000 and up) within 50 km, using a data file that
ships with the app. Month, year and place words then become searchable, and typo
matching works on them too ("berlinn" still finds Berlin). Nothing is looked up online.

**Text inside documents.** Word (.docx), Excel (.xlsx), PowerPoint (.pptx), LibreOffice
(.odt, .ods, .odp), e-books (.epub) and RTF are zip or text files. The app cuts the words
out with plain string handling, without an XML parser or any document library, and with
hard limits on size, so a crafted file has nothing to exploit. Old .doc, .xls and .ppt files are read
roughly (readable stretches are pulled out of the binary data); this needs olefile.

**Typos inside texts too.** Words found inside files (up to 300,000 of them) join the
typo vocabulary, so "kubernetis" finds a note that says "kubernetes". When two words are
equally close, a word used in file names wins over a word only seen in texts.

Every result carries a plain language reason, for example
`"invoce" looks like "invoice" in the name (typo)` or
`"return" often appears together with "tax" in your files`.
Related words and typo matches rank below exact matches.

## Speed and live updates

Searching never scans the disk. It queries the index on a background thread, and
a new keystroke cancels the meaning of the previous search (stale answers are
dropped). Measured on 180,000 files (including 30,000 photos with EXIF and 30,000 Word
documents): first scan about 42 s, rescan with no changes under 2 s, searches 20 to 320 ms.
With plain files only (200,000) the first scan took about 17 s.

Creating, moving, renaming and deleting files updates the index within about a
second using inotify, including whole folders. If the system runs out of watches,
the status bar says so. Check and raise the limit with:

    cat /proc/sys/fs/inotify/max_user_watches
    echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/60-filefinder.conf
    sudo sysctl -p /etc/sysctl.d/60-filefinder.conf

The app also does a quick rescan at every start, which catches anything that
changed while it was closed. If the watch limit is used up anyway, the app falls back to
rescanning every 5 minutes, so nothing stays stale for long.

System folders are rescanned every 15 minutes, only your home folder and external drives
are watched live (the system limit on watches is far too small for a whole disk).

Related words are relearned after 30 changes, at most every 2 minutes, so a new pairing
in your files shows up within a couple of minutes.

## Safety

* Nothing leaves the machine. There is no network code.
* Files are never run by this app. Opening hands the file to the application you chose (or
  your normal one), and the app asks first for anything that could run as a program
  (executable bit, .sh, .desktop, AppImage and similar). Applications are started directly
  with the file name as one argument, never through a shell, so odd characters in a file
  name cannot run anything.
* Whole computer mode only lists what your own user can already see. It runs without any
  special rights, follows no links, and reads inside files only in your home folder and
  external drives.
* File names and file text are treated as untrusted: shown as plain text, never
  as markup; search words are quoted and passed as SQL parameters; links are not
  followed; odd file types (pipes, devices) and names that are not valid text are skipped.
* Delete means move to trash, with a confirmation. Rename and move never
  overwrite an existing file. Moving to another drive happens in the background:
  the file is copied under a hidden temporary name, renamed into place, and only then is
  the original removed. If anything fails, the original stays and the temporary copy is deleted.
* PDF reading is off by default because it hands the file to the pdftotext program.

## What it does not do

* Photo places need GPS in the photo and a town of 5,000+ people within 50 km. Photos with
  a date but no GPS still get the date. The camera date has no time zone. Videos are not
  read for date or place.
* The text inside system files (in /usr, /etc and so on) is not searched, only their names.
  Add a folder under "Read inside files and update live" in Settings if you want one covered.
* Scanned PDFs and pictures of text would need OCR, which is not included. PDFs with real
  text work after you tick the PDF box.
* Password protected documents are skipped. Old .doc, .xls and .ppt files are read only
  roughly. The reading was tested on hand built containers of that format, not on files
  made by real Microsoft Office, so treat it as best effort.
* A move to another drive cannot be cancelled once started.
* Results for a folder you moved elsewhere in the window may show the old paths until the
  index catches up, which takes about a second.

Place data credit: GeoNames (https://www.geonames.org), licensed CC BY 4.0, taken from the
data file of the reverse_geocode package.

## Layout

    run.py                 start here
    build.sh, install.sh   make and install the standalone app
    filefinder/config.py   settings and locations
    filefinder/db.py       the SQLite schema
    filefinder/textutil.py words, trigrams, edit distance
    filefinder/queryparse.py  natural language rules
    filefinder/search.py   search, ranking, reasons, suggestions, click learning
    filefinder/related.py  related word learning
    filefinder/exif.py     date and GPS from photos
    filefinder/places.py   GPS to place names, offline (data in filefinder/data)
    filefinder/extract.py  file types, text from documents
    filefinder/fileops.py  safe moves, also across drives
    filefinder/openwith.py applications for a file type, starting one
    filefinder/mounts.py   which mounts to skip (/proc, network drives and so on)
    filefinder/indexer.py  scan, update and reading details (one background thread)
    filefinder/watcher.py  inotify
    filefinder/ui.py       the window
    tests/test_core.py     run: python3 -m unittest discover -s tests -v
