from pyzotero import zotero as pyzotero
from pydash import _
import os
import subprocess
import zipfile
import io
import json
import hashlib
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
load_dotenv()

# import pprint
# pp = pprint.PrettyPrinter(indent=4)
# # usage pp.pprint

LIBRARY_TYPE = 'user'

# user config variables. set these in a .env
API_KEY = os.getenv('API_KEY')
LIBRARY_ID = os.getenv('LIBRARY_ID')
COLLECTION_NAME = os.getenv('COLLECTION_NAME') #in Zotero
FOLDER_NAME = os.getenv('FOLDER_NAME') #on the Remarkable device, this must exist!
STORAGE_BASE_PATH = os.getenv('STORAGE_BASE_PATH') #on local computer, used to store files downloaded from WebDAV

WEBDAV_URL = os.getenv('WEBDAV_URL') #e.g. https://example.com/remote.php/dav/files/user/zotero
WEBDAV_USERNAME = os.getenv('WEBDAV_USERNAME')
WEBDAV_PASSWORD = os.getenv('WEBDAV_PASSWORD')

RMAPI_HOST = os.getenv('RMAPI_HOST') #e.g. https://remarkable.example.com, for a self-hosted rmfakecloud instance
if RMAPI_HOST:
    os.environ['RMAPI_HOST'] = RMAPI_HOST

RMAPI_LS = f"rmapi ls /{FOLDER_NAME}"

# tracks the last known state of each file on the Remarkable, so we can detect local edits/annotations
SYNC_STATE_PATH = os.path.join(STORAGE_BASE_PATH, '.rm_sync_state.json')

zotero = pyzotero.Zotero(LIBRARY_ID, LIBRARY_TYPE, API_KEY)

def getCollectionId(zotero, collection_name):
    collections = zotero.collections(limit=200)
    for collection in collections:
        if (collection.get('data').get('name') == collection_name):
            return collection.get('data').get('key')

def getPapersFromZoteroCollection(zotero, collection_id):
    papers = []
    collection_items = zotero.collection_items(collection_id);
    for item in collection_items:
        data = item.get('data')
        if (data.get('contentType') == 'application/pdf') and data.get('linkMode') in ('imported_file', 'imported_url'):
            item_key = data.get('key')
            item_filename = data.get('filename')
            item_title = data.get('title')[:-4] if data.get('title', '').lower().endswith('.pdf') else data.get('title')
            if (item_key and item_filename and item_title):
                papers.append({ 'title': item_title, 'key': item_key, 'filename': item_filename })
    return papers

def downloadPaperFromWebDAV(paper, download_dir):
    key = paper.get('key')
    filename = paper.get('filename')
    title = paper.get('title')
    zip_url = f"{WEBDAV_URL.rstrip('/')}/{key}.zip"
    response = requests.get(zip_url, auth=HTTPBasicAuth(WEBDAV_USERNAME, WEBDAV_PASSWORD))
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        member = filename if filename in z.namelist() else next(
            (name for name in z.namelist() if name.lower().endswith('.pdf')), None
        )
        if not member:
            raise FileNotFoundError(f"No PDF found in WebDAV archive for {key}")
        pdf_bytes = z.read(member)
    dest_path = os.path.join(download_dir, f"{title}.pdf")
    with open(dest_path, 'wb') as f:
        f.write(pdf_bytes)
    return dest_path

def downloadPapers(papers, download_dir):
    print(f'downloading {len(papers)} papers from WebDAV')
    for paper in papers:
        try:
            paper['path'] = downloadPaperFromWebDAV(paper, download_dir)
            print(f"downloaded {paper.get('title')}")
        except Exception as e:
            print(f"Failed to download {paper.get('title')} from WebDAV: {e}")

def uploadPaperToWebDAV(zotero, item_key, filename, local_path):
    with open(local_path, 'rb') as f:
        file_bytes = f.read()
    mtime_ms = int(os.path.getmtime(local_path) * 1000)
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as z:
        z.writestr(filename, file_bytes)
    auth = HTTPBasicAuth(WEBDAV_USERNAME, WEBDAV_PASSWORD)
    zip_url = f"{WEBDAV_URL.rstrip('/')}/{item_key}.zip"
    prop_url = f"{WEBDAV_URL.rstrip('/')}/{item_key}.prop"
    prop_xml = f'<properties version="1"><mtime>{mtime_ms}</mtime><hash>{md5_hash}</hash></properties>'
    requests.put(zip_url, data=zip_buffer.getvalue(), auth=auth).raise_for_status()
    requests.put(prop_url, data=prop_xml, auth=auth).raise_for_status()
    # tell Zotero the attachment file changed, otherwise it will keep serving the stale version
    item = zotero.item(item_key)
    item['data']['mtime'] = mtime_ms
    item['data']['md5'] = md5_hash
    zotero.update_item(item)

def loadSyncState(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}

def saveSyncState(path, state):
    with open(path, 'w') as f:
        json.dump(state, f)

