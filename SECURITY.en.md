# Security

[Deutsch](SECURITY.md) | **English**

## Reporting vulnerabilities

Please do **not** report vulnerabilities as a public issue. Report them privately through GitHub instead: the "Security" tab, then "Report a vulnerability". A sample file or a command that shows the problem helps, together with the affected version (`skptool --version`).

## What skptool protects against

`skptool` is built to process files from untrusted sources as well. The following protections are built in and covered by tests (`tests/test_sicherheit.py`, `tests/test_live.py`, `tests/test_fuzz.py`). All inputs were also tested with a good 15,000 hostile inputs (`tools/fuzz.py`, repeatable through a fixed seed):

**Untrusted files**

- Code from files is never executed. Blender always runs with script auto-execution disabled (`-Y`), and in the background additionally with factory settings.
- **Network paths** (`\\server\share`, `//server/...`, `file://server/...`) in `.blend`, glTF/GLB, OBJ/MTL, USD, FBX and Alembic always lead to rejection, before Blender opens the file. On Windows, merely accessing such a path would send the credentials (NTLM hash) to the foreign server.
- **External files** are not used by default. Otherwise an untrusted file could pull arbitrary images or data from this computer into an output that you then pass on. Local external files are only used with `--allow-external`. This covers images, linked `.blend` libraries, caches, fonts, file paths in modifiers and import nodes in Geometry Nodes.
- A `.blend` is also checked before it is opened in the Blender window (`skptool open`, `--live`): network paths never, external files only with `--allow-external`.
- ZIP containers (`.skp` from 2021 on, `.usdz`) are limited before reading: at most 8 GB unpacked, 100,000 entries, compression ratio 100. Exploding nesting stops at 5 million placements, and oversized textures are detected from their header alone, without decoding them.
- PLY files are checked against their announced sizes before loading: a header that announces more vertices or faces than fit into the file is rejected before Blender reserves memory for them.
- For version detection, only the first 200 bytes of a `.skp` header are read, never the whole file.
- Blender steps are aborted after one hour (`SKPTOOL_TIMEOUT`).
- Names from files cannot control the terminal: control characters, escape sequences and bidi characters are masked visibly in every output.
- Every error message is a single German line without a Python traceback; `-v` shows the technical details.
- Preview images contain no metadata such as file paths or dates.

**Your own files**

- The input file is never overwritten, not even through other spellings of the same path. When one call converts several files, existing target files are only overwritten with `--force`; duplicate targets and targets that are themselves inputs are rejected.
- Output is written atomically: if a step fails, an existing file stays unchanged.
- `skptool open` runs all checks before writing, and replaces a `.blend` that is newer than its `.skp` only with `--force`.

**Program start**

- `skptool.cmd` never loads Python modules from the current folder, and Blender is never started from the current folder. A `glob.py` or `blender.bat` next to a downloaded model has no effect. The launcher keeps only absolute paths from an inherited `PYTHONPATH`, Python runs with `-P`, and on import `skptool` additionally removes every search path that points to the current folder.
- When installed with pipx, uv tool or pip, a launcher of the package manager starts Python without `-P`. Even then `skptool` loads no modules from the current folder, because the search path starts at the launcher's folder (checked in `tests/test_paket.py`, on Windows and Linux). Limitation: if `PYTHONPATH` contains empty or relative entries, Python itself already loads code from the current folder at startup, before `skptool` can do anything. So do not set `PYTHONPATH`, or only with absolute paths; `skptool.cmd` cleans it up by itself.
- The launcher created by `tools/aufruf_einrichten.py` only calls this project's `skptool.cmd` by its absolute path and never overwrites files it did not create.
- All dependencies are pinned to versions that had been published for at least 14 days when they were chosen, with checksums in `requirements.lock`. An installation with pipx or uv tool uses the same fixed versions, but without checksums. The only exception to the 14-day rule is OpenSKP 1.3.0, approved after reviewing the package and its source code. `mapbox-earcut` is a compiled extension. It is only installed from the lock file with checksums; the package is published through Trusted Publishing from the binding's GitHub project.

**Blender extension (`skptool_io`)**

- Requests only the "files" permission, no network. skptool is only started from the path in the add-on preferences, never from a path taken from a `.blend`, and always without a shell. `.cmd`/`.bat` files are never run directly, so that special characters in file names are not interpreted by cmd.exe.
- External files of the scene are only used with "Allow External Files".

**Editing (`edit`, `live --ops`)**

- Only a fixed set of operations; none of them runs code or touches files.
- Numbers must be finite and within sensible limits, names are limited to 63 characters without control characters, at most 1000 operations per call and 100,000 objects in the scene.

**Live mode**

- The server listens only on `127.0.0.1`. Client and server authenticate each other with HMAC-SHA256 over random challenges; the token itself never goes over the wire. A foreign process on the port learns nothing and cannot forge a valid answer.
- Web pages in the browser cannot make a valid request (the protocol is not HTTP).
- Each request is limited to 1 MB and 10 seconds in total, with at most 8 connections at a time.
- The client, too, waits for an answer only for a limited total time (not per packet): a foreign process on the port cannot hold `skptool live` by sending data drop by drop.
- The status file, its folder and the log are readable only by your own user (0600/0700 on Linux and macOS, symlinks are rejected).
- Images are only written to your own temp folder, and the export target is fixed at startup and is never the original file.

**MCP server (`skptool mcp`)**

An AI assistant can be manipulated by content it reads (prompt injection). The server therefore treats every argument as untrusted:

- Output never overwrites an existing file unless `ueberschreiben: true` ("overwrite") is given, and never overwrites an input. The result is created in a separate temp folder and then moved into place in a way that does not replace a file that appeared in the meantime.
- Only formats that produce exactly one file are allowed as output (including `.3mf`). Inputs only with extensions that skptool knows.
- Network, device and URL paths, network drives, alternate data streams (`file.skp:stream`) and reserved device names are rejected before they are accessed. With `--ordner` ("folder") only files inside the allowed folders are accepted.
- External files that an input refers to are never used; there is no switch for this.
- Operations are validated as with `--ops`, and then again by `ops.py` in Blender. No tool deletes files.
- Each call runs in its own process with a time limit (default 900 s); afterwards the process and any Blender it started are terminated. At most 2 calls work at the same time.
- Text output is limited to 200 KB, control and bidi characters are masked, and stdout carries only JSON-RPC.
- Limitation: names and texts from models reach the assistant as data. Whether it mistakes them for instructions is up to the client. Have the client confirm writing tools, or start the server with `--nur-lesen` ("read only").

## Known limitations

- **Blender and OpenSKP read the files.** Bugs in their readers (such as memory errors in an importer) are outside of `skptool`. For untrusted files, the same applies as when opening them in Blender itself.
- For binary FBX and Alembic files, `skptool` searches the raw file content for network paths. A path hidden in compressed blocks is not found this way. `.blend` (including compressed ones), glTF, OBJ/MTL and USD, on the other hand, are checked completely.
- **cmd.exe** looks for commands in the current folder first. If you type `skptool` in cmd inside an untrusted folder, a `skptool.cmd` or `skptool.bat` there would run first. PowerShell does not search the current folder. In cmd, the environment variable `NoDefaultCurrentDirectoryInExePath=1` helps.
- On Linux, all tests also run in CI with Blender 5.2.0, plus a check with real files in Docker (`tools/linux_e2e.sh`); on macOS without Blender. The window tests of the live mode run on Windows and on Linux on a virtual display (Xvfb), not on macOS.
