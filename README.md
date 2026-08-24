# Zotero Remarkable Sync

This is a little utility that I made to keep a collection/folder in sync with Zotero and Remarkable.
My zotero setup uses WebDAV storage for attachments (e.g. self-hosted or Nextcloud/ownCloud WebDAV).
Collection and item metadata is fetched via the Zotero web API, but the actual PDF files are downloaded from your WebDAV storage server.

## Setup
 - install rmapi
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
- set `STORAGE_BASE_PATH` to a local temp folder used to hold PDFs downloaded from WebDAV before uploading to Remarkable
- set `WEBDAV_URL`, `WEBDAV_USERNAME`, and `WEBDAV_PASSWORD` to match your Zotero WebDAV storage settings (found in Zotero preferences under Sync > File Syncing)

### Usage
_(ensure you have a .env file, with zotero api key, and rmapi setup)_  
Then to sync, just run:  
  `python3 sync.py`
