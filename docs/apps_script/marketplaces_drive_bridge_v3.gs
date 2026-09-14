/**
 * Marketplaces MCP -> Google Drive bridge v3.
 *
 * v3 keeps the existing small-file bridge and adds control-plane actions:
 * - resumable_start: Apps Script authenticates the initial Drive resumable request;
 * - metadata_by_id: returns Drive metadata/checksums without reading file bytes;
 * - trash_by_id: authenticated cleanup for verified diagnostic copies.
 *
 * Large file bytes never pass through Apps Script.
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
  }catch(err){return json_({ok:false,error:String(err&&err.message||err)});}
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
  }catch(err){return json_({ok:false,error:String(err&&err.message||err)});}
  finally{lock.releaseLock();}
}

function startResumableSession_(folder,existingFile,filename,mimeType,totalBytes){
  const token=ScriptApp.getOAuthToken();
  const fileId=existingFile?existingFile.getId():'';
  const fields='id,name,size,md5Checksum,sha256Checksum,mimeType,modifiedTime';
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
  const fields='id,name,size,md5Checksum,sha256Checksum,mimeType,modifiedTime';
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
