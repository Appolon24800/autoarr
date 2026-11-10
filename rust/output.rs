use crate::{Error, Result, args::Args, err, text, truth};
use regex::{Regex, RegexBuilder};
use serde_json::{Value, json};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, HashSet},
    sync::OnceLock,
};

pub fn parse_value(s: &str) -> Value {
    serde_json::from_str(s).unwrap_or_else(|_| json!(s))
}
pub fn pairs(values: &[String]) -> Result<Vec<(String, Value)>> {
    values
        .iter()
        .map(|s| {
            let (k, v) = s
                .split_once('=')
                .ok_or_else(|| Error::new("Expected KEY=VALUE"))?;
            if k.is_empty() {
                return err("Empty field name");
            }
            Ok((k.into(), parse_value(v)))
        })
        .collect()
}
pub fn get_path(value: &Value, path: &str) -> Value {
    static R: OnceLock<Regex> = OnceLock::new();
    let r = R.get_or_init(|| Regex::new(r"\[([0-9]*)\]").unwrap());
    let path = r.replace_all(path, |c: &regex::Captures<'_>| {
        format!(".{}", if c[1].is_empty() { "*" } else { &c[1] })
    });
    fn walk(v: &Value, parts: &[&str]) -> Value {
        if parts.is_empty() {
            return v.clone();
        }
        let (part, rest) = (parts[0], &parts[1..]);
        if part == "*" {
            let items: Vec<&Value> = match v {
                Value::Array(a) => a.iter().collect(),
                Value::Object(m) => m.values().collect(),
                _ => vec![],
            };
            return Value::Array(
                items
                    .iter()
                    .flat_map(|v| match walk(v, rest) {
                        Value::Array(a) => a,
                        Value::Null => vec![],
                        v => vec![v],
                    })
                    .collect(),
            );
        }
        if let Some(m) = v.as_object() {
            return walk(m.get(part).unwrap_or(&Value::Null), rest);
        }
        if let (Some(a), Ok(i)) = (v.as_array(), part.parse::<usize>()) {
            return walk(a.get(i).unwrap_or(&Value::Null), rest);
        }
        Value::Null
    }
    walk(value, &path.split('.').collect::<Vec<_>>())
}
pub fn set_path(value: &mut Value, path: &str, new: Value) -> Result<()> {
    let (first, rest) = path
        .split_once('.')
        .map(|(a, b)| (a, Some(b)))
        .unwrap_or((path, None));
    match value {
        Value::Object(m) => {
            if let Some(rest) = rest {
                set_path(m.entry(first.to_string()).or_insert(json!({})), rest, new)
            } else {
                m.insert(first.into(), new);
                Ok(())
            }
        }
        Value::Array(a) => {
            let index: usize = first
                .parse()
                .map_err(|_| Error::new("Cannot set nested array field"))?;
            let v = a
                .get_mut(index)
                .ok_or_else(|| Error::new("Array field index out of range"))?;
            if let Some(rest) = rest {
                set_path(v, rest, new)
            } else {
                *v = new;
                Ok(())
            }
        }
        _ => err("Cannot set nested field"),
    }
}
pub fn records(v: &Value) -> Option<&Vec<Value>> {
    if let Some(a) = v.as_array() {
        return Some(a);
    }
    for key in ["records", "Items", "data", "results", "rows"] {
        if let Some(a) = v[key].as_array() {
            return Some(a);
        }
    }
    None
}
pub fn record_key(v: &Value) -> Option<&'static str> {
    ["records", "Items", "data", "results", "rows"]
        .into_iter()
        .find(|k| v[*k].is_array())
}
fn eq(a: &Value, b: &Value) -> bool {
    if let (Some(a), Some(b)) = (a.as_f64(), b.as_f64()) {
        a == b
    } else {
        a == b
    }
}
fn order(a: &Value, b: &Value) -> Option<Ordering> {
    match (a, b) {
        (Value::Number(a), Value::Number(b)) => a.as_f64()?.partial_cmp(&b.as_f64()?),
        (Value::String(a), Value::String(b)) => Some(a.cmp(b)),
        _ => None,
    }
}
#[derive(Clone)]
pub struct Predicate {
    field: String,
    op: String,
    expected: Value,
    regex: Option<Regex>,
}
impl Predicate {
    pub fn parse(expression: &str) -> Result<Self> {
        static WORD: OnceLock<Regex> = OnceLock::new();
        static SYMBOL: OnceLock<Regex> = OnceLock::new();
        let word = WORD.get_or_init(|| {
            Regex::new(
                r"^\s*(.+?)\s+(notin|in|contains|overlaps|between|exists|missing)(?:\s+(.*))?\s*$",
            )
            .unwrap()
        });
        let sym = SYMBOL.get_or_init(|| {
            Regex::new(r"^\s*(.+?)\s*(!~=|~=|=~|!=|>=|<=|=|>|<)\s*(.*?)\s*$").unwrap()
        });
        let caps = word
            .captures(expression)
            .or_else(|| sym.captures(expression))
            .ok_or_else(|| {
                Error::new(
                    "Invalid filter; use FIELD=VALUE, FIELD~=TEXT, FIELD in [1,2], or FIELD exists",
                )
            })?;
        let field = caps[1].trim().to_owned();
        let op = caps[2].to_owned();
        let expected = parse_value(caps.get(3).map(|c| c.as_str()).unwrap_or("").trim());
        if ["in", "notin", "overlaps", "between"].contains(&op.as_str()) && !expected.is_array() {
            return err(format!("{op} needs a JSON array"));
        }
        if op == "between" && expected.as_array().unwrap().len() != 2 {
            return err("between needs exactly two bounds");
        }
        let regex = if op == "=~" {
            let pattern = text(&expected);
            if pattern.len() > 512 {
                return err("Filter regex is too long (maximum 512 characters)");
            }
            Some(RegexBuilder::new(&pattern).case_insensitive(true).build()?)
        } else {
            None
        };
        Ok(Self {
            field,
            op,
            expected,
            regex,
        })
    }
    fn compare(&self, value: &Value) -> bool {
        let e = &self.expected;
        match self.op.as_str() {
            "=" => eq(value, e),
            "!=" => !eq(value, e),
            "~=" => text(value).to_lowercase().contains(&text(e).to_lowercase()),
            "!~=" => !text(value).to_lowercase().contains(&text(e).to_lowercase()),
            "=~" => self.regex.as_ref().unwrap().is_match(&text(value)),
            "in" => e.as_array().unwrap().iter().any(|e| eq(value, e)),
            "notin" => !e.as_array().unwrap().iter().any(|e| eq(value, e)),
            ">" => order(value, e) == Some(Ordering::Greater),
            "<" => order(value, e) == Some(Ordering::Less),
            ">=" => matches!(order(value, e), Some(Ordering::Greater | Ordering::Equal)),
            "<=" => matches!(order(value, e), Some(Ordering::Less | Ordering::Equal)),
            "between" => {
                matches!(
                    order(value, &e[0]),
                    Some(Ordering::Greater | Ordering::Equal)
                ) && matches!(order(value, &e[1]), Some(Ordering::Less | Ordering::Equal))
            }
            _ => false,
        }
    }
    pub fn matches(&self, row: &Value) -> bool {
        let value = get_path(row, &self.field);
        match self.op.as_str() {
            "exists" => !value.is_null() && value != json!([]),
            "missing" => value.is_null() || value == json!([]),
            "contains" => match &value {
                Value::Array(a) => a.iter().any(|v| eq(v, &self.expected)),
                Value::Object(m) => m.contains_key(&text(&self.expected)),
                _ => text(&value)
                    .to_lowercase()
                    .contains(&text(&self.expected).to_lowercase()),
            },
            "overlaps" => value.as_array().is_some_and(|a| {
                a.iter()
                    .any(|v| self.expected.as_array().unwrap().iter().any(|e| eq(v, e)))
            }),
            _ => {
                if let Some(a) = value.as_array() {
                    if ["!=", "!~=", "notin"].contains(&self.op.as_str()) {
                        a.iter().all(|v| self.compare(v))
                    } else {
                        a.iter().any(|v| self.compare(v))
                    }
                } else {
                    self.compare(&value)
                }
            }
        }
    }
}
pub fn compact(value: &Value, depth: usize) -> Value {
    match value {
        Value::String(s) if s.chars().count() > 180 => {
            json!(s.chars().take(177).collect::<String>() + "…")
        }
        Value::Object(m) if depth >= 3 => {
            json!({"_fields":m.keys().take(12).collect::<Vec<_>>(),"_count":m.len()})
        }
        Value::Object(m) => {
            let mut result = serde_json::Map::new();
            let priority = [
                "id",
                "Id",
                "name",
                "Name",
                "title",
                "path",
                "rootFolderPath",
                "OriginalTitle",
                "OriginalLanguage",
                "monitored",
                "qualityProfileId",
                "movieIds",
                "seriesId",
                "files",
            ];
            let keys = priority
                .iter()
                .copied()
                .filter(|k| m.contains_key(*k))
                .chain(
                    m.keys()
                        .map(String::as_str)
                        .filter(|k| !priority.contains(k)),
                );
            for key in keys.take(12) {
                result.insert(key.into(), compact(&m[key], depth + 1));
            }
            if m.len() > 12 {
                result.insert("_omittedFields".into(), json!(m.len() - 12));
            }
            Value::Object(result)
        }
        Value::Array(a) => {
            let mut r: Vec<_> = a.iter().take(8).map(|v| compact(v, depth + 1)).collect();
            if a.len() > 8 {
                r.push(json!({"_more":a.len()-8}));
            }
            Value::Array(r)
        }
        _ => value.clone(),
    }
}
fn enrich(row: &Value, reference: Option<String>) -> Value {
    if !row.is_object() {
        return row.clone();
    }
    let mut r = row.clone();
    if let Some(reference) = reference {
        r["ref"] = json!(reference);
    }
    if let Some(n) = row["size"].as_f64() {
        r["sizeMiB"] = json!((n / 1048576.0 * 10.0).round() / 10.0);
    }
    if !row["customFormatScore"].is_null() {
        r["score"] = row["customFormatScore"].clone();
    }
    if let Some(statuses) = row["statusMessages"].as_array() {
        let mut issues = Vec::new();
        for status in statuses {
            let messages = status["messages"]
                .as_array()
                .cloned()
                .filter(|a| !a.is_empty())
                .unwrap_or(vec![status["title"].clone()]);
            for msg in messages {
                if truth(&msg) && !issues.contains(&msg) {
                    issues.push(msg);
                }
            }
        }
        r["issueCount"] = json!(issues.len());
        r["issues"] = json!(issues.into_iter().take(3).collect::<Vec<_>>());
    }
    if let (Some(s), Some(e)) = (row["seasonNumber"].as_i64(), row["episodeNumber"].as_i64()) {
        r["code"] = json!(format!("S{s:02}E{e:02}"));
    }
    r
}
fn default_fields(row: &Value) -> Vec<String> {
    let list = if !row["operationId"].is_null() {
        if !row["body"].is_null() {
            "operationId,method,path,parameters,body,source"
        } else {
            "operationId,method,path"
        }
    } else if !row["guid"].is_null() && !row["indexer"].is_null() {
        "ref,title,quality.quality.name,seeders,sizeMiB,indexer,score,rejections"
    } else if !row["episodeNumber"].is_null() {
        "id,code,title,hasFile,monitored,airDateUtc"
    } else if !row["trackedDownloadStatus"].is_null() {
        "ref,id,title,status,trackedDownloadStatus,trackedDownloadState,issues,issueCount"
    } else if !row["app"].is_null() && !row["ok"].is_null() {
        "app,ok,version,error"
    } else if !row["relativePath"].is_null() {
        "id,relativePath,languages.*.name,mediaInfo.audioLanguages,mediaInfo.subtitles"
    } else {
        "ref,id,Id,title,Name,name,OriginalTitle,OriginalLanguage,path,Path,status,State,ok,version,monitored,enable,seeders,progress,statistics.episodeFileCount,statistics.episodeCount,trackedDownloadStatus,statusMessages,error,ImageType,Width,Height"
    };
    let mut found: Vec<String> = list
        .split(',')
        .filter(|k| !get_path(row, k).is_null())
        .map(String::from)
        .collect();
    if found.is_empty()
        && let Some(m) = row.as_object()
    {
        found = m.keys().take(8).cloned().collect();
    }
    found
}
fn project(row: &Value, fields: &[String]) -> Value {
    if !row.is_object() {
        return row.clone();
    }
    Value::Object(
        fields
            .iter()
            .map(|k| (k.clone(), get_path(row, k)))
            .collect(),
    )
}
pub fn canonical(v: &Value) -> String {
    match v {
        Value::Object(m) => {
            let sorted: BTreeMap<_, _> = m
                .iter()
                .map(|(k, v)| (k, serde_json::from_str::<Value>(&canonical(v)).unwrap()))
                .collect();
            serde_json::to_string(&sorted).unwrap()
        }
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(canonical).collect::<Vec<_>>().join(",")
        ),
        _ => v.to_string(),
    }
}
pub fn prepare(value: &Value, a: &Args, result: Option<&str>) -> Result<Value> {
    let action = if a.output_action.is_empty() {
        &a.action
    } else {
        &a.output_action
    };
    let selection: Vec<String> = a
        .s("select")
        .split(',')
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();
    if let Some(source) = records(value) {
        let required = a
            .list("filter")
            .iter()
            .map(|e| Predicate::parse(e))
            .collect::<Result<Vec<_>>>()?;
        let alternatives = a
            .list("any_filter")
            .iter()
            .map(|e| Predicate::parse(e))
            .collect::<Result<Vec<_>>>()?;
        let mut rows: Vec<(usize, Value)> = source
            .iter()
            .enumerate()
            .map(|(i, r)| (i, enrich(r, result.map(|k| format!("{k}:{i}")))))
            .filter(|(_, r)| {
                required.iter().all(|p| p.matches(r))
                    && (alternatives.is_empty() || alternatives.iter().any(|p| p.matches(r)))
            })
            .collect();
        let sort = if !a.b("sort") && action == "releases" && !a.b("full") {
            "-customFormatScore,-seeders,size".into()
        } else {
            a.s("sort")
        };
        for field in sort
            .split(',')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
        {
            let name = field.trim_start_matches(['-', '+']);
            let desc = field.starts_with('-');
            rows.sort_by(|(_, a), (_, b)| {
                let av = get_path(a, name);
                let bv = get_path(b, name);
                if av.is_null() || bv.is_null() {
                    return match (av.is_null(), bv.is_null()) {
                        (true, false) => Ordering::Greater,
                        (false, true) => Ordering::Less,
                        _ => Ordering::Equal,
                    };
                }
                let cmp = match (av.as_f64(), bv.as_f64()) {
                    (Some(x), Some(y)) => x.total_cmp(&y),
                    (Some(_), None) => Ordering::Less,
                    (None, Some(_)) => Ordering::Greater,
                    _ => canonical(&av)
                        .to_lowercase()
                        .cmp(&canonical(&bv).to_lowercase()),
                };
                if desc { cmp.reverse() } else { cmp }
            });
        }
        if a.b("unique_by") {
            let fields: Vec<_> = a.s("unique_by").split(',').map(String::from).collect();
            let mut seen = HashSet::new();
            rows.retain(|(_, r)| {
                let values: Vec<_> = fields.iter().map(|f| get_path(r, f)).collect();
                values.iter().all(|v| v.is_null() || v == "")
                    || seen.insert(canonical(&json!(values)))
            });
        }
        let matched = rows.len();
        let mut extra = json!({});
        if let Some(id) = result {
            extra["result"] = json!(id);
        }
        if a.b("count") {
            extra["matched"] = json!(matched);
            return Ok(extra);
        }
        if a.b("group_by") {
            let fields: Vec<_> = a.s("group_by").split(',').map(String::from).collect();
            let mut groups: BTreeMap<String, (Value, usize)> = BTreeMap::new();
            for (_, row) in &rows {
                let key = project(row, &fields);
                let entry = groups.entry(canonical(&key)).or_insert((key, 0));
                entry.1 += 1;
            }
            let mut groups: Vec<_> = groups.into_values().collect();
            groups.sort_by_key(|g| std::cmp::Reverse(g.1));
            let limit = if a.v["limit"].is_null() {
                groups.len()
            } else {
                a.n("limit") as usize
            };
            extra["matched"] = json!(matched);
            extra["groups"] = json!(
                groups
                    .into_iter()
                    .take(limit)
                    .map(|(key, count)| json!({"key":key,"count":count}))
                    .collect::<Vec<_>>()
            );
            return Ok(extra);
        }
        let limit = if a.v["limit"].is_null() {
            if a.b("full") { rows.len() } else { 10 }
        } else {
            a.n("limit") as usize
        };
        rows.truncate(limit);
        let shown: Vec<Value> = rows
            .iter()
            .map(|(i, r)| {
                if !selection.is_empty() {
                    project(r, &selection)
                } else if a.b("full") {
                    source[*i].clone()
                } else {
                    compact(&project(r, &default_fields(r)), 0)
                }
            })
            .collect();
        if a.b("full") {
            if value.is_array() {
                return Ok(json!(shown));
            }
            let mut r = value.clone();
            r[record_key(value).unwrap()] = json!(shown);
            return Ok(r);
        }
        extra["fetched"] = json!(source.len());
        extra["matched"] = json!(matched);
        extra["shown"] = json!(shown.len());
        extra["rows"] = json!(shown);
        for key in ["totalRecords", "TotalRecordCount", "total"] {
            if !value[key].is_null() {
                extra["available"] = value[key].clone();
                break;
            }
        }
        if rows.len() < matched {
            extra["more"] = json!(matched - rows.len());
        }
        return Ok(extra);
    }
    if !a.list("filter").is_empty()
        || !a.list("any_filter").is_empty()
        || a.b("count")
        || a.b("group_by")
    {
        return err("Filters/count/group-by need a list response");
    }
    let mut value = if !selection.is_empty() {
        project(value, &selection)
    } else if action == "schema" && !a.b("full") {
        schema_brief(value)
    } else if value.is_object()
        && (!value["Id"].is_null() || !value["OriginalTitle"].is_null())
        && !a.b("full")
    {
        let mut fields = default_fields(value);
        if !value["Overview"].is_null() {
            fields.push("Overview".into());
        }
        project(value, &fields)
    } else {
        value.clone()
    };
    if a.b("full") {
        return Ok(value);
    }
    if value["wait"] == true {
        if value.get("progress").is_some() {
            value["progress"] = compact(&value["progress"], 0);
        }
        if value.get("response").is_some() {
            value["response"] = compact(&value["response"], 0);
        }
    } else if action != "batch" && action != "find-missing" {
        value = compact(&value, 0);
    }
    if let Some(id) = result
        && value.is_object()
    {
        value["result"] = json!(id);
    }
    Ok(value)
}
pub fn render(value: &Value, a: &Args, result: Option<&str>) -> Result<String> {
    let mut report = prepare(value, a, result)?;
    if !a.b("full") {
        for (key, source) in [("cachedAt", "cached_at"), ("warning", "cache_error")] {
            if a.b(source) && report.is_object() {
                report[key] = a.v[source].clone();
            }
        }
    }
    let budget = if a.b("max_output") {
        a.n("max_output") as usize
    } else if a.b("full") {
        usize::MAX
    } else {
        6000
    };
    let dump = |v: &Value| -> String {
        if a.b("pretty") {
            serde_json::to_string_pretty(v).unwrap()
        } else {
            v.to_string()
        }
    };
    if dump(&report).chars().count() > budget {
        if a.b("full") {
            return err("Full output exceeds --max-output; use --select/--limit or omit --full");
        }
        let key = ["rows", "groups", "jobs", "episodes"]
            .into_iter()
            .find(|k| report[*k].is_array());
        if let Some(key) = key {
            let original = report[key].as_array().unwrap().len();
            report["outputLimited"] = json!(true);
            if key != "rows" {
                report["omitted"] = json!(0);
            }
            while !report[key].as_array().unwrap().is_empty()
                && dump(&report).chars().count() > budget
            {
                report[key].as_array_mut().unwrap().pop();
                if key != "rows" {
                    report["omitted"] = json!(original - report[key].as_array().unwrap().len());
                }
            }
            if key == "rows" {
                let shown = report[key].as_array().unwrap().len();
                report["shown"] = json!(shown);
                report["more"] = json!(
                    report["matched"]
                        .as_u64()
                        .unwrap_or(0)
                        .saturating_sub(shown as u64)
                );
            }
        }
        if dump(&report).chars().count() > budget && report["wait"] == true {
            report.as_object_mut().unwrap().remove("response");
            report.as_object_mut().unwrap().remove("progress");
            if dump(&report).chars().count() > budget {
                report = project(
                    &report,
                    &["wait", "status", "app", "target", "result"].map(String::from),
                );
                report["outputLimited"] = json!(true);
            }
            if dump(&report).chars().count() > budget && result.is_some() {
                report = json!({"wait":true,"status":report["status"],"app":report["app"],"result":result,"outputLimited":true,"hint":"Read result --id RESULT --full for resume. Do not repeat the original action."});
            }
        }
        if dump(&report).chars().count() > budget {
            if report["wait"] == true {
                return err(
                    "Wait output exceeds --max-output; do not repeat the original action. Increase the budget and enable caching.",
                );
            }
            report = json!({"outputLimited":true,"hint":"Use --select to retrieve specific fields, or --full."});
            if let Some(id) = result {
                report["result"] = json!(id);
            }
        }
    }
    if a.b("table") {
        let mut table = table(&report);
        if report["rows"].is_array() {
            let mut extra = report.clone();
            extra.as_object_mut().unwrap().remove("rows");
            table.push('\n');
            table.push_str(&dump(&extra));
        }
        if table.chars().count() <= budget {
            return Ok(table);
        }
    }
    Ok(dump(&report))
}
fn table(value: &Value) -> String {
    let rows = records(value)
        .cloned()
        .unwrap_or_else(|| vec![value.clone()]);
    if rows.is_empty() {
        return "(no records)".into();
    }
    let mut keys = Vec::new();
    for row in &rows {
        if let Some(m) = row.as_object() {
            for k in m.keys() {
                if !keys.contains(k) && keys.len() < 10 {
                    keys.push(k.clone());
                }
            }
        } else {
            return rows.iter().map(text).collect::<Vec<_>>().join("\n");
        }
    }
    fn cell(v: &Value) -> String {
        let s = text(v);
        let s: String = s
            .chars()
            .flat_map(|c| {
                if c.is_control() {
                    c.escape_default().collect::<Vec<_>>()
                } else {
                    vec![c]
                }
            })
            .collect();
        if s.chars().count() > 90 {
            s.chars().take(87).collect::<String>() + "..."
        } else {
            s
        }
    }
    let data: Vec<Vec<String>> = rows
        .iter()
        .map(|r| keys.iter().map(|k| cell(&r[k])).collect())
        .collect();
    let widths: Vec<usize> = keys
        .iter()
        .enumerate()
        .map(|(i, k)| {
            data.iter()
                .map(|r| r[i].chars().count())
                .max()
                .unwrap_or(0)
                .max(k.chars().count())
        })
        .collect();
    let line = |r: &[String]| {
        r.iter()
            .zip(&widths)
            .map(|(s, w)| format!("{}{}", s, " ".repeat(w.saturating_sub(s.chars().count()))))
            .collect::<Vec<_>>()
            .join("  ")
    };
    let mut lines = vec![
        line(&keys),
        line(&widths.iter().map(|w| "-".repeat(*w)).collect::<Vec<_>>()),
    ];
    lines.extend(data.iter().map(|r| line(r)));
    lines.join("\n")
}
pub fn schema_brief(v: &Value) -> Value {
    if let Some(paths) = v["paths"].as_object() {
        let n: usize = paths
            .values()
            .map(|p| {
                p.as_object()
                    .map(|m| {
                        m.keys()
                            .filter(|k| {
                                ["get", "post", "put", "patch", "delete", "head", "options"]
                                    .contains(&k.as_str())
                            })
                            .count()
                    })
                    .unwrap_or(0)
            })
            .sum();
        return json!({"info":v["info"],"paths":paths.len(),"operations":n,"source":v["x-autoarr-source"],"hint":"Use ops --search TERM --details; schema OPERATION_ID --full returns full details."});
    }
    fn typename(v: &Value) -> String {
        if let Some(t) = v["type"].as_str() {
            return t.into();
        }
        if let Some(r) = v["$ref"].as_str() {
            return r.rsplit('/').next().unwrap_or("?").into();
        }
        for key in ["allOf", "anyOf"] {
            if let Some(a) = v[key].as_array() {
                return a.iter().map(typename).collect::<Vec<_>>().join("|");
            }
        }
        "?".into()
    }
    fn choices(v: &Value) -> Value {
        if !v["enum"].is_null() {
            v["enum"].clone()
        } else {
            v["items"]["enum"].clone()
        }
    }
    let body = &v["requestBody"];
    let mut schema = body["content"]
        .as_object()
        .and_then(|m| m.values().next())
        .map(|v| v["schema"].clone())
        .unwrap_or(json!({}));
    while schema["allOf"].as_array().is_some_and(|a| a.len() == 1) {
        schema = schema["allOf"][0].clone();
    }
    let props = if schema["properties"].is_object() {
        &schema["properties"]
    } else {
        &schema["items"]["properties"]
    };
    let empty = serde_json::Map::new();
    let props = props.as_object().unwrap_or(&empty);
    let mut b = json!({"required":body["required"].as_bool().unwrap_or(false),"type":typename(&schema),"fields":Value::Object(props.iter().map(|(k,v)|(k.clone(),json!(typename(v)))).collect())});
    if truth(&schema["required"]) {
        b["requiredFields"] = schema["required"].clone();
    }
    let enums: serde_json::Map<String, Value> = props
        .iter()
        .filter(|(_, v)| !choices(v).is_null())
        .map(|(k, v)| (k.clone(), choices(v)))
        .collect();
    if !enums.is_empty() {
        b["choices"] = Value::Object(enums);
    }
    let parameters:Vec<_>=v["parameters"].as_array().into_iter().flatten().map(|p|{let s=if p["schema"].is_null(){p}else{&p["schema"]};let mut r=json!({"name":p["name"],"in":p["in"],"type":typename(s),"required":p["required"].as_bool().unwrap_or(false)});if !choices(s).is_null(){r["choices"]=choices(s);}r}).collect();
    json!({"operationId":v["operationId"],"method":v["method"],"path":v["path"],"parameters":parameters,"body":b,"source":v["schemaSource"]})
}
