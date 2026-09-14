/**
 * Marketplaces MCP -> Google Drive bridge v3 (hardened).
 *
 * Small files keep the existing bridge path. Large files use the bridge only
 * as a control plane:
 * - resumable_start: authenticate the initial Drive resumable request;
 * - metadata_by_id: obtain Drive size/checksums without moving file bytes;
 * - promote_verified: after exact ID/name/parent/size/SHA256 verification,
 *   rename the staged upload to the canonical filename and only then trash the
 *   explicitly identified previous canonical;
 * - trash_by_id: cleanup verified diagnostic copies.
 *
 * Large file bytes never pass through Apps Script. The resumable session URI is
 * a bearer credential and must never be logged.
 */
const ARCHIVE_ROOT_ID='1UVKUcFfDhCDk6nMHX1mWg9cRL05DT-OJ';
const ARCHIVE_ROOT_NAME='MCP архив базы данных';
const SECRET_PROPERTY='MCP_DRIVE_BRIDGE_SECRET';
const BRIDGE_VERSION=3;

function setupBridge(){
  const props=PropertiesService.getScriptProperties();
  let secret=props.getProperty(SECRET_PROPERTY);
  if(!secret){
    secret=[Utilities.getUuid(),Utilities.getUuid(),Utilities.getUuid()].join('').replace(/-/g,'');
    props.setProperty(SECRET_PROPERTY,secret);
  }
  const root=DriveApp.getFolderById(ARCHIVE_ROOT_ID);
  if(root.getName()!==ARCHIVE_ROOT_NAME) throw new Error('wrong_root');
  console.log('ROOT_OK='+root.getName());
  return 'READY';
}

function doGet(){
  try{
    const root=DriveApp.getFolderById(ARCHIVE_ROOT_ID);
    return json_({ok:true,service:'marketplaces-mcp-drive-bridge',version:BRIDGE_VERSION,root_id:root.getId(),root_name:root.getName()});
  }catch(err){
    const message=String(err&&err.message||err);
    return json_({ok:false,error:message,retryable:isRetryableError_(message)});
  }
}

function doPost(e){
  const lock=LockService.getScriptLock(); lock.waitLock(30000);
  try{
    const body=JSON.parse((e&&e.postData&&e.postData.contents)||'{}');
    const expected=PropertiesService.getScriptProperties().getProperty(SECRET_PROPERTY);
    if(!expected||!body.secret||body.secret!==expected) return json_({ok:false,error:'unauthorized'});
    const action=String(body.action||'').trim().toLowerCase();

    if(action==='health'){
      const root=DriveApp.getFolderById(ARCHIVE_ROOT_ID);
      return json_({ok:true,version:BRIDGE_VERSION,root_id:root.getId(),root_name:root.getName()});
    }
    if(action==='stat'){
      const file=findFile_(String(body.path||''),String(body.filename||''));
      return file?json_({ok:true,found:true,file:metadata_(file)}):json_({ok:true,found:false});
    }
    if(action==='read'){
      const file=findFile_(String(body.path||''),String(body.filename||''));
      if(!file) return json_({ok:true,found:false});
      return json_({ok:true,found:true,file:metadata_(file),content_base64:Utilities.base64Encode(file.getBlob().getBytes())});
    }
    if(action==='read_by_id'){
      const file=DriveApp.getFileById(String(body.file_id||'').trim());
      return json_({ok:true,found:true,file:metadata_(file),content_base64:Utilities.base64Encode(file.getBlob().getBytes())});
    }
    if(action==='metadata_by_id'){
      const fileId=String(body.file_id||'').trim();
      if(!fileId) return json_({ok:false,error:'missing_file_id'});
      return json_({ok:true,found:true,file:driveApiMetadata_(fileId)});
    }
    if(action==='trash_by_id'){
      const fileId=String(body.file_id||'').trim();
      if(!fileId) return json_({ok:false,error:'missing_file_id'});
      const file=DriveApp.getFileById(fileId);
      file.setTrashed(true);
      return json_({ok:true,file_id:fileId,trashed:true});
    }
    if(action==='promote_verified'){
      return json_(promoteVerified_(body));
    }
    if(action==='resumable_start'){
      const filename=validateFilename_(String(body.filename||''));
      const path=String(body.path||'');
      const mimeType=String(body.mime_type||'application/octet-stream');
      const totalBytes=Number(body.total_bytes||0);
      if(!Number.isFinite(totalBytes)||totalBytes<=0) return json_({ok:false,error:'invalid_total_bytes'});
      const folder=resolveFolder_(path,true);
      const existing=findSingleFileInFolder_(folder,filename);
      const session=startResumableSession_(folder,existing,filename,mimeType,totalBytes);
      return json_({ok:true,session_uri:session.session_uri,file_id:session.file_id||null,path:normalizePath_(path),filename:filename});
    }
    if(action==='write'){
      const filename=validateFilename_(String(body.filename||''));
      const path=String(body.path||'');
      const mimeType=String(body.mime_type||'application/octet-stream');
      const bytes=Utilities.base64Decode(String(body.content_base64||''));
      const sha=sha256HexBytes_(bytes);
      if(body.sha256&&String(body.sha256).toLowerCase()!==sha) return json_({ok:false,error:'sha256_mismatch',sha256:sha});
      const folder=resolveFolder_(path,true);
      const old=folder.getFilesByName(filename); while(old.hasNext()) old.next().setTrashed(true);
      const file=folder.createFile(Utilities.newBlob(bytes,mimeType,filename));
      file.setDescription('Managed by Marketplaces MCP Drive Bridge v3; sha256='+sha);
      return json_({ok:true,file:metadata_(file),sha256:sha,path:normalizePath_(path)});
    }
    return json_({ok:false,error:'unknown_action'});
  }catch(err){
    const message=String(err&&err.message||err);
    return json_({ok:false,error:message,retryable:isRetryableError_(message)});
  }finally{lock.releaseLock();}
}

