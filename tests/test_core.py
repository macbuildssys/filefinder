"""Core tests. Run with:  python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from filefinder.config import Config
from filefinder.db import connect, init_db
from filefinder.indexer import Indexer
from filefinder.queryparse import parse_query
from filefinder.search import nearby, record_click, run_search, suggest
from filefinder.textutil import edit_distance, split_words

def spit(path, data, mode="wb"):
    with open(path, mode) as fh:
        fh.write(data)

def slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def touch(path, text="", age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))

class TextTests(unittest.TestCase):
    def test_split_words(self):
        self.assertEqual(split_words("TaxReturn_2025.pdf"), ["tax", "return", "2025", "pdf"])
        self.assertEqual(split_words("Café-Menü"), ["cafe", "menu"])
        self.assertEqual(split_words("Tromsø Straße Łódź"), ["tromso", "strasse", "lodz"])

    def test_edit_distance(self):
        self.assertEqual(edit_distance("invoice", "invoice"), 0)
        self.assertEqual(edit_distance("invoce", "invoice"), 1)
        self.assertEqual(edit_distance("recieve", "receive"), 1)   # swapped letters
        self.assertEqual(edit_distance("abcdef", "uvwxyz"), 4)     # gives up above the limit of 3

class ParseTests(unittest.TestCase):
    NOW = time.mktime((2026, 10, 3, 12, 0, 0, 0, 0, -1))   # Saturday 3 Oct 2026

    def test_plain_words_and_year(self):
        q = parse_query("tax 2025", self.NOW)
        self.assertEqual(q.terms, ["tax", "2025"])
        self.assertFalse(q.has_filters())

    def test_large_videos(self):
        q = parse_query("large videos", self.NOW)
        self.assertEqual(q.kinds, ["video"])
        self.assertEqual(q.min_size, 50 * 1024 ** 2)
        self.assertEqual(q.terms, [])

    def test_pdf_downloaded_last_month(self):
        q = parse_query("PDF I downloaded last month", self.NOW)
        self.assertEqual(q.kinds, ["pdf"])
        self.assertEqual(q.folder_words, ["downloads"])
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(q.after)), "2026-09-01")
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(q.before)), "2026-10-01")
        self.assertEqual(q.terms, [])

    def test_photos_from_berlin_and_sizes(self):
        q = parse_query("photos from Berlin", self.NOW)
        self.assertEqual((q.kinds, q.terms), (["image"], ["berlin"]))
        q = parse_query("backup over 2 GB", self.NOW)
        self.assertEqual((q.terms, q.min_size), (["backup"], 2 * 1024 ** 3))

    def test_pdfs_in_downloads(self):
        q = parse_query("PDFs in Downloads", self.NOW)
        self.assertEqual((q.kinds, q.folder_words, q.terms), (["pdf"], ["downloads"], []))

class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.db_path = os.path.join(self.tmp, "index.db")
        h = self.home
        touch(f"{h}/Documents/Invoice_Acme_2025.pdf")
        touch(f"{h}/Documents/invoice_old.txt", "payment due for the invoice")
        touch(f"{h}/Downloads/report_final.pdf", age_days=2)
        touch(f"{h}/Downloads/notes.txt", "meeting about the budget and the roadmap")
        touch(f"{h}/Pictures/Berlin Trip/IMG_001.jpg")
        touch(f"{h}/Pictures/Berlin Trip/IMG_002.jpg")
        touch(f"{h}/Pictures/Paris/IMG_003.jpg")
        touch(f"{h}/.hidden/secret_invoice.txt", "invoice")
        os.symlink(f"{h}/Documents", f"{h}/Downloads/link_to_docs")
        # Files where "tax" and "return" always show up together, plus filler.
        for i in range(6):
            touch(f"{h}/Taxes/tax_return_{2018 + i}.pdf")
        for i in range(60):
            touch(f"{h}/Misc/holiday_photo_{i}_beach.jpg" if i % 2 else f"{h}/Misc/recipe_{i}_pasta.txt")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.indexer = Indexer(Config(whole_system=False, include=[h]), self.db_path)
        self.indexer.rescan(self.conn)

    def names(self, text, **kw):
        return [h.name for h in run_search(self.conn, text, **kw).hits]

    def test_exact_name_search(self):
        names = self.names("invoice")
        self.assertIn("Invoice_Acme_2025.pdf", names)
        self.assertIn("invoice_old.txt", names)

    def test_hidden_and_symlinks_skipped(self):
        self.assertNotIn("secret_invoice.txt", self.names("invoice"))
        self.assertEqual([n for n in self.names("link") if n == "link_to_docs"], [])

    def test_typo_tolerance_and_reason(self):
        result = run_search(self.conn, "invoce")
        self.assertIn("Invoice_Acme_2025.pdf", [h.name for h in result.hits])
        top = next(h for h in result.hits if h.name == "Invoice_Acme_2025.pdf")
        self.assertIn("typo", top.reason)

    def test_did_you_mean_when_nothing_found(self):
        result = run_search(self.conn, "reprt")
        self.assertEqual(result.did_you_mean, "report")

    def test_text_inside_files_with_reason(self):
        result = run_search(self.conn, "roadmap")
        self.assertEqual([h.name for h in result.hits], ["notes.txt"])
        self.assertIn("text inside the file", result.hits[0].reason)

    def test_related_words_learned(self):
        # Nobody typed "return" but the files in the Taxes folder are named tax_return_*.
        row = self.conn.execute("SELECT 1 FROM related WHERE word='tax' AND other='return'").fetchone()
        self.assertIsNotNone(row)

    def test_folder_and_type_filters(self):
        self.assertEqual(sorted(self.names("pdfs in downloads")), ["report_final.pdf"])
        hits = run_search(self.conn, "photos from berlin").hits
        self.assertEqual(sorted(h.name for h in hits[:2]), ["IMG_001.jpg", "IMG_002.jpg"])
        for h in hits[2:]:   # anything after the real matches must be honest about why
            self.assertIn("often appears together", h.reason)

    def test_date_filter(self):
        result = run_search(self.conn, "pdf this week", now=time.time())
        self.assertTrue(result.hits)    # files were just created, so they are from this week

    def test_clicks_change_ranking(self):
        before = self.names("invoice")
        self.assertEqual(before[0], "Invoice_Acme_2025.pdf")
        for _ in range(4):
            record_click(self.conn, "invoice", f"{self.home}/Documents/invoice_old.txt")
        after = run_search(self.conn, "invoice").hits
        self.assertEqual(after[0].name, "invoice_old.txt")
        self.assertIn("opened this before", after[0].reason)

    def test_suggestions(self):
        record_click(self.conn, "invoice acme", f"{self.home}/Documents/Invoice_Acme_2025.pdf")
        self.assertIn("invoice acme", suggest(self.conn, "inv"))
        self.assertIn("invoice", suggest(self.conn, "inv"))

    def test_nearby(self):
        names = [h.name for h in nearby(self.conn, f"{self.home}/Pictures/Berlin Trip/IMG_001.jpg").hits]
        self.assertEqual(names, ["IMG_002.jpg"])

    def test_live_changes(self):
        new = f"{self.home}/Documents/zebra_contract.txt"
        touch(new, "signed")
        self.indexer._dispatch(self.conn, ("changed", new))
        self.assertIn("zebra_contract.txt", self.names("zebra"))
        os.remove(new)
        self.indexer._dispatch(self.conn, ("deleted", new))
        self.assertNotIn("zebra_contract.txt", self.names("zebra"))

    def test_folder_delete_removes_children(self):
        folder = f"{self.home}/Pictures/Berlin Trip"
        self.indexer._dispatch(self.conn, ("deleted", folder))
        hits = run_search(self.conn, "berlin").hits
        self.assertTrue(all("Berlin Trip" not in h.path for h in hits))
        self.assertIn("IMG_003.jpg", self.names("paris"))   # sibling folder untouched

    def test_hostile_query_text_is_harmless(self):
        for text in ['"', "'; DROP TABLE files;--", "a AND OR NOT (", "*", "\x00", "NEAR(", "col:x"]:
            run_search(self.conn, text)    # must not raise
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

# Helpers that build tiny test files by hand

def tiff_with_exif(taken=None, lat=None, lon=None, big_endian=False):
    """A minimal TIFF block with an EXIF date and GPS position, like a camera writes."""
    import struct
    e = ">" if big_endian else "<"
    p = lambda fmt, *a: struct.pack(e + fmt, *a)
    entry_count = (1 if taken else 0) + (1 if lat is not None else 0)
    data_start = 8 + 2 + 12 * entry_count + 4     # header, count, entries, next ifd
    exif_ifd = b""
    gps_ifd = b""
    extra = b""

    def add_extra(blob):
        nonlocal extra
        offset = data_start + len(extra)
        extra += blob
        return offset

    if taken:
        text = taken.encode() + b"\x00"
        off_text = add_extra(text)
        exif_ifd = p("H", 1) + p("HHII", 0x9003, 2, len(text), off_text) + p("I", 0)
    if lat is not None:
        def dms(v):
            v = abs(v); d = int(v); m = int((v - d) * 60); s_ = round(((v - d) * 60 - m) * 60 * 100)
            return p("IIIIII", d, 1, m, 1, s_, 100)
        off_lat = add_extra(dms(lat)); off_lon = add_extra(dms(lon))
        ref_lat = b"N\x00" if lat >= 0 else b"S\x00"
        ref_lon = b"E\x00" if lon >= 0 else b"W\x00"
        gps_ifd = (p("H", 4)
                   + p("HHI", 1, 2, 2) + ref_lat + b"\x00\x00"
                   + p("HHII", 2, 5, 3, off_lat)
                   + p("HHI", 3, 2, 2) + ref_lon + b"\x00\x00"
                   + p("HHII", 4, 5, 3, off_lon) + p("I", 0))
    off_exif = add_extra(exif_ifd) if exif_ifd else 0
    off_gps = add_extra(gps_ifd) if gps_ifd else 0
    ifd0 = p("H", entry_count)
    if taken:
        ifd0 += p("HHII", 0x8769, 4, 1, off_exif)
    if lat is not None:
        ifd0 += p("HHII", 0x8825, 4, 1, off_gps)
    ifd0 += p("I", 0)
    head = (b"MM\x00*" if big_endian else b"II*\x00") + p("I", 8)
    return head + ifd0 + extra

def write_jpeg_with_exif(path, **kw):
    import struct
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tiff = tiff_with_exif(**kw)
    app1 = b"Exif\x00\x00" + tiff
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
                 + b"\xff\xda\x00\x02" + b"not really picture data" + b"\xff\xd9")

def write_zip(path, members):
    import zipfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            zf.writestr(name, text)

def build_ole(stream_name, payload):
    """Smallest valid OLE2 container (what old .doc, .xls and .ppt files are made of)."""
    import struct
    payload = payload.ljust(4096, b"\x00")          # 4096 bytes or more lives in normal sectors
    n = len(payload) // 512
    END, FREE, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    header = bytearray(512)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, END, 0, END, 0)
    struct.pack_into("<109I", header, 76, *([0] + [FREE] * 108))
    fat = [FATSECT, END] + list(range(3, 2 + n)) + [END] + [FREE] * (128 - 2 - n)
    fat_sector = struct.pack("<128I", *fat)

    def entry(name, kind, child, start, size):
        raw = name.encode("utf-16-le")
        e = bytearray(128)
        e[0:len(raw)] = raw
        struct.pack_into("<HBB", e, 64, len(raw) + 2 if raw else 0, kind, 1 if kind else 0)
        struct.pack_into("<III", e, 68, NOSTREAM, NOSTREAM, child)
        struct.pack_into("<I", e, 116, start)
        struct.pack_into("<Q", e, 120, size)
        return bytes(e)

    directory = (entry("Root Entry", 5, 1, END, 0) + entry(stream_name, 2, NOSTREAM, 2, len(payload))
                 + entry("", 0, NOSTREAM, 0, 0) + entry("", 0, NOSTREAM, 0, 0))
    return bytes(header) + fat_sector + directory + payload

class ExifTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_jpeg_date_and_gps(self):
        from filefinder.exif import read_photo_info
        path = os.path.join(self.tmp, "a.jpg")
        write_jpeg_with_exif(path, taken="2024:07:13 14:22:05", lat=52.52, lon=13.405)
        info = read_photo_info(path)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(info.taken)), "2024-07-13 14:22")
        self.assertAlmostEqual(info.lat, 52.52, places=2)
        self.assertAlmostEqual(info.lon, 13.405, places=2)

    def test_southern_and_western_positions_are_negative(self):
        from filefinder.exif import read_photo_info
        path = os.path.join(self.tmp, "b.jpg")
        write_jpeg_with_exif(path, taken="2023:01:02 03:04:05", lat=-1.2864, lon=-36.8172, big_endian=True)
        info = read_photo_info(path)
        self.assertAlmostEqual(info.lat, -1.2864, places=2)
        self.assertAlmostEqual(info.lon, -36.8172, places=2)

    def test_other_containers_and_junk(self):
        from filefinder.exif import read_photo_info
        tiff = tiff_with_exif(taken="2022:05:06 07:08:09")
        raw = os.path.join(self.tmp, "x.dng"); spit(raw, tiff, "wb")
        heic = os.path.join(self.tmp, "y.heic")
        spit(heic, b"\x00\x00\x00\x18ftypheic" + b"\x00" * 50 + b"Exif\x00\x00" + tiff, "wb")
        png = os.path.join(self.tmp, "z.png")
        spit(png, b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x30eXIf" + tiff + b"\x00" * 4, "wb")
        for path in (raw, heic, png):
            info = read_photo_info(path)
            self.assertIsNotNone(info, path)
            self.assertEqual(time.strftime("%Y", time.localtime(info.taken)), "2022")
        for junk in (b"", b"\xff\xd8\xff\xe1\xff\xff" + b"A" * 100, os.urandom(5000), b"II*\x00\xff\xff\xff\xff"):
            path = os.path.join(self.tmp, "junk.jpg"); spit(path, junk, "wb")
            self.assertIsNone(read_photo_info(path))      # must not raise

class ExifRobustnessTests(unittest.TestCase):
    def test_broken_gps_block_keeps_the_date(self):
        import struct
        from filefinder.exif import _parse_tiff
        good = bytearray(tiff_with_exif(taken="2024:07:13 14:22:05", lat=52.52, lon=13.405))
        # Point the GPS block far outside the file.
        marker = struct.pack("<H", 0x8825) + struct.pack("<HI", 4, 1)
        pos = bytes(good).find(marker) + 8
        good[pos:pos + 4] = struct.pack("<I", 0x7FFFFFF0)
        info = _parse_tiff(bytes(good))
        self.assertIsNotNone(info)
        self.assertIsNotNone(info.taken)
        self.assertIsNone(info.lat)

class PlaceTests(unittest.TestCase):
    def test_known_cities_offline(self):
        from filefinder.places import place_words
        berlin = place_words(52.52, 13.405)      # the nearest named place is the district Mitte
        self.assertIn("berlin", berlin)
        self.assertIn("germany", berlin)
        self.assertIn("nairobi", place_words(-1.2864, 36.8172))
        self.assertEqual(place_words(0.0, -30.0), [])        # middle of the Atlantic
        self.assertIn("tromso", place_words(69.65, 18.96))   # far north, where longitude cells are narrow
        self.assertEqual(place_words(78.22, 15.65), [])      # Longyearbyen is below the 5,000 people cutoff

class DocumentTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def read(self, name, members):
        from filefinder.extract import read_text, split_ext
        path = os.path.join(self.tmp, name)
        write_zip(path, members)
        return read_text(path, split_ext(name), os.path.getsize(path))

    def test_word_excel_powerpoint(self):
        self.assertIn("quarterly roadmap", self.read("a.docx", {
            "word/document.xml": "<w:document><w:p><w:t>The quarterly</w:t> <w:t>roadmap &amp; plan</w:t></w:p></w:document>"}))
        self.assertIn("budget", self.read("b.xlsx", {"xl/sharedStrings.xml": "<sst><si><t>budget</t></si></sst>"}))
        self.assertIn("launch", self.read("c.pptx", {"ppt/slides/slide1.xml": "<p><a:t>launch</a:t></p>"}))

    def test_libreoffice_and_epub(self):
        self.assertIn("invoice", self.read("d.odt", {"content.xml": "<office:text><text:p>invoice</text:p></office:text>"}))
        self.assertIn("whale", self.read("e.epub", {"OEBPS/ch1.xhtml": "<html><body><p>Call me whale</p></body></html>",
                                                   "META-INF/container.xml": "<container/>"}))

    def test_hostile_files_are_harmless(self):
        from filefinder.extract import read_text
        bad = os.path.join(self.tmp, "bad.docx"); spit(bad, b"PK\x03\x04 this is not a zip", "wb")
        self.assertEqual(read_text(bad, "docx", os.path.getsize(bad)), "")
        # An XML bomb style entity file must come out as plain text, never expanded.
        bomb = self.read("bomb.docx", {"word/document.xml": '<!DOCTYPE x [<!ENTITY a "AAAA">]><w:p><w:t>&a;&a;&a;</w:t></w:p>'})
        self.assertLess(len(bomb), 200)
        # A huge compressed part is cut off at the read limit.
        huge = self.read("huge.docx", {"word/document.xml": "<w:t>" + "word " * 5_000_000 + "</w:t>"})
        self.assertLessEqual(len(huge), 20000)

    def test_rtf_and_rough_legacy_strings(self):
        from filefinder.extract import _rough_strings, read_text
        rtf = os.path.join(self.tmp, "r.rtf"); spit(rtf, r"{\\rtf1\\ansi {\\b Hello} contract terms}", "w")
        self.assertIn("contract terms", read_text(rtf, "rtf", os.path.getsize(rtf)))
        blob = b"\x01\x02\xff" * 50 + b"Annual report for the board" + b"\x00\x01" * 40 + "Ünïcode?".encode("utf-16-le")
        blob += b"B\x00u\x00d\x00g\x00e\x00t\x00 \x00p\x00l\x00a\x00n\x00"
        text = _rough_strings(blob)
        self.assertIn("Annual report for the board", text)
        self.assertIn("Budget plan", text)

class LegacyOfficeTests(unittest.TestCase):
    def test_damaged_old_office_files_never_raise(self):
        pytest_skip = False
        try:
            import olefile  # noqa: F401
        except ImportError:
            pytest_skip = True
        if pytest_skip:
            self.skipTest("olefile is not installed")
        from filefinder.extract import read_text
        tmp = tempfile.mkdtemp()
        header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        samples = [os.urandom(4000), b"", header + b"\x00" * 600, header + os.urandom(2000)]
        for i, data in enumerate(samples):
            for ext in ("doc", "xls", "ppt"):
                path = os.path.join(tmp, f"s{i}.{ext}")
                with open(path, "wb") as fh:
                    fh.write(data)
                self.assertEqual(read_text(path, ext, len(data)), "")

class LegacyOfficeContentTests(unittest.TestCase):
    def test_text_comes_out_of_old_office_containers(self):
        try:
            import olefile  # noqa: F401
        except ImportError:
            self.skipTest("olefile is not installed")
        from filefinder.extract import read_text
        tmp = tempfile.mkdtemp()
        payload = "Annual contract review for Acme".encode("ascii") + b"\x00\x13\x01" * 30 \
            + "Termination clause".encode("utf-16-le")
        for ext, stream in (("doc", "WordDocument"), ("xls", "Workbook"), ("ppt", "PowerPoint Document")):
            path = os.path.join(tmp, f"real.{ext}")
            with open(path, "wb") as fh:
                fh.write(build_ole(stream, payload))
            text = read_text(path, ext, os.path.getsize(path))
            self.assertIn("Annual contract review for Acme", text)
            self.assertIn("Termination clause", text)

class RichIndexTests(unittest.TestCase):
    """Photos, office files and the vocabulary, through a real index."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.db_path = os.path.join(self.tmp, "index.db")
        h = self.home
        write_jpeg_with_exif(f"{h}/Pictures/IMG_0001.jpg", taken="2024:07:13 14:22:05", lat=52.52, lon=13.405)
        write_jpeg_with_exif(f"{h}/Pictures/IMG_0002.jpg", taken="2023:12:24 10:00:00", lat=48.8566, lon=2.3522)
        write_jpeg_with_exif(f"{h}/Pictures/IMG_0003.jpg", taken="2024:07:15 09:00:00")      # no GPS
        touch(f"{h}/Pictures/plain.jpg")                                                    # no EXIF
        write_zip(f"{h}/Work/plan.docx", {"word/document.xml": "<w:t>kubernetes migration roadmap</w:t>"})
        touch(f"{h}/Work/notes.txt", "kubernetes cluster upgrade")
        for i in range(30):
            touch(f"{h}/Misc/file_{i}_{'abcdefghij'[i % 10]}.txt", "filler words here")
        self.conn = connect(self.db_path); init_db(self.conn)
        self.indexer = Indexer(Config(whole_system=False, include=[h]), self.db_path)
        self.indexer.rescan(self.conn)

    def names(self, text, **kw):
        return [h.name for h in run_search(self.conn, text, **kw).hits]

    def test_photos_found_by_place_without_it_being_in_the_name(self):
        result = run_search(self.conn, "photos from Berlin")
        self.assertEqual([h.name for h in result.hits][:1], ["IMG_0001.jpg"])
        self.assertIn("photo place or date", result.hits[0].reason)
        self.assertIn("IMG_0002.jpg", self.names("paris"))
        self.assertIn("IMG_0002.jpg", self.names("germany") + self.names("france"))

    def test_photos_found_by_month_and_year_taken(self):
        self.assertEqual(sorted(self.names("photos july 2024")), ["IMG_0001.jpg", "IMG_0003.jpg"])

    def test_date_filters_use_date_taken_not_file_date(self):
        # All files were created just now, but the photo dates are in the past.
        far_future = time.mktime((2024, 8, 20, 12, 0, 0, 0, 0, -1))
        hits = run_search(self.conn, "photos last month", now=far_future).hits
        self.assertEqual(sorted(h.name for h in hits), ["IMG_0001.jpg", "IMG_0003.jpg"])
        self.assertTrue(all(h.taken for h in hits))
        december = time.mktime((2024, 1, 20, 12, 0, 0, 0, 0, -1))
        hits = run_search(self.conn, "photos last month", now=december).hits
        self.assertEqual([h.name for h in hits], ["IMG_0002.jpg"])

    def test_text_inside_word_documents(self):
        result = run_search(self.conn, "roadmap")
        self.assertEqual([h.name for h in result.hits], ["plan.docx"])
        self.assertIn("text inside the file", result.hits[0].reason)

    def test_typos_work_for_words_inside_files_and_photo_places(self):
        result = run_search(self.conn, "kubernetis")
        self.assertIn("notes.txt", [h.name for h in result.hits])
        reason = next(h.reason for h in result.hits if h.name == "notes.txt")
        self.assertIn("kubernetes", reason)
        self.assertIn("IMG_0001.jpg", self.names("berlinn"))

    def test_changed_photo_is_reread_and_live_photo_is_added(self):
        new = f"{self.home}/Pictures/IMG_0009.jpg"
        write_jpeg_with_exif(new, taken="2022:03:04 05:06:07", lat=52.52, lon=13.405)
        self.indexer._dispatch(self.conn, ("changed", new))
        self.assertIn("IMG_0009.jpg", self.names("berlin"))
        self.assertIn("IMG_0009.jpg", self.names("march 2022"))

    def test_photo_setting_off_skips_exif(self):
        cfg = Config(whole_system=False, include=[self.home], index_photos=False)
        conn = connect(os.path.join(self.tmp, "other.db")); init_db(conn)
        Indexer(cfg, os.path.join(self.tmp, "other.db")).rescan(conn)
        self.assertNotIn("IMG_0001.jpg", [h.name for h in run_search(conn, "berlin").hits])

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.db_path = os.path.join(self.tmp, "index.db")
        for i in range(40):
            touch(f"{self.home}/Misc/note_{i}_{'abcdefghijklmnopqrstuvwxyz'[i % 26]}.txt", "text")
        self.conn = connect(self.db_path); init_db(self.conn)
        self.indexer = Indexer(Config(whole_system=False, include=[self.home]), self.db_path)
        self.indexer.rescan(self.conn)

    def test_related_words_relearned_soon_after_changes(self):
        for i in range(5):
            touch(f"{self.home}/Sport/kite_surf_{i}.txt", "x")
        for i in range(40):
            self.indexer._dispatch(self.conn, ("changed", f"{self.home}/Sport/kite_surf_{i % 5}.txt"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM related WHERE word='kite' AND other='surf'").fetchone())
        self.indexer.last_learn = time.time() - 1000       # pretend two minutes have passed
        self.indexer._idle_work(self.conn)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM related WHERE word='kite' AND other='surf'").fetchone())

    def test_rescan_covers_folders_when_watch_limit_is_hit(self):
        class FullWatcher:
            full = True
            def add(self, folder): pass
        self.indexer.watcher = FullWatcher()
        touch(f"{self.home}/Misc/appears_without_event.txt", "hello")
        self.indexer.last_scan = time.time() - 1000
        self.indexer._idle_work(self.conn)
        self.assertIn("appears_without_event.txt", [h.name for h in run_search(self.conn, "appears").hits])

    def test_old_index_is_upgraded_and_clicks_are_kept(self):
        import sqlite3
        old = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(old)
        c.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);"
                        "CREATE TABLE term_clicks(term TEXT NOT NULL, path TEXT NOT NULL, weight REAL NOT NULL, ts REAL NOT NULL, PRIMARY KEY(term, path));"
                        "INSERT INTO term_clicks VALUES ('tax', '/x', 3.0, 1.0);")
        c.commit(); c.close()
        conn = connect(old); init_db(conn)
        self.assertEqual(conn.execute("SELECT weight FROM term_clicks").fetchone()[0], 3.0)
        conn.execute("SELECT taken FROM files LIMIT 1")      # new column exists
        init_db(conn)                                         # running it again changes nothing
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM term_clicks").fetchone()[0], 1)

