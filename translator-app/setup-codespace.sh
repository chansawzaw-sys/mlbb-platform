#!/usr/bin/env bash
# Codespaces trial setup for video-translator.
# Idempotent: safe to re-run. Fetches everything that is NOT in git
# (fonts are binary, the whisper model is ~480MB) and installs deps.
set -e
cd "$(dirname "$0")"

echo "==> [1/5] ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  echo "    ffmpeg already present: $(ffmpeg -version | head -1)"
else
  SUDO=""
  if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ffmpeg
  echo "    ffmpeg installed: $(ffmpeg -version | head -1)"
fi

echo "==> [2/5] Python venv + requirements"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "    venv ready"

echo "==> [3/5] Myanmar fonts -> assets/fonts/ (binary, not in git)"
mkdir -p assets/fonts
FONT_TMP="$(mktemp -d)"
need_font() { [ ! -s "assets/fonts/$1" ]; }

# --- Pyidaungsu 2.3 from the Ubuntu fonts-myanmar .deb (+ Padauk fallback src)
if need_font Pyidaungsu-2.3_Regular.ttf || need_font Pyidaungsu-2.3_Bold.ttf; then
  DEB="$(curl -s http://archive.ubuntu.com/ubuntu/pool/universe/f/fonts-myanmar/ \
    | grep -oE 'fonts-myanmar_[^"]*_all\.deb' | grep -v '~' | sort -u | tail -1)"
  DEB="${DEB:-fonts-myanmar_0.0-1_all.deb}"
  echo "    downloading $DEB"
  curl -sL -o "$FONT_TMP/fonts-myanmar.deb" \
    "http://archive.ubuntu.com/ubuntu/pool/universe/f/fonts-myanmar/$DEB"
  dpkg-deb -x "$FONT_TMP/fonts-myanmar.deb" "$FONT_TMP/fm"
  cp "$FONT_TMP/fm/usr/share/fonts/truetype/mm/Pyidaungsu-2.3_Regular.ttf" assets/fonts/
  cp "$FONT_TMP/fm/usr/share/fonts/truetype/mm/Pyidaungsu-2.3_Bold.ttf" assets/fonts/
fi

# --- Padauk from the Debian fonts-sil-padauk .deb
if need_font Padauk-Regular.ttf || need_font Padauk-Bold.ttf; then
  DEB="$(curl -s https://deb.debian.org/debian/pool/main/f/fonts-sil-padauk/ \
    | grep -oE 'fonts-sil-padauk_[^"]*_all\.deb' | grep -v '~' | sort -uV | tail -1)"
  DEB="${DEB:-fonts-sil-padauk_5.001-1_all.deb}"
  echo "    downloading $DEB"
  curl -sL -o "$FONT_TMP/padauk.deb" \
    "https://deb.debian.org/debian/pool/main/f/fonts-sil-padauk/$DEB"
  dpkg-deb -x "$FONT_TMP/padauk.deb" "$FONT_TMP/pad"
  cp "$FONT_TMP/pad/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf" assets/fonts/
  cp "$FONT_TMP/pad/usr/share/fonts/truetype/padauk/Padauk-Bold.ttf" assets/fonts/
fi

# --- Noto Sans Myanmar from the Debian fonts-noto-core .deb
# (Google Fonts CSS subsets were rejected: their first TTF is the LATIN
# subset with zero Myanmar glyphs. The .deb files are byte-identical to
# the locally verified fonts.)
if need_font NotoSansMyanmar-Regular.ttf || need_font NotoSansMyanmar-Bold.ttf; then
  DEB="$(curl -s https://deb.debian.org/debian/pool/main/f/fonts-noto/ \
    | grep -oE 'fonts-noto-core_[^"]*_all\.deb' | grep -v '~' | sort -uV | tail -1)"
  DEB="${DEB:-fonts-noto-core_20201225-6_all.deb}"
  echo "    downloading $DEB (12MB, extracting only the 2 Myanmar fonts)"
  curl -sL -o "$FONT_TMP/notocore.deb" \
    "https://deb.debian.org/debian/pool/main/f/fonts-noto/$DEB"
  dpkg-deb -x "$FONT_TMP/notocore.deb" "$FONT_TMP/nc"
  cp "$FONT_TMP/nc/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf" assets/fonts/
  cp "$FONT_TMP/nc/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf" assets/fonts/
fi

# --- Verify all six fonts are real font files
for f in NotoSansMyanmar-Regular.ttf NotoSansMyanmar-Bold.ttf \
         Pyidaungsu-2.3_Regular.ttf Pyidaungsu-2.3_Bold.ttf \
         Padauk-Regular.ttf Padauk-Bold.ttf; do
  if ! file "assets/fonts/$f" | grep -qi "font"; then
    echo "ERROR: assets/fonts/$f is not a font file" >&2
    exit 1
  fi
done
rm -rf "$FONT_TMP"
echo "    6 fonts OK"

echo "==> [4/5] faster-whisper small model -> models/faster-whisper-small/"
if [ -s models/faster-whisper-small/model.bin ]; then
  echo "    model already present, skipping"
else
  .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='models/faster-whisper-small')"
  echo "    model downloaded"
fi

echo "==> [5/5] Runtime dirs"
mkdir -p jobs previews uploads

echo "SETUP DONE"
