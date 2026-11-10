use crate::{
    Error, Result,
    config::{self, Redactor},
    err, object, text, truth,
};
use reqwest::{
    Method,
    blocking::{Client as Http, multipart},
    redirect::Policy,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::fs::OpenOptionsExt;
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
pub type Query = Vec<(String, Value)>;
pub fn query(items: &[(&str, Value)]) -> Query {
    items
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}
pub fn quote(s: &str) -> String {
    const SET: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC
        .remove(b'-')
        .remove(b'_')
        .remove(b'.')
        .remove(b'~');
    percent_encoding::utf8_percent_encode(s, SET).to_string()
}
pub fn subsonic_write(path: &str) -> bool {
    let mut decoded = path.to_string();
    for _ in 0..3 {
        decoded = percent_encoding::percent_decode_str(&decoded)
            .decode_utf8_lossy()
            .into_owned();
    }
    let path = decoded.as_str();
    let p = path
        .split('?')
        .next()
        .unwrap_or("")
        .rsplit('/')
        .next()
        .unwrap_or("");
    [
        "create",
        "update",
        "delete",
        "set",
        "change",
        "start",
        "star",
        "unstar",
        "scrobble",
        "jukeboxControl",
    ]
    .iter()
    .any(|s| p.starts_with(s))
}
#[derive(Default, Clone)]
pub struct Data {
    pub body: Option<Value>,
    pub form: Option<Query>,
    pub raw: Option<Vec<u8>>,
    pub files: Vec<(String, PathBuf)>,
    pub content_type: Option<String>,
    pub output: Option<PathBuf>,
    pub site: bool,
}
pub struct Client {
    pub app: Value,
    pub kind: String,
    pub state: PathBuf,
    pub redactor: Redactor,
    pub timeout: f64,
    pub dry_run: bool,
    pub deadline: Option<Instant>,
    http: Http,
    authenticated: bool,
}
impl Client {
    pub fn new(
        app: Value,
        state: PathBuf,
        mut redactor: Redactor,
        timeout: f64,
        dry_run: bool,
    ) -> Result<Self> {
        redactor.collect(&app);
        let http = Http::builder()
            .redirect(Policy::none())
            .cookie_store(true)
            .user_agent(concat!("autoarr/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self {
            kind: text(&app["kind"]),
            app,
            state,
            redactor,
            timeout,
            dry_run,
            deadline: None,
            http,
            authenticated: false,
        })
    }
    pub fn name(&self) -> String {
        text(&self.app["name"])
    }
    pub fn url(&self, path: &str, site: bool) -> Result<(String, Query)> {
        if path.contains('\\')
            || path.starts_with("//")
            || path.contains('#')
            || url::Url::parse(path).is_ok()
        {
            return err("API paths must be relative to the configured application");
        }
        let (path, search) = path.split_once('?').unwrap_or((path, ""));
        let mut decoded = path.to_owned();
        for _ in 0..3 {
            decoded = percent_encoding::percent_decode_str(&decoded)
                .decode_utf8_lossy()
                .into_owned();
        }
        if decoded.starts_with("//") || decoded.split('/').any(|s| s == "." || s == "..") {
            return err("Path traversal is not allowed in API paths");
        }
        let mut path = format!("/{}", path.trim_start_matches('/'));
        let prefix = text(&self.app["api_prefix"])
            .trim_end_matches('/')
            .to_string();
        if !site
            && !prefix.is_empty()
            && path != prefix
            && !path.starts_with(&(prefix.clone() + "/"))
        {
            path = prefix + &path;
        }
        Ok((
            text(&self.app["url"]) + &path,
            url::form_urlencoded::parse(search.as_bytes())
                .map(|(k, v)| (k.into_owned(), json!(v)))
                .collect(),
        ))
    }
    pub fn journal(&mut self, event: Value) -> Result<()> {
        self.redactor.collect(&event);
        config::mkdir(&self.state)?;
        let mut entry = object(&event)?.clone();
        entry.insert("time".into(), json!(chrono::Utc::now().to_rfc3339()));
        entry.insert("app".into(), self.app["name"].clone());
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.state.join("requests.jsonl"))?;
        writeln!(file, "{}", self.redactor.clean(&Value::Object(entry)))?;
        Ok(())
    }
    pub fn get(&mut self, path: &str, query: Query) -> Result<Value> {
        self.request("GET", path, query, Data::default())
    }
    pub fn send(&mut self, method: &str, path: &str, query: Query, body: Value) -> Result<Value> {
        self.request(
            method,
            path,
            query,
            Data {
                body: Some(body),
                ..Default::default()
            },
        )
    }
    pub fn request(&mut self, method: &str, path: &str, qs: Query, data: Data) -> Result<Value> {
        if self.deadline.is_some_and(|d| Instant::now() >= d) {
            return err("Wait deadline reached");
        }
        if !["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].contains(&method) {
            return err("Unsupported HTTP method");
        }
        let (mut url, mut qs0) = self.url(path, data.site)?;
        qs0.extend(qs);
        let is_write = !["GET", "HEAD", "OPTIONS"].contains(&method)
            || (self.kind == "navidrome" && subsonic_write(path));
        if [data.body.is_some(), data.form.is_some(), data.raw.is_some()]
            .iter()
            .filter(|b| **b)
            .count()
            > 1
            || (!data.files.is_empty() && (data.body.is_some() || data.raw.is_some()))
        {
            return err(
                "Use only one of JSON data, form data, or binary upload; files combine with forms only",
            );
        }
        if let Some(output) = &data.output {
            if output.exists() || fs::symlink_metadata(output).is_ok() {
                return err("Output file already exists");
            }
            if !output.parent().unwrap_or(Path::new(".")).is_dir() {
                return err("Output directory is not writable");
            }
        }
        let mut plan = json!({"method":method,"url":url,"query":qs0});
        if let Some(body) = &data.body {
            plan["body"] = body.clone();
        }
        if let Some(form) = &data.form {
            plan["form"] = json!(form);
        }
        if let Some(raw) = &data.raw {
            plan["uploadBytes"] = json!(raw.len());
        }
        if !data.files.is_empty() {
            plan["files"] = json!(
                data.files
                    .iter()
                    .map(|(k, p)| Ok(json!({"field":k,"path":p,"bytes":fs::metadata(p)?.len()})))
                    .collect::<Result<Vec<_>>>()?
            );
        }
        if self.dry_run && is_write {
            return Ok(json!({"dryRun":true,"request":self.redactor.clean(&plan)}));
        }
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert("Accept", "application/json".parse().unwrap());
        let key = text(&self.app["api_key"]);
        let header_value = |s: &str| {
            s.parse::<reqwest::header::HeaderValue>()
                .map_err(|_| Error::new("Invalid configured authentication header"))
        };
        match text(&self.app["auth"]).as_str() {
            "jellyfin" => {
                headers.insert(
                    "Authorization",
                    header_value(&format!("MediaBrowser Token=\"{key}\""))?,
                );
            }
            "bearer" => {
                headers.insert("Authorization", header_value(&format!("Bearer {key}"))?);
            }
            "query" => qs0.push(("api_key".into(), json!(key))),
            "none" => (),
            "subsonic" => {
                if !truth(&self.app["username"])
                    || (!truth(&self.app["password"]) && key.is_empty())
                {
                    return err("Navidrome needs privately configured user authentication");
                }
                qs0.extend(query(&[
                    ("u", self.app["username"].clone()),
                    ("v", json!("1.16.1")),
                    ("c", json!("autoarr")),
                    ("f", json!("json")),
                ]));
                if !key.is_empty() {
                    qs0.push(("apiKey".into(), json!(key)));
                } else {
                    use md5::Md5;
                    let salt = config::random_hex(12)?;
                    let hash = format!(
                        "{:x}",
                        Md5::digest((text(&self.app["password"]) + &salt).as_bytes())
                    );
                    qs0.extend(query(&[("s", json!(salt)), ("t", json!(hash))]));
                }
            }
            _ => {
                let name = reqwest::header::HeaderName::from_bytes(
                    text(&self.app["api_key_header"]).as_bytes(),
                )
                .map_err(|_| Error::new("Invalid authentication header name"))?;
                headers.insert(name, header_value(&key)?);
            }
        }
        if self.kind == "qbittorrent" {
            headers.insert("Referer", header_value(&(text(&self.app["url"]) + "/"))?);
            headers.insert("Origin", header_value(&text(&self.app["url"]))?);
            if truth(&self.app["username"]) && !self.authenticated {
                let login = self
                    .http
                    .post(text(&self.app["url"]) + &text(&self.app["api_prefix"]) + "/auth/login")
                    .headers(headers.clone())
                    .timeout(Duration::from_secs_f64(self.timeout))
                    .form(&[
                        ("username", text(&self.app["username"])),
                        ("password", text(&self.app["password"])),
                    ])
                    .send()
                    .map_err(|_| Error::new("qBittorrent login failed"))?;
                if !login.status().is_success() || login.text().unwrap_or_default().trim() != "Ok."
                {
                    return err("qBittorrent login failed");
                }
                self.authenticated = true;
            }
        }
        if !qs0.is_empty() {
            let mut parsed = url::Url::parse(&url)?;
            for (k, v) in &qs0 {
                parsed.query_pairs_mut().append_pair(k, &text(v));
            }
            url = parsed.into();
        }
        if is_write {
            self.journal(json!({"phase":"started","request":plan}))?;
        }
        let result = (|| -> Result<Value> {
            let origin = url::Url::parse(&url)?.origin();
            for redirects in 0..=10 {
                if self.deadline.is_some_and(|d| Instant::now() >= d) {
                    return err("Wait deadline reached");
                }
                let timeout = self
                    .deadline
                    .map(|d| {
                        d.saturating_duration_since(Instant::now())
                            .as_secs_f64()
                            .min(self.timeout)
                    })
                    .unwrap_or(self.timeout);
                let mut req = self
                    .http
                    .request(Method::from_bytes(method.as_bytes()).unwrap(), &url)
                    .headers(headers.clone())
                    .timeout(Duration::from_secs_f64(timeout.max(0.000001)));
                if !data.files.is_empty() {
                    let mut form = multipart::Form::new();
                    for (k, v) in data.form.as_ref().unwrap_or(&vec![]) {
                        form = form.text(k.clone(), text(v));
                    }
                    for (k, p) in &data.files {
                        let filename = p
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .to_string();
                        if k.contains(['\r', '\n']) || filename.contains(['\r', '\n']) {
                            return err("Invalid multipart field or filename");
                        }
                        let mime = mime(p);
                        form = form.part(
                            k.clone(),
                            multipart::Part::bytes(fs::read(p)?)
                                .file_name(filename)
                                .mime_str(&mime)
                                .map_err(|_| Error::new("Invalid MIME type"))?,
                        );
                    }
                    req = req.multipart(form);
                } else if let Some(body) = &data.body {
                    if self.kind == "qbittorrent" {
                        return err("qBittorrent expects --form fields, not JSON --data");
                    }
                    req = req.json(body);
                } else if let Some(form) = &data.form {
                    req = req.form(
                        &form
                            .iter()
                            .map(|(k, v)| (k.clone(), text(v)))
                            .collect::<Vec<_>>(),
                    );
                } else if let Some(raw) = &data.raw {
                    req = req
                        .header(
                            "Content-Type",
                            data.content_type
                                .as_deref()
                                .unwrap_or("application/octet-stream"),
                        )
                        .body(raw.clone());
                }
                let mut response = req.send().map_err(|e| {
                    Error::new(format!(
                        "Request failed for {}: {}{}",
                        self.name(),
                        e.without_url(),
                        if is_write {
                            "; write outcome may be uncertain, inspect before retrying"
                        } else {
                            ""
                        }
                    ))
                })?;
                let status = response.status().as_u16();
                if response.status().is_redirection() {
                    if is_write {
                        return err(
                            "Refusing to redirect a write request; check the configured URL",
                        );
                    }
                    let location = response
                        .headers()
                        .get("Location")
                        .and_then(|v| v.to_str().ok())
                        .ok_or_else(|| Error::new("Redirect missing Location"))?;
                    let next = url::Url::parse(&url)?.join(location)?;
                    if next.origin() != origin {
                        return err("Refusing a cross-origin API redirect");
                    }
                    if redirects == 10 {
                        return err("Too many API redirects");
                    }
                    url = next.into();
                    continue;
                }
                if !response.status().is_success() {
                    let mut bytes = Vec::new();
                    response.take(16384).read_to_end(&mut bytes)?;
                    let detail = serde_json::from_slice::<Value>(&bytes)
                        .unwrap_or(json!({"message":"API request failed"}));
                    return Err(Error {
                        message: format!(
                            "HTTP {status}: {method} {path}: {}",
                            self.redactor.clean(&detail)
                        ),
                        status: Some(status),
                    });
                }
                if let Some(output) = &data.output {
                    let mut file = fs::OpenOptions::new()
                        .create_new(true)
                        .write(true)
                        .mode(0o600)
                        .open(output)?;
                    let bytes = std::io::copy(&mut response, &mut file)?;
                    return Ok(json!({"savedTo":output,"bytes":bytes,"httpStatus":status}));
                }
                if !data.site
                    && response
                        .headers()
                        .get("Content-Type")
                        .and_then(|v| v.to_str().ok())
                        .is_some_and(|s| s.to_lowercase().contains("text/html"))
                {
                    return err(
                        "API returned HTML instead of data; check authentication and configured URL base",
                    );
                }
                let bytes = response.bytes()?;
                let value = if bytes.is_empty() {
                    json!({"httpStatus":status})
                } else {
                    match serde_json::from_slice::<Value>(&bytes) {
                        Ok(v) => v,
                        Err(_) => json!(
                            std::str::from_utf8(&bytes)
                                .map_err(|_| Error::new("Binary response; use --output FILE"))?
                        ),
                    }
                };
                if let Some(envelope) = value.get("subsonic-response")
                    && envelope["status"] != "ok"
                {
                    return err(format!(
                        "Subsonic API error: {}",
                        self.redactor.clean(&envelope["error"])
                    ));
                }
                return Ok(value);
            }
            err("Too many API redirects")
        })();
        if is_write {
            self.journal(json!({"phase":if result.is_ok() { "completed" } else if result.as_ref().err().and_then(|e| e.status).is_some() { "failed" } else { "uncertain" },"method":method,"path":path}))?;
        }
        result
    }
    pub fn schema_path(&self) -> PathBuf {
        let hash = format!("{:x}", Sha256::digest(text(&self.app["url"]).as_bytes()));
        self.state
            .join("schemas")
            .join(format!("{}-{}.json", self.name(), &hash[..12]))
    }
}
pub fn mime(path: &Path) -> String {
    mime_guess::from_path(path)
        .first_or_octet_stream()
        .essence_str()
        .to_string()
}