class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_file_and_folder_moves(self):
        from filefinder.fileops import move_path
        src = os.path.join(self.tmp, "a", "doc.txt"); touch(src, "hello")
        dst_dir = os.path.join(self.tmp, "b"); os.makedirs(dst_dir)
        move_path(src, os.path.join(dst_dir, "doc.txt"))
        self.assertFalse(os.path.exists(src))
        self.assertEqual(slurp(os.path.join(dst_dir, "doc.txt")), "hello")
        touch(os.path.join(self.tmp, "folder", "sub", "x.txt"), "x")
        move_path(os.path.join(self.tmp, "folder"), os.path.join(dst_dir, "folder"))
        self.assertTrue(os.path.exists(os.path.join(dst_dir, "folder", "sub", "x.txt")))

    def test_never_overwrites(self):
        from filefinder.fileops import move_path
        a = os.path.join(self.tmp, "a.txt"); b = os.path.join(self.tmp, "b.txt")
        touch(a, "A"); touch(b, "B")
        with self.assertRaises(FileExistsError):
            move_path(a, b)
        self.assertEqual(slurp(b), "B")
        self.assertTrue(os.path.exists(a))

    def test_copy_then_delete_path_used_for_other_drives(self):
        from filefinder.fileops import _move_across_drives
        src = os.path.join(self.tmp, "big", "inner", "f.txt"); touch(src, "data")
        os.symlink("/etc/hostname", os.path.join(self.tmp, "big", "link"))
        dst = os.path.join(self.tmp, "dest"); os.makedirs(dst)
        _move_across_drives(os.path.join(self.tmp, "big"), os.path.join(dst, "big"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "big")))
        self.assertEqual(slurp(os.path.join(dst, "big", "inner", "f.txt")), "data")
        self.assertTrue(os.path.islink(os.path.join(dst, "big", "link")))     # links stay links
        self.assertEqual([n for n in os.listdir(dst) if n.startswith(".filefinder")], [])
        with self.assertRaises(OSError):                                        # folder into itself
            _move_across_drives(dst, os.path.join(dst, "big", "again"))

    def test_failed_copy_leaves_original_and_no_leftovers(self):
        from filefinder.fileops import _move_across_drives
        src = os.path.join(self.tmp, "f.txt"); touch(src, "keep me")
        dst_dir = os.path.join(self.tmp, "d"); os.makedirs(dst_dir)
        dst = os.path.join(dst_dir, "f.txt"); touch(dst, "already here")
        with self.assertRaises(FileExistsError):
            _move_across_drives(src, dst)
        self.assertEqual(slurp(src), "keep me")
        self.assertEqual(slurp(dst), "already here")
        self.assertEqual(sorted(os.listdir(dst_dir)), ["f.txt"])

    def test_real_other_drive_if_available(self):
        from filefinder.fileops import move_path
        shm = "/dev/shm"
        if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat(self.tmp).st_dev:
            self.skipTest("no second filesystem available here")
        src = os.path.join(self.tmp, "x.txt"); touch(src, "across")
        dst = os.path.join(shm, f"filefinder_test_{os.getpid()}.txt")
        try:
            move_path(src, dst)
            self.assertEqual(slurp(dst), "across")
            self.assertFalse(os.path.exists(src))
        finally:
            if os.path.exists(dst):
                os.unlink(dst)