def getRemarkableFileFingerprint(title):
    COMMAND = f"rmapi stat \"/{FOLDER_NAME}/{title}\""
    try:
        output = subprocess.check_output(COMMAND, shell=True).decode('utf-8')
        stat = json.loads(output)
        return f"{stat.get('ModifiedClient')}:{stat.get('Version')}"
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

def getPapersChangedOnRemarkable(papers, remarkable_files, state):
    changed = []
    for paper in papers:
        title = paper.get('title')
        if title not in remarkable_files:
            continue
        fingerprint = getRemarkableFileFingerprint(title)
        if fingerprint is None:
            continue
        # no prior state means this is the first time we've seen the file; assume it's already in sync
        if title in state and state[title] != fingerprint:
            paper['fingerprint'] = fingerprint
            changed.append(paper)
        else:
            state[title] = fingerprint
    return changed

def syncChangedPapersToZotero(zotero, papers, download_dir, state):
    print(f'syncing {len(papers)} papers changed on Remarkable back to Zotero')
    for paper in papers:
        title = paper.get('title')
        dest_path = os.path.join(download_dir, f"{title}.pdf")
        COMMAND = f"rmapi geta \"/{FOLDER_NAME}/{title}\" -o \"{download_dir}\""
        try:
            print(COMMAND)
            subprocess.check_call(COMMAND, shell=True)
            uploadPaperToWebDAV(zotero, paper.get('key'), paper.get('filename'), dest_path)
            state[title] = paper.get('fingerprint')
            print(f'uploaded changes to {title} back to Zotero')
        except Exception as e:
            print(f'Failed to sync {title} back to Zotero: {e}')
        finally:
            if os.path.exists(dest_path):
                os.remove(dest_path)

def getPapersFromRemarkable(RMAPI_LS):
    remarkable_files = []
    for f in subprocess.check_output(RMAPI_LS, shell=True).decode("utf-8").split('\n')[1:-1]:
        if '[d]\t' not in f:
            remarkable_files.append(f.strip('[f]\t'))
    return remarkable_files

def getUploadListOfPapers(remarkable_files, papers):
    upload_list = []
    for paper in papers:
        title = paper.get('title')
        if title not in remarkable_files:
            upload_list.append(paper)
    return upload_list

def uploadPapers(papers):
    print(f'uploading {len(papers)} papers')
    for paper in papers:
        path = paper.get('path')
        COMMAND = f"rmapi put \"{path}\" /{FOLDER_NAME}"
        try:
            print(COMMAND)
            os.system(COMMAND)
        except:
            print(f'Failed to upload {path}')
        finally:
            if path and os.path.exists(path):
                os.remove(path)

def getDeleteListOfPapers(remarkable_files, papers):
    delete_list = []
    paperNames = _(papers).map(lambda p: p.get('title')).value()
    for f in remarkable_files:
        if (f not in paperNames):
            delete_list.append(f)
    return delete_list

def deletePapers(delete_list):
    print(f'deleting {len(delete_list)} papers')
    for paper in delete_list:
        COMMAND = f"rmapi rm /{FOLDER_NAME}/\"{paper}\""
        try:
            print(COMMAND)
            os.system(COMMAND)
        except:
            print(f'Failed to delete {paper}')

print('------- sync started -------')
collection_id = getCollectionId(zotero, COLLECTION_NAME)

# get papers that we want from Zetero Remarkable collection
papers = getPapersFromZoteroCollection(zotero, collection_id)
print(f"{len(papers)} papers in Zotero {COLLECTION_NAME} collection name")
for paper in papers:
    print(paper.get('title'))

#get papers that are currently on remarkable
remarkable_files = getPapersFromRemarkable(RMAPI_LS)
print(f"{len(remarkable_files)} papers on Remarkable Device, /{FOLDER_NAME}")

os.makedirs(STORAGE_BASE_PATH, exist_ok=True)
sync_state = loadSyncState(SYNC_STATE_PATH)

# Remarkable is the source of truth for content changes: push any edited/annotated files back to Zotero first
changed_papers = getPapersChangedOnRemarkable(papers, remarkable_files, sync_state)
syncChangedPapersToZotero(zotero, changed_papers, STORAGE_BASE_PATH, sync_state)

# then bring anything new from Zotero down to the Remarkable
upload_list = getUploadListOfPapers(remarkable_files, papers)
downloadPapers(upload_list, STORAGE_BASE_PATH)
for paper in upload_list:
    if paper.get('path'):
        sync_state[paper.get('title')] = None
upload_list = [p for p in upload_list if p.get('path')]
uploadPapers(upload_list)
for paper in upload_list:
    sync_state[paper.get('title')] = getRemarkableFileFingerprint(paper.get('title'))

delete_list = getDeleteListOfPapers(remarkable_files, papers)
deletePapers(delete_list)
for title in delete_list:
    sync_state.pop(title, None)

saveSyncState(SYNC_STATE_PATH, sync_state)

print('------- sync complete -------')
