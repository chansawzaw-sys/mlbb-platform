"""Story Video asset factory (build 216; expression pack
build 218) — draws the whole cartoon asset library in code
with PIL: 10 backgrounds (720x1280) and 10 characters
(transparent PNGs), each character also in 5 expression
variants (neutral / happy / sad / surprised / angry — face
variants on the same body, same flat ink style).

The art is ORIGINAL flat-style work drawn for this product
(simple shapes, one ink outline colour, one warm palette) —
the same cast in every video is the consistency trick the
reference workflow gets from code. Nothing here is copied
from any third-party artwork.

Run at BUILD time in the sandbox (needs Pillow, which is in
requirements.txt):  .venv/bin/python assets/story/make_assets.py
Outputs land next to this script: bg/<name>.png and
chars/<name>.png. The .png.b64 companions the deploy channel
needs are produced by the build's mirror step, not here.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SS = 2                      # supersample factor (smoother edges)
INK = (38, 50, 56, 255)     # the one ink outline colour
SKIN = (242, 199, 155, 255)
SKIN2 = (226, 172, 120, 255)
HAIR = (38, 32, 30, 255)
GRAY = (196, 200, 204, 255)

BW, BH = 720, 1280          # background size
CW, CH = 360, 540           # character canvas


def _sc(box):
    return [v * SS for v in box]


class Pen:
    """ImageDraw wrapper that supersamples every coordinate."""

    def __init__(self, img):
        self.d = ImageDraw.Draw(img)

    def rect(self, box, **kw):
        self.d.rectangle(_sc(box), **kw)

    def ell(self, box, **kw):
        self.d.ellipse(_sc(box), **kw)

    def poly(self, pts, **kw):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], **kw)

    def line(self, pts, **kw):
        if "width" in kw:
            kw["width"] = kw["width"] * SS
        self.d.line([(x * SS, y * SS) for x, y in pts], **kw)

    def arc(self, box, a0, a1, **kw):
        if "width" in kw:
            kw["width"] = kw["width"] * SS
        self.d.arc(_sc(box), a0, a1, **kw)

    def pieslice(self, box, a0, a1, **kw):
        self.d.pieslice(_sc(box), a0, a1, **kw)


def canvas(w, h, color=(0, 0, 0, 0)):
    return Image.new("RGBA", (w * SS, h * SS), color)


def finish(img, w, h):
    return img.resize((w, h), Image.LANCZOS)


def vgrad(img, top, bottom):
    """Vertical gradient fill over the whole image."""
    w, h = img.size
    strip = Image.new("RGBA", (1, 256))
    for i in range(256):
        t = i / 255.0
        strip.putpixel((0, i), tuple(
            int(top[c] + (bottom[c] - top[c]) * t) for c in range(4)))
    img.alpha_composite(strip.resize((w, h)))


def sun(p, x, y, r=46, color=(255, 214, 92, 255)):
    p.ell([x - r, y - r, x + r, y + r], fill=color)
    p.ell([x - r, y - r, x + r, y + r], outline=(250, 176, 32, 255),
          width=4)


def cloud(p, x, y, s=1.0, color=(255, 255, 255, 235)):
    for dx, dy, r in ((-52, 8, 30), (0, -8, 42), (54, 8, 32),
                      (24, 14, 30), (-26, 14, 30)):
        p.ell([x + (dx - r) * s, y + (dy - r) * s,
               x + (dx + r) * s, y + (dy + r) * s], fill=color)


def hills(p, y, color, amp=46, seed=3):
    pts = [(-20, BH)]
    x = -20
    i = seed
    while x < BW + 20:
        pts.append((x, y - ((i * 37) % amp)))
        x += 90
        i += 1
    pts.append((BW + 20, BH))
    p.poly(pts, fill=color)


def tree(p, x, y, s=1.0, leaf=(56, 142, 60, 255),
         leaf2=(76, 166, 82, 255), trunk=(110, 76, 50, 255)):
    p.rect([x - 9 * s, y - 92 * s, x + 9 * s, y + 8 * s], fill=trunk)
    for dx, dy, r in ((0, -168, 74), (-58, -120, 52), (58, -120, 52),
                      (0, -104, 56)):
        p.ell([x + (dx - r) * s, y + (dy - r) * s,
               x + (dx + r) * s, y + (dy + r) * s], fill=leaf)
    for dx, dy, r in ((-24, -176, 30), (30, -140, 24)):
        p.ell([x + (dx - r) * s, y + (dy - r) * s,
               x + (dx + r) * s, y + (dy + r) * s], fill=leaf2)


def palm(p, x, y, s=1.0):
    pts = [(x + int(26 * s * math.sin(t / 9.0)), y - int(t * 14 * s))
           for t in range(10)]
    p.line(pts, fill=(121, 85, 52, 255), width=max(3, int(9 * s)))
    tx, ty = pts[-1]
    for a in (-165, -120, -75, -30, 10):
        mx = tx + int(52 * s * math.cos(math.radians(a)))
        my = ty + int(40 * s * math.sin(math.radians(a))) - int(10 * s)
        ex = tx + int(100 * s * math.cos(math.radians(a)))
        ey = my + int(38 * s)
        p.line([(tx, ty), (mx, my), (ex, ey)],
               fill=(46, 139, 87, 255), width=max(3, int(8 * s)))


def hut(p, x, y, s=1.0, wall=(222, 184, 135, 255),
        roof=(150, 104, 58, 255)):
    p.rect([x - 78 * s, y - 96 * s, x + 78 * s, y], fill=wall,
           outline=INK, width=3)
    p.poly([(x - 98 * s, y - 92 * s), (x + 98 * s, y - 92 * s),
            (x + 58 * s, y - 168 * s), (x - 58 * s, y - 168 * s)],
           fill=roof, outline=INK)
    for i in range(-3, 4):
        p.line([(x + i * 24 * s, y - 100 * s),
                (x + i * 15 * s, y - 160 * s)],
               fill=(120, 80, 40, 255), width=3)
    p.rect([x - 20 * s, y - 66 * s, x + 20 * s, y],
           fill=(96, 62, 34, 255), outline=INK, width=3)
    p.rect([x - 62 * s, y - 76 * s, x - 34 * s, y - 44 * s],
           fill=(255, 236, 170, 255), outline=INK, width=3)


# ---------------- backgrounds ----------------

def bg_base(sky_top, sky_bot, ground):
    img = canvas(BW, BH)
    vgrad(img, sky_top, sky_bot)
    p = Pen(img)
    p.rect([0, 830, BW, BH], fill=ground)
    return img, p


def bg_village():
    img, p = bg_base((135, 206, 250, 255), (214, 240, 253, 255),
                     (124, 179, 96, 255))
    sun(p, 606, 128)
    cloud(p, 150, 120), cloud(p, 470, 210, 0.8)
    hills(p, 800, (96, 165, 90, 255))
    p.rect([0, 830, BW, BH], fill=(124, 179, 96, 255))
    p.poly([(0, BH), (300, 830), (430, 830), (240, BH)],
           fill=(196, 164, 116, 255))
    hut(p, 128, 856, 0.95)
    hut(p, 596, 872, 0.8, wall=(232, 198, 152, 255))
    palm(p, 268, 880, 1.0)
    palm(p, 668, 900, 0.8)
    tree(p, 44, 940, 0.9)
    return finish(img, BW, BH)


def bg_town():
    img, p = bg_base((147, 210, 245, 255), (220, 242, 252, 255),
                     (158, 158, 158, 255))
    sun(p, 96, 120)
    cloud(p, 520, 140), cloud(p, 240, 230, 0.8)
    palette = [(239, 154, 154), (255, 204, 128), (179, 157, 217),
               (144, 202, 249), (165, 214, 167)]
    x = -10
    for i, wdt in enumerate((128, 104, 148, 112, 138, 120)):
        top = 300 + (i * 67) % 170
        col = palette[i % len(palette)]
        p.rect([x, top, x + wdt, 880], fill=col + (255,),
               outline=INK, width=3)
        for wy in range(top + 34, 840, 74):
            for wx in range(x + 16, x + wdt - 24, 40):
                p.rect([wx, wy, wx + 24, wy + 34],
                       fill=(255, 244, 190, 255), outline=INK, width=2)
        x += wdt + 6
    p.rect([0, 880, BW, BH], fill=(120, 120, 120, 255))
    for wx in range(20, BW, 110):
        p.rect([wx, 1052, wx + 56, 1064], fill=(245, 245, 245, 255))
    p.poly([(60, 880), (240, 880), (224, 812), (76, 812)],
           fill=(229, 57, 53, 255), outline=INK)
    p.rect([84, 880, 216, 940], fill=(255, 241, 205, 255),
           outline=INK, width=3)
    return finish(img, BW, BH)


def bg_forest():
    img, p = bg_base((120, 190, 130, 255), (186, 226, 170, 255),
                     (62, 110, 54, 255))
    for x, s in ((60, 1.5), (180, 1.9), (330, 1.6), (470, 2.0),
                 (610, 1.7), (700, 1.4)):
        tree(p, x, 900, s, leaf=(34, 106, 46, 255),
             leaf2=(46, 125, 50, 255))
    p.rect([0, 980, BW, BH], fill=(52, 94, 46, 255))
    for x, s in ((110, 1.0), (560, 1.1), (300, 0.8)):
        tree(p, x, 1150, s, leaf=(43, 122, 54, 255),
             leaf2=(67, 160, 71, 255))
    for bx, by, s in ((90, 1120, 1.0), (620, 1080, 0.8),
                      (430, 1180, 0.9)):
        p.ell([bx - 44 * s, by - 30 * s, bx + 44 * s, by + 30 * s],
              fill=(76, 166, 82, 255), outline=INK, width=3)
    return finish(img, BW, BH)


def bg_river():
    img, p = bg_base((135, 206, 250, 255), (215, 240, 252, 255),
                     (94, 178, 224, 255))
    sun(p, 110, 130)
    cloud(p, 480, 150), cloud(p, 200, 240, 0.75)
    hills(p, 690, (110, 178, 100, 255))
    p.rect([0, 690, BW, 1060], fill=(94, 178, 224, 255))
    for i in range(26):
        x = (i * 173) % 660 + 20
        y = 730 + (i * 89) % 290
        p.line([(x, y), (x + 44, y)], fill=(190, 230, 252, 255),
               width=4)
    p.poly([(0, BH), (0, 1060), (BW, 1010), (BW, BH)],
           fill=(233, 213, 160, 255))
    p.poly([(120, 950), (430, 950), (380, 990), (170, 990)],
           fill=(141, 90, 50, 255), outline=INK)
    p.line([(275, 950), (275, 872)], fill=(96, 62, 34, 255), width=6)
    p.poly([(275, 872), (275, 928), (330, 928)], fill=(255, 255, 255, 255),
           outline=INK)
    tree(p, 664, 1010, 0.9)
    return finish(img, BW, BH)


def bg_field():
    img, p = bg_base((140, 208, 248, 255), (218, 242, 253, 255),
                     (139, 195, 74, 255))
    sun(p, 620, 118)
    cloud(p, 170, 150), cloud(p, 430, 90, 0.8)
    hills(p, 760, (121, 184, 92, 255))
    bands = [(760, (156, 204, 101)), (880, (139, 195, 74)),
             (1000, (165, 214, 110)), (1120, (124, 179, 66))]
    for y, col in bands:
        p.rect([0, y, BW, y + 122], fill=col + (255,))
        p.line([(0, y), (BW, y)], fill=(104, 159, 56, 255), width=4)
        for x in range(30, BW, 92):
            p.line([(x, y + 26), (x - 14, y + 96)],
                   fill=(104, 159, 56, 200), width=3)
    p.ell([470, 806, 660, 856], fill=(178, 226, 245, 220))
    tree(p, 84, 830, 1.1)
    return finish(img, BW, BH)


def bg_market():
    img, p = bg_base((150, 212, 246, 255), (222, 243, 252, 255),
                     (188, 160, 118, 255))
    sun(p, 640, 110, 40)
    cloud(p, 180, 130)
    p.rect([0, 400, BW, 900], fill=(255, 224, 178, 255))
    stalls = [(28, (229, 57, 53)), (262, (30, 136, 229)),
              (496, (251, 140, 0))]
    for x, col in stalls:
        for px in (x + 8, x + 196):
            p.rect([px, 560, px + 12, 900], fill=(121, 85, 52, 255))
        for i in range(6):
            c = col if i % 2 == 0 else (255, 255, 255)
            p.poly([(x + i * 36, 560), (x + (i + 1) * 36, 560),
                    (x + (i + 1) * 36 - 12, 492), (x + i * 36 - 12, 492)],
                   fill=c + (255,), outline=INK)
        p.rect([x, 760, x + 216, 900], fill=(161, 110, 62, 255),
               outline=INK, width=3)
        for fx in range(x + 18, x + 190, 44):
            for fy, r, fc in ((716, 17, (255, 152, 0)),
                              (742, 15, (229, 57, 53)),
                              (726, 14, (124, 179, 66))):
                p.ell([fx, fy, fx + r * 2, fy + r * 2], fill=fc + (255,),
                      outline=INK, width=2)
    p.rect([0, 900, BW, BH], fill=(176, 148, 106, 255))
    return finish(img, BW, BH)


def bg_house():
    img = canvas(BW, BH, (255, 243, 224, 255))
    p = Pen(img)
    p.rect([0, 0, BW, 880], fill=(255, 224, 178, 255))
    p.rect([70, 130, 330, 470], fill=(178, 226, 245, 255),
           outline=(141, 90, 50, 255), width=10)
    p.line([(200, 130), (200, 470)], fill=(141, 90, 50, 255), width=8)
    p.line([(70, 300), (330, 300)], fill=(141, 90, 50, 255), width=8)
    sun(p, 140, 210, 30)
    p.rect([440, 160, 650, 330], fill=(255, 255, 255, 255),
           outline=INK, width=4)
    p.ell([505, 196, 585, 276], fill=(255, 183, 77, 255), outline=INK,
          width=3)
    p.rect([444, 282, 646, 326], fill=(139, 195, 74, 255))
    p.rect([0, 880, BW, BH], fill=(188, 140, 92, 255))
    for x in range(0, BW, 90):
        p.line([(x, 880), (x + 60, BH)], fill=(161, 110, 62, 255),
               width=4)
    for x in range(40, BW - 40, 150):
        p.rect([x, 1010, x + 120, 1090], fill=(183, 58, 44, 255),
               outline=(141, 42, 32, 255), width=3)
        for gx in range(x + 10, x + 112, 26):
            p.line([(gx, 1014), (gx, 1086)], fill=(216, 96, 80, 255),
                   width=3)
    p.rect([250, 700, 470, 740], fill=(121, 85, 52, 255), outline=INK,
           width=3)
    for lx in (272, 428):
        p.rect([lx, 740, lx + 14, 830], fill=(96, 62, 34, 255))
    p.ell([318, 636, 402, 700], fill=(93, 64, 55, 255), outline=INK,
          width=3)
    p.arc([330, 590, 390, 660], 180, 360, fill=(120, 120, 120, 255),
          width=6)
    return finish(img, BW, BH)


def bg_night():
    img = canvas(BW, BH)
    vgrad(img, (13, 20, 54, 255), (38, 55, 110, 255))
    p = Pen(img)
    import random
    rnd = random.Random(216)
    for _ in range(90):
        x, y, r = rnd.randrange(8, BW - 8), rnd.randrange(20, 700), \
            rnd.choice((2, 3, 3, 4))
        p.ell([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 230))
    p.ell([548, 84, 668, 204], fill=(255, 241, 180, 255))
    p.ell([528, 70, 620, 162], fill=(23, 34, 74, 255))
    hills(p, 860, (16, 26, 52, 255))
    p.rect([0, 900, BW, BH], fill=(13, 20, 40, 255))
    for x, y, s in ((90, 930, 0.9), (330, 950, 0.75), (560, 936, 0.85)):
        p.rect([x - 70 * s, y - 84 * s, x + 70 * s, y],
               fill=(10, 16, 34, 255))
        p.poly([(x - 88 * s, y - 80 * s), (x + 88 * s, y - 80 * s),
                (x, y - 150 * s)], fill=(8, 12, 28, 255))
        p.rect([x - 18 * s, y - 56 * s, x + 18 * s, y - 20 * s],
               fill=(255, 196, 64, 255))
    return finish(img, BW, BH)


def bg_school():
    img, p = bg_base((140, 208, 248, 255), (219, 242, 253, 255),
                     (156, 190, 110, 255))
    sun(p, 90, 116, 42)
    cloud(p, 500, 130)
    p.rect([90, 420, 630, 860], fill=(255, 241, 205, 255),
           outline=INK, width=4)
    p.poly([(60, 430), (660, 430), (580, 330), (140, 330)],
           fill=(183, 58, 44, 255), outline=INK)
    p.rect([330, 640, 390, 860], fill=(121, 85, 52, 255), outline=INK,
           width=3)
    for wx in (140, 460):
        for wy in (480, 590):
            p.rect([wx, wy, wx + 110, wy + 76],
                   fill=(178, 226, 245, 255), outline=INK, width=3)
    p.rect([352, 210, 360, 340], fill=(96, 62, 34, 255))
    p.poly([(360, 214), (448, 232), (360, 252)], fill=(229, 57, 53, 255),
           outline=INK)
    tree(p, 40, 900, 1.0)
    tree(p, 690, 900, 1.0)
    p.rect([0, 980, BW, BH], fill=(141, 178, 96, 255))
    return finish(img, BW, BH)


def bg_bridge():
    img, p = bg_base((137, 207, 249, 255), (216, 241, 253, 255),
                     (94, 178, 224, 255))
    sun(p, 616, 122)
    cloud(p, 160, 140), cloud(p, 420, 220, 0.8)
    p.rect([0, 760, BW, BH], fill=(94, 178, 224, 255))
    for i in range(22):
        x = (i * 211) % 660 + 16
        y = 900 + (i * 97) % 320
        p.line([(x, y), (x + 40, y)], fill=(190, 230, 252, 255),
               width=4)
    p.arc([-60, 470, 780, 1010], 180, 360, fill=(183, 58, 44, 255),
          width=64)
    p.line([(0, 742), (BW, 742)], fill=(141, 42, 32, 255), width=10)
    for x in range(36, BW, 84):
        p.rect([x, 668, x + 12, 742], fill=(183, 58, 44, 255))
    p.poly([(0, 760), (150, 760), (0, 900)], fill=(124, 179, 96, 255))
    p.poly([(BW, 760), (570, 760), (BW, 900)], fill=(124, 179, 96, 255))
    tree(p, 44, 800, 0.8)
    tree(p, 690, 800, 0.8)
    return finish(img, BW, BH)


# ---------------- characters ----------------

def face(p, cx, cy, r, hair_top=True, gray=False, expr="neutral"):
    p.ell([cx - r, cy - r, cx + r, cy + r], fill=SKIN, outline=INK,
          width=4)
    if hair_top:
        p.pieslice([cx - r, cy - r, cx + r, cy + r * 0.72], 180, 360,
                   fill=GRAY if gray else HAIR)
    # Brows carry the feeling for sad / angry / surprised;
    # the neutral face (the build-216 original) has none.
    if expr == "sad":
        p.line([(cx - 0.52 * r, cy - 0.30 * r),
                (cx - 0.16 * r, cy - 0.44 * r)], fill=INK, width=4)
        p.line([(cx + 0.52 * r, cy - 0.30 * r),
                (cx + 0.16 * r, cy - 0.44 * r)], fill=INK, width=4)
    elif expr == "angry":
        p.line([(cx - 0.52 * r, cy - 0.44 * r),
                (cx - 0.16 * r, cy - 0.28 * r)], fill=INK, width=5)
        p.line([(cx + 0.52 * r, cy - 0.44 * r),
                (cx + 0.16 * r, cy - 0.28 * r)], fill=INK, width=5)
    elif expr == "surprised":
        p.arc([cx - 0.54 * r, cy - 0.52 * r, cx - 0.14 * r,
               cy - 0.30 * r], 180, 360, fill=INK, width=4)
        p.arc([cx + 0.14 * r, cy - 0.52 * r, cx + 0.54 * r,
               cy - 0.30 * r], 180, 360, fill=INK, width=4)
    for ex in (-0.36, 0.36):
        exx = cx + ex * r
        if expr == "happy":
            # closed happy eyes (upward arcs)
            p.arc([exx - 10, cy - 2, exx + 10, cy + 16], 180, 360,
                  fill=INK, width=4)
        elif expr == "surprised":
            p.ell([exx - 8, cy - 4, exx + 8, cy + 16], fill=INK)
        else:
            p.ell([exx - 5, cy + 2, exx + 5, cy + 14],
                  fill=INK)
    if expr == "happy":
        p.arc([cx - r * 0.40, cy + r * 0.10, cx + r * 0.40,
               cy + r * 0.72], 10, 170, fill=INK, width=5)
    elif expr == "sad":
        p.arc([cx - r * 0.32, cy + r * 0.40, cx + r * 0.32,
               cy + r * 0.78], 200, 340, fill=INK, width=4)
    elif expr == "surprised":
        p.ell([cx - 10, cy + r * 0.30, cx + 10, cy + r * 0.30 + 26],
              fill=(120, 60, 40, 255), outline=INK, width=3)
    elif expr == "angry":
        p.line([(cx - r * 0.28, cy + r * 0.44),
                (cx + r * 0.28, cy + r * 0.44)], fill=INK, width=4)
    else:
        p.arc([cx - r * 0.34, cy + r * 0.18, cx + r * 0.34,
               cy + r * 0.62], 20, 160, fill=INK, width=4)


def human(shirt, bottom, long_bottom=True, girl=False, elder=False,
          bun=False, beard=False, stick=False, shawl=None,
          expr="neutral"):
    img = canvas(CW, CH)
    p = Pen(img)
    cx = CW // 2
    gray = elder
    if girl:
        p.poly([(cx - 62, 66), (cx + 62, 66), (cx + 84, 300),
                (cx - 84, 300)], fill=GRAY if gray else HAIR)
    if long_bottom:
        p.rect([cx - 56, 300, cx + 56, 500], fill=bottom, outline=INK,
               width=4)
        for gx in range(cx - 40, cx + 44, 28):
            p.line([(gx, 312), (gx, 492)], fill=(255, 255, 255, 90),
                   width=5)
    else:
        p.rect([cx - 52, 300, cx + 52, 392], fill=bottom, outline=INK,
               width=4)
        p.rect([cx - 50, 392, cx - 8, 496], fill=SKIN2, outline=INK,
               width=4)
        p.rect([cx + 8, 392, cx + 50, 496], fill=SKIN2, outline=INK,
               width=4)
    p.ell([cx - 58, 492, cx - 6, 516], fill=(62, 39, 35, 255),
          outline=INK, width=3)
    p.ell([cx + 6, 492, cx + 58, 516], fill=(62, 39, 35, 255),
          outline=INK, width=3)
    for sx in (-1, 1):
        p.line([(cx + sx * 62, 190), (cx + sx * 84, 330)],
               fill=SKIN2, width=20)
        p.line([(cx + sx * 60, 178), (cx + sx * 76, 268)],
               fill=shirt, width=30)
    p.rect([cx - 62, 168, cx + 62, 316], fill=shirt, outline=INK,
           width=4)
    if shawl:
        p.poly([(cx - 62, 168), (cx + 62, 168), (cx, 300)],
               fill=shawl, outline=INK)
    if bun:
        p.ell([cx - 22, 6, cx + 22, 48], fill=GRAY if gray else HAIR,
              outline=INK, width=3)
    face(p, cx, 108, 62, gray=gray, expr=expr)
    if beard:
        p.pieslice([cx - 46, 108, cx + 46, 196], 0, 180, fill=GRAY)
        face_eyes = None
    if stick:
        p.line([(cx + 108, 250), (cx + 108, 512)],
               fill=(121, 85, 52, 255), width=9)
        p.arc([cx + 84, 218, cx + 132, 266], 180, 360,
              fill=(121, 85, 52, 255), width=9)
    return finish(img, CW, CH)


def ch_dog(expr="neutral"):
    img = canvas(CW, CH)
    p = Pen(img)
    body = (176, 124, 74, 255)
    p.ell([52, 250, 268, 380], fill=body, outline=INK, width=4)
    for lx in (84, 128, 196, 240):
        p.rect([lx, 356, lx + 20, 452], fill=body, outline=INK, width=3)
        p.ell([lx - 4, 442, lx + 28, 460], fill=body, outline=INK,
              width=3)
    p.arc([238, 168, 330, 300], 300, 80, fill=body, width=16)
    p.ell([232, 196, 336, 300], fill=body, outline=INK, width=4)
    if expr == "sad":
        # ear droops down/forward
        p.poly([(250, 218), (290, 214), (264, 274)],
               fill=(140, 96, 52, 255), outline=INK)
    else:
        p.poly([(252, 210), (292, 204), (268, 268)],
               fill=(140, 96, 52, 255), outline=INK)
    p.ell([292, 246, 344, 282], fill=(196, 150, 100, 255), outline=INK,
          width=3)
    p.ell([330, 252, 346, 268], fill=INK)
    if expr == "happy":
        p.arc([264, 220, 286, 240], 180, 360, fill=INK, width=4)
        # tongue out under the snout
        p.ell([306, 270, 332, 302], fill=(255, 150, 170, 255),
              outline=INK, width=3)
    elif expr == "surprised":
        p.ell([264, 220, 286, 242], fill=INK)
    else:
        p.ell([268, 224, 282, 238], fill=INK)
        p.ell([262, 220, 270, 228], fill=(255, 255, 255, 255))
    if expr == "angry":
        p.line([(258, 216), (288, 224)], fill=INK, width=4)
    return finish(img, CW, CH)


def ch_cat(expr="neutral"):
    img = canvas(CW, CH)
    p = Pen(img)
    body = (245, 152, 52, 255)
    p.arc([196, 330, 356, 470], 180, 330, fill=body, width=26)
    p.ell([88, 300, 272, 500], fill=body, outline=INK, width=4)
    p.ell([96, 148, 264, 316], fill=body, outline=INK, width=4)
    p.poly([(108, 168), (122, 96), (168, 142)], fill=body, outline=INK)
    p.poly([(252, 168), (238, 96), (192, 142)], fill=body, outline=INK)
    p.poly([(118, 156), (126, 116), (152, 144)], fill=(255, 183, 197, 255))
    p.poly([(242, 156), (234, 116), (208, 144)], fill=(255, 183, 197, 255))
    for ex in (140, 206):
        if expr == "happy":
            p.arc([ex - 1, 218, ex + 15, 238], 180, 360, fill=INK,
                  width=3)
        elif expr == "surprised":
            p.ell([ex - 3, 220, ex + 17, 244], fill=INK)
        else:
            p.ell([ex, 224, ex + 14, 240], fill=INK)
    if expr == "angry":
        p.line([(130, 202), (162, 214)], fill=INK, width=3)
        p.line([(230, 202), (198, 214)], fill=INK, width=3)
    p.poly([(172, 252), (188, 252), (180, 264)], fill=(233, 30, 99, 255))
    if expr == "happy":
        p.arc([150, 246, 210, 290], 10, 170, fill=INK, width=4)
    elif expr == "sad":
        p.arc([162, 266, 198, 292], 200, 340, fill=INK, width=3)
    elif expr == "surprised":
        p.ell([172, 266, 188, 284], fill=(150, 70, 60, 255),
              outline=INK, width=2)
    elif expr == "angry":
        p.line([(164, 270), (196, 270)], fill=INK, width=3)
    else:
        p.arc([156, 252, 204, 284], 20, 160, fill=INK, width=3)
    for wy in (238, 256):
        p.line([(104, wy), (148, wy + 4)], fill=INK, width=3)
        p.line([(256, wy), (212, wy + 4)], fill=INK, width=3)
    for sx in (120, 226):
        p.ell([sx, 452, sx + 44, 478], fill=body, outline=INK, width=3)
    return finish(img, CW, CH)


def ch_bird(expr="neutral"):
    img = canvas(CW, CH)
    p = Pen(img)
    body = (66, 165, 245, 255)
    p.ell([96, 190, 280, 320], fill=body, outline=INK, width=4)
    p.ell([228, 128, 322, 222], fill=body, outline=INK, width=4)
    if expr == "happy":
        # beak open, singing
        p.poly([(314, 156), (358, 168), (312, 180)], fill=(255, 167, 38, 255),
               outline=INK)
        p.poly([(312, 182), (352, 190), (310, 194)],
               fill=(255, 167, 38, 255), outline=INK)
    elif expr == "surprised":
        p.poly([(314, 152), (360, 164), (312, 178)],
               fill=(255, 167, 38, 255), outline=INK)
        p.poly([(312, 184), (356, 198), (310, 196)],
               fill=(255, 167, 38, 255), outline=INK)
    elif expr == "sad":
        p.poly([(314, 166), (350, 182), (312, 192)],
               fill=(255, 167, 38, 255), outline=INK)
    else:
        p.poly([(314, 158), (356, 176), (312, 188)], fill=(255, 167, 38, 255),
               outline=INK)
    if expr == "happy":
        p.arc([268, 148, 292, 168], 180, 360, fill=INK, width=4)
    elif expr == "surprised":
        p.ell([268, 148, 292, 172], fill=INK)
    else:
        p.ell([272, 152, 288, 168], fill=INK)
        p.ell([276, 155, 282, 161], fill=(255, 255, 255, 255))
    if expr == "angry":
        p.line([(264, 146), (292, 152)], fill=INK, width=4)
    p.ell([130, 208, 226, 282], fill=(129, 212, 250, 255), outline=INK,
          width=3)
    p.poly([(104, 240), (44, 208), (58, 268), (40, 300), (110, 282)],
           fill=(30, 136, 229, 255), outline=INK)
    p.rect([258, 128, 300, 148], fill=(30, 136, 229, 255))
    for lx in (168, 216):
        p.line([(lx, 316), (lx, 372)], fill=(255, 167, 38, 255),
               width=6)
        p.line([(lx - 12, 372), (lx + 14, 372)],
               fill=(255, 167, 38, 255), width=5)
    return finish(img, CW, CH)


def ch_buffalo(expr="neutral"):
    img = canvas(CW, CH)
    p = Pen(img)
    body = (128, 139, 148, 255)
    dark = (96, 106, 114, 255)
    p.ell([30, 190, 300, 350], fill=body, outline=INK, width=4)
    p.ell([66, 168, 176, 240], fill=dark)
    for lx in (64, 116, 216, 262):
        p.rect([lx, 322, lx + 24, 452], fill=body, outline=INK, width=3)
        p.rect([lx, 438, lx + 24, 456], fill=INK)
    p.line([(34, 230), (14, 360)], fill=dark, width=9)
    p.poly([(8, 356), (22, 356), (16, 396)], fill=INK)
    p.ell([238, 200, 348, 330], fill=body, outline=INK, width=4)
    p.ell([286, 282, 344, 322], fill=(160, 170, 178, 255), outline=INK,
          width=3)
    p.ell([300, 292, 314, 306], fill=INK)
    p.ell([322, 292, 336, 306], fill=INK)
    p.arc([216, 128, 300, 216], 200, 330, fill=(236, 230, 220, 255),
          width=13)
    p.arc([286, 128, 370, 216], 210, 340, fill=(236, 230, 220, 255),
          width=13)
    if expr == "happy":
        p.arc([248, 222, 272, 244], 180, 360, fill=INK, width=4)
    elif expr == "surprised":
        p.ell([248, 222, 272, 248], fill=INK)
    else:
        p.ell([252, 226, 268, 244], fill=INK)
        p.ell([256, 229, 263, 236], fill=(255, 255, 255, 255))
    p.poly([(240, 240), (216, 226), (232, 262)], fill=dark, outline=INK)
    if expr == "happy":
        p.arc([294, 312, 338, 334], 20, 160, fill=INK, width=3)
    elif expr == "sad":
        p.arc([294, 320, 338, 340], 200, 340, fill=INK, width=3)
    elif expr == "angry":
        p.arc([246, 212, 274, 238], 180, 360, fill=INK, width=5)
        p.line([(300, 332), (332, 332)], fill=INK, width=3)
    return finish(img, CW, CH)


BACKGROUNDS = {
    "village": bg_village, "town": bg_town, "forest": bg_forest,
    "river": bg_river, "field": bg_field, "market": bg_market,
    "house": bg_house, "night": bg_night, "school": bg_school,
    "bridge": bg_bridge,
}
EXPRESSIONS = ("neutral", "happy", "sad", "surprised", "angry")

CHARACTERS = {
    "boy": lambda expr="neutral": human(
        (66, 165, 245), (255, 112, 67), long_bottom=False, expr=expr),
    "girl": lambda expr="neutral": human(
        (233, 30, 99), (171, 71, 188), girl=True, expr=expr),
    "man": lambda expr="neutral": human(
        (245, 245, 245), (30, 136, 229), expr=expr),
    "woman": lambda expr="neutral": human(
        (255, 167, 38), (142, 36, 170), girl=True, bun=True,
        expr=expr),
    "elder-man": lambda expr="neutral": human(
        (141, 110, 99), (93, 64, 55), elder=True, beard=True,
        stick=True, expr=expr),
    "elder-woman": lambda expr="neutral": human(
        (0, 137, 123), (94, 53, 177), girl=True, elder=True,
        bun=True, shawl=(0, 105, 95), expr=expr),
    "dog": ch_dog, "cat": ch_cat, "bird": ch_bird,
    "buffalo": ch_buffalo,
}


def main():
    (HERE / "bg").mkdir(exist_ok=True)
    (HERE / "chars").mkdir(exist_ok=True)
    for name, fn in BACKGROUNDS.items():
        out = HERE / "bg" / f"{name}.png"
        fn().save(out)
        print("bg", name, out.stat().st_size)
    for name, fn in CHARACTERS.items():
        out = HERE / "chars" / f"{name}.png"
        fn().save(out)
        print("char", name, out.stat().st_size)
        # Build 218: the expression pack — five face variants
        # per character next to the base sprite (the base IS
        # the neutral variant; boy-neutral.png == boy.png).
        for expr in EXPRESSIONS:
            vout = HERE / "chars" / f"{name}-{expr}.png"
            fn(expr).save(vout)
            print("char-expr", name, expr, vout.stat().st_size)


if __name__ == "__main__":
    main()
