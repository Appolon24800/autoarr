use crate::{
    Error, Result,
    args::Args,
    client::{Client, Data, Query, query, quote},
    config::{self, Results},
    err, object,
    output::{self, pairs, records, set_path},
    text, truth,
};
use base64::Engine;
use serde_json::{Value, json};
use std::{fs, io::Read};

pub fn require(client: &Client, kinds: &[&str]) -> Result<()> {
    if !kinds.contains(&client.kind.as_str()) {
        err("This shortcut is not supported for this application; use ops or api")
    } else {
        Ok(())
    }
}
pub fn load_json(source: &str) -> Result<Value> {
    let data = if source == "-" {
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s)?;
        s
    } else if let Some(p) = source.strip_prefix('@') {
        fs::read_to_string(config::expand_path(p))?
    } else {
        source.into()
    };
    serde_json::from_str(&data).map_err(|_| {
        Error::new("Could not read JSON data; use a JSON literal, @file, or - for stdin")
    })
}
pub fn payload(a: &Args) -> Result<Option<Value>> {
    let mut data = if a.b("data") {
        Some(load_json(&a.s("data"))?)
    } else {
        None
    };
    if !a.list("set").is_empty() {
        let data = data.get_or_insert(json!({}));
        object(data)?;
        for (k, v) in pairs(&a.list("set"))? {
            set_path(data, &k, v)?;
        }
    }
    Ok(data)
}
pub fn request_data(a: &Args) -> Result<Data> {
    let mut d = Data {
        body: payload(a)?,
        form: if a.list("form").is_empty() {
            None
        } else {
            Some(pairs(&a.list("form"))?)
        },
        site: a.b("site"),
        ..Default::default()
    };
    if a.b("output") {
        d.output = Some(config::expand_path(&a.s("output")));
    }
    if a.b("upload") {
        let path = config::expand_path(&a.s("upload"));
        let mut bytes = fs::read(&path)?;
        if a.b("base64_upload") {
            bytes = base64::engine::general_purpose::STANDARD
                .encode(bytes)
                .into_bytes();
        }
        d.raw = Some(bytes);
        d.content_type = Some(if a.b("content_type") {
            a.s("content_type")
        } else {
            crate::client::mime(&path)
        });
    } else if a.b("base64_upload") {
        return err("--base64-upload requires --upload FILE");
    }
    for f in a.list("file") {
        let (k, p) = f
            .split_once('=')
            .ok_or_else(|| Error::new("--file needs FIELD=FILE"))?;
        d.files.push((k.into(), config::expand_path(p)));
    }
    Ok(d)
}
pub fn resource(client: &Client, name: &str, id: Option<&Value>) -> String {
    let name = if config::is_arr(&client.kind) {
        match name {
            "movies" => "movie",
            "shows" => "series",
            "artists" => "artist",
            "albums" => "album",
            "tracks" => "track",
            "authors" => "author",
            "books" => "book",
            "profiles" => "qualityprofile",
            "roots" => "rootfolder",
            "tags" => "tag",
            "indexers" => "indexer",
            "clients" => "downloadclient",
            _ => name,
        }
    } else if client.kind == "jellyfin" {
        match name {
            "items" => "Items",
            "users" => "Users",
            "sessions" => "Sessions",
            "libraries" => "Library/VirtualFolders",
            "tasks" => "ScheduledTasks",
            _ => name,
        }
    } else {
        name
    };
    format!(
        "/{}{}",
        name.trim_start_matches('/'),
        id.filter(|v| !v.is_null())
            .map(|id| format!("/{}", quote(&text(id))))
            .unwrap_or_default()
    )
}
pub fn paginated(client: &mut Client, path: &str, qs: Query, all: bool) -> Result<Value> {
    if !all {
        return client.get(path, qs);
    }
    let (size, keys) = if config::is_arr(&client.kind) {
        (250, ["page", "pageSize"])
    } else {
        match client.kind.as_str() {
            "jellyfin" => (250, ["startIndex", "limit"]),
            "bazarr" => (250, ["start", "length"]),
            "seerr" => (50, ["skip", "take"]),
            _ => return client.get(path, qs),
        }
    };
    let mut combined = Vec::new();
    let mut first = None;
    let mut prior = None;
    for page in 0..10000 {
        let mut q: Query = qs
            .iter()
            .filter(|(k, _)| !keys.contains(&k.as_str()))
            .cloned()
            .collect();
        q.push((
            keys[0].into(),
            json!(if keys[0] == "page" {
                page + 1
            } else {
                page * size
            }),
        ));
        q.push((keys[1].into(), json!(size)));
        let data = client.get(path, q)?;
        if data.is_array() || records(&data).is_none() {
            return Ok(data);
        }
        let rows = records(&data).unwrap();
        if first.is_none() {
            first = Some(data.clone());
        }
        let fingerprint = output::canonical(&json!(rows));
        if !rows.is_empty() && prior.as_ref() == Some(&fingerprint) {
            return err(
                "Pagination repeated a page; the endpoint may ignore pagination parameters",
            );
        }
        prior = Some(fingerprint);
        combined.extend(rows.clone());
        let total = ["totalRecords", "TotalRecordCount", "total"]
            .iter()
            .find_map(|k| data[*k].as_u64())
            .or_else(|| data["pageInfo"]["results"].as_u64());
        if rows.is_empty()
            || total.is_some_and(|n| combined.len() >= n as usize)
            || total.is_none() && rows.len() < size
        {
            let mut first = first.unwrap();
            let key = output::record_key(&first).unwrap();
            first["autoarrPagination"] = json!({"pages":page+1,"returnedRecords":combined.len()});
            first[key] = json!(combined);
            return Ok(first);
        }
    }
    err("Pagination exceeded 10,000 pages")
}
pub fn native_command(c: &mut Client, name: &str, body: Value) -> Result<Value> {
    if !config::is_arr(&c.kind) {
        return err("Native commands require an Arr application");
    }
    let mut body = body;
    object(&body)?;
    body["name"] = json!(name);
    c.send("POST", "/command", vec![], body)
}
fn norm(path: &str) -> String {
    let absolute = path.starts_with('/');
    let mut parts = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => (),
            ".." => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    let s = parts.join("/");
    if absolute {
        format!("/{s}")
    } else if s.is_empty() {
        ".".into()
    } else {
        s
    }
}
pub fn move_media(c: &mut Client, a: &Args) -> Result<Value> {
    require(c, &["radarr", "sonarr"])?;
    let path = format!("/{}/{}", config::entity(&c.kind), a.s("id"));
    let item = c.get(&path, vec![])?;
    let roots = c.get("/rootfolder", vec![])?;
    let roots = records(&roots).ok_or_else(|| Error::new("Root API did not return an array"))?;
    let (root, destination) = if a.b("root") {
        let root = norm(&a.s("root"));
        let base = text(&item["path"]);
        let base = base.trim_end_matches('/').rsplit('/').next().unwrap_or("");
        (
            root.clone(),
            format!("{}/{base}", root.trim_end_matches('/')),
        )
    } else {
        let destination = norm(&a.s("path"));
        let root = destination
            .rsplit_once('/')
            .map(|(p, _)| if p.is_empty() { "/" } else { p })
            .unwrap_or(".")
            .to_string();
        (root, destination)
    };
    if !roots.iter().any(|r| norm(&text(&r["path"])) == root) {
        return err("Destination parent must be an existing app root folder; inspect roots");
    }
    if destination == norm(&text(&item["path"])) {
        return Ok(json!({"changed":false,"path":item["path"]}));
    }
    let listing = c.get(
        "/filesystem",
        query(&[
            ("path", json!(format!("{}/", root.trim_end_matches('/')))),
            ("includeFiles", json!(true)),
        ]),
    )?;
    if !listing["directories"].is_array() {
        return err(
            "Cannot check destination through the app filesystem API; no move was requested",
        );
    }
    let parent = root
        .trim_end_matches('/')
        .rsplit_once('/')
        .map(|(p, _)| if p.is_empty() { "/" } else { p })
        .unwrap_or("/");
    if norm(&text(&listing["parent"])) != parent {
        return err(
            "Filesystem API did not list the requested destination root; no move was requested",
        );
    }
    if listing["directories"]
        .as_array()
        .into_iter()
        .flatten()
        .chain(listing["files"].as_array().into_iter().flatten())
        .any(|e| norm(&text(&e["path"])) == destination)
    {
        return err("Destination already exists; inspect it before requesting a move");
    }
    let mut updated = item.clone();
    updated["path"] = json!(destination);
    updated["rootFolderPath"] = json!(root);
    let response = c.send("PUT", &path, query(&[("moveFiles", json!(true))]), updated)?;
    if c.dry_run {
        return Ok(response);
    }
    Ok(
        json!({"requested":true,"mover":c.kind,"moveFiles":true,"oldPath":item["path"],"newPath":destination,"response":response}),
    )
}
pub fn updated(before: Value, a: &Args) -> Result<Value> {
    let mut body = object(&before)?.clone();
    if let Some(data) = if a.b("data") {
        Some(load_json(&a.s("data"))?)
    } else {
        None
    } {
        body.extend(object(&data)?.clone());
    }
    let mut body = Value::Object(body);
    for (k, v) in pairs(&a.list("set"))? {
        set_path(&mut body, &k, v)?;
    }
    Ok(body)
}
pub fn user(c: &mut Client, supplied: &str) -> Result<String> {
    if !supplied.is_empty() {
        return Ok(supplied.into());
    }
    if truth(&c.app["user_id"]) {
        return Ok(text(&c.app["user_id"]));
    }
    let users = c.get("/Users", vec![])?;
    records(&users)
        .into_iter()
        .flatten()
        .find(|u| u["Policy"]["IsAdministrator"] == true)
        .map(|u| text(&u["Id"]))
        .ok_or_else(|| Error::new("Specify --user-id or configure user_id for Jellyfin"))
}
const EDITABLE: &str = "Id,Name,OriginalTitle,OriginalLanguage,Overview,SortName,ForcedSortName,IndexNumber,ParentIndexNumber,IndexNumberEnd,Genres,Tags,Studios,People,ProviderIds,ProductionYear,PremiereDate,EndDate,CommunityRating,OfficialRating,CriticRating,CustomRating,PreferredMetadataLanguage,PreferredMetadataCountryCode,LockData,LockedFields,RunTimeTicks,DisplayOrder,Status,AirDays,AirTime,DateCreated,AspectRatio,Taglines,ProductionLocations,AirsAfterSeasonNumber,AirsBeforeEpisodeNumber,AirsBeforeSeasonNumber,Video3DFormat";
pub fn metadata(c: &mut Client, a: &Args) -> Result<Value> {
    require(c, &["jellyfin"])?;
    let changes = payload(a)?.unwrap_or(json!({}));
    let fields = object(&changes)?;
    if fields
        .keys()
        .any(|k| k == "Id" || !EDITABLE.split(',').any(|f| f == k))
    {
        return err("Unsupported metadata fields; Id and read-only fields cannot be changed");
    }
    let uid = user(c, &a.s("user_id"))?;
    let path = format!("/Items/{}", quote(&a.s("id")));
    let before = c.get(&path, query(&[("userId", json!(uid))]))?;
    if fields.is_empty() {
        return Ok(before);
    }
    let collections = [
        "People",
        "ProviderIds",
        "Genres",
        "Tags",
        "Studios",
        "ProductionLocations",
        "LockedFields",
    ];
    let mut body: serde_json::Map<_, _> = object(&before)?
        .iter()
        .filter(|(k, _)| {
            EDITABLE.split(',').any(|s| s == k.as_str())
                && (!collections.contains(&k.as_str()) || fields.contains_key(*k))
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    body.extend(fields.clone());
    let response = c.send("POST", &path, vec![], Value::Object(body))?;
    if c.dry_run {
        return Ok(response);
    }
    let after = c.get(&path, query(&[("userId", json!(uid))]))?;
    if fields
        .iter()
        .any(|(k, v)| !["SortName", "ForcedSortName"].contains(&k.as_str()) && after[k] != *v)
    {
        return err(
            "Metadata write returned but readback differs; inspect the item before retrying",
        );
    }
    let mut changed: Vec<_> = fields.keys().cloned().collect();
    changed.sort();
    Ok(json!({"updated":a.v["id"],"changedFields":changed,"item":after}))
}
fn image_set(c: &mut Client, a: &Args) -> Result<Value> {
    require(c, &["jellyfin"])?;
    let remote = url::Url::parse(&a.s("url"))?;
    if !["http", "https"].contains(&remote.scheme()) {
        return err("Artwork URL must use HTTP or HTTPS");
    }
    let id = quote(&a.s("id"));
    let image_type = a.s("image_type");
    let path = format!("/Items/{id}/Images");
    let before = c.get(&path, vec![])?;
    let old: Vec<_> = records(&before)
        .into_iter()
        .flatten()
        .filter(|i| i["ImageType"] == image_type)
        .collect();
    let replace = image_type == "Backdrop" && !old.is_empty() && !a.b("append");
    if replace
        && !old
            .iter()
            .any(|i| i["ImageIndex"].as_f64() == Some(a.n("index")))
    {
        return err("No backdrop exists at the requested replacement index");
    }
    let download = c.request(
        "POST",
        &format!("/Items/{id}/RemoteImages/Download"),
        query(&[("type", json!(image_type)), ("imageUrl", json!(a.s("url")))]),
        Data::default(),
    )?;
    if c.dry_run {
        return Ok(
            json!({"download":download,"replaceBackdropIndex":if replace{a.v["index"].clone()}else{Value::Null}}),
        );
    }
    let mut after = c.get(&path, vec![])?;
    if replace {
        let images: Vec<_> = records(&after)
            .into_iter()
            .flatten()
            .filter(|i| i["ImageType"] == image_type)
            .collect();
        if images.len() != old.len() + 1 {
            return err("Unexpected backdrop count after download; no prior artwork was removed");
        }
        let new = images
            .iter()
            .filter_map(|i| i["ImageIndex"].as_i64())
            .max()
            .ok_or_else(|| Error::new("Missing backdrop index"))?;
        c.request(
            "POST",
            &format!("{path}/Backdrop/{new}/Index"),
            query(&[("newIndex", a.v["index"].clone())]),
            Data::default(),
        )?;
        c.request(
            "DELETE",
            &format!("{path}/Backdrop/{new}"),
            vec![],
            Data::default(),
        )?;
        after = c.get(&path, vec![])?;
    }
    Ok(json!({"itemId":a.v["id"],"images":after}))
}
fn add_media(c: &mut Client, a: &Args) -> Result<Value> {
    if !config::manager(&c.kind) {
        return err("Add requires a media manager");
    }
    let roots = c.get("/rootfolder", vec![])?;
    if !records(&roots)
        .into_iter()
        .flatten()
        .any(|r| norm(&text(&r["path"])) == norm(&a.s("root")))
    {
        return err("Choose an existing root from roots");
    }
    let profiles = c.get("/qualityprofile", vec![])?;
    if !records(&profiles)
        .into_iter()
        .flatten()
        .any(|p| p["id"] == a.v["profile_id"])
    {
        return err("Choose an existing quality profile ID from profiles");
    }
    if ["lidarr", "bookshelf", "readarr"].contains(&c.kind.as_str()) && !a.b("metadata_profile_id")
    {
        return err("This app requires --metadata-profile-id");
    }
    let entity = config::entity(&c.kind);
    let lookup = c.get(
        &format!("/{entity}/lookup"),
        query(&[("term", a.v["term"].clone())]),
    )?;
    let items = lookup.as_array().cloned().unwrap_or(vec![lookup]);
    if items.len() != 1 {
        return err(format!(
            "Lookup returned {} candidates; use lookup first with an exact provider ID",
            items.len()
        ));
    }
    let mut body = items[0].clone();
    if truth(&body["id"]) {
        return err("This item is already managed; use get or update");
    }
    object(&body)?;
    body.as_object_mut().unwrap().remove("id");
    body["rootFolderPath"] = a.v["root"].clone();
    body["qualityProfileId"] = a.v["profile_id"].clone();
    body["monitored"] = json!(a.s("monitor") != "none");
    for (k, f) in [
        ("metadataProfileId", "metadata_profile_id"),
        ("languageProfileId", "language_profile_id"),
    ] {
        if a.b(f) {
            body[k] = a.v[f].clone();
        }
    }
    let search = a.v["search"].clone();
    match c.kind.as_str() {
        "radarr" => {
            body["minimumAvailability"] = json!("released");
            body["addOptions"] = json!({"searchForMovie":search});
        }
        "sonarr" => {
            body["seriesType"] = a.v["series_type"].clone();
            body["seasonFolder"] = json!(true);
            body["addOptions"] =
                json!({"monitor":a.v["monitor"],"searchForMissingEpisodes":search});
        }
        "lidarr" => {
            body["addOptions"] = json!({"monitor":a.v["monitor"],"searchForMissingAlbums":search})
        }
        _ => body["addOptions"] = json!({"monitor":a.v["monitor"],"searchForMissingBooks":search}),
    }
    c.send("POST", &format!("/{entity}"), vec![], body)
}
pub fn action(c: &mut Client, a: &Args) -> Result<Value> {
    let mut qs = pairs(&a.list("query"))?;

    let act = a.action.as_str();

    match act{
    "api"=>c.request(&a.s("method"),&a.s("path"),qs,request_data(a)?),
    "ops"|"schema"|"call"=>{let spec=crate::catalog::load(c,a.b("refresh_schema"))?;
let prefix=text(&c.app["api_prefix"]);
match act{"ops"=>Ok(json!(crate::catalog::operations(&spec,&prefix,&a.s("search"),&a.s("method"),a.b("details"))?)),"schema"=>if a.b("operation"){crate::catalog::operation(&spec,&prefix,&a.s("operation"),a.b("full"))}else{Ok(spec)},_=>crate::catalog::invoke(c,&spec,&a.s("operation"),&a.list("path"),qs,request_data(a)?)}},
    "status"=>{let path=c.app["status_path"].as_str().unwrap_or(config::status(&c.kind)).to_string();
c.get(&path,qs)},
    "health"=>c.get(if c.kind=="bazarr"{"/system/health"}else{"/health"},qs),
    "list"|"get"|"create"|"update"|"delete"=>{let path=resource(c,&a.s("resource"),a.v.get("id"));
if ["list","get"].contains(&act){let qs=crate::catalog::server_filters(c,&path,qs,&a.list("filter"));
return paginated(c,&path,qs,a.b("all"));
}
if act=="create"{let body=payload(a)?.ok_or_else(||Error::new("create requires --data or --set"))?;
return c.send("POST",&path,qs,body);
}
if act=="update"{if !a.b("data")&&a.list("set").is_empty(){return err("update requires --data or --set");
}let before=c.get(&path,qs.clone())?;
let body=updated(before.clone(),a)?;
if ["sonarr","radarr"].contains(&c.kind.as_str())&&["/movie","/series"].contains(&resource(c,&a.s("resource"),None).as_str())&&["path","rootFolderPath"].iter().any(|k|before[*k]!=body[*k]){return err("Use move to change managed paths; it checks destination and requests moveFiles=true");
}return c.send(&a.s("method"),&path,qs,body);
}
if config::manager(&c.kind)&&["/movie","/series","/artist","/author"].contains(&resource(c,&a.s("resource"),None).as_str()){if !a.b("id"){return err("Deleting managed media requires an explicit ID");
}
if qs.iter().any(|(k,_)|k=="deleteFiles"){return err("Use --delete-files explicitly");
}qs.push(("deleteFiles".into(),json!(a.b("delete_files"))));
}c.request("DELETE",&path,qs,Data::default())},
    "queue"|"history"|"roots"|"profiles"|"tags"|"indexers"|"clients"|"files"=>{let path=match act{"roots"=>"/rootfolder","profiles"=>if c.kind=="prowlarr"{"/appprofile"}else if c.kind=="bazarr"{"/system/languages/profiles"}else{"/qualityprofile"},"tags"=>"/tag","indexers"=>"/indexer","clients"=>"/downloadclient","files"=>match c.kind.as_str(){"sonarr"=>"/episodefile","radarr"=>"/moviefile","lidarr"=>"/trackfile",_=>"/bookfile"},"queue"=>"/queue",_=>"/history"};
let qs=crate::catalog::server_filters(c,path,qs,&a.list("filter"));
paginated(c,path,qs,a.b("all"))},
    "wanted"=>{let path=if c.kind=="bazarr"{format!("/{}/wanted",a.s("media"))}else{format!("/wanted/{}",if a.b("cutoff"){"cutoff"}else{"missing"})};
paginated(c,&path,qs,a.b("all"))},
    "lookup"=>{qs.insert(0,("term".into(),a.v["term"].clone()));
c.get(&format!("/{}/lookup",config::entity(&c.kind)),qs)},"add"=>add_media(c,a),
    "releases"=>{if c.kind=="prowlarr"{if !a.b("term"){return err("Prowlarr releases needs --term");
}qs.insert(0,("query".into(),a.v["term"].clone()));
return c.get("/search",qs);
}for(k,n)in[("movie_id","movieId"),("series_id","seriesId"),("episode_id","episodeId"),("season_number","seasonNumber"),("artist_id","artistId"),("album_id","albumId")]{if !a.v[k].is_null(){qs.push((n.into(),a.v[k].clone()));
}}
if qs.is_empty(){return err("Supply an explicit media ID for release search");
}c.get("/release",qs)},
    "missing"|"find-missing"=>crate::missing::find(c,a),
    "grab"=>{if a.b("ref")&&(a.b("data")||!a.list("set").is_empty()||a.b("guid")||a.b("indexer_id")){return err("Use either --ref or explicit release body/flags");
}let mut body=if a.v.get("grab_body").is_some(){a.v["grab_body"].clone()}else if a.b("ref"){Results::new(&c.state).reference(&a.s("ref"),&c.name())?}else{payload(a)?.unwrap_or(json!({}))};
object(&body)?;
if a.b("guid"){body["guid"]=a.v["guid"].clone();
}
if !a.v["indexer_id"].is_null(){body["indexerId"]=a.v["indexer_id"].clone();
}
if !truth(&body["guid"])||body["indexerId"].is_null(){return err("grab needs a release guid and indexerId");
}let path=if c.kind=="prowlarr"{"/search"}else{"/release"};
c.send("POST",path,qs,body)},
    "queue-remove"=>c.request("DELETE",&format!("/queue/{}",a.s("id")),query(&[("removeFromClient",json!(a.b("remove_from_client"))),("blocklist",json!(a.b("blocklist"))),("skipRedownload",json!(a.b("skip_redownload")))]),Data::default()),
    "command"=>native_command(c,&a.s("name"),payload(a)?.unwrap_or(json!({}))),"move"=>move_media(c,a),
    "rename"=>{if !a.b("apply"){let key=if c.kind=="radarr"{"movieId"}else{"seriesId"};
return c.get("/rename",query(&[(key,a.v["id"].clone())]));
}
if c.kind=="radarr"{native_command(c,"RenameMovie",json!({"movieIds":[a.v["id"]]}))}else{native_command(c,"RenameSeries",json!({"seriesId":a.v["id"]}))}},
    "manual-import"=>{if a.b("apply"){let body=payload(a)?.unwrap_or(Value::Null);
if !body["files"].as_array().is_some_and(|a|!a.is_empty()){return err("Apply requires an exact reviewed command object containing nonempty files");
}return native_command(c,"ManualImport",body);
}
if !a.b("folder"){return err("Inspect requires --folder");
}qs.insert(0,("folder".into(),a.v["folder"].clone()));
c.get("/manualimport",qs)},
    "items"=>{qs.push(("recursive".into(),json!(true)));
for(k,n)in[("item_type","includeItemTypes"),("parent","parentId"),("search","searchTerm")]{if a.b(k){qs.push((n.into(),a.v[k].clone()));
}}paginated(c,"/Items",qs,a.b("all"))},"metadata"=>metadata(c,a),"images"=>c.get(&format!("/Items/{}/Images",quote(&a.s("id"))),qs),"image-set"=>image_set(c,a),
    "refresh"=>c.request("POST",&format!("/Items/{}/Refresh",quote(&a.s("id"))),query(&[("recursive",json!(a.b("recursive"))),("metadataRefreshMode",json!(if a.b("replace_metadata"){"FullRefresh"}else{"Default"})),("imageRefreshMode",json!(if a.b("replace_images"){"FullRefresh"}else{"Default"})),("replaceAllMetadata",json!(a.b("replace_metadata"))),("replaceAllImages",json!(a.b("replace_images")))]),Data::default()),
    "libraries"=>c.get("/Library/VirtualFolders",qs),"tasks"=>c.get("/ScheduledTasks",qs),"task-run"=>c.request("POST",&format!("/ScheduledTasks/Running/{}",quote(&a.s("id"))),vec![],Data::default()),
    "collection"=>c.request("POST","/Collections",query(&[("name",a.v["name"].clone()),("ids",a.v["ids"].clone())]),Data::default()),"playlist"=>c.send("POST","/Playlists",vec![],json!({"Name":a.v["name"],"Ids":a.s("ids").split(',').collect::<Vec<_>>(),"UserId":a.v["user_id"],"MediaType":"Video","IsPublic":false})),
    "library-options"=>{let libraries=c.get("/Library/VirtualFolders",vec![])?;
let matches:Vec<_>=records(&libraries).into_iter().flatten().filter(|i|i["ItemId"]==a.v["id"]).collect();
if matches.len()!=1{return err("No unique library with that ID");
}let before=matches[0]["LibraryOptions"].clone();
if !a.b("data")&&a.list("set").is_empty(){return Ok(before);
}let options=updated(before,a)?;
c.send("POST","/Library/VirtualFolders/LibraryOptions",vec![],json!({"Id":a.v["id"],"LibraryOptions":options}))},
    "subtitles"=>c.request(&a.s("method"),&format!("/{}/subtitles",a.s("media")),qs,request_data(a)?),
    "subtitle-search"=>{let mut q=query(&[("language",a.v["language"].clone()),("forced",json!(a.b("forced"))),("hi",json!(a.b("hi")))]);
let path=if a.b("episode_id"){if !a.b("series_id"){return err("Episode subtitle search also needs --series-id");
}q.extend(query(&[("seriesid",a.v["series_id"].clone()),("episodeid",a.v["episode_id"].clone())]));
"/episodes/subtitles"}else{q.push(("radarrid".into(),a.v["movie_id"].clone()));
"/movies/subtitles"};
c.request("PATCH",path,q,Data::default())},
    "subtitle-sync"=>{let mut q=query(&[("action",json!("sync")),("language",a.v["language"].clone()),("path",a.v["path"].clone()),("type",a.v["media_type"].clone()),("id",a.v["id"].clone())]);
if a.b("reference"){q.push(("reference".into(),a.v["reference"].clone()));
}c.request("PATCH","/subtitles",q,Data::default())},
    "torrents"=>c.get("/torrents/info",qs),"transfer"=>c.get("/transfer/info",qs),"torrent"=>c.get(&format!("/torrents/{}",a.s("view")),query(&[("hash",a.v["hash"].clone())])),
    "torrent-action"=>{let operation=a.s("operation");
let native=match operation.as_str(){"category"=>"setCategory","location"=>"setLocation",_=>&operation};
let mut form=query(&[("hashes",a.v["hashes"].clone())]);
for flag in ["category","location"]{if operation==flag{if a.v[flag].is_null(){return err(format!("{flag} requires --{flag}"));
}form.push((flag.into(),a.v[flag].clone()));
}}
if a.b("delete_files")&&operation!="delete"{return err("--delete-files is only valid for torrent-action delete");
}
if operation=="delete"{form.push(("deleteFiles".into(),json!(a.b("delete_files"))));
}c.request("POST",&format!("/torrents/{native}"),vec![],Data{form:Some(form),..Default::default()})},
    "search"=>c.get(if c.kind=="navidrome"{"/search3.view"}else{"/search"},query(&[("query",a.v["term"].clone())]).into_iter().chain(qs).collect()),_=>err("Unknown application action"),
}
}
