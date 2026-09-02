#!/usr/bin/env python3
"""Drive CSV Uploader — send content to Google Drive, get back a CSV of links.

Runs in two shapes from the same file:

* **Local** (`streamlit run app.py` on your Mac) — you type a folder path and it
  reads straight off the disk, so size is limited only by your connection.
* **Server** (Render) — there is no local disk to point at, so you drag files or
  a .zip into the browser instead. Everything downstream is identical.
"""
import datetime
import io
import mimetypes
import os
import shutil
import tempfile
import time

import pandas as pd
import streamlit as st

import drive_client as dc
from sources import FILTERS, human_size, scan_folder, stage_uploads

st.set_page_config(page_title="Drive CSV Uploader", page_icon="📁", layout="wide")

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")

# Render sets RENDER=true on every service. On a server the disk is ephemeral
# and there is no folder of the user's to point at, so the UI adapts.
IS_SERVER = bool(os.environ.get("RENDER") or os.environ.get("SERVER_MODE"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()

if not IS_SERVER:
    os.makedirs(OUT_DIR, exist_ok=True)

# ------------------------------------------------------------- password ----
def gate():
    """Block the page behind APP_PASSWORD when one is configured.

    The service URL is public, and uploads land in a real Google Drive, so a
    server deploy without a password would let any visitor write to it.
    """
    if not APP_PASSWORD:
        if IS_SERVER:
            st.error(
                "**APP_PASSWORD is not set.** This service is publicly reachable "
                "and would let anyone upload into your Drive. Set APP_PASSWORD in "
                "the Render dashboard and redeploy."
            )
            st.stop()
        return
    if st.session_state.get("authed"):
        return
    st.title("📁 Drive CSV Uploader")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


gate()


# --------------------------------------------------------------- sidebar ----
st.sidebar.header("Google Drive")
status, detail = dc.token_state()
badge = {"ok": "✅", "refreshable": "🔄", "missing": "⚠️", "expired": "⚠️"}[status]
st.sidebar.write("{} {}".format(badge, detail))

if IS_SERVER:
    if status in ("missing", "expired"):
        st.sidebar.error(
            "Set `GOOGLE_TOKEN_JSON` in the Render dashboard to the contents of a "
            "`token.json` minted by running this tool locally once."
        )
else:
    if st.sidebar.button("Authorize / re-authorize"):
        with st.sidebar:
            with st.spinner("Opening browser…"):
                try:
                    dc.get_service(allow_browser=True)
                    st.success("Authorized.")
                except Exception as exc:
                    st.error(str(exc))
        st.rerun()

st.sidebar.caption(
    "Scope is `drive.file` — this app can only see files it created itself, "
    "never the rest of your Drive."
)
if IS_SERVER:
    st.sidebar.caption(
        "Running on Render free tier: the service sleeps after 15 minutes idle "
        "(first request then takes ~50s), and files pass through the server's "
        "memory, so keep a batch under ~200 MB."
    )

# ------------------------------------------------------------------- main ---
st.title("📁 Drive CSV Uploader")
st.caption("Send content to Drive → download a CSV of file names and their links.")

col_l, col_r = st.columns([3, 2])

src = None          # local folder to read from, when in local mode
uploaded = None     # browser-uploaded files, when in server mode

with col_l:
    if IS_SERVER:
        uploaded = st.file_uploader(
            "Files to upload (or a .zip of a folder)",
            accept_multiple_files=True,
            help="Drag files straight in. To keep a folder structure, zip the "
                 "folder first and drop the .zip — it gets expanded and mirrored "
                 "into Drive.",
        )
    else:
        src = st.text_input(
            "Local folder to upload",
            value=st.session_state.get("src", ""),
            placeholder="/Users/krishnaprasun/Desktop/my-content",
            help="Full path to the folder on this Mac. Copy it from Finder with ⌥⌘C.",
        ).strip().strip("'\"")
        src = os.path.expanduser(src)

    dest = st.text_input(
        "Drive destination folder",
        value=st.session_state.get(
            "dest", "Uploads/{}".format(datetime.date.today().isoformat())),
        help="Slash-separated path created inside My Drive, e.g. "
             "`Content/2026-09-02`. Reused if it already exists.",
    ).strip()

with col_r:
    kind = st.selectbox("File types", list(FILTERS.keys()), index=0)
    if not IS_SERVER:
        recurse = st.checkbox("Include subfolders", value=True)
    else:
        recurse = True
    mirror = st.checkbox(
        "Mirror subfolder structure in Drive", value=True,
        help="Off = every file lands flat in the destination folder.",
    )
    share = st.checkbox("Make links viewable by anyone with the link", value=True)
    skip_existing = st.checkbox(
        "Skip files already in the destination", value=True,
        help="Matches on file name, so re-running after a failure resumes "
             "instead of creating duplicates.",
    )

exts = FILTERS[kind]

# --------------------------------------------------------------- preview ----
files = []
staging = None

if IS_SERVER:
    if uploaded:
        staging = tempfile.mkdtemp(prefix="drivecsv_")
        stage_uploads(uploaded, staging)
        files = scan_folder(staging, exts, True)
        source_label = "upload"
elif src:
    if not os.path.isdir(src):
        st.error("Not a folder: `{}`".format(src))
    else:
        files = scan_folder(src, exts, recurse)
        source_label = os.path.basename(src.rstrip("/")) or "upload"

if files:
    total = sum(os.path.getsize(f) for f, _ in files)
    st.success("Found **{}** files · {} total".format(len(files), human_size(total)))
    with st.expander("Preview file list"):
        st.dataframe(
            pd.DataFrame([
                {"file": rel, "size": human_size(os.path.getsize(f))}
                for f, rel in files[:500]
            ]),
            width="stretch", hide_index=True,
        )
        if len(files) > 500:
            st.caption("Showing first 500 of {}.".format(len(files)))
elif (uploaded or (src and os.path.isdir(src))):
    st.warning("No matching files — check the **File types** filter.")

go = st.button("🚀 Upload to Drive", type="primary", disabled=not files)

# ---------------------------------------------------------------- upload ----
if go:
    try:
        service = dc.get_service(allow_browser=not IS_SERVER)
    except Exception as exc:
        st.error("Could not authorize: {}".format(exc))
        st.stop()

    prog = st.progress(0.0)
    log = st.empty()
    started = time.time()

    folder_cache = {}

    def folder_for(rel_dir):
        """Drive folder id for a relative subdirectory, created on demand."""
        if rel_dir not in folder_cache:
            path = dest if not rel_dir else "{}/{}".format(dest, rel_dir)
            folder_cache[rel_dir] = dc.resolve_folder_path(service, path)
        return folder_cache[rel_dir]

    existing_cache = {}

    def existing_for(folder_id):
        if folder_id not in existing_cache:
            existing_cache[folder_id] = (
                dc.list_folder_files(service, folder_id) if skip_existing else {}
            )
        return existing_cache[folder_id]

    rows = []
    n = len(files)
    for i, (full, rel) in enumerate(files):
        rel_dir = os.path.dirname(rel) if mirror else ""
        name = os.path.basename(rel)
        drive_folder = "/".join(x for x in [dest, rel_dir] if x)
        log.write("Uploading **{}** ({}/{})".format(rel, i + 1, n))

        row = {
            "file_name": name,
            "relative_path": rel,
            "size_bytes": os.path.getsize(full),
            "mime_type": mimetypes.guess_type(full)[0] or "",
            "drive_folder": drive_folder,
        }
        try:
            parent = folder_for(rel_dir)
            prior = existing_for(parent).get(name)
            if prior:
                fid = prior["id"]
                row["status"] = "skipped (already there)"
            else:
                res = dc.upload_file(
                    service, full, parent, share=share,
                    progress_cb=lambda p, i=i: prog.progress(min((i + p) / n, 1.0)),
                )
                fid = res["id"]
                row["status"] = "uploaded"
            row["drive_file_id"] = fid
            row["drive_link"] = "https://drive.google.com/file/d/{}/view".format(fid)
            row["direct_link"] = dc.direct_link(fid)
            row["preview_link"] = dc.preview_link(fid)
        except Exception as exc:
            row["status"] = "FAILED: {}".format(exc)
            row["drive_file_id"] = ""
            row["drive_link"] = ""
            row["direct_link"] = ""
            row["preview_link"] = ""
        row["uploaded_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        rows.append(row)
        prog.progress((i + 1) / n)

    log.empty()
    prog.empty()

    st.session_state["result_df"] = pd.DataFrame(rows)
    st.session_state["result_meta"] = {
        "folder": source_label,
        "elapsed": time.time() - started,
    }
    if staging:
        shutil.rmtree(staging, ignore_errors=True)

# ---------------------------------------------------------------- result ----
if "result_df" in st.session_state:
    df = st.session_state["result_df"]
    meta = st.session_state["result_meta"]

    ok = int((df["status"] == "uploaded").sum())
    skipped = int(df["status"].str.startswith("skipped").sum())
    failed = int(df["status"].str.startswith("FAILED").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Uploaded", ok)
    c2.metric("Skipped", skipped)
    c3.metric("Failed", failed)
    c4.metric("Took", "{:.0f}s".format(meta["elapsed"]))

    if failed:
        st.error("{} file(s) failed — re-run to retry just those.".format(failed))
        with st.expander("Show failures"):
            st.dataframe(df[df["status"].str.startswith("FAILED")][
                ["relative_path", "status"]], width="stretch", hide_index=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = "drive_links_{}_{}.csv".format(meta["folder"], stamp)

    st.subheader("Updated CSV")
    st.dataframe(
        df[["file_name", "drive_folder", "drive_link", "status"]],
        width="stretch", hide_index=True,
        column_config={"drive_link": st.column_config.LinkColumn("drive_link")},
    )

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    st.download_button("⬇️ Download CSV", buf.getvalue(), file_name=fname, mime="text/csv")

    if IS_SERVER:
        # The Render filesystem is wiped on every restart, so the download button
        # is the only copy — say so rather than pretending it is archived.
        st.caption("Download it now — this server keeps no copy.")
    else:
        saved = os.path.join(OUT_DIR, fname)
        df.to_csv(saved, index=False)
        st.caption("Also saved to `{}`".format(saved))
