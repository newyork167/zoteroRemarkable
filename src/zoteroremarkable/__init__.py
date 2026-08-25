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

LIBRARY_TYPE = 'user'

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

def downloadPaperFromWebDAV(paper, download_dir, webdav_url, webdav_username, webdav_password):
    key = paper.get('key')
    filename = paper.get('filename')
    title = paper.get('title')
    zip_url = f"{webdav_url.rstrip('/')}/{key}.zip"
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
    zip_url = f"{webdav_url.rstrip('/')}/{item_key}.zip"
    prop_url = f"{webdav_url.rstrip('/')}/{item_key}.prop"
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

def getRemarkableFileFingerprint(title, folder_name):
    COMMAND = f"rmapi stat \"/{folder_name}/{title}\""
    try:
        output = subprocess.check_output(COMMAND, shell=True).decode('utf-8')
        stat = json.loads(output)
        return f"{stat.get('ModifiedClient')}:{stat.get('Version')}"
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

def getPapersChangedOnRemarkable(papers, remarkable_files, state, folder_name):
    changed = []
    for paper in papers:
        title = paper.get('title')
        if title not in remarkable_files:
            continue
        fingerprint = getRemarkableFileFingerprint(title, folder_name)
        if fingerprint is None:
            continue
        # no prior state means this is the first time we've seen the file; assume it's already in sync
        if title in state and state[title] != fingerprint:
            paper['fingerprint'] = fingerprint
            changed.append(paper)
        else:
            state[title] = fingerprint
    return changed

def syncChangedPapersToZotero(zotero, papers, download_dir, state, folder_name, webdav_url, webdav_username, webdav_password):
    print(f'syncing {len(papers)} papers changed on Remarkable back to Zotero')
    for paper in papers:
        title = paper.get('title')
        dest_path = os.path.join(download_dir, f"{title}.pdf")
        COMMAND = f"rmapi geta \"/{folder_name}/{title}\" -o \"{download_dir}\""
        try:
            print(COMMAND)
            subprocess.check_call(COMMAND, shell=True)
            uploadPaperToWebDAV(zotero, paper.get('key'), paper.get('filename'), dest_path, webdav_url, webdav_username, webdav_password)
            state[title] = paper.get('fingerprint')
            print(f'uploaded changes to {title} back to Zotero')
        except Exception as e:
            print(f'Failed to sync {title} back to Zotero: {e}')
        finally:
            if os.path.exists(dest_path):
                os.remove(dest_path)

def getPapersFromRemarkable(rmapi_ls):
    remarkable_files = []
    for f in subprocess.check_output(rmapi_ls, shell=True).decode("utf-8").split('\n')[1:-1]:
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

def uploadPapers(papers, folder_name):
    print(f'uploading {len(papers)} papers')
    for paper in papers:
        path = paper.get('path')
        COMMAND = f"rmapi put \"{path}\" /{folder_name}"
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

def deletePapers(delete_list, folder_name):
    print(f'deleting {len(delete_list)} papers')
    for paper in delete_list:
        COMMAND = f"rmapi rm /{folder_name}/\"{paper}\""
        try:
            print(COMMAND)
            os.system(COMMAND)
        except:
            print(f'Failed to delete {paper}')

def main() -> None:
    load_dotenv()

    # user config variables. set these in a .env
    api_key = os.getenv('API_KEY')
    library_id = os.getenv('LIBRARY_ID')
    collection_name = os.getenv('COLLECTION_NAME') #in Zotero
    folder_name = os.getenv('FOLDER_NAME') #on the Remarkable device, this must exist!
    storage_base_path = os.getenv('STORAGE_BASE_PATH') #on local computer, used to store files downloaded from WebDAV

    webdav_url = os.getenv('WEBDAV_URL') #e.g. https://example.com/remote.php/dav/files/user/zotero
    webdav_username = os.getenv('WEBDAV_USERNAME')
    webdav_password = os.getenv('WEBDAV_PASSWORD')

    rmapi_host = os.getenv('RMAPI_HOST') #e.g. https://remarkable.example.com, for a self-hosted rmfakecloud instance
    if rmapi_host:
        os.environ['RMAPI_HOST'] = rmapi_host

    rmapi_ls = f"rmapi ls /{folder_name}"

    # tracks the last known state of each file on the Remarkable, so we can detect local edits/annotations
    sync_state_path = os.path.join(storage_base_path, '.rm_sync_state.json')

    zotero = pyzotero.Zotero(library_id, LIBRARY_TYPE, api_key)

    print('------- sync started -------')
    collection_id = getCollectionId(zotero, collection_name)

    # get papers that we want from Zetero Remarkable collection
    papers = getPapersFromZoteroCollection(zotero, collection_id)
    print(f"{len(papers)} papers in Zotero {collection_name} collection name")
    for paper in papers:
        print(paper.get('title'))

    #get papers that are currently on remarkable
    remarkable_files = getPapersFromRemarkable(rmapi_ls)
    print(f"{len(remarkable_files)} papers on Remarkable Device, /{folder_name}")

    os.makedirs(storage_base_path, exist_ok=True)
    sync_state = loadSyncState(sync_state_path)

    # Remarkable is the source of truth for content changes: push any edited/annotated files back to Zotero first
    changed_papers = getPapersChangedOnRemarkable(papers, remarkable_files, sync_state, folder_name)
    syncChangedPapersToZotero(zotero, changed_papers, storage_base_path, sync_state, folder_name, webdav_url, webdav_username, webdav_password)

    # then bring anything new from Zotero down to the Remarkable
    upload_list = getUploadListOfPapers(remarkable_files, papers)
    downloadPapers(upload_list, storage_base_path, webdav_url, webdav_username, webdav_password)
    for paper in upload_list:
        if paper.get('path'):
            sync_state[paper.get('title')] = None
    upload_list = [p for p in upload_list if p.get('path')]
    uploadPapers(upload_list, folder_name)
    for paper in upload_list:
        sync_state[paper.get('title')] = getRemarkableFileFingerprint(paper.get('title'), folder_name)

    delete_list = getDeleteListOfPapers(remarkable_files, papers)
    deletePapers(delete_list, folder_name)
    for title in delete_list:
        sync_state.pop(title, None)

    saveSyncState(sync_state_path, sync_state)

    print('------- sync complete -------')