class MountTests(unittest.TestCase):
    SAMPLE = (
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
        "23 22 0:21 / /proc rw,nosuid shared:5 - proc proc rw\n"
        "24 22 0:22 / /sys rw,nosuid shared:6 - sysfs sysfs rw\n"
        "25 22 0:6 / /dev rw shared:2 - devtmpfs udev rw\n"
        "26 22 0:23 / /run rw shared:3 - tmpfs tmpfs rw\n"
        "27 22 7:0 / /snap/core/1 ro - squashfs /dev/loop0 ro\n"
        "28 22 8:17 / /media/dev/USB\\040STICK rw - vfat /dev/sdb1 rw\n"
        "29 22 0:40 / /mnt/nas rw - nfs4 server:/share rw\n"
        "30 22 0:41 / /home/dev/remote rw - fuse.sshfs dev@host:/ rw\n"
        "31 22 8:3 / /home rw - ext4 /dev/sda3 rw\n")

    def test_pseudo_and_network_mounts_skipped_real_disks_kept(self):
        from filefinder.mounts import parse_mountinfo
        skip = parse_mountinfo(self.SAMPLE)
        for path in ("/proc", "/sys", "/dev", "/run", "/snap/core/1", "/mnt/nas", "/home/dev/remote"):
            self.assertIn(path, skip)
        for path in ("/", "/home", "/media/dev/USB STICK"):
            self.assertNotIn(path, skip)

    def test_garbage_lines_are_ignored(self):
        from filefinder.mounts import parse_mountinfo
        self.assertEqual(parse_mountinfo("nonsense\n\n - \n1 2 3"), set())

