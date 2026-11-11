"""Entry point for the packaged desktop app."""

import json
import sys
import traceback

try:
    from redactor.desktop import main

    main()
except Exception:
    # A windowed app has no console. During a build's self-test, put the crash
    # where the build script will read it instead of a dialog nobody can click.
    if "--selftest" in sys.argv:
        out = sys.argv[sys.argv.index("--selftest") + 2]
        with open(out + ".json", "w") as fh:
            json.dump({"error": traceback.format_exc()}, fh)
        sys.exit(1)
    raise
