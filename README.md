# Bitwarden duplicate mover

Local helper for Bitwarden and **Vaultwarden**. It finds duplicate **login**
items, moves the extras into a folder for review, then deletes whatever is
still in that folder after you have checked it.

Bitwarden desktop has no select-all, so the delete step is the way to clear
the review folder in one go.

It talks only to the official Bitwarden CLI (`bw`). Decryption happens on
your machine. Passwords and TOTP secrets are never written to the report.

A third-party browser extension cannot do this: other extensions cannot read
the Bitwarden vault.

## Requirements

- Python 3.10+
- [Bitwarden CLI](https://bitwarden.com/help/cli/) (`bw`)
- An unlocked CLI session (`BW_SESSION`)

```bash
npm install -g @bitwarden/cli
# or: snap install bw
```

## Point `bw` at your vault

`bw login` never asks for a URL. It uses whatever server you configured last
(Bitwarden cloud by default).

### Vaultwarden / self-hosted

Use the same HTTPS origin as the web vault (no `/#/` path):

```bash
bw logout                              # if you were logged into bitwarden.com
bw config server https://vault.example.com
bw config server                       # should print that URL
bw login
export BW_SESSION=$(bw unlock --raw)
```

While logged out you can also set the URL through this script:

```bash
python3 dedupe_vault.py --server https://vault.example.com
```

That only runs `bw config server`. You still need `bw login` afterward.

### Bitwarden cloud

```bash
bw login
export BW_SESSION=$(bw unlock --raw)
```

`BW_SESSION` is per-terminal. When you are done: `bw lock`.

## Workflow

All commands are dry-run unless you pass `--apply`. Keep the report file; later
steps use it so keepers and unrelated items are not deleted.

### 1. Preview duplicates

```bash
python3 dedupe_vault.py --report /tmp/bw-dupes.json
```

### 2. Move extras into **Duplicates - Review**

The richest copy (2FA, extra URIs, notes) stays where it is. The others go
into the review folder.

```bash
python3 dedupe_vault.py --apply --report /tmp/bw-dupes.json
```

### 3. Review in the Bitwarden app

Open **Duplicates - Review**. Drag false positives out of the folder (back to
wherever they belong). Leave real duplicates in the folder.

### 4. Delete leftovers

Preview, then send remaining extras to Bitwarden **Trash** (recoverable for
about 30 days):

```bash
python3 dedupe_vault.py --delete-reviewed --report /tmp/bw-dupes.json
python3 dedupe_vault.py --delete-reviewed --report /tmp/bw-dupes.json --apply --yes
```

`--report` limits deletion to items this script moved and will not delete the
kept originals.

Without `--report`, every item still in the review folder is deleted. Move
anything you want to keep out of it first.

Skip trash (not recoverable):

```bash
python3 dedupe_vault.py --delete-reviewed --report /tmp/bw-dupes.json --permanent --apply --yes
```

### Undo a move (before you delete)

```bash
python3 dedupe_vault.py --undo /tmp/bw-dupes.json
python3 dedupe_vault.py --undo /tmp/bw-dupes.json --apply
```

## What counts as a duplicate

| `--strategy` | Two logins match when… |
| --- | --- |
| `host-user` (default) | Same website host + same username (`github.com` = `www.github.com`) |
| `name-user` | Same item name + same username |
| `exact` | Same name, username, password, and hosts |

`--keep` (default `richest`) chooses which copy stays put: 2FA / notes / extra
URIs when possible, otherwise the newest revision. `newest` and `oldest` are
also available.

`--move-groups` moves every item in a group, including the keeper, so you can
compare them in one folder. Autofill will not see those logins until you move
a keeper back. Pass the same `--report` on `--delete-reviewed` so keepers are
not deleted.

## Options

```
--apply                 actually move or delete (default is dry-run)
--strategy host-user    host-user | name-user | exact
--keep richest          richest | newest | oldest
--move-groups           move keepers into the review folder too
--folder NAME           review folder (default: Duplicates - Review)
--skip-org              ignore organization items
--include-empty-usernames
--from-export FILE      analyze an unencrypted JSON export (report only)
--report FILE           write a password-free JSON report; with
                        --delete-reviewed, also used as a delete filter
--undo FILE             put moved items back in their previous folders
--delete-reviewed       delete leftovers still in the review folder
--permanent             skip trash (irreversible; with --delete-reviewed)
--include-keepers       also delete keeper copies still in the folder
--yes                   skip the DELETE confirmation prompt
--no-sync               skip `bw sync`
--delay SECONDS         pause between edits (default: 0.15)
--session KEY           session key (default: $BW_SESSION)
--server URL            Vaultwarden/self-hosted origin (`bw config server`)
```

## Safety

- Dry-run unless you pass `--apply`.
- The move step does not delete, merge, or change passwords.
- `--delete-reviewed` defaults to Bitwarden trash, not permanent delete.
- Applying a delete requires typing `DELETE` or passing `--yes`.
- Report JSON omits passwords and TOTP.
- Organization items can be filed into your personal folders; use `--skip-org`
  if you do not want those touched.
- `--from-export` never writes back to the vault.

## Tests

```bash
python3 -m unittest test_dedupe.py
```
