# Drive CSV Uploader

Point it at a folder on this Mac → it uploads everything to Google Drive →
you get a CSV with a shareable link for every file.

## Run it

```bash
~/drive-csv-uploader/run.sh
```

Opens at http://localhost:8501. First run may ask you to click
**Authorize / re-authorize** in the sidebar (opens a browser once).

## What you fill in

| Field | Meaning |
|---|---|
| **Local folder to upload** | Full path, e.g. `/Users/krishnaprasun/Desktop/my-content`. Copy it from Finder with ⌥⌘C. |
| **Drive destination folder** | Slash path created inside My Drive, e.g. `Content/2026-09-02`. Reused if it already exists. |
| **File types** | All / Images / Videos / Images+videos / Documents. |
| **Include subfolders** | Walk the tree instead of just the top level. |
| **Mirror subfolder structure** | Off = everything lands flat in one Drive folder. |
| **Make links viewable by anyone** | Sets *anyone with the link → viewer* on each file. On by default. |
| **Skip files already in the destination** | Matches on file name — a re-run resumes instead of duplicating. |

## The CSV you get back

Downloaded from the browser and also saved to `output/drive_links_<folder>_<timestamp>.csv`.

| Column | |
|---|---|
| `file_name` | Base name, e.g. `reel_01.mp4` |
| `relative_path` | Path within the source folder, e.g. `sub/reel_01.mp4` |
| `local_path` | Where it came from on disk |
| `size_bytes`, `mime_type` | |
| `drive_folder` | Destination path in Drive |
| `drive_file_id` | Drive's id |
| `drive_link` | Normal Drive viewer link — the one to hand to people |
| `direct_link` | Streams/downloads the bytes; use when another tool needs the raw file |
| `preview_link` | Inline image URL — works in `<img>` and in Sheets `=IMAGE(...)` |
| `status` | `uploaded` / `skipped (already there)` / `FAILED: <reason>` |
| `uploaded_at` | |

## Notes

- **Scope is `drive.file`** — this app can only ever see files it created itself,
  never the rest of your Drive. That also means "skip already there" only knows
  about files uploaded through this tool.
- **Failures don't lose work.** A failed file gets `FAILED:` in the `status`
  column and the rest keep going. Re-run with *Skip files already in the
  destination* on and only the failures upload again.
- **Large files are safe** — uploads are resumable and chunked (8 MB), with
  automatic retry/backoff on Drive rate limits and 5xx.
- **Re-auth**: if the OAuth consent screen is still in *Testing* mode in Google
  Cloud, the refresh token dies after 7 days and the sidebar will say
  *"Cached grant is no longer usable"*. Click **Authorize / re-authorize**.
  Publishing the consent screen makes that permanent.
- Runs on the system Python 3.9; the Google libraries print end-of-life
  `FutureWarning`s on startup, which are harmless.

## Files

- `app.py` — the Streamlit UI
- `drive_client.py` — OAuth, folder resolution, resumable upload, sharing
- `credentials.json` / `token.json` — OAuth client + cached grant (git-ignored)
- `output/` — generated CSVs

## Deploying to Render (free)

The app runs in two shapes from the same code. On Render there is no folder of
yours to point at and no browser to run Google's consent flow in, so:

| | Local | Render |
|---|---|---|
| Input | type a folder path | drag files in, or drop a **.zip** of a folder |
| Google auth | one-time browser consent, cached in `token.json` | `GOOGLE_TOKEN_JSON` env var |
| Access | your Mac only | public URL, gated by `APP_PASSWORD` |
| CSV | downloaded **and** saved to `output/` | download only — the disk is wiped on restart |

### One-time setup

1. **Mint a token locally** (skip if `token.json` already exists): run
   `./run.sh`, click *Authorize* in the sidebar, finish the Google consent.
2. Push this repo to GitHub, then in Render: **New → Blueprint**, pick the repo.
   `render.yaml` defines the service.
3. Set the two secret env vars in the Render dashboard:
   - `APP_PASSWORD` — anything you like; the app refuses to serve without it.
   - `GOOGLE_TOKEN_JSON` — the entire contents of `token.json`.
     Copy it with `cat token.json | pbcopy`.
4. Deploy. First load after a period of inactivity takes ~50s (see below).

### Free-tier realities

- **Sleeps after 15 minutes idle.** The next visit takes ~50 seconds to wake.
- **512 MB RAM**, and browser uploads are buffered in memory — keep a batch
  under roughly 200 MB. Bigger jobs are what the local mode is for.
- **Ephemeral disk.** Nothing is archived server-side; download the CSV when
  the run finishes. Uploaded files are staged in a temp dir and deleted after.
- `GOOGLE_TOKEN_JSON` is refreshed in memory only — Render never writes it back,
  so if the grant dies you update the env var, not a file.

### Security notes

- `credentials.json` and `token.json` are git-ignored — the Google grant reaches
  the server only through the Render env var, never through the repo.
- The password gate is a single shared secret, not real user accounts. It stops
  a stranger who finds the URL from writing into your Drive; it is not a
  substitute for SSO if this ever holds anything sensitive.
- The `drive.file` scope means a leaked deploy could add files to your Drive
  and read back only what it created — never the rest of your Drive.
