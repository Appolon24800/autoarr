use crate::{
    Error, Result,
    args::Args,
    engine::Engine,
    err, object,
    output::{self, Predicate, records},
    text, truth, workflows,
};
use rayon::prelude::*;
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::{OnceLock, atomic::Ordering},
};
fn pattern() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}").unwrap())
}
fn lookup(values: &HashMap<String, Value>, field: &str, defer: bool) -> Result<Value> {
    if defer && field.starts_with("steps.") {
        return Ok(json!(format!("{{{field}}}")));
    }
    values
        .get(field)
        .cloned()
        .ok_or_else(|| Error::new(format!("Unknown batch placeholder: {field}")))
}
pub fn substitute(
    argv: &[String],
    values: &HashMap<String, Value>,
    defer: bool,
) -> Result<Vec<String>> {
    fn replace(s: &str, values: &HashMap<String, Value>, defer: bool) -> Result<String> {
        let mut out = String::new();
        let mut last = 0;
        for cap in pattern().captures_iter(s) {
            let m = cap.get(0).unwrap();
            out.push_str(&s[last..m.start()]);
            out.push_str(&text(&lookup(values, &cap[1], defer)?));
            last = m.end();
        }
        out.push_str(&s[last..]);
        Ok(out)
    }
    fn walk(v: &Value, values: &HashMap<String, Value>, defer: bool) -> Result<Value> {
        match v {
            Value::Object(m) => Ok(Value::Object(
                m.iter()
                    .map(|(k, v)| Ok((k.clone(), walk(v, values, defer)?)))
                    .collect::<Result<_>>()?,
            )),
            Value::Array(a) => Ok(Value::Array(
                a.iter()
                    .map(|v| walk(v, values, defer))
                    .collect::<Result<_>>()?,
            )),
            Value::String(s) => {
                if let Some(cap) = pattern().captures(s)
                    && cap.get(0).unwrap().as_str() == s
                {
                    return lookup(values, &cap[1], defer);
                }
                Ok(json!(replace(s, values, defer)?))
            }
            _ => Ok(v.clone()),
        }
    }
    argv.iter()
        .map(|arg| {
            if arg.starts_with(['{', '['])
                && let Ok(v) = serde_json::from_str::<Value>(arg)
            {
                return Ok(walk(&v, values, defer)?.to_string());
            }
            replace(arg, values, defer)
        })
        .collect()
}
fn strings(v: &Value) -> Result<Vec<String>> {
    let a = v
        .as_array()
        .ok_or_else(|| Error::new("Each batch argv must be an array of strings"))?;
    if a.is_empty() {
        return err("Each batch argv must be nonempty");
    }
    a.iter()
        .map(|v| {
            v.as_str()
                .map(String::from)
                .ok_or_else(|| Error::new("Each batch argv must contain strings"))
        })
        .collect()
}
fn ids(s: &str) -> Result<Vec<Value>> {
    let mut values = Vec::new();
    for part in s.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        if let Some((lo, hi)) = part.split_once('-')
            && let (Ok(lo), Ok(hi)) = (lo.parse::<i64>(), hi.parse::<i64>())
        {
            if lo < 0 || hi < lo || hi - lo > 999 {
                return err("ID ranges must be ascending and contain at most 1,000 entries");
            }
            values.extend((lo..=hi).map(|n| json!(n)));
            continue;
        }
        values.push(json!(part));
    }
    if values.is_empty() || values.len() > 1000 {
        return err("A batch needs 1–1,000 explicit targets");
    }
    Ok(values)
}
fn expand(a: &Args) -> Result<Vec<Value>> {
    let mut jobs = Vec::new();
    if a.b("file") {
        if !a.remainder.is_empty() || a.b("apps") || a.b("ids") {
            return err("Use either --file or --apps/--ids with a template");
        }
        let data = workflows::load_json(&a.s("file"))?;
        let values = data.as_array().ok_or_else(|| {
            Error::new("Batch JSON must be an array of argv arrays or task objects")
        })?;
        for item in values {
            if item.is_array() {
                strings(item)?;
                jobs.push(json!({"argv":item}));
            } else {
                object(item)?;
                let argv = strings(&item["argv"])?;
                let each = item.get("foreach").cloned().unwrap_or(json!([null]));
                let each = each
                    .as_array()
                    .ok_or_else(|| Error::new("foreach must be an array"))?;
                for value in each {
                    let mut context = HashMap::from([("item".to_string(), value.clone())]);
                    if let Some(m) = value.as_object() {
                        context.extend(m.iter().map(|(k, v)| (format!("item.{k}"), v.clone())));
                    }
                    let mut job = json!({"argv":substitute(&argv,&context,true)?,"expect":item.get("expect").cloned().unwrap_or(json!([]))});
                    if let Some(name) = item.get("name") {
                        let name = name
                            .as_str()
                            .ok_or_else(|| Error::new("Batch step names must be strings"))?;
                        job["name"] = json!(substitute(&[name.into()], &context, false)?[0]);
                    }
                    jobs.push(job);
                }
            }
            if jobs.len() > 1000 {
                return err("A batch must have 1–1,000 jobs");
            }
        }
    } else {
        if a.remainder.is_empty() || !a.b("apps") && !a.b("ids") {
            return err("Provide --file or --apps/--ids followed by -- COMMAND");
        }
        let apps = if a.b("apps") {
            a.s("apps")
                .split(',')
                .map(|v| json!(v.trim()))
                .collect::<Vec<_>>()
        } else {
            vec![Value::Null]
        };
        if a.b("apps") && apps.iter().any(|v| !truth(v)) {
            return err("App names cannot be empty");
        }
        let targets = if a.b("ids") {
            ids(&a.s("ids"))?
        } else {
            vec![Value::Null]
        };
        for app in apps {
            for id in &targets {
                let context =
                    HashMap::from([("app".into(), app.clone()), ("id".into(), id.clone())]);
                let mut argv = substitute(&a.remainder, &context, false)?;
                if !app.is_null() {
                    argv.insert(0, text(&app));
                }
                jobs.push(json!({"argv":argv}));
            }
        }
    }
    if jobs.is_empty() || jobs.len() > 1000 {
        return err("A batch must contain 1–1,000 jobs");
    }
    Ok(jobs)
}
pub fn read_only(a: &Args) -> bool {
    if ["apps", "doctor", "result"].contains(&a.scope.as_str()) {
        return true;
    }
    match a.action.as_str() {
        "wait" => ["command_id", "download_id", "torrent_hash", "task_id"]
            .iter()
            .any(|k| a.b(k)),
        "status" | "health" | "ops" | "schema" | "list" | "get" | "queue" | "history" | "roots"
        | "profiles" | "tags" | "indexers" | "clients" | "files" | "wanted" | "lookup"
        | "releases" | "items" | "images" | "libraries" | "tasks" | "torrents" | "torrent"
        | "transfer" | "search" | "missing" | "find-missing" => true,
        "metadata" | "library-options" => !a.b("data") && a.list("set").is_empty(),
        "manual-import" | "rename" => !a.b("apply"),
        _ => false,
    }
}
struct Job {
    argv: Vec<String>,
    args: Args,
    name: Option<String>,
    references: HashMap<String, String>,
    expect: Vec<String>,
    checks: Vec<Predicate>,
}
fn step_value(completed: &HashMap<String, Value>, field: &str) -> Result<Value> {
    let parts: Vec<_> = field.split('.').collect();
    let mut v = completed
        .get(parts[1])
        .ok_or_else(|| Error::new("Missing source step"))?;
    for part in &parts[2..] {
        v = if v.is_object() {
            v.get(*part)
        } else {
            part.parse::<usize>()
                .ok()
                .and_then(|i| v.as_array().and_then(|a| a.get(i)))
        }
        .ok_or_else(|| Error::new(format!("Step reference has no value: {field}")))?;
    }
    Ok(v.clone())
}
fn one(
    engine: &Engine,
    jobs: &[Job],
    index: usize,
    completed: &HashMap<String, Value>,
    needed: &HashSet<String>,
) -> (Value, Value) {
    let job = &jobs[index];
    let mut identity = json!({"job":index+1,"app":job.args.scope,"action":if job.args.action.is_empty(){&job.args.scope}else{&job.args.action}});
    if let Some(name) = &job.name {
        identity["name"] = json!(name);
    }
    let result = (|| -> Result<(Value, Value)> {
        if crate::INTERRUPTED.load(Ordering::SeqCst) {
            return err("Interrupted; pending batch steps were not started");
        }
        let mut args = job.args.clone();
        if !job.references.is_empty() {
            if job
                .references
                .values()
                .any(|source| completed.get(source).is_none_or(|s| s["ok"] != true))
            {
                identity["skipped"] = json!(true);
                identity["reason"] = json!("A referenced step did not succeed");
                return Ok((identity.clone(), json!({"ok":false})));
            }
            let values = job
                .references
                .keys()
                .map(|field| Ok((field.clone(), step_value(completed, field)?)))
                .collect::<Result<HashMap<_, _>>>()?;
            let resolved = substitute(&job.argv, &values, false)?;
            for (original, new) in job.argv.iter().zip(&resolved) {
                if job.references.keys().any(|field| original.contains(field)) {
                    let ov = if original.starts_with("--") {
                        original.split_once('=').map(|(_, v)| v).unwrap_or(original)
                    } else {
                        original
                    };
                    let nv = if original.starts_with("--") {
                        new.split_once('=').map(|(_, v)| v).unwrap_or(new)
                    } else {
                        new
                    };
                    if (nv.starts_with('-') && !ov.starts_with('-'))
                        || (nv.starts_with('@') && !ov.starts_with('@'))
                    {
                        return err(
                            "A step reference cannot introduce CLI flags or file/stdin input",
                        );
                    }
                }
            }
            args = engine.parse(&resolved).map_err(|_| {
                Error::new("Resolved batch arguments are invalid; this job was not started")
            })?;
        }
        let mut outcome = engine.run(args)?;
        if outcome.args.scope == "doctor" && outcome.exit_code() != 0 {
            return err("A doctor check failed");
        }
        let code = crate::waiting::exit_code(&outcome.data);
        outcome.args.set(
            "limit",
            json!(if outcome.args.v["limit"].is_null() {
                3
            } else {
                outcome.args.n("limit").min(3.0) as usize
            }),
        );
        outcome.args.set("full", json!(false));
        outcome.args.set("max_output", json!(2000));
        let summary = output::prepare(
            &outcome.redactor.clean(&outcome.data),
            &outcome.args,
            outcome.result.as_deref(),
        )?;
        let frame =
            json!({"data":outcome.data,"count":records(&outcome.data).map(Vec::len).unwrap_or(1)});
        let unmet: Vec<_> = job
            .expect
            .iter()
            .zip(&job.checks)
            .filter(|(_, p)| !p.matches(&frame))
            .map(|(s, _)| s.clone())
            .collect();
        let ok = code == 0 && unmet.is_empty();
        identity["ok"] = json!(ok);
        identity["data"] = summary;
        if code != 0 {
            identity["exitCode"] = json!(code);
        }
        if !unmet.is_empty() {
            identity["error"] = outcome
                .redactor
                .clean(&json!(format!("Expectation failed: {}", unmet.join(", "))));
        }
        let mut private = json!({"ok":ok});
        if job.name.as_ref().is_some_and(|n| needed.contains(n)) {
            private["data"] = outcome.data;
            private["result"] = json!(outcome.result);
        }
        Ok((identity.clone(), private))
    })();
    match result {
        Ok(v) => v,
        Err(e) => {
            identity["ok"] = json!(false);
            identity["error"] = json!(e.message);
            (identity, json!({"ok":false}))
        }
    }
}
pub fn execute(engine: &Engine, a: &Args) -> Result<Value> {
    let expanded = expand(a)?;
    let mut jobs: Vec<Job> = Vec::new();
    let mut named: HashMap<String, usize> = HashMap::new();
    let valid_name = Regex::new("^[A-Za-z_][A-Za-z0-9_]*$").unwrap();
    for job in expanded {
        let argv = strings(&job["argv"])?;
        let name = job["name"].as_str().map(String::from);
        if name
            .as_ref()
            .is_some_and(|n| !valid_name.is_match(n) || named.contains_key(n))
        {
            return err(
                "Batch step names must be unique identifiers (letters, digits, underscores)",
            );
        }
        let mut references = HashMap::new();
        for arg in &argv {
            for cap in pattern().captures_iter(arg) {
                let field = &cap[1];
                if !field.starts_with("steps.") {
                    continue;
                }
                let parts: Vec<_> = field.split('.').collect();
                if parts.len() < 3
                    || !named.contains_key(parts[1])
                    || !["data", "result"].contains(&parts[2])
                    || parts[2] == "result" && parts.len() != 3
                {
                    return err(
                        "Step references need an earlier named step: {steps.NAME.data.FIELD} or {steps.NAME.result}",
                    );
                }
                references.insert(field.to_string(), parts[1].to_string());
            }
        }
        let expect = job.get("expect").cloned().unwrap_or(json!([]));
        let expect: Vec<String> = expect
            .as_array()
            .ok_or_else(|| Error::new("Batch expect must be an array of filter expressions"))?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(String::from)
                    .ok_or_else(|| Error::new("Batch expectations must be strings"))
            })
            .collect::<Result<_>>()?;
        let checks = expect
            .iter()
            .map(|s| Predicate::parse(s))
            .collect::<Result<_>>()?;
        if argv
            .iter()
            .any(|s| s == "--config" || s.starts_with("--config=") || s == "--api-key-stdin")
        {
            return err(
                "Batch children cannot replace configuration or read credentials from stdin",
            );
        }
        let mut command = vec![
            "--config".into(),
            engine.path.to_string_lossy().to_string(),
            "--timeout".into(),
            a.n("timeout").to_string(),
        ];
        if a.b("dry_run") {
            command.push("--dry-run".into());
        }
        if a.b("no_cache") {
            command.push("--no-cache".into());
        }
        command.extend(argv);
        let preview = if references.is_empty() {
            command.clone()
        } else {
            substitute(
                &command,
                &references.keys().map(|k| (k.clone(), json!(1))).collect(),
                false,
            )?
        };
        let parsed = engine
            .parse(&preview)
            .map_err(|_| Error::new("Invalid batch child arguments; no jobs were started"))?;
        if ["batch", "config", "init"].contains(&parsed.scope.as_str()) {
            return err(
                "Batch nesting, credential configuration, and SSH initialization are not batch tasks",
            );
        }
        if !["apps", "doctor", "result"].contains(&parsed.scope.as_str()) {
            crate::config::app_config(&engine.config, &parsed.scope)?;
        }
        if parsed.s("data") == "-" {
            return err("Batch bodies must use JSON literals or @files, not shared stdin");
        }
        if let Some(name) = &name {
            named.insert(name.clone(), jobs.len());
        }
        jobs.push(Job {
            argv: command,
            args: parsed,
            name,
            references,
            expect,
            checks,
        });
    }
    for job in &jobs {
        for (field, source) in &job.references {
            let source = &jobs[named[source]].args;
            if field.split('.').nth(2) == Some("result")
                && (source.b("no_cache") || ["apps", "doctor"].contains(&source.scope.as_str()))
            {
                return err(
                    "A result reference needs a cached source; use .data for uncached steps",
                );
            }
        }
    }
    let needed: HashSet<_> = jobs
        .iter()
        .flat_map(|j| j.references.values().cloned())
        .collect();
    let mut completed = HashMap::new();
    let mut results = Vec::new();
    let (mut failed, mut used_parallel, mut index) = (false, false, 0);
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(a.n("workers") as usize)
        .build()
        .map_err(|e| Error::new(e.to_string()))?;
    while index < jobs.len() {
        if failed && !a.b("keep_going") {
            let mut item = json!({"job":index+1,"skipped":true});
            if let Some(name) = &jobs[index].name {
                item["name"] = json!(name);
                completed.insert(name.clone(), json!({"ok":false}));
            }
            results.push(item);
            index += 1;
            continue;
        }
        let mut end = index + 1;
        if a.n("workers") > 1.0 && read_only(&jobs[index].args) && jobs[index].references.is_empty()
        {
            while end < jobs.len() && read_only(&jobs[end].args) && jobs[end].references.is_empty()
            {
                end += 1;
            }
        }
        let outcomes = if end - index > 1 {
            used_parallel = true;
            pool.install(|| {
                (index..end)
                    .into_par_iter()
                    .map(|i| one(engine, &jobs, i, &completed, &needed))
                    .collect::<Vec<_>>()
            })
        } else {
            vec![one(engine, &jobs, index, &completed, &needed)]
        };
        for (offset, (item, private)) in outcomes.into_iter().enumerate() {
            if let Some(name) = &jobs[index + offset].name {
                completed.insert(name.clone(), private);
            }
            failed |= item["ok"] == false;
            results.push(item);
        }
        index = end;
    }
    let execution = if used_parallel {
        if jobs.iter().all(|j| read_only(&j.args)) {
            "parallel reads"
        } else {
            "parallel reads with sequential writes"
        }
    } else {
        "sequential"
    };
    Ok(
        json!({"total":jobs.len(),"succeeded":results.iter().filter(|r|r["ok"]==true).count(),"failed":results.iter().filter(|r|r["ok"]==false).count(),"skipped":results.iter().filter(|r|r["skipped"]==true).count(),"execution":execution,"dryRun":a.b("dry_run"),"jobs":results}),
    )
}