function promoteVerified_(body){
  const path=String(body.path||'');
  const fileId=String(body.file_id||'').trim();
  const previousId=String(body.previous_file_id||'').trim();
  const stagingName=validateFilename_(String(body.staging_filename||''));
  const canonicalName=validateFilename_(String(body.canonical_filename||''));
  const expectedBytes=Number(body.expected_bytes||0);
  const expectedSha=String(body.expected_sha256||'').trim().toLowerCase();
  if(!fileId) throw new Error('missing_file_id');
  if(!Number.isFinite(expectedBytes)||expectedBytes<=0) throw new Error('invalid_expected_bytes');
  if(!/^[0-9a-f]{64}$/.test(expectedSha)) throw new Error('invalid_expected_sha256');
  const folder=resolveFolder_(path,false);
  if(!folder) throw new Error('target_folder_missing');

  const before=driveApiMetadata_(fileId);
  if(before.trashed) throw new Error('candidate_trashed');
  if(Number(before.size||0)!==expectedBytes) throw new Error('candidate_size_mismatch');
  if(String(before.sha256Checksum||'').toLowerCase()!==expectedSha) throw new Error('candidate_sha256_mismatch');
  if(!Array.isArray(before.parents)||before.parents.indexOf(folder.getId())<0) throw new Error('candidate_wrong_parent');
  const beforeName=String(before.name||'');
  if(beforeName!==stagingName&&beforeName!==canonicalName) throw new Error('candidate_wrong_name');

  // Validate the exact old canonical BEFORE renaming the candidate, so a bad
  // durable previous-file ID can never cause an unrelated file to be trashed.
  let previous=null;
  if(previousId&&previousId!==fileId){
    previous=driveApiMetadata_(previousId);
    if(!previous.trashed){
      if(!Array.isArray(previous.parents)||previous.parents.indexOf(folder.getId())<0) throw new Error('previous_wrong_parent');
      if(String(previous.name||'')!==canonicalName) throw new Error('previous_wrong_name');
    }
  }

  // Promote the verified staged file first. The script lock serializes all
  // bridge operations, so MCP readers cannot observe the short duplicate-name
  // interval between rename and trash. A retry with the same explicit IDs is
  // idempotent if the response is lost after this point.
  const candidate=DriveApp.getFileById(fileId);
  if(candidate.getName()!==canonicalName) candidate.setName(canonicalName);

  try{
    if(previousId&&previousId!==fileId&&previous&&!previous.trashed){
      DriveApp.getFileById(previousId).setTrashed(true);
    }
    const after=driveApiMetadata_(fileId);
    if(after.trashed) throw new Error('promoted_file_trashed');
    if(String(after.name||'')!==canonicalName) throw new Error('promoted_name_mismatch');
    if(Number(after.size||0)!==expectedBytes) throw new Error('promoted_size_mismatch');
    if(String(after.sha256Checksum||'').toLowerCase()!==expectedSha) throw new Error('promoted_sha256_mismatch');
    return {ok:true,file:after,previous_file_trashed:!!(previousId&&previousId!==fileId)};
  }catch(err){
    // The candidate may already have been renamed. Mark the response retryable
    // so the durable worker re-enters this exact-ID idempotent promotion path.
    return {ok:false,error:'promotion_post_rename_retry',retryable:true};
  }
}

