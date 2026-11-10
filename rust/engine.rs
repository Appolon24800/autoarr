use crate::{
    Error, Result,
    args::{self, Args},
    client::Client,
    config::{self, Redactor, Results},
    err, object, text, truth, workflows,
};
use rayon::prelude::*;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::Ordering,
    time::{Duration, Instant},
};

pub struct Engine {
    pub config: Value,
    pub path: PathBuf,
}
pub struct Outcome {
    pub args: Args,
    pub data: Value,
    pub result: Option<String>,
    pub redactor: Redactor,
}
impl Outcome {
    pub fn exit_code(&self) -> i32 {
        if (self.args.scope == "doctor"
            && self
                .data
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v["ok"] != true)))
            || (self.args.scope == "batch" && self.data["failed"].as_u64().unwrap_or(0) > 0)
        {
            1
        } else {
            crate::waiting::exit_code(&self.data)
        }
    }
}
impl Engine {
    pub fn new(config: Value, path: PathBuf) -> Self {
        Self { config, path }
    }
    pub fn state(&self) -> &Path {
        self.path.parent().unwrap_or(Path::new("."))
    }
    pub fn client(&self, name: &str, a: &Args) -> Result<Client> {
        let app = config::app_config(&self.config, name)?;
        let mut redactor = Redactor::default();
        redactor.collect(&self.config);
        Client::new(
            app,
            self.state().to_path_buf(),
            redactor,
            a.n("timeout"),
            a.b("dry_run"),
        )
    }
    pub fn parse(&self, argv: &[String]) -> Result<Args> {
        args::parse(argv, &self.config)
    }
    pub fn run(&self, mut a: Args) -> Result<Outcome> {
        if !matches!(a.scope.as_str(), "init" | "config" | "result") && !self.path.exists() {
            return err("No configuration. Run autoarr init or autoarr config add");
        }
        let mut redactor = Redactor::default();
        redactor.collect(&self.config);
        let mut result = None;
        let data = match a.scope.as_str() {
            "init" => {
                let host = if a.b("host") {
                    a.s("host")
                } else {
                    a.s("ssh").rsplit('@').next().unwrap_or("").to_string()
                };
                if a.b("dry_run") {
                    json!({"dryRun":true,"ssh":a.v["ssh"],"host":host,"config":self.path,"operation":"read existing credentials; save private local configuration"})
                } else {
                    bootstrap(&a.s("ssh"), &host, &self.path, a.b("overwrite"))?
                }
            }
            "config" => {
                if a.action == "show" {
                    self.config.clone()
                } else {
                    let name = a.s("name");
                    if ["init", "apps", "doctor", "config", "batch", "result"]
                        .contains(&name.as_str())
                        || name.is_empty()
                        || !name
                            .chars()
                            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
                    {
                        return err(
                            "App name must be alphanumeric with hyphens/underscores and not a reserved command",
                        );
                    }
                    if a.b("api_key_env") && a.b("api_key_stdin") {
                        return err("Use an API key environment reference or stdin, not both");
                    }
                    let mut app = json!({"kind":a.v["kind"],"url":a.v["url"]});
                    for key in [
                        "api_prefix",
                        "api_key_env",
                        "auth",
                        "api_key_header",
                        "username",
                        "password_env",
                        "schema_path",
                        "status_path",
                        "user_id",
                    ] {
                        if !a.v[key].is_null() {
                            app[key] = a.v[key].clone();
                        }
                    }
                    if a.b("api_key_stdin") {
                        let mut key = String::new();
                        std::io::stdin().read_to_string(&mut key)?;
                        if key.trim().is_empty() {
                            return err("No API key received on stdin");
                        }
                        app["api_key"] = json!(key.trim());
                    }
                    let mut candidate = self.config.clone();
                    candidate["apps"][&name] = app;
                    config::app_config(&candidate, &name)?;
                    if !a.b("dry_run") {
                        config::private_json(&self.path, &candidate)?;
                    }
                    json!({"configured":name,"kind":a.v["kind"],"url":a.v["url"],"config":self.path,"dryRun":a.b("dry_run")})
                }
            }
            "result" => {
                if a.b("cached_id") {
                    let entry = Results::new(self.state()).load(&a.s("cached_id"))?;
                    a.output_action = text(&entry["action"]);
                    result = Some(text(&entry["id"]));
                    a.set("cached_at", entry["createdAt"].clone());
                    entry["data"].clone()
                } else {
                    let data = workflows::load_json(&format!("@{}", a.s("result_file")))?;
                    if crate::output::records(&data)
                        .and_then(|a| a.first())
                        .is_some_and(|v| v.get("guid").is_some())
                    {
                        a.output_action = "releases".into();
                    }
                    data
                }
            }
            "batch" => {
                a.output_action = "batch".into();
                crate::batch::execute(self, &a)?
            }
            "apps" => {
                let mut rows = Vec::new();
                let mut names: Vec<_> = object(&self.config["apps"])?.keys().collect();
                names.sort();
                for name in names {
                    let app = config::app_config(&self.config, name)?;
                    redactor.collect(&app);
                    rows.push(json!({"name":name,"kind":app["kind"],"url":app["url"],"apiPrefix":app["api_prefix"],"auth":app["auth"],"credentialConfigured":truth(&app["api_key"])||truth(&app["password"])||app["auth"]=="none"}));
                }
                json!(rows)
            }
            "doctor" => {
                let mut names: Vec<_> = object(&self.config["apps"])?.keys().cloned().collect();
                names.sort();
                let pool = rayon::ThreadPoolBuilder::new()
                    .num_threads(4)
                    .build()
                    .map_err(|e| Error::new(e.to_string()))?;
                let rows=pool.install(||names.par_iter().map(|name|match self.client(name,&a).and_then(|mut c|{redactor_for_config(&mut c,&self.config);let path=c.app["status_path"].as_str().unwrap_or(config::status(&c.kind)).to_string();let data=c.get(&path,vec![])?;if truth(&data["error"]){return err("Application returned an error");}let version=if c.kind=="bazarr"{data["data"]["bazarr_version"].clone()}else if !data["version"].is_null(){data["version"].clone()}else if !data["Version"].is_null(){data["Version"].clone()}else if !data["result"]["version"].is_null(){data["result"]["version"].clone()}else if data.is_string(){data}else{Value::Null};Ok(version)}){Ok(version)=>json!({"app":name,"ok":true,"version":version}),Err(e)=>json!({"app":name,"ok":false,"error":redactor.clean(&json!(e.message))})}).collect::<Vec<_>>());
                json!(rows)
            }
            _ => {
                let mut client = self.client(&a.scope, &a)?;
                let data = crate::waiting::action(&mut client, &mut a, &self.path)?;
                redactor = client.redactor;
                data
            }
        };
        if !a.b("no_cache")
            && !matches!(a.scope.as_str(), "config" | "init" | "apps" | "doctor")
            && result.is_none()
        {
            let action = if a.output_action.is_empty() {
                &a.scope
            } else {
                &a.output_action
            };
            match Results::new(self.state()).save(&data,&a.scope,action){Ok(key)=>result=Some(key),Err(_)=>a.set("cache_error",json!("Result cache unavailable; the application request already returned. Do not repeat a write just to cache its response."))}
        }
        redactor.collect(&data);
        Ok(Outcome {
            args: a,
            data,
            result,
            redactor,
        })
    }
}
fn redactor_for_config(c: &mut Client, config: &Value) {
    c.redactor.collect(config);
}
fn bootstrap(ssh: &str, host: &str, path: &Path, overwrite: bool) -> Result<Value> {
    if path.exists() && !overwrite {
        return err("Configuration already exists; use config add or explicitly pass --overwrite");
    }
    if ssh.starts_with('-')
        || host.starts_with('-')
        || !regex::Regex::new(r"^[A-Za-z0-9_.@:\[\]-]+$")
            .unwrap()
            .is_match(ssh)
        || !regex::Regex::new(r"^[A-Za-z0-9_.:\[\]-]+$")
            .unwrap()
            .is_match(host)
    {
        return err("Invalid SSH destination or HTTP host");
    }
    let input = json!({"host":host}).to_string();
    let literal = serde_json::to_string(&input)?;
    let code = include_str!("bootstrap_remote.py").replacen("INPUT", &literal, 1);
    let mut child = Command::new("ssh")
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            ssh,
            "python3",
            "-",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| Error::new("SSH discovery could not start; no configuration changed"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| Error::new("SSH stdin unavailable"))?
        .write_all(code.as_bytes())?;
    let started = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        if started.elapsed() > Duration::from_secs(240) || crate::INTERRUPTED.load(Ordering::SeqCst)
        {
            let _ = child.kill();
            let _ = child.wait();
            return err("SSH discovery did not complete; no configuration changed");
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let output = child.wait_with_output()?;
    if !output.status.success() {
        return err(
            "SSH discovery failed; requires the existing SSH key and passwordless Docker access",
        );
    }
    let data: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        Error::new("Invalid SSH discovery response; credentials were not printed or saved")
    })?;
    if !truth(&data["apps"]) {
        return err("No applications discovered");
    }
    config::private_json(path, &data)?;
    let mut names: Vec<_> = object(&data["apps"])?.keys().collect();
    names.sort();
    Ok(
        json!({"config":path,"apps":names,"discovery":data["discovery"],"transport":"direct HTTP; SSH bootstrap only"}),
    )
}
