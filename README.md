<p align="center">
  <img src="packaging/icons/filefinder-512.png" alt="File Finder icon" width="200">
</p>

# File Finder

This is a native Linux desktop app that quickly finds files and folders anywhere on your computer by name, folder and the text inside your files. For photos, it also finds the place and date they were taken. It is forgiving of typos, even when you misspell a name or only remember part of it, and it learns from you.

## Why this project matters to me

I wanted a file search that worked the way I thought. Often, I remember a word from the file name or folder name, or roughly what the file was about, but not the exact spelling or where I saved it. The search tools I tried were either slow, too strict about spelling or wanted to send my information elsewhere.

So I built one with a few rules I care about:

* Your files and searches never leave your computer.
* It stays light and quick.
* It never runs your files. It simply finds and opens them with the program you choose.
* The code is simple enough to read and trust.

## How it works

File Finder does not use a neural network or chatbot technology. Instead, it uses traditional methods of counting and comparison that adapt to you. This makes it fast, small and private.

**1. It builds a small index.** It reads file and folder names, as well as the words inside your documents and the dates and locations of your photos, if you choose to include them. All this information is saved in one file on your computer. You can think of it as the index at the back of a book.

**2. It forgives typos.** Even if you type "invoce", it still finds "invoice". It measures how many small errors (missing or extra letters, incorrect letters, or two letters swapped) separate your word from words in your files. Short words allow one slip, while longer words allow two.

**3. It learns which words belong together.** If 'tax' and 'return' appear frequently in the same file names and documents, searching for 'tax' will also bring up 'return' files. The system counts how often two words appear together and compares this with what would be expected by chance. This is determined based on your own files on your own computer.

**4. It learns from what you open.** Every time you open a file after searching for something, that file gets a point for those search terms. The next time, it ranks higher. However, points fade by half every 60 days, so old habits slowly stop mattering.

**5. It ranks and explains.** A match in the file name counts much more than a match inside the text. Your own files rank above system files, and recent files get a small boost. Every result shows a plain reason, such as "you opened this before after similar searches".

**6. It suggests.** If a search looks like a typo, it offers a "Did you mean?" suggestion and suggests your past searches as you type.

For those who are interested, the ranking uses a standard search formula called BM25 and the spelling checker uses edit distance. Both are well-known methods that do not require training data.

## Install

You can use either the .deb or the AppImage. Download one of them from the Releases page. It runs on 64 bit Linux, Ubuntu 24.04 or newer.

### Option 1: the .deb (adds a menu entry)

```
sudo apt install ./filefinder_0.0.1_amd64.deb
```

Then open File Finder from your application menu, or type `filefinder` in a terminal.

### Option 2: the AppImage (one file, nothing installed)

```
chmod +x File_Finder-x86_64.AppImage
./File_Finder-x86_64.AppImage
```

If you are using Ubuntu 24.04, you may need to do this first:

```
sudo apt install libfuse2t64
```

If it still will not start, run it with `./File_Finder-x86_64.AppImage --appimage-extract-and-run`. To remove it, delete the file.

### Optional

To search inside PDFs, install `poppler-utils` and tick the PDF box in Settings. 

## Enjoy

Enjoy using the app and, if you can, use it as inspiration to build something far more awesome than this one!

## License

Distributed under the MIT License. See [LICENSE](LICENSE).
