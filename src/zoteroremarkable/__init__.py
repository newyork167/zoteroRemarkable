from pyzotero import zotero as pyzotero
from pydash import _
import os
import subprocess
import zipfile
import io
import json
import hashlib
import requests
import yaml
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

LIBRARY_TYPE = 'user'

def loadSyncMap(path, default_collection_name, default_folder_name):
    if not os.path.exists(path):
        # fall back to the single COLLECTION_NAME/FOLDER_NAME env vars for backwards compatibility
        return [{ 'collection': default_collection_name, 'folder': default_folder_name }]
    with open(path, 'r') as f:
        data = yaml.safe_load(f) or []
    mappings = []
    for entry in data:
        collection = entry.get('collection')
        folder = entry.get('folder') or collection
        if collection:
            mappings.append({ 'collection': collection, 'folder': folder })
    return mappings

def resolveCollectionId(zotero, collection_path):
    parent_id = None
    for name in [p for p in collection_path.split('/') if p]:
        candidates = zotero.collections_top(limit=200) if parent_id is None else zotero.collections_sub(parent_id, limit=200)
        match = next((c for c in candidates if c.get('data').get('name') == name), None)
        if not match:
            return None
        parent_id = match.get('data').get('key')
    return parent_id

def joinRemotePath(*parts):
    segments = []
    for part in parts:
        if not part:
            continue
        segments.extend(p for p in part.split('/') if p)
    return '/'.join(segments)

def ensureRemarkableFolder(folder_path):
    parts = folder_path.split('/')
    for i in range(1, len(parts) + 1):
        path = '/'.join(parts[:i])
        COMMAND = f"rmapi mkdir \"/{path}\""
        subprocess.run(COMMAND, shell=True, capture_output=True)

def getPapersFromZoteroCollection(zotero, collection_id):
    papers = []
    collection_items = zotero.collection_items(collection_id);
    titles_by_key = { item.get('data').get('key'): item.get('data').get('title') for item in collection_items }
    for item in collection_items:
        data = item.get('data')
        if (data.get('contentType') == 'application/pdf') and data.get('linkMode') in ('imported_file', 'imported_url'):
            item_key = data.get('key')
            item_filename = data.get('filename')
            # attachment titles are often just "PDF"; prefer the parent (actual paper) title when available
            parent_key = data.get('parentItem')
            raw_title = titles_by_key.get(parent_key) or data.get('title')
            item_title = raw_title[:-4] if raw_title and raw_title.lower().endswith('.pdf') else raw_title
            if (item_key and item_filename and item_title):
                papers.append({ 'title': item_title, 'key': item_key, 'filename': item_filename })
    return papers

# Zotero's WebDAV file sync always stores attachments under a 'zotero' subfolder of the configured WebDAV URL
def getWebdavStorageUrl(webdav_url):
    return f"{webdav_url.rstrip('/')}/zotero"

def downloadPaperFromWebDAV(paper, download_dir, webdav_url, webdav_username, webdav_password):
    key = paper.get('key')
    filename = paper.get('filename')
    title = paper.get('title')
    zip_url = f"{getWebdavStorageUrl(webdav_url)}/{key}.zip"
    response = requests.get(zip_url, auth=HTTPBasicAuth(webdav_username, webdav_password))
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

def downloadPapers(papers, download_dir, webdav_url, webdav_username, webdav_password):
    print(f'downloading {len(papers)} papers from WebDAV')
    for paper in papers:
        try:
            paper['path'] = downloadPaperFromWebDAV(paper, download_dir, webdav_url, webdav_username, webdav_password)
            print(f"downloaded {paper.get('title')}")
        except Exception as e:
            print(f"Failed to download {paper.get('title')} from WebDAV: {e}")