function startResumableSession_(folder,existingFile,filename,mimeType,totalBytes){
  const token=ScriptApp.getOAuthToken();
  const fileId=existingFile?existingFile.getId():'';
  const fields='id,name,size,md5Checksum,sha256Checksum,mimeType,modifiedTime,parents,trashed';
  const base='https://www.googleapis.com/upload/drive/v3/files';
  const url=fileId
    ? base+'/'+encodeURIComponent(fileId)+'?uploadType=resumable&supportsAllDrives=true&fields='+encodeURIComponent(fields)
    : base+'?uploadType=resumable&supportsAllDrives=true&fields='+encodeURIComponent(fields);
  const metadata=fileId
    ? {name:filename,mimeType:mimeType}
    : {name:filename,mimeType:mimeType,parents:[folder.getId()]};
  const response=UrlFetchApp.fetch(url,{
    method:fileId?'patch':'post',
    contentType:'application/json; charset=UTF-8',
    headers:{
      Authorization:'Bearer '+token,
      Accept:'application/json',
      'X-Upload-Content-Type':mimeType,
      'X-Upload-Content-Length':String(totalBytes)
    },
    payload:JSON.stringify(metadata),
    muteHttpExceptions:true,
    followRedirects:false
  });
  const code=response.getResponseCode();
  if(code<200||code>=300) throw new Error('drive_resumable_start_http_'+code);
  const headers=response.getAllHeaders();
  const location=String(headers.Location||headers.location||'').trim();
  if(!location) throw new Error('drive_resumable_missing_location');
  if(!/^https:\/\/www\.googleapis\.com\/upload\/drive\//.test(location)) throw new Error('drive_resumable_invalid_location');
  return {session_uri:location,file_id:fileId||null};
}

function driveApiMetadata_(fileId){
  const fields='id,name,size,md5Checksum,sha256Checksum,mimeType,modifiedTime,parents,trashed';
  const url='https://www.googleapis.com/drive/v3/files/'+encodeURIComponent(fileId)+'?supportsAllDrives=true&fields='+encodeURIComponent(fields);
  const response=UrlFetchApp.fetch(url,{
    method:'get',
    headers:{Authorization:'Bearer '+ScriptApp.getOAuthToken(),Accept:'application/json'},
    muteHttpExceptions:true
  });
  const code=response.getResponseCode();
  if(code<200||code>=300) throw new Error('drive_metadata_http_'+code);
  return JSON.parse(response.getContentText()||'{}');
}

function isRetryableError_(message){
  const m=String(message||'');
  if(/drive_(resumable_start|metadata)_http_(408|425|429|500|502|503|504)/.test(m)) return true;
  if(/promotion_post_rename_retry/.test(m)) return true;
  if(/Service invoked too many times|Server error occurred|Service unavailable/i.test(m)) return true;
  return false;
}

function normalizePath_(path){
  const parts=String(path||'').split('/').map(x=>x.trim()).filter(Boolean);
  if(parts.some(x=>x==='.'||x==='..'||x.includes('\\'))) throw new Error('invalid_path');
  return parts.join('/');
}
function validateFilename_(name){
  const f=String(name||'').trim();
  if(!f||f.includes('/')||f.includes('\\')) throw new Error('invalid_filename');
  return f;
}
function resolveFolder_(path,createMissing){
  let folder=DriveApp.getFolderById(ARCHIVE_ROOT_ID);
  if(folder.getName()!==ARCHIVE_ROOT_NAME) throw new Error('wrong_root');
  const normalized=normalizePath_(path); if(!normalized) return folder;
  for(const name of normalized.split('/')){
    const it=folder.getFoldersByName(name);
    if(it.hasNext()) folder=it.next();
    else if(createMissing) folder=folder.createFolder(name);
    else return null;
  }
  return folder;
}
function findSingleFileInFolder_(folder,filenameRaw){
  const files=folder.getFilesByName(validateFilename_(filenameRaw));
  if(!files.hasNext()) return null;
  const file=files.next();
  if(files.hasNext()) throw new Error('duplicate_filename');
  return file;
}
function findFile_(path,filenameRaw){
  const folder=resolveFolder_(path,false); if(!folder) return null;
  return findSingleFileInFolder_(folder,filenameRaw);
}
function metadata_(file){
  return {id:file.getId(),name:file.getName(),mime_type:file.getMimeType()||'application/octet-stream',size:file.getSize(),url:file.getUrl(),modified_time:file.getLastUpdated().toISOString()};
}
function sha256HexBytes_(bytes){
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,bytes)
    .map(b=>(b<0?b+256:b).toString(16).padStart(2,'0')).join('');
}
function json_(obj){
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
