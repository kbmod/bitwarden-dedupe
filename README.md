# Bitwarden duplicate mover

Local helper that finds duplicate **login** items in your Bitwarden vault and
moves the extras into a folder named **Duplicates - Review** so you can inspect
and delete them in the Bitwarden app.

It talks only to the official Bitwarden CLI (`bw`). Nothing is uploaded
anywhere else. Passwords and TOTP secrets are never written to the report.

A browser extension cannot do this: Chrome/Firefox isolate extensions from each
other, so a third-party extension cannot read the Bitwarden vault.

## Setup

```bash
# Official CLI
npm install -g @bitwarden/cli
# or: snap install bw
# or: https://bitwarden.com/help/cli/

bw login
export BW_SESSION=$(bw unlock --raw)
```

`BW_SESSION` stays in that terminal only. Lock when you are done: `bw lock`.

## Usage

Dry-run (default — does not change the vault):

```bash
cd ~/bitwarden-dedupe
python3 dedupe_vault.py --report /tmp/bw-dupes.json
```

Move extras into **Duplicates - Review**:

```bash
python3 dedupe_vault.py --apply --report /tmp/bw-dupes.json
```

Then open Bitwarden, open that folder, compare, and delete what you do not
need. **This script never deletes items.**

Undo the moves from the report:

```bash
python3 dedupe_vault.py --undo /tmp/bw-dupes.json        # preview
python3 dedupe_vault.py --undo /tmp/bw-dupes.json --apply
```

## What counts as a duplicate

| `--strategy` | Two logins match when… |
| --- | --- |
| `host-user` (default) | Same website host + same username (`github.com` = `www.github.com`) |
| `name-user` | Same item name + same username |
| `exact` | Same name, username, password, and hosts |

The copy that stays put (`--keep`, default `richest`) is the one with 2FA /
notes / extra URIs / attachments when possible, otherwise the newest revision.

`--move-groups` moves every item in a group (including the keeper) so you can
review the whole set in one folder. Autofill will not see those logins until
you move a keeper back.

## Options

```
--apply                 actually move items
--strategy host-user    host-user | name-user | exact
--keep richest          richest | newest | oldest
--move-groups           move keepers too
--folder NAME           review folder (default: Duplicates - Review)
--skip-org              ignore organization items
--include-empty-usernames
--from-export FILE      analyze an unencrypted JSON export (report only)
--report FILE           write a password-free JSON report
--undo FILE             restore folderIds from a report
--no-sync               skip `bw sync`
```

## Safety

- Dry-run unless you pass `--apply`.
- Does not delete, merge, or change passwords.
- Report JSON omits passwords and TOTP.
- Organization items can be filed into your personal folders; use `--skip-org`
  if you do not want those touched.
- `--from-export` never writes back to Bitwarden.
