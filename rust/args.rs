use crate::{Error, Result, err, text, truth};
use serde_json::{Value, json};
use std::sync::OnceLock;

pub fn spec() -> &'static Value {
    static SPEC: OnceLock<Value> = OnceLock::new();
    SPEC.get_or_init(|| {
        serde_json::from_str(include_str!("cli-spec.json")).expect("embedded CLI specification")
    })
}
#[derive(Clone, Debug)]
pub struct Args {
    pub scope: String,
    pub action: String,
    pub v: Value,
    pub remainder: Vec<String>,
    pub output_action: String,
}
impl Args {
    pub fn s(&self, key: &str) -> String {
        text(&self.v[key])
    }
    pub fn b(&self, key: &str) -> bool {
        truth(&self.v[key])
    }
    pub fn n(&self, key: &str) -> f64 {
        self.v[key].as_f64().unwrap_or(0.0)
    }
    pub fn list(&self, key: &str) -> Vec<String> {
        self.v[key]
            .as_array()
            .map(|a| a.iter().map(text).collect())
            .unwrap_or_default()
    }
    pub fn set(&mut self, key: &str, val: Value) {
        self.v[key] = val;
    }
}
pub fn config_argument(argv: &[String]) -> Option<String> {
    for (i, arg) in argv.iter().enumerate() {
        if arg == "--" {
            break;
        }
        if arg == "--config" {
            return argv.get(i + 1).cloned();
        }
        if let Some(v) = arg.strip_prefix("--config=") {
            return Some(v.into());
        }
    }
    None
}
fn options(items: &Value) -> Vec<&Value> {
    items.as_array().into_iter().flatten().collect()
}
fn find<'a>(items: &[&'a Value], flag: &str) -> Option<&'a Value> {
    items.iter().copied().find(|s| {
        s["flags"]
            .as_array()
            .is_some_and(|a| a.iter().any(|f| f == flag))
    })
}
fn defaults(v: &mut Value, items: &[&Value]) {
    for item in items {
        v[text(&item["name"])] = item["default"].clone();
    }
}
fn value(raw: String, item: &Value) -> Result<Value> {
    let v = match text(&item["type"]).as_str() {
        "int" => json!(
            raw.parse::<i64>()
                .map_err(|_| Error::new(format!("{} needs an integer", item["name"])))?
        ),
        "float" => {
            let n = raw
                .parse::<f64>()
                .map_err(|_| Error::new("Expected a finite number"))?;
            if !n.is_finite() {
                return err("Expected a finite number");
            }
            json!(n)
        }
        _ => json!(raw),
    };
    if let Some(choices) = item["choices"].as_array()
        && !choices.contains(&v)
    {
        return err(format!("Invalid choice for {}", item["name"]));
    }
    Ok(v)
}
fn consume(argv: &[String], i: &mut usize, item: &Value, v: &mut Value) -> Result<()> {
    let arg = &argv[*i];
    let name = text(&item["name"]);
    let val = if item["bool"] == true {
        if arg.contains('=') {
            return err(format!("{arg} does not take a value"));
        }
        json!(true)
    } else {
        let raw = if let Some((_, val)) = arg.split_once('=') {
            val.to_owned()
        } else {
            *i += 1;
            argv.get(*i)
                .cloned()
                .ok_or_else(|| Error::new(format!("Missing value for {arg}")))?
        };
        value(raw, item)?
    };
    if item["repeat"] == true {
        if !v[&name].is_array() {
            v[&name] = json!([]);
        }
        v[&name].as_array_mut().unwrap().push(val);
    } else {
        v[&name] = val;
    }
    *i += 1;
    Ok(())
}
pub fn command_spec<'a>(scope: &str, action: &str, config: &Value) -> Result<&'a Value> {
    let s = spec();
    if matches!(scope, "apps" | "doctor") {
        return Ok(&Value::Null);
    }
    if matches!(scope, "batch" | "result" | "init") {
        return Ok(&s["root"][scope]);
    }
    if scope == "config" {
        return s["root"]["config"]
            .get(action)
            .ok_or_else(|| Error::new("Use config show or config add"));
    }
    let kind = config["apps"][scope]["kind"].as_str().unwrap_or(scope);
    if !s["apps"][kind]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| v == action))
    {
        return err(format!(
            "Unknown {scope} action {action}; use {scope} --help"
        ));
    }
    Ok(&s["commands"][action])
}
pub fn parse(argv: &[String], config: &Value) -> Result<Args> {
    let globals = options(&spec()["global"]);
    let mut v = json!({});
    defaults(&mut v, &globals);
    let mut rest = Vec::new();
    let mut i = 0;
    while i < argv.len() {
        if argv[i] == "--" {
            rest.extend_from_slice(&argv[i..]);
            break;
        }
        let flag = argv[i].split('=').next().unwrap();
        if let Some(item) = find(&globals, flag) {
            consume(argv, &mut i, item, &mut v)?;
        } else {
            rest.push(argv[i].clone());
            i += 1;
        }
    }
    let scope = rest
        .first()
        .ok_or_else(|| Error::new("An application or command is required"))?
        .clone();
    let root = matches!(
        scope.as_str(),
        "apps" | "doctor" | "batch" | "result" | "init"
    );
    let action = if root {
        String::new()
    } else {
        rest.get(1)
            .cloned()
            .ok_or_else(|| Error::new(format!("{scope} requires an action")))?
    };
    let items = options(command_spec(&scope, &action, config)?);
    defaults(&mut v, &items);
    let mut positions = Vec::new();
    let mut remainder = Vec::new();
    i = if root { 1 } else { 2 };
    while i < rest.len() {
        if rest[i] == "--" {
            remainder.extend_from_slice(&rest[i + 1..]);
            break;
        }
        if rest[i].starts_with('-') && rest[i] != "-" {
            let flag = rest[i].split('=').next().unwrap();
            let item = find(&items, flag)
                .ok_or_else(|| Error::new(format!("Unknown option {flag}; use --help")))?;
            consume(&rest, &mut i, item, &mut v)?;
        } else if scope == "batch" {
            remainder.extend_from_slice(&rest[i..]);
            break;
        } else {
            positions.push(rest[i].clone());
            i += 1;
        }
    }
    let positional: Vec<_> = items
        .iter()
        .copied()
        .filter(|s| s["flags"].as_array().is_some_and(Vec::is_empty))
        .collect();
    if positions.len() > positional.len() {
        return err("Too many positional arguments");
    }
    for (j, item) in positional.iter().enumerate() {
        let name = text(&item["name"]);
        if let Some(raw) = positions.get(j) {
            v[&name] = value(raw.clone(), item)?;
        } else if item["nargs"] != "?" && scope != "batch" {
            return err(format!("Missing argument {name}"));
        }
    }
    for item in &items {
        if item["required"] == true
            && item["flags"].as_array().is_some_and(|a| !a.is_empty())
            && v[text(&item["name"])].is_null()
        {
            return err(format!("Missing required option {}", item["name"]));
        }
    }
    let args = Args {
        scope,
        action: action.clone(),
        v,
        remainder,
        output_action: action,
    };
    validate(&args)?;
    Ok(args)
}
fn validate(a: &Args) -> Result<()> {
    for name in ["timeout", "wait_timeout", "poll_interval"] {
        let duration = std::time::Duration::try_from_secs_f64(a.n(name))
            .map_err(|_| Error::new(format!("Invalid or excessive duration for {name}")))?;
        if std::time::Instant::now().checked_add(duration).is_none() {
            return err(format!("Excessive duration for {name}"));
        }
    }
    if a.n("timeout") <= 0.0 || !a.n("timeout").is_finite() || a.n("limit") < 0.0 {
        return err("--timeout must be positive and --limit cannot be negative");
    }
    if a.n("wait_timeout") < 0.0
        || !a.n("wait_timeout").is_finite()
        || a.n("poll_interval") <= 0.0
        || !a.n("poll_interval").is_finite()
    {
        return err("Invalid --wait-timeout or --poll-interval");
    }
    if !a.v["max_output"].is_null() && a.n("max_output") < 256.0 {
        return err("--max-output must be at least 256 characters");
    }
    if a.scope == "batch" && !(1.0..=8.0).contains(&a.n("workers")) {
        return err("--workers must be between 1 and 8");
    }
    if a.scope == "result" && (a.b("cached_id") == a.b("result_file")) {
        return err("Use exactly one of result --id or --file");
    }
    if a.action == "wait"
        && [
            "command_id",
            "download_id",
            "torrent_hash",
            "task_id",
            "path",
            "resume",
        ]
        .iter()
        .filter(|k| a.b(k))
        .count()
            != 1
    {
        return err("Specify exactly one wait target");
    }
    if a.action == "move" && a.b("root") == a.b("path") {
        return err("Specify exactly one of --root or --path");
    }
    if a.action == "subtitle-search" && a.b("episode_id") == a.b("movie_id") {
        return err("Specify exactly one episode or movie ID");
    }
    if a.action == "find-missing" && !(0.0..=10.0).contains(&a.n("top")) {
        return err("--top must be between 0 and 10");
    }
    let condition = a.b("wait_path") || (a.action == "wait" && a.b("path"));
    for e in a.list("until").iter().chain(a.list("fail_if").iter()) {
        crate::output::Predicate::parse(e)?;
    }
    if condition && a.list("until").is_empty() {
        return err("A condition wait needs --until");
    }
    if !condition && (!a.list("until").is_empty() || !a.list("fail_if").is_empty()) {
        return err("--until/--fail-if require a condition path");
    }
    if a.b("wait_path") && !a.b("wait") {
        return err("--wait-path requires --wait");
    }
    if a.b("wait")
        && a.action != "wait"
        && !condition
        && !(matches!(a.action.as_str(), "command" | "grab" | "task-run")
            || matches!(a.action.as_str(), "rename" | "manual-import") && a.b("apply"))
    {
        return err("This action needs --wait-path and --until to verify completion");
    }
    Ok(())
}
pub fn help(argv: &[String], config: &Value) -> String {
    let globals = options(&spec()["global"]);
    let mut rest = Vec::new();
    let mut i = 0;
    while i < argv.len() {
        if argv[i] == "--" {
            break;
        }
        let flag = argv[i].split('=').next().unwrap();
        if let Some(item) = find(&globals, flag) {
            i += if item["bool"] == true || argv[i].contains('=') {
                1
            } else {
                2
            };
        } else {
            if !["--help", "-h"].contains(&flag) {
                rest.push(argv[i].as_str());
            }
            i += 1;
        }
    }
    let scope = rest.first().copied().unwrap_or("");
    let action = rest
        .get(1)
        .copied()
        .filter(|s| !s.starts_with('-'))
        .unwrap_or("");
    let mut out = format!(
        "AutoARR {} — native application APIs\nUsage: autoarr [OPTIONS] APP ACTION [ARGS]\n",
        env!("CARGO_PKG_VERSION")
    );
    let kind = config["apps"][scope]["kind"].as_str().unwrap_or(scope);
    if let Some(actions) = spec()["apps"][kind].as_array() {
        if action.is_empty() {
            out.push_str(&format!(
                "Actions: {}\n",
                actions.iter().map(text).collect::<Vec<_>>().join(", ")
            ));
        }
    } else if scope.is_empty() {
        out.push_str(
            "Commands: apps, doctor, batch, result, config, init; or an application name\n",
        );
    }
    if let Ok(cmd) = command_spec(scope, action, config) {
        for item in options(cmd) {
            out.push_str(&format!(
                "  {} {}\n",
                item["flags"]
                    .as_array()
                    .unwrap_or(&vec![])
                    .iter()
                    .map(text)
                    .collect::<Vec<_>>()
                    .join(", "),
                text(&item["help"])
            ));
        }
    }
    out.push_str("Global options:\n");
    for item in options(&spec()["global"]) {
        out.push_str(&format!(
            "  {} {}\n",
            item["flags"]
                .as_array()
                .unwrap()
                .iter()
                .map(text)
                .collect::<Vec<_>>()
                .join(", "),
            text(&item["help"])
        ));
    }
    if scope == "batch" {
        out.push_str("JSON steps: name, argv, foreach, expect. Bind {steps.NAME.data.FIELD} or {steps.NAME.result}.\n");
    }
    out
}