class PathRangeTests(unittest.TestCase):
    def test_ranges(self):
        from filefinder.db import path_ranges
        self.assertEqual(path_ranges("p", ["/"]), ("", []))
        sql, params = path_ranges("p", ["/home/a/", "/mnt"])
        self.assertEqual(params, ["/home/a/", "/home/a0", "/mnt/", "/mnt0"])
        self.assertEqual(path_ranges("p", []), ("0", []))

class WholeSystemTests(unittest.TestCase):
    """A stand in for the whole computer: a system area and a home folder."""

    def setUp(self):
        import filefinder.search as search_module
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "fake_root")
        self.home = os.path.join(self.root, "home", "dev")
        self.db_path = os.path.join(self.tmp, "index.db")
        r = self.root
        touch(f"{r}/usr/share/doc/report_template.txt", "system documentation about quarterly figures")
        touch(f"{r}/etc/report.conf", "setting=1")
        touch(f"{r}/proc/1/status", "must never appear")
        touch(f"{r}/mnt/nas/report_nas.txt", "network drive")
        touch(f"{self.home}/Documents/report_mine.txt", "my own quarterly figures")
        touch(f"{self.home}/.cache/report_hidden.txt")
        self.search_module = search_module
        self._old_home = search_module.HOME
        search_module.HOME = self.home
        self.cfg = Config(whole_system=True, deep_folders=[self.home])
        self.conn = connect(self.db_path); init_db(self.conn)
        self.indexer = Indexer(self.cfg, self.db_path)
        self.indexer._roots = lambda: [self.root]               # pretend this folder is "/"
        self.indexer._skip_paths = {f"{r}/proc"}
        self.indexer._skip_prefixes = (f"{r}/proc/",)
        self.indexer.skip_mounts = {f"{r}/mnt/nas"}
        self.prepare = self.indexer._prepare
        self.indexer._prepare = lambda: None                    # keep the pretend settings above
        self.indexer.rescan(self.conn)

    def tearDown(self):
        self.search_module.HOME = self._old_home

    def names(self, text, **kw):
        return [h.name for h in run_search(self.conn, text, **kw).hits]

    def test_every_file_is_found_by_name_but_never_the_skipped_places(self):
        names = self.names("report")
        for expected in ("report_template.txt", "report.conf", "report_mine.txt"):
            self.assertIn(expected, names)
        self.assertNotIn("status", self.names("status"))         # proc is skipped
        self.assertNotIn("report_nas.txt", names)                # network mount is skipped
        self.assertNotIn("report_hidden.txt", names)             # hidden stays hidden

    def test_only_deep_folders_are_read_inside(self):
        self.assertIn("report_mine.txt", self.names("own quarterly"))
        self.assertEqual([n for n in self.names("documentation") if n == "report_template.txt"], [])

    def test_your_files_rank_above_system_files(self):
        self.assertEqual(self.names("report")[0], "report_mine.txt")

    def test_scope_limits_results_to_your_folders(self):
        names = self.names("report", scope=[self.home])
        self.assertEqual(names, ["report_mine.txt"])
        self.assertIn("report.conf", self.names("report", scope=None))
        everything = run_search(self.conn, "report", scope=["/"]).hits
        self.assertGreater(len(everything), 1)

    def test_only_deep_folders_get_a_live_watch(self):
        class Recorder:
            full = False
            def __init__(self): self.added = []
            def add(self, folder): self.added.append(folder)
        recorder = Recorder()
        self.indexer.watcher = recorder
        self.indexer.rescan(self.conn)
        self.assertIn(f"{self.home}/Documents", recorder.added)
        self.assertNotIn(f"{self.root}/usr/share/doc", recorder.added)
        self.assertNotIn(f"{self.root}/etc", recorder.added)

    def test_system_folders_are_refreshed_by_a_periodic_rescan(self):
        touch(f"{self.root}/etc/added_later.conf", "x")
        self.assertNotIn("added_later.conf", self.names("added_later"))
        self.indexer.last_scan = time.time() - 5000
        self.indexer._idle_work(self.conn)
        self.assertIn("added_later.conf", self.names("added_later"))

    def test_real_skip_rules(self):
        from filefinder.indexer import Indexer as Idx
        idx = Idx(Config(whole_system=True), self.db_path)
        for path in ("/proc", "/proc/1/status", "/sys/kernel", "/dev/null", "/run/user", "/snap/x"):
            self.assertTrue(idx._skipped(path, os.path.basename(path), os.path.isdir(path)), path)
        self.assertFalse(idx._skipped("/usr/share/doc", "doc", True))
        self.assertFalse(idx._skipped("/procurement/notes.txt", "notes.txt", False))   # not under /proc

    def test_old_config_without_the_new_setting_searches_everything(self):
        import json
        import filefinder.config as config_module
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            json.dump({"include": ["/home/x/Documents"], "skip_hidden": True}, fh)
        old = config_module.CONFIG_PATH
        config_module.CONFIG_PATH = path
        try:
            cfg = config_module.load_config()
        finally:
            config_module.CONFIG_PATH = old
        self.assertTrue(cfg.whole_system)
        self.assertEqual(cfg.include, ["/home/x/Documents"])        # kept in case it is switched off
        self.assertEqual(cfg.open_with, {})

class OpenWithTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.apps_dir = os.path.join(self.tmp, "applications")
        os.makedirs(self.apps_dir)
        self.out = os.path.join(self.tmp, "launched.txt")

    def desktop(self, name, body):
        with open(os.path.join(self.apps_dir, name), "w") as fh:
            fh.write("[Desktop Entry]\nType=Application\n" + body)

    def test_which_applications_are_offered_and_in_what_order(self):
        from filefinder.openwith import apps_for_mime, load_apps
        self.desktop("writer.desktop", "Name=Writer\nExec=writer %F\nMimeType=application/vnd.oasis.opendocument.text;text/plain;\n")
        self.desktop("editor.desktop", "Name=Fancy Editor\nExec=editor %F\nMimeType=text/markdown;text/plain;\n")
        self.desktop("plain.desktop", "Name=Basic Notes\nExec=notes %f\nMimeType=text/plain;\n")
        self.desktop("viewer.desktop", "Name=Picture Viewer\nExec=view %f\nMimeType=image/png;\n")
        self.desktop("helper.desktop", "Name=Hidden Helper\nExec=h %f\nMimeType=text/plain;\nNoDisplay=true\n")
        self.desktop("shell.desktop", "Name=Terminal Tool\nExec=t %f\nMimeType=text/plain;\nTerminal=true\n")
        self.desktop("gone.desktop", "Name=Not Installed\nExec=x %f\nMimeType=text/plain;\nTryExec=/no/such/program\n")
        self.desktop("broken.desktop", "Name=No Exec\nMimeType=text/plain;\n")
        apps = load_apps([self.apps_dir])
        names = [a.name for a in apps_for_mime(apps, ["text/markdown", "text/plain"])]
        self.assertEqual(names, ["Fancy Editor", "Basic Notes", "Writer"])   # exact type first, then general
        self.assertEqual([a.name for a in apps_for_mime(apps, ["image/png"])], ["Picture Viewer"])
        self.assertEqual(apps_for_mime(apps, ["video/mp4"]), [])

    def test_earlier_folder_wins_and_language_names(self):
        from filefinder.openwith import load_apps
        other = os.path.join(self.tmp, "other"); os.makedirs(other)
        self.desktop("same.desktop", "Name=Mine\nExec=mine %f\n")
        with open(os.path.join(other, "same.desktop"), "w") as fh:
            fh.write("[Desktop Entry]\nType=Application\nName=System copy\nExec=sys %f\n")
        self.assertEqual(load_apps([self.apps_dir, other])["same.desktop"].name, "Mine")
        os.environ["LANG"] = "de_DE.UTF-8"
        try:
            self.desktop("de.desktop", "Name=Calculator\nName[de]=Taschenrechner\nExec=calc %f\n")
            self.assertEqual(load_apps([self.apps_dir])["de.desktop"].name, "Taschenrechner")
        finally:
            os.environ.pop("LANG", None)

    def test_command_line_is_built_without_a_shell(self):
        from filefinder.openwith import App, build_command
        app = App("a.desktop", "App", 'myapp --flag "two words" %i -f %f', "icon")
        self.assertEqual(build_command(app, "/tmp/x y; touch pwned"),
                         ["myapp", "--flag", "two words", "-f", "/tmp/x y; touch pwned"])
        app = App("b.desktop", "App", "viewer %U", "")
        self.assertEqual(build_command(app, "/tmp/a b.png"), ["viewer", "file:///tmp/a%20b.png"])
        app = App("c.desktop", "App", "opener", "")
        self.assertEqual(build_command(app, "/tmp/z"), ["opener", "/tmp/z"])      # no place named: file appended
        app = App("d.desktop", "App", "tool --path=%f 100%%", "")
        self.assertEqual(build_command(app, "/p"), ["tool", "--path=/p", "100%"])

    def test_launching_runs_the_chosen_program_with_the_exact_file_name(self):
        from filefinder.openwith import App, launch
        hostile = os.path.join(self.tmp, "a b; touch pwned.txt")
        with open(hostile, "w") as fh:
            fh.write("content")
        app = App("cp.desktop", "Copier", f"/bin/cp %f {self.out}", "")
        launch(app, hostile).wait(timeout=10)
        self.assertEqual(slurp(self.out), "content")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned.txt")))
        self.assertFalse(os.path.exists("pwned.txt"))

    def test_broken_applications_fail_cleanly(self):
        from filefinder.openwith import App, launch
        with self.assertRaises(OSError):
            launch(App("x.desktop", "X", "/no/such/program %f", ""), "/tmp/f")
        with self.assertRaises(ValueError):
            launch(App("y.desktop", "Y", 'prog "unclosed %f', ""), "/tmp/f")

    def fake_tool(self, name, body):
        folder = os.path.join(self.tmp, "bin")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return folder

    def test_opening_with_the_system_default_reports_success_and_failure(self):
        from filefinder.openwith import open_default
        target = os.path.join(self.tmp, "book.epub")
        good = self.fake_tool("gio", f'echo "$@" > {self.out}')
        with mock.patch.dict(os.environ, {"PATH": good}):
            self.assertTrue(open_default(target))
        self.assertEqual(slurp(self.out).strip(), f"open {target}")
        bad = self.fake_tool("gio", "exit 4")
        with mock.patch.dict(os.environ, {"PATH": bad}):
            self.assertFalse(open_default(target))
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.tmp, "empty")}):
            self.assertFalse(open_default(target))      # no opening tool installed at all

    def test_a_failing_first_tool_falls_back_to_the_next(self):
        from filefinder.openwith import open_default
        folder = self.fake_tool("gio", "exit 1")
        self.fake_tool("xdg-open", f'echo "$@" > {self.out}')
        with mock.patch.dict(os.environ, {"PATH": folder}):
            self.assertTrue(open_default("/tmp/x.txt"))
        self.assertEqual(slurp(self.out).strip(), "/tmp/x.txt")

    def test_started_programs_get_the_original_library_path_back(self):
        from filefinder.openwith import clean_env
        fake = {"LD_LIBRARY_PATH": "/bundle/_internal", "LD_LIBRARY_PATH_ORIG": "/usr/lib/mine",
                "LD_PRELOAD": "/bundle/x.so", "QT_PLUGIN_PATH": "/bundle/_internal/plugins",
                "QML2_IMPORT_PATH": "/somewhere/else"}
        with mock.patch.dict(os.environ, fake), mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", "/bundle/_internal", create=True):
            env = clean_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/lib/mine")     # restored
        self.assertNotIn("LD_PRELOAD", env)                           # was not set before
        self.assertNotIn("QT_PLUGIN_PATH", env)                       # pointed into the bundle
        self.assertEqual(env["QML2_IMPORT_PATH"], "/somewhere/else")  # not ours, kept
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/keep"}):
            self.assertEqual(clean_env()["LD_LIBRARY_PATH"], "/keep")  # not a built copy

if __name__ == "__main__":
    unittest.main()
