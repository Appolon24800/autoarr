use crate::{Error, Result, err, text, truth};
use chrono::Utc;
use regex::Regex;
use serde_json::{Value, json};
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::{
    collections::BTreeSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::{Duration, SystemTime},
};

pub const KINDS: &[&str] = &[
    "radarr",
    "sonarr",
    "lidarr",
    "bookshelf",
    "readarr",
    "prowlarr",
    "bazarr",
    "jellyfin",
    "seerr",
    "qbittorrent",
    "kapowarr",
    "cleanuparr",
    "autobrr",
    "navidrome",
    "generic",
];
pub fn is_arr(kind: &str) -> bool {
    [
        "radarr",
        "sonarr",
        "lidarr",
        "bookshelf",
        "readarr",
        "prowlarr",
    ]
    .contains(&kind)
}
pub fn manager(kind: &str) -> bool {
    is_arr(kind) && kind != "prowlarr"
}
pub fn prefix(kind: &str) -> &'static str {
    match kind {
        "radarr" | "sonarr" => "/api/v3",
        "lidarr" | "bookshelf" | "readarr" | "prowlarr" | "seerr" => "/api/v1",
        "qbittorrent" => "/api/v2",
        "bazarr" | "kapowarr" | "autobrr" | "cleanuparr" => "/api",
        "navidrome" => "/rest",
        _ => "",
    }
}
pub fn status(kind: &str) -> &'static str {
    match kind {
        "jellyfin" => "/System/Info",
        "qbittorrent" => "/app/version",
        "seerr" => "/status",
        "kapowarr" => "/system/about",
        "cleanuparr" => "/health",
        "autobrr" => "/config",
        "navidrome" => "/ping.view",
        "generic" => "/",
        _ => "/system/status",
    }
}
pub fn entity(kind: &str) -> &'static str {
    match kind {
        "radarr" => "movie",
        "sonarr" => "series",
        "lidarr" => "artist",
        _ => "author",
    }
}
pub fn mkdir(path: &Path) -> Result<()> {
    if !path.as_os_str().is_empty() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)?;
    }
    Ok(())
}
pub fn private_json(path: &Path, value: &Value) -> Result<()> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        return err("Refusing to overwrite a symlink");
    }
    let parent = path.parent().unwrap_or(Path::new("."));
    mkdir(parent)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.as_file()
        .set_permissions(fs::Permissions::from_mode(0o600))?;
    serde_json::to_writer(&mut temp, value)?;
    temp.write_all(b"\n")?;
    temp.persist(path)
        .map_err(|e| Error::new(e.error.to_string()))?;
    Ok(())
}
pub fn random_hex(count: usize) -> Result<String> {
    let mut bytes = vec![0; count];
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
pub fn expand_path(s: &str) -> PathBuf {
    if let Some(rest) = s.strip_prefix("~/") {
        PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(rest)
    } else {
        PathBuf::from(s)
    }
}
pub fn config_path(explicit: Option<String>) -> Result<PathBuf> {
    if let Some(path) = explicit.or_else(|| std::env::var("AUTOARR_CONFIG").ok()) {
        let p = expand_path(&path);
        return Ok(if p.is_absolute() {
            p
        } else {
            std::env::current_dir()?.join(p)
        });
    }
    let local = std::env::current_dir()?.join(".local/autoarr/config.json");
    if local.exists() {
        return Ok(local);
    }
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join(".local/autoarr/config.json");
    if checkout.exists() {
        return Ok(checkout);
    }
    Ok(std::env::var("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".config")
        })
        .join("autoarr/config.json"))
}
pub fn read_config(path: &Path, required: bool) -> Result<Value> {
    if !path.exists() {
        return if required {
            err("No configuration. Run autoarr init or autoarr config add; see --help.")
        } else {
            Ok(json!({"version":1,"apps":{}}))
        };
    }
    if fs::metadata(path)?.permissions().mode() & 0o077 != 0 {
        return err("Configuration must be private; run chmod 600 on its file");
    }
    let value: Value = serde_json::from_slice(&fs::read(path)?)?;
    if value["version"] != 1 || !value["apps"].is_object() {
        return err("Expected configuration version 1 and an apps object");
    }
    Ok(value)
}
pub fn app_config(config: &Value, name: &str) -> Result<Value> {
    let mut app = config["apps"]
        .get(name)
        .cloned()
        .ok_or_else(|| Error::new(format!("App {name} is not configured; run autoarr apps")))?;
    if !app.is_object() {
        return err("Application configuration must be an object");
    }
    let kind = app["kind"].as_str().unwrap_or(name).to_owned();
    if !KINDS.contains(&kind.as_str()) {
        return err("Unknown app kind");
    }
    let env = format!(
        "AUTOARR_{}",
        name.chars()
            .map(|c| if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            })
            .collect::<String>()
    );
    let base = std::env::var(format!("{env}_URL"))
        .unwrap_or_else(|_| text(&app["url"]))
        .trim_end_matches('/')
        .to_owned();
    let url = url::Url::parse(&base)?;
    if !["http", "https"].contains(&url.scheme())
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return err("App URL must be an HTTP(S) base URL without credentials, query, or fragment");
    }
    app["kind"] = json!(kind);
    app["url"] = json!(base);
    app["name"] = json!(name);
    let key = std::env::var(format!("{env}_API_KEY"))
        .ok()
        .or_else(|| std::env::var(text(&app["api_key_env"])).ok())
        .unwrap_or_else(|| text(&app["api_key"]));
    app["api_key"] = json!(key);
    if truth(&app["password_env"]) {
        app["password"] = json!(std::env::var(text(&app["password_env"])).unwrap_or_default());
    }
    if app["api_prefix"].is_null() {
        app["api_prefix"] = json!(prefix(&kind));
    }
    if app["api_key_header"].is_null() {
        app["api_key_header"] = json!(if kind == "autobrr" {
            "X-API-Token"
        } else {
            "X-Api-Key"
        });
    }
    if app["auth"].is_null() {
        app["auth"] = json!(match kind.as_str() {
            "qbittorrent" => "none",
            "kapowarr" => "query",
            "navidrome" => "subsonic",
            _ => "header",
        });
    }
    if kind == "jellyfin" {
        app["auth"] = json!("jellyfin");
    }
    Ok(app)
}
fn secret() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"(?i)(?:password|passwd|passkey|auth.?key|api.?key|access.?token|refresh.?token|session.?secret|client.?secret|authorization|cookie|private.?key|secret|^token$|^tokenhash$|^salt$|^t$|^p$)").unwrap())
}
#[derive(Clone, Default)]
pub struct Redactor {
    secrets: BTreeSet<String>,
}
impl Redactor {
    pub fn collect(&mut self, value: &Value) {
        self.collect_key(value, "");
    }
    fn collect_key(&mut self, v: &Value, key: &str) {
        match v {
            Value::Object(m) => {
                let sensitive = secret().is_match(&text(
                    m.get("name")
                        .or_else(|| m.get("key"))
                        .unwrap_or(&Value::Null),
                )) || ["password", "apiKey"]
                    .contains(&text(&v["privacy"]).as_str());
                for (k, value) in m {
                    self.collect_key(
                        value,
                        if k == "value" && sensitive {
                            "secret"
                        } else {
                            k
                        },
                    );
                }
            }
            Value::Array(a) => {
                for value in a {
                    self.collect_key(value, key);
                }
            }
            Value::String(s)
                if !key.ends_with("_env") && secret().is_match(key) && s.len() >= 4 =>
            {
                self.secrets.insert(s.clone());
            }
            _ => (),
        }
    }
    pub fn clean(&self, v: &Value) -> Value {
        self.clean_key(v, "")
    }
    fn clean_key(&self, v: &Value, key: &str) -> Value {
        if secret().is_match(key)
            && !key.ends_with("_env")
            && !v.is_object()
            && !v.is_array()
            && !v.is_null()
            && v != ""
        {
            return json!("[REDACTED]");
        }
        match v {
            Value::Object(m) => {
                let sensitive = secret().is_match(&text(
                    m.get("name")
                        .or_else(|| m.get("key"))
                        .unwrap_or(&Value::Null),
                )) || ["password", "apiKey"]
                    .contains(&text(&v["privacy"]).as_str());
                Value::Object(
                    m.iter()
                        .map(|(k, value)| {
                            (
                                k.clone(),
                                self.clean_key(
                                    value,
                                    if k == "value" && sensitive {
                                        "secret"
                                    } else {
                                        k
                                    },
                                ),
                            )
                        })
                        .collect(),
                )
            }
            Value::Array(a) => Value::Array(a.iter().map(|v| self.clean(v)).collect()),
            Value::String(s) => {
                let mut s = s.clone();
                let mut secrets: Vec<_> = self.secrets.iter().collect();
                secrets.sort_by_key(|s| std::cmp::Reverse(s.len()));
                for key in secrets {
                    s = s.replace(key, "[REDACTED]");
                }
                static QUERY: OnceLock<Regex> = OnceLock::new();
                static USER: OnceLock<Regex> = OnceLock::new();
                let q = QUERY.get_or_init(|| Regex::new(r#"(?i)([?&](?:apikey|api_key|api-key|passkey|authkey|token|password|access_token|t|p|s)=)[^&\s\"<>]+"#).unwrap());
                s = q.replace_all(&s, "${1}[REDACTED]").into_owned();
                let u = USER.get_or_init(|| Regex::new(r"(https?://)[^/@\s]+:[^/@\s]+@").unwrap());
                json!(u.replace_all(&s, "${1}[REDACTED]@").into_owned())
            }
            _ => v.clone(),
        }
    }
}
#[derive(Clone)]
pub struct Results {
    pub directory: PathBuf,
}
impl Results {
    pub fn new(state: &Path) -> Self {
        Self {
            directory: state.join("results"),
        }
    }
    pub fn save(&self, data: &Value, app: &str, action: &str) -> Result<String> {
        let key = random_hex(6)?;
        private_json(
            &self.directory.join(format!("{key}.json")),
            &json!({"id":key,"app":app,"action":action,"createdAt":Utc::now().to_rfc3339(),"data":data}),
        )?;
        static LOCK: Mutex<()> = Mutex::new(());
        let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
        self.prune()?;
        Ok(key)
    }
    pub fn load(&self, key: &str) -> Result<Value> {
        if key.len() != 12
            || !key
                .chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
        {
            return err("Result IDs are 12 hexadecimal characters");
        }
        let path = self.directory.join(format!("{key}.json"));
        let meta = fs::symlink_metadata(&path)
            .map_err(|_| Error::new("Result not found; repeat the source query"))?;
        if meta.file_type().is_symlink() {
            return err("Refusing a symlink result");
        }
        if meta.modified()?.elapsed().unwrap_or_default() > Duration::from_secs(86400) {
            return err("Result expired (24 hours); repeat the source query");
        }
        Ok(serde_json::from_slice(&fs::read(path)?)?)
    }
    pub fn reference(&self, reference: &str, app: &str) -> Result<Value> {
        let (key, index) = reference
            .split_once(':')
            .ok_or_else(|| Error::new("A row reference has the form RESULT_ID:ROW_NUMBER"))?;
        let index: usize = index
            .parse()
            .map_err(|_| Error::new("Invalid row reference"))?;
        let entry = self.load(key)?;
        if entry["app"] != app || entry["action"] != "releases" {
            return err("Reference is not a release result for this app");
        }
        crate::output::records(&entry["data"])
            .and_then(|a| a.get(index))
            .cloned()
            .ok_or_else(|| Error::new("Reference row does not exist"))
    }
    fn prune(&self) -> Result<()> {
        let mut files = Vec::new();
        for file in fs::read_dir(&self.directory)? {
            let file = file?;
            if file.path().extension().is_some_and(|s| s == "json")
                && let Ok(meta) = file.path().symlink_metadata()
                && !meta.file_type().is_symlink()
            {
                files.push((
                    meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                    meta.len(),
                    file.path(),
                ));
            }
        }
        files.sort_by_key(|f| std::cmp::Reverse(f.0));
        let mut size = 0;
        for (i, (time, len, path)) in files.into_iter().enumerate() {
            size += len;
            if i >= 100
                || size > 128 * 1024 * 1024
                || time.elapsed().unwrap_or_default() > Duration::from_secs(86400)
            {
                let _ = fs::remove_file(path);
            }
        }
        Ok(())
    }
}
