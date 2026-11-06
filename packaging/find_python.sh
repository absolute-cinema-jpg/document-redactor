# Finds a Python new enough for this project. Sourced by run.sh and build_mac.sh.

PYTHON_HELP="Python 3.11 or later is needed (macOS's built-in one is too old).
Install it from https://www.python.org/downloads/ or with 'uv python install 3.13',
or point the PYTHON variable at one."

# python_ok ARCH COMMAND...: true if COMMAND is a Python 3.11+ running as ARCH.
python_ok() {
  local arch="$1"; shift
  "$@" -c "import platform, sys; sys.exit(not (sys.version_info >= (3, 11) and platform.machine() == '$arch'))" 2>/dev/null
}

# find_python ARCH [PREFIX...]: prints the first suitable Python, run through PREFIX
# (for example `arch -x86_64`). $PYTHON, when set, is the only one considered.
find_python() {
  local arch="$1" uv_arch="$1" candidate; shift
  [ "$arch" = arm64 ] && uv_arch=aarch64
  if [ -n "${PYTHON:-}" ]; then
    python_ok "$arch" "$@" "$PYTHON" && echo "$PYTHON"
    return
  fi
  for candidate in python3.13 python3.12 python3.11 python3 \
      "$HOME/.local/share/uv/python/cpython-3.13-macos-$uv_arch-none/bin/python3" \
      /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
      /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if python_ok "$arch" "$@" "$candidate"; then echo "$candidate"; return; fi
  done
  return 1
}