def uploadPaperToWebDAV(zotero, item_key, filename, local_path, webdav_url, webdav_username, webdav_password):
    with open(local_path, 'rb') as f:
        file_bytes = f.read()
    mtime_ms = int(os.path.getmtime(local_path) * 1000)
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as z:
        z.writestr(filename, file_bytes)
    auth = HTTPBasicAuth(webdav_username, webdav_password)
    storage_url = getWebdavStorageUrl(webdav_url)
    zip_url = f"{storage_url}/{item_key}.zip"
    prop_url = f"{storage_url}/{item_key}.prop"
    prop_xml = f'<properties version="1"><mtime>{mtime_ms}</mtime><hash>{md5_hash}</hash></properties>'
    requests.put(zip_url, data=zip_buffer.getvalue(), auth=auth).raise_for_status()
    requests.put(prop_url, data=prop_xml, auth=auth).raise_for_status()
    # tell Zotero the attachment file changed, otherwise it will keep serving the stale version
    item = zotero.item(item_key)
    item['data']['mtime'] = mtime_ms
    item['data']['md5'] = md5_hash
    # the API returns read-only fields (e.g. lastRead) that it then rejects if sent back on write
    item['data'].pop('lastRead', None)
    zotero.update_item(item)

def loadSyncState(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}

def saveSyncState(path, state):
    with open(path, 'w') as f:
        json.dump(state, f)

def getRemarkableFileFingerprint(title, folder_path):
    COMMAND = f"rmapi stat \"/{folder_path}/{title}\""
    try:
        output = subprocess.check_output(COMMAND, shell=True).decode('utf-8')
        stat = json.loads(output)
        return f"{stat.get('ModifiedClient')}:{stat.get('Version')}"
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

def getPapersChangedOnRemarkable(papers, remarkable_files, state, folder_path):
    changed = []
    for paper in papers:
        title = paper.get('title')
        if title not in remarkable_files:
            continue
        fingerprint = getRemarkableFileFingerprint(title, folder_path)
        if fingerprint is None:
            continue
        state_key = f"{folder_path}::{title}"
        # no prior state means this is the first time we've seen the file; assume it's already in sync
        if state_key in state and state[state_key] != fingerprint:
            paper['fingerprint'] = fingerprint
            changed.append(paper)
        else:
            state[state_key] = fingerprint
    return changed

def syncChangedPapersToZotero(zotero, papers, download_dir, state, folder_path, webdav_url, webdav_username, webdav_password):
    print(f'syncing {len(papers)} papers changed on Remarkable back to Zotero')
    for paper in papers:
        title = paper.get('title')
        # geta has no output-dir flag (writes to cwd) and names the file '<title>-annotations.pdf'
        dest_path = os.path.join(download_dir, f"{title}-annotations.pdf")
        COMMAND = f"rmapi geta \"/{folder_path}/{title}\""
        try:
            print(COMMAND)
            subprocess.check_call(COMMAND, shell=True, cwd=download_dir)
            uploadPaperToWebDAV(zotero, paper.get('key'), paper.get('filename'), dest_path, webdav_url, webdav_username, webdav_password)
            state[f"{folder_path}::{title}"] = paper.get('fingerprint')
            print(f'uploaded changes to {title} back to Zotero')
        except Exception as e:
            print(f'Failed to sync {title} back to Zotero: {e}')
        finally:
            if os.path.exists(dest_path):
                os.remove(dest_path)

def getPapersFromRemarkable(folder_path):
    COMMAND = f"rmapi ls \"/{folder_path}\""
    remarkable_files = []
    try:
        output = subprocess.check_output(COMMAND, shell=True).decode("utf-8")
    except subprocess.CalledProcessError:
        return remarkable_files
    # rmapi ls has no header row; just drop the trailing empty line from the final newline
    for f in output.split('\n')[:-1]:
        if '[d]\t' not in f:
            remarkable_files.append(f.removeprefix('[f]\t'))
    return remarkable_files

def getUploadListOfPapers(remarkable_files, papers):
    upload_list = []
    for paper in papers:
        title = paper.get('title')
        if title not in remarkable_files:
            upload_list.append(paper)
    return upload_list

def uploadPapers(papers, folder_path):
    print(f'uploading {len(papers)} papers')
    for paper in papers:
        path = paper.get('path')
        COMMAND = f"rmapi put \"{path}\" \"/{folder_path}\""
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

def deletePapers(delete_list, folder_path):
    print(f'deleting {len(delete_list)} papers')
    for paper in delete_list:
        COMMAND = f"rmapi rm \"/{folder_path}/{paper}\""
        try:
            print(COMMAND)
            os.system(COMMAND)
        except:
            print(f'Failed to delete {paper}')

