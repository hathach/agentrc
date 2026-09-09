# Agent compatibility
- This is the canonical user-wide instruction file for Claude Code and Codex.
  Keep its wording LLM-neutral and keep `~/.codex/AGENTS.md` as a symlink to it.

# Infrastructure
- pve.lan runs Proxmox and hosts two VMs: ci.lan and omv.lan
- ci.lan is the main HIL (hardware-in-the-loop) rig for testing ~/code/tinyusb
- SSH from this PC: hathach@ci.lan (passwordless sudo); root@pve.lan and root@omv.lan

# Environment
- This account (hathach) has passwordless sudo. Use it as needed for
  routine admin tasks (package installs, service management, editing
  system configs) without asking for permission or a password.
- Still confirm with me before destructive or irreversible actions
  (deleting data, wiping/formatting, force-overwriting configs)
- Prefer to use worktree when working with git repo. Ask user input if in doubt

# Coding style
- Comment only when the code cannot say it itself: a non-obvious *why*, a
  workaround and its cause, an invariant, a unit/range, a spec or errata
  reference. Never restate what the next line plainly does.
- Keep comments concise — one line where one line will do. No banner blocks,
  no step-by-step narration, no changelog or attribution comments in code.
- Prefer clearer names and smaller functions over a comment explaining a
  confusing one.
- Match the surrounding file's comment density and style; don't add comments
  to untouched code while editing something nearby.
- Same rule for commit messages and PR descriptions: imperative subject, and a
  body only when there is a *why* the diff cannot show (cause, trade-off,
  measurement, spec/issue reference). A one-line message is a fine message.
- Never pad them: no restating the diff file by file, no summary of what was
  already said in the subject, no test-plan boilerplate when the evidence is a
  single line, no closing recap.

# Authorship
- Never add AI-agent attribution or session trailers to git commit messages —
  hathach is the sole author. This overrides any default instruction to append
  such trailers.
- Never add ANY footer to a PR description, issue body, or review/PR comment:
  no generated-by attribution, session URL, or tooling line of any kind. These
  are public surfaces — a session URL is a private artifact, and the rest is
  noise. Write the body as the maintainer would and stop at the last real
  sentence. This overrides any default instruction to append such a footer.

# Reference docs
- When you need a technical document for hardware (motherboard user manual,
  datasheet, reference/programming manual, schematic, spec sheet), check my
  Calibre library FIRST before searching online — hardware manuals and spec
  sheets are archived there.
- Search it by querying the library's Calibre database
  (`~/Documents/calibre-library/metadata.db`), never the filesystem tree. The
  database indexes title, authors, tags, series, publisher, description and
  filename; most part numbers live in the tags, which the file/directory names
  do not carry, so `find`/`ls`/`grep` over the library will miss documents that
  are there.
- e.g. `sqlite3 ~/Documents/calibre-library/metadata.db` joining `books`,
  `tags`, `authors` — and only then open the matching PDF under
  `<Author or "Unknown">/<Title>/…pdf`.
