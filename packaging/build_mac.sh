#!/usr/bin/env bash
# Builds "Sanitization Bureau.app" -- a standalone, double-clickable Mac app with
# Python and every library bundled inside -- then self-tests it and zips it for
# sharing.
#
#   packaging/build_mac.sh            # for this Mac's chip
#   packaging/build_mac.sh x86_64     # Intel build (on Apple Silicon: via Rosetta)
#   packaging/build_mac.sh arm64      # Apple Silicon build
set -euo pipefail
cd "$(dirname "$0")/.."

APP="Sanitization Bureau"
HOST_ARCH="$(uname -m)"
ARCH="${1:-$HOST_ARCH}"
case "$ARCH" in
  arm64)  MACHINE="Apple Silicon Macs (M1 and later)" ;;
  x86_64) MACHINE="Intel Macs (it also runs on Apple Silicon via Rosetta)" ;;
  *) echo "Unknown architecture '$ARCH' (use arm64 or x86_64)"; exit 2 ;;
esac

# Each architecture gets its own Python environment; a cross build runs every
# Python step through `arch`, so it needs a Python with that architecture (universal,
# or `uv python install cpython-3.13-macos-x86_64-none` for Intel). An environment
# on a Python that's too old or the wrong architecture is rebuilt.
if [ "$ARCH" = "$HOST_ARCH" ]; then VENV=".venv"; RUN=(); else VENV=".venv-$ARCH"; RUN=(arch "-$ARCH"); fi
PY=(${RUN[@]+"${RUN[@]}"} "$VENV/bin/python")

source packaging/find_python.sh
if ! python_ok "$ARCH" "${PY[@]}"; then
  BASE="$(find_python "$ARCH" ${RUN[@]+"${RUN[@]}"})" || { echo "$PYTHON_HELP"; exit 1; }
  echo "Creating $VENV ($ARCH) with $BASE..."
  rm -rf "$VENV"
  ${RUN[@]+"${RUN[@]}"} "$BASE" -m venv "$VENV"
  "${PY[@]}" -m pip install -q --upgrade pip
fi
"${PY[@]}" -m pip install -q -r requirements.txt -r packaging/requirements-build.txt
[ -f packaging/icon.icns ] || "${PY[@]}" packaging/make_icon.py

DIST="dist/mac-$ARCH"
WORK="build/mac-$ARCH"
echo "Building $APP.app for $ARCH..."
rm -rf "$WORK" "$DIST/$APP.app"
"${PY[@]}" -m PyInstaller --noconfirm --clean --log-level WARN \
  --windowed --name "$APP" --target-arch "$ARCH" \
  --icon "$PWD/packaging/icon.icns" \
  --osx-bundle-identifier local.sanitization-bureau \
  --paths "$PWD" \
  --add-data "$PWD/redactor/static:redactor/static" \
  --hidden-import fontTools.subset --hidden-import fontTools.ttLib \
  --distpath "$DIST" --workpath "$WORK" --specpath "$WORK" \
  packaging/bureau_app.py

BIN="$DIST/$APP.app/Contents/MacOS/$APP"
echo "  binary: $(file -b "$BIN" | cut -c1-60)"

if [ "${BUREAU_SKIP_SELFTEST:-0}" != "1" ]; then
  echo "Self-testing the built app..."
  TMP="$(mktemp -d)"
  "${PY[@]}" tests/make_sample.py "$TMP/sample.pdf"
  "$BIN" --selftest "$TMP/sample.pdf" "$TMP/out.pdf" > "$TMP/selftest.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 180); do kill -0 $PID 2>/dev/null || break; sleep 1; done
  kill $PID 2>/dev/null || true
  if ! "${PY[@]}" packaging/check_selftest.py "$TMP/out.pdf.json"; then
    tail -20 "$TMP/selftest.log"
    echo "  SELF-TEST FAILED -- not packaging"
    exit 1
  fi
  rm -rf "$TMP"
fi

# The zip unpacks to a folder holding the app plus plain-language instructions.
STAGE="$WORK/stage/$APP"
rm -rf "$WORK/stage" && mkdir -p "$STAGE"
ditto "$DIST/$APP.app" "$STAGE/$APP.app"
sed "s|{{MACHINE}}|$MACHINE|" packaging/READ_ME_FIRST.txt > "$STAGE/READ ME FIRST.txt"
ZIP="dist/Sanitization-Bureau-mac-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --norsrc --keepParent "$STAGE" "$ZIP"
rm -rf "$WORK/stage"
echo
echo "Done ($ARCH):"
echo "  $DIST/$APP.app"
echo "  $ZIP   <- send this to people with $MACHINE"