def syncCollection(zotero, mapping, base_folder_name, storage_base_path, sync_state, webdav_url, webdav_username, webdav_password):
    collection_path = mapping.get('collection')
    folder_path = joinRemotePath(base_folder_name, mapping.get('folder'))

    collection_id = resolveCollectionId(zotero, collection_path)
    if not collection_id:
        print(f"Could not find Zotero collection: {collection_path}")
        return

    print(f'------- syncing "{collection_path}" -> /{folder_path} -------')
    papers = getPapersFromZoteroCollection(zotero, collection_id)
    print(f"{len(papers)} papers in Zotero {collection_path} collection")
    for paper in papers:
        print(paper.get('title'))

    ensureRemarkableFolder(folder_path)

    #get papers that are currently on remarkable
    remarkable_files = getPapersFromRemarkable(folder_path)
    print(f"{len(remarkable_files)} papers on Remarkable Device, /{folder_path}")

    # Remarkable is the source of truth for content changes: push any edited/annotated files back to Zotero first
    changed_papers = getPapersChangedOnRemarkable(papers, remarkable_files, sync_state, folder_path)
    for paper in changed_papers:
        print(f"paper changed on Remarkable: {paper.get('title')}")
    syncChangedPapersToZotero(zotero, changed_papers, storage_base_path, sync_state, folder_path, webdav_url, webdav_username, webdav_password)

    # then bring anything new from Zotero down to the Remarkable
    upload_list = getUploadListOfPapers(remarkable_files, papers)
    downloadPapers(upload_list, storage_base_path, webdav_url, webdav_username, webdav_password)
    for paper in upload_list:
        if paper.get('path'):
            sync_state[f"{folder_path}::{paper.get('title')}"] = None
    upload_list = [p for p in upload_list if p.get('path')]
    uploadPapers(upload_list, folder_path)
    for paper in upload_list:
        sync_state[f"{folder_path}::{paper.get('title')}"] = getRemarkableFileFingerprint(paper.get('title'), folder_path)

    delete_list = getDeleteListOfPapers(remarkable_files, papers)
    deletePapers(delete_list, folder_path)
    for title in delete_list:
        sync_state.pop(f"{folder_path}::{title}", None)

def main() -> None:
    load_dotenv()

    # user config variables. set these in a .env
    api_key = os.getenv('API_KEY')
    library_id = os.getenv('LIBRARY_ID')
    collection_name = os.getenv('COLLECTION_NAME') #in Zotero, used if no SYNC_MAP_PATH file is set
    folder_name = os.getenv('FOLDER_NAME') #base folder on the Remarkable device, this must exist!
    storage_base_path = os.getenv('STORAGE_BASE_PATH') #on local computer, used to store files downloaded from WebDAV
    sync_map_path = os.getenv('SYNC_MAP_PATH', 'sync_map.yaml') #maps Zotero collections (incl. nested subcollections) to Remarkable folders under FOLDER_NAME

    webdav_url = os.getenv('WEBDAV_URL') #e.g. https://example.com/remote.php/dav/files/user/zotero
    webdav_username = os.getenv('WEBDAV_USERNAME')
    webdav_password = os.getenv('WEBDAV_PASSWORD')

    if webdav_url and not (webdav_url.startswith('http://') or webdav_url.startswith('https://')):
        raise ValueError(f"WEBDAV_URL must include a scheme (http:// or https://), got: {webdav_url}")

    rmapi_host = os.getenv('RMAPI_HOST') #e.g. https://remarkable.example.com, for a self-hosted rmfakecloud instance
    if rmapi_host:
        os.environ['RMAPI_HOST'] = rmapi_host

    # tracks the last known state of each file on the Remarkable, so we can detect local edits/annotations
    sync_state_path = os.path.join(storage_base_path, '.rm_sync_state.json')

    zotero = pyzotero.Zotero(library_id, LIBRARY_TYPE, api_key)

    print('------- sync started -------')
    sync_map = loadSyncMap(sync_map_path, collection_name, '')

    os.makedirs(storage_base_path, exist_ok=True)
    sync_state = loadSyncState(sync_state_path)

    for mapping in sync_map:
        syncCollection(zotero, mapping, folder_name, storage_base_path, sync_state, webdav_url, webdav_username, webdav_password)

    saveSyncState(sync_state_path, sync_state)

    print('------- sync complete -------')
