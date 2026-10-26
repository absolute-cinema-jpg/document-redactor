"""Builds a fake confidential PDF full of the usual privacy traps."""

import os
import sys

import fitz

FONT_DIR = "/System/Library/Fonts/Supplemental"


def build(path):
    doc = fitz.open()
    page = doc.new_page()
    arial = os.path.join(FONT_DIR, "Arial.ttf")
    has_arial = os.path.exists(arial)
    if has_arial:
        page.insert_font(fontname="arial", fontfile=arial)
        body = "arial"
    else:
        body = "helv"

    page.insert_text((72, 72), "ACME MUTUAL INSURANCE - CLAIM SUMMARY", fontname="hebo", fontsize=14)
    lines = [
        "Claimant: Jonathan Q. Smithers",
        "Address: 1428 Elm Street, Springfield, IL 62704",
        "Phone: (217) 555-0143    Email: j.smithers@example.com",
        "Policy number: PX-2291-4471-0093",
        "Date of loss: 03/14/2025",
        "Mr. Smithers reported water damage to the basement.",
        "Adjuster notes: Smithers was cooperative; estimate $12,450.00.",
    ]
    y = 110
    for line in lines:
        page.insert_text((72, y), line, fontname=body, fontsize=11)
        y += 20

    # a table with rules
    y += 10
    rows = [("Item", "Qty", "Amount"), ("Drywall", "14", "$3,200.00"),
            ("Carpet", "1", "$2,150.00"), ("Labour", "32", "$7,100.00")]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((80 + c * 150, y + 15), cell,
                             fontname="tibo" if r == 0 else "tiro", fontsize=10)
        page.draw_line((72, y), (522, y), color=(0, 0, 0), width=0.6)
        y += 22
    page.draw_line((72, y), (522, y), color=(0, 0, 0), width=0.6)

    # an "ID photo" image
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 80), False)
    pix.set_rect(pix.irect, (120, 140, 200))
    pix.set_rect(fitz.IRect(15, 10, 45, 40), (230, 200, 170))
    page.insert_image(fitz.Rect(440, 100, 520, 207), pixmap=pix)

    page.insert_text((72, y + 40), "Signed: J. Q. Smithers   Account 7731 0094 5521",
                     fontname="cour", fontsize=10)

    # traps
    page.add_text_annot((300, 300), "Internal: Smithers has prior fraud flag, SSN 123-45-6789")
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 140, 300, 155),
                      "uri": "https://crm.internal.example/claim/88213"})

    # page 2 rotated
    p2 = doc.new_page()
    p2.insert_text((72, 100), "Page two: Smithers follow-up call on 03/20/2025.",
                   fontname="helv", fontsize=11)
    p2.insert_text((72, 130), "Reference PX-2291-4471-0093 confirmed.", fontname="helv", fontsize=11)
    p2.set_rotation(90)

    doc.set_metadata({"title": "Smithers claim 88213", "author": "Jane Adjuster",
                      "subject": "Confidential", "keywords": "smithers fraud"})
    doc.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'><secret>Smithers</secret></x:xmpmeta>")
    doc.set_toc([[1, "Smithers claim", 1], [1, "Follow-up", 2]])
    doc.embfile_add("notes.txt", b"Smithers private notes")
    if has_arial:
        doc.subset_fonts()
    doc.save(path, garbage=3, deflate=True)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
