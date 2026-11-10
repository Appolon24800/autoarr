use crate::{
    Error, Result,
    args::Args,
    client::{Client, Query, query, quote},
    config::{self, Results},
    err, object,
    output::{self, Predicate, records},
    text, truth, workflows,
};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    io::Write,
    path::Path,
    sync::atomic::Ordering,
    time::{Duration, Instant},
};

pub fn validate(c: &Client, t: &Value) -> Result<()> {
    object(t)?;
    let kind = text(&t["type"]);
    if kind == "condition" {
        let path = t["path"]
            .as_str()
            .ok_or_else(|| Error::new("A condition wait needs --path"))?;
        c.url(path, false)?;
        if c.kind == "navidrome" && crate::client::subsonic_write(path) {
            return err("Wait conditions must use a read-only endpoint");
        }
        if !t["until"].as_array().is_some_and(|a| !a.is_empty()) {
            return err("A condition wait needs --until expressions");
        }
        for key in ["until", "failIf"] {
            if t.get(key).is_some() {
                let values = t[key]
                    .as_array()
                    .ok_or_else(|| Error::new("Wait conditions must be arrays"))?;
                for expr in values {
                    Predicate::parse(
                        expr.as_str()
                            .ok_or_else(|| Error::new("Wait expressions must be strings"))?,
                    )?;
                }
            }
        }
        if let Some(q) = t.get("query") {
            serde_json::from_value::<Query>(q.clone())
                .map_err(|_| Error::new("Wait query must contain key/value pairs"))?;
        }
        return Ok(());
    }
    let supported = match kind.as_str() {
        "command" => config::is_arr(&c.kind),
        "download" | "release" => config::manager(&c.kind),
        "torrent" => c.kind == "qbittorrent",
        "task" => c.kind == "jellyfin",
        _ => false,
    };
    if !supported {
        return err("Wait target is not supported for this application; use a condition wait");
    }
    if kind == "release" {
        if !truth(&t["title"]) || !truth(&t["indexer"]) || t["afterHistoryId"].as_u64().is_none() {
            return err(
                "Tracking a grabbed release needs exact title, indexer, and history boundary",
            );
        }
    } else if !(t["id"].is_string() || t["id"].is_i64() || t["id"].is_u64())
        || text(&t["id"]).trim().is_empty()
    {
        return err("A wait needs an explicit target ID");
    }
    if ["download", "release"].contains(&kind.as_str())
        && t.get("goal").is_some()
        && !["downloaded", "imported"].contains(&text(&t["goal"]).as_str())
    {
        return err("Download waits finish at downloaded or imported");
    }
    if kind == "task" && !t["baseline"].is_null() && !t["baseline"].is_object() {
        return err("Task baseline must be a native execution result");
    }
    Ok(())
}
fn progress(v: &Value) -> Value {
    let mut result = json!({});
    for key in [
        "id",
        "title",
        "name",
        "status",
        "state",
        "trackedDownloadState",
        "trackedDownloadStatus",
        "size",
        "sizeleft",
        "timeleft",
        "progress",
        "dlspeed",
        "eta",
        "message",
        "statusMessages",
        "CurrentProgressPercentage",
    ] {
        if let Some(v) = v.get(key) {
            result[key] = v.clone();
        }
    }
    if let (Some(size), Some(left)) = (v["size"].as_f64(), v["sizeleft"].as_f64()) {
        if size > 0.0 {
            result["percent"] =
                json!(((1.0 - left / size).clamp(0.0, 1.0) * 1000.0).round() / 10.0);
        }
    } else if let Some(p) = v["progress"].as_f64() {
        result["percent"] = json!((p * 1000.0).round() / 10.0);
    }
    output::compact(&result, 0)
}
fn history(c: &mut Client, after: Option<i64>, qs: Query) -> Result<Vec<Value>> {
    if let Some(after) = after {
        let mut found = Vec::new();
        for page in 1..=10000 {
            let mut q = qs.clone();
            q.extend(query(&[
                ("page", json!(page)),
                ("pageSize", json!(250)),
                ("sortKey", json!("date")),
                ("sortDirection", json!("descending")),
            ]));
            let data = c.get("/history", q)?;
            let rows =
                records(&data).ok_or_else(|| Error::new("History API did not return records"))?;
            found.extend(
                rows.iter()
                    .filter(|r| r["id"].as_i64().unwrap_or(0) > after)
                    .cloned(),
            );
            if rows.is_empty()
                || rows.iter().any(|r| r["id"].as_i64().unwrap_or(0) <= after)
                || data["totalRecords"]
                    .as_i64()
                    .is_some_and(|n| page * 250 >= n)
            {
                return Ok(found);
            }
        }
        return err("Grab history pagination exceeded 10,000 pages");
    }
    let data = workflows::paginated(c, "/history", qs, true)?;
    records(&data)
        .cloned()
        .ok_or_else(|| Error::new("History API did not return records"))
}
fn event(v: &Value, names: &[&str]) -> bool {
    names.contains(&text(&v["eventType"]).to_lowercase().as_str())
}
fn probe(c: &mut Client, t: &mut Value) -> Result<(String, Value, Value)> {
    let kind = text(&t["type"]);
    let id = quote(&text(&t["id"]));
    match kind.as_str() {
        "condition" => {
            let qs = serde_json::from_value::<Query>(t.get("query").cloned().unwrap_or(json!([])))
                .map_err(|_| Error::new("Invalid wait query"))?;
            let data = c.get(&text(&t["path"]), qs)?;
            let rows = data.as_array().cloned().unwrap_or(vec![data.clone()]);
            let fails = t["failIf"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|e| Predicate::parse(&text(e)))
                .collect::<Result<Vec<_>>>()?;
            let checks = t["until"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|e| Predicate::parse(&text(e)))
                .collect::<Result<Vec<_>>>()?;
            let failed = fails.iter().any(|p| rows.iter().any(|r| p.matches(r)));
            let done = !rows.is_empty() && checks.iter().all(|p| rows.iter().all(|r| p.matches(r)));
            let mut detail = if data.is_object() {
                progress(&data)
            } else if data.is_array() {
                json!({"records":rows.len()})
            } else {
                json!({})
            };
            if !truth(&detail) {
                detail = json!({"state":"checking condition"});
            }
            return Ok((
                if failed {
                    "failed"
                } else if done {
                    "completed"
                } else {
                    "pending"
                }
                .into(),
                detail,
                data,
            ));
        }
        "command" => {
            let data = c.get(&format!("/command/{id}"), vec![])?;
            object(&data)?;
            let status = text(&data["status"]).to_lowercase();
            let state = if status == "completed" {
                "completed"
            } else if ["failed", "aborted", "cancelled", "orphaned"].contains(&status.as_str()) {
                "failed"
            } else {
                "pending"
            };
            return Ok((state.into(), progress(&data), data));
        }
        "torrent" => {
            let data = c.get("/torrents/info", query(&[("hashes", t["id"].clone())]))?;
            let rows = data
                .as_array()
                .ok_or_else(|| Error::new("Torrent API did not return an array"))?;
            let matches: Vec<_> = rows
                .iter()
                .filter(|r| text(&r["hash"]).eq_ignore_ascii_case(&text(&t["id"])))
                .collect();
            if matches.len() != 1 {
                return Ok((
                    "pending".into(),
                    json!({"state":"not found; disappearance is not completion"}),
                    data,
                ));
            }
            let row = matches[0];
            let status = text(&row["state"]).to_lowercase();
            let done = row["progress"].as_f64().unwrap_or(0.0) >= 1.0
                && [
                    "uploading",
                    "stalledup",
                    "forcedup",
                    "queuedup",
                    "pausedup",
                    "stoppedup",
                ]
                .contains(&status.as_str());
            let state = if ["error", "missingfiles"].contains(&status.as_str()) {
                "failed"
            } else if done {
                "completed"
            } else {
                "pending"
            };
            return Ok((state.into(), progress(row), row.clone()));
        }
        "task" => {
            let data = c.get(&format!("/ScheduledTasks/{id}"), vec![])?;
            object(&data)?;
            let last = data
                .get("LastExecutionResult")
                .filter(|v| truth(v))
                .cloned()
                .unwrap_or(json!({}));
            let running =
                ["running", "cancelling"].contains(&text(&data["State"]).to_lowercase().as_str());
            if running && t.get("baseline").is_none() {
                t["baseline"] = data["LastExecutionResult"].clone();
            }
            let fresh = t.get("baseline").is_none() || truth(&last) && last != t["baseline"];
            let status = text(&last["Status"]).to_lowercase();
            let state = if !running && fresh {
                match status.as_str() {
                    "completed" => "completed",
                    "failed" | "cancelled" | "aborted" => "failed",
                    _ => "pending",
                }
            } else {
                "pending"
            };
            return Ok((
                state.into(),
                json!({"state":data["State"],"percent":data["CurrentProgressPercentage"],"lastResult":output::compact(&last,0)}),
                data,
            ));
        }
        "release" => {
            let boundary = t["afterHistoryId"].as_i64().unwrap_or(0);
            let rows = history(c, Some(boundary), query(&[("eventType", json!(1))]))?;
            let matches: Vec<_> = rows
                .into_iter()
                .filter(|r| {
                    r["id"].as_i64().unwrap_or(0) > boundary
                        && r["sourceTitle"] == t["title"]
                        && r["data"]["indexer"] == t["indexer"]
                        && truth(&r["downloadId"])
                        && event(r, &["grabbed", "1"])
                })
                .collect();
            let ids: HashSet<_> = matches
                .iter()
                .map(|r| text(&r["downloadId"]).to_lowercase())
                .collect();
            if ids.len() > 1 {
                return Ok((
                    "failed".into(),
                    json!({"state":"ambiguous grab; inspect history before choosing a download ID"}),
                    json!(matches),
                ));
            }
            if ids.is_empty() {
                return Ok((
                    "pending".into(),
                    json!({"state":"waiting for grabbed release to appear in history"}),
                    json!({}),
                ));
            }
            *t = json!({"type":"download","id":matches[0]["downloadId"],"goal":t.get("goal").cloned().unwrap_or(json!("imported")),"afterHistoryId":boundary});
        }
        _ => (),
    }
    let queue = workflows::paginated(c, "/queue", vec![], true)?;
    let rows = records(&queue).ok_or_else(|| Error::new("Queue API did not return records"))?;
    let id = text(&t["id"]);
    let matches: Vec<_> = rows
        .iter()
        .filter(|r| text(&r["downloadId"]).eq_ignore_ascii_case(&id))
        .cloned()
        .collect();
    if !matches.is_empty() {
        let failed = matches.iter().any(|r| {
            ["failed", "failedpending", "importblocked", "ignored"]
                .contains(&text(&r["trackedDownloadState"]).to_lowercase().as_str())
                || ["failed", "warning"].contains(&text(&r["status"]).to_lowercase().as_str())
                || text(&r["trackedDownloadStatus"]).eq_ignore_ascii_case("error")
        });
        let done = matches
            .iter()
            .all(|r| text(&r["trackedDownloadState"]).eq_ignore_ascii_case("imported"))
            || t["goal"] == "downloaded"
                && matches
                    .iter()
                    .all(|r| text(&r["status"]).eq_ignore_ascii_case("completed"));
        let mut detail = progress(&matches[0]);
        if matches.len() > 1 {
            detail["queueRecords"] = json!(matches.len());
        }
        return Ok((
            if failed {
                "failed"
            } else if done {
                "completed"
            } else {
                "pending"
            }
            .into(),
            detail,
            json!(matches),
        ));
    }
    let rows: Vec<_> = history(c, None, query(&[("downloadId", t["id"].clone())]))?
        .into_iter()
        .filter(|r| {
            text(&r["downloadId"]).eq_ignore_ascii_case(&id)
                && r["id"].as_i64().unwrap_or(0) > t["afterHistoryId"].as_i64().unwrap_or(-1)
        })
        .collect();
    let imports: Vec<_> = rows
        .iter()
        .filter(|r| event(r, &["downloadfolderimported", "3"]))
        .collect();
    let failed = rows
        .iter()
        .any(|r| event(r, &["downloadfailed", "downloadignored", "4", "7"]));
    let expected: HashSet<_> = rows
        .iter()
        .filter(|r| event(r, &["grabbed", "1"]) && truth(&r["episodeId"]))
        .map(|r| text(&r["episodeId"]))
        .collect();
    let imported: HashSet<_> = imports
        .iter()
        .filter(|r| truth(&r["episodeId"]))
        .map(|r| text(&r["episodeId"]))
        .collect();
    let done = !imports.is_empty() && expected.is_subset(&imported);
    Ok((
        if failed {
            "failed"
        } else if done {
            "completed"
        } else {
            "pending"
        }
        .into(),
        json!({"state":if done{"imported"}else if failed{"failed"}else{"not in queue; waiting for import evidence"}}),
        json!(rows),
    ))
}
pub fn wait_for(c: &mut Client, mut target: Value, a: &Args, config: &Path) -> Result<Value> {
    validate(c, &target)?;
    let started = Instant::now();
    let budget = Duration::from_secs_f64(a.n("wait_timeout"));
    let deadline = started
        .checked_add(budget)
        .ok_or_else(|| Error::new("Wait budget is too large"))?;
    c.deadline = Some(deadline);
    let mut state = "pending".to_string();
    let (mut polls, mut detail, mut response) = (0, json!({}), Value::Null);
    let mut previous = String::new();
    let mut last = started;
    while state == "pending" {
        if crate::INTERRUPTED.load(Ordering::SeqCst) {
            state = "interrupted".into();
            break;
        }
        if Instant::now() >= deadline {
            state = "timeout".into();
            break;
        }
        polls += 1;
        match probe(c, &mut target) {
            Ok((s, d, r)) => {
                state = s;
                detail = d;
                response = r;
            }
            Err(e) => {
                if e.message == "Wait deadline reached" {
                    state = "timeout".into();
                    break;
                }
                state = if e.status.is_some_and(|s| s == 429 || s >= 500)
                    || e.message.starts_with("Request failed for ")
                {
                    "pending"
                } else {
                    "failed"
                }
                .into();
                detail = json!({"state":"API error","message":e.message});
                if let Some(status) = e.status {
                    detail["httpStatus"] = json!(status);
                }
            }
        }
        if !a.b("quiet_progress") {
            c.redactor.collect(&detail);
            let clean = c.redactor.clean(&detail);
            let fingerprint = output::canonical(&clean);
            let now = Instant::now();
            if fingerprint != previous
                || now.duration_since(last).as_secs() >= 30
                || state != "pending"
            {
                eprintln!(
                    "{}",
                    json!({"event":"wait-progress","app":c.name(),"status":state,"elapsedSeconds":(now.duration_since(started).as_secs_f64()*10.0).round()/10.0,"progress":clean})
                );
                let _ = std::io::stderr().flush();
                previous = fingerprint;
                last = now;
            }
        }
        if state == "pending" {
            let next = Instant::now()
                .checked_add(Duration::from_secs_f64(a.n("poll_interval")))
                .unwrap_or(deadline)
                .min(deadline);
            while Instant::now() < next && !crate::INTERRUPTED.load(Ordering::SeqCst) {
                std::thread::sleep(
                    next.saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(100)),
                );
            }
        }
    }
    c.deadline = None;
    let mut resume = vec![
        "autoarr".to_string(),
        "--config".into(),
        config.to_string_lossy().to_string(),
        c.name(),
        "wait".into(),
        "--resume".into(),
        target.to_string(),
        "--wait-timeout".into(),
        a.n("wait_timeout").to_string(),
        "--poll-interval".into(),
        a.n("poll_interval").to_string(),
    ];
    if a.b("quiet_progress") {
        resume.push("--quiet-progress".into());
    }
    Ok(
        json!({"wait":true,"status":state,"app":c.name(),"target":target,"elapsedSeconds":(started.elapsed().as_secs_f64()*10.0).round()/10.0,"polls":polls,"progress":detail,"resume":resume,"response":response}),
    )
}
pub fn exit_code(v: &Value) -> i32 {
    if v["wait"] != true {
        return 0;
    }
    match text(&v["status"]).as_str() {
        "completed" => 0,
        "timeout" => 3,
        "interrupted" => 130,
        _ => 2,
    }
}
pub fn action(c: &mut Client, a: &mut Args, config: &Path) -> Result<Value> {
    if a.action != "wait" && !a.b("wait") {
        return workflows::action(c, a);
    }
    let mut target = None;
    let condition = |path: String, a: &Args| -> Result<Value> {
        Ok(
            json!({"type":"condition","path":path,"until":a.list("until"),"failIf":a.list("fail_if"),"query":if a.action=="wait"{output::pairs(&a.list("query"))?}else{vec![]}}),
        )
    };
    if a.action == "wait" {
        if a.b("resume") {
            target = Some(serde_json::from_str(&a.s("resume"))?);
        } else if a.b("path") {
            target = Some(condition(a.s("path"), a)?);
        } else {
            for (flag, kind) in [
                ("command_id", "command"),
                ("download_id", "download"),
                ("torrent_hash", "torrent"),
                ("task_id", "task"),
            ] {
                if a.b(flag) {
                    let mut t = json!({"type":kind,"id":a.v[flag]});
                    if kind == "download" {
                        t["goal"] = a.v["wait_until"].clone();
                    }
                    target = Some(t);
                    break;
                }
            }
        }
    } else if a.b("wait_path") {
        target = Some(condition(a.s("wait_path"), a)?);
    } else if a.action == "grab" {
        if !config::manager(&c.kind) {
            return err("grab --wait requires a media manager");
        }
        let body = if a.b("ref") {
            Results::new(&c.state).reference(&a.s("ref"), &c.name())?
        } else {
            workflows::payload(a)?.unwrap_or(json!({}))
        };
        let indexer = if !a.v["indexer_id"].is_null() {
            a.v["indexer_id"].clone()
        } else {
            body["indexerId"].clone()
        };
        if !truth(&body["title"]) || indexer.is_null() {
            return err(
                "grab --wait needs a cached release --ref or --data with title and indexerId",
            );
        }
        let indexer = c.get(&format!("/indexer/{}", quote(&text(&indexer))), vec![])?;
        let latest = c.get(
            "/history",
            query(&[
                ("eventType", json!(1)),
                ("page", json!(1)),
                ("pageSize", json!(1)),
                ("sortKey", json!("date")),
                ("sortDirection", json!("descending")),
            ]),
        )?;
        let rows = records(&latest).ok_or_else(|| {
            Error::new("Cannot establish a grab history boundary; no release was submitted")
        })?;
        target = Some(
            json!({"type":"release","title":body["title"],"indexer":indexer["name"],"afterHistoryId":rows.iter().filter_map(|r|r["id"].as_i64()).max().unwrap_or(0),"goal":a.v["wait_until"]}),
        );
        a.set("grab_body", body);
    } else if a.action == "task-run" {
        let before = c.get(&format!("/ScheduledTasks/{}", quote(&a.s("id"))), vec![])?;
        if !text(&before["State"]).eq_ignore_ascii_case("idle") {
            return err(
                "Task is already active; use wait --task-id to follow it without starting it again",
            );
        }
        target =
            Some(json!({"type":"task","id":a.v["id"],"baseline":before["LastExecutionResult"]}));
    }
    if let Some(t) = &target {
        validate(c, t)?;
        if a.b("no_cache") && !a.b("full") && !a.b("select") {
            let min = json!({"wait":true,"status":"interrupted","app":c.name(),"target":t});
            let budget = if a.b("max_output") {
                a.n("max_output") as usize
            } else {
                6000
            };
            if min.to_string().chars().count() > budget {
                return err(
                    "Wait target exceeds output budget; increase --max-output or enable caching before submitting the action",
                );
            }
        }
    }
    if a.action != "wait" {
        let result = workflows::action(c, a)?;
        if c.dry_run {
            return Ok(result);
        }
        if target.is_none() {
            if result["id"].is_null() {
                return err(
                    "Command accepted without an ID; inspect native command history before retrying",
                );
            }
            target = Some(json!({"type":"command","id":result["id"]}));
        } else if target.as_ref().unwrap()["type"] == "release" && truth(&result["downloadId"]) {
            target = Some(
                json!({"type":"download","id":result["downloadId"],"goal":a.v["wait_until"],"afterHistoryId":target.unwrap()["afterHistoryId"]}),
            );
        }
    }
    let target = target.ok_or_else(|| Error::new("Missing wait target"))?;
    a.output_action = "wait".into();
    if c.dry_run {
        return Ok(json!({"dryRun":true,"wait":false,"target":target}));
    }
    wait_for(c, target, a, config)
}
