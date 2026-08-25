# Zotero Remarkable Sync

This is a little utility that I made to keep a collection/folder in sync with Zotero and Remarkable.
My zotero setup uses WebDAV storage for attachments (e.g. self-hosted or Nextcloud/ownCloud WebDAV).
Collection and item metadata is fetched via the Zotero web API, but the actual PDF files are downloaded from your WebDAV storage server.

Sync is bidirectional:
- New papers added to the Zotero collection are downloaded from WebDAV and uploaded to the Remarkable.
- The Remarkable is treated as the source of truth for content: if a paper already on the Remarkable has changed (e.g. you annotated it), the annotated PDF is pulled from the device and pushed back to Zotero via WebDAV, overwriting the Zotero version.
- Papers removed from the Zotero collection are deleted from the Remarkable.

A small state file (`.rm_sync_state.json`, stored in `STORAGE_BASE_PATH`) is used to detect when a file on the Remarkable has changed since the last sync.

## Setup
 - install rmapi (a version with the `geta` "get annotated PDF" command)
 - Download the [sync.py](https://raw.githubusercontent.com/oscarmorrison/zoteroRemarkableO/master/sync.py)
 - create a `.env` file

### Dependancies
- python3
- [rmapi](https://github.com/juruen/rmapi)
- pyzotero
- pydash
- dotenv
- requests
(the above python libraries can be installed using pip3)

### Env file
- Create a zotero api key
- get zotero library_id (from zotero web)
- create a folder on remarkable and a collection in zotero
- set `STORAGE_BASE_PATH` to a local folder used to hold PDFs downloaded from/to WebDAV and the sync state file (must persist between runs)
- set `WEBDAV_URL`, `WEBDAV_USERNAME`, and `WEBDAV_PASSWORD` to match your Zotero WebDAV storage settings (found in Zotero preferences under Sync > File Syncing)
- optionally set `RMAPI_HOST` if you run your own rmfakecloud (or other self-hosted) instance instead of the official Remarkable cloud

### Usage
_(ensure you have a .env file, with zotero api key, and rmapi setup)_  
Then to sync, just run:  
  `python3 sync.py`
