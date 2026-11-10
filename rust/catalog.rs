use crate::{
    Error, Result,
    client::{Client, Data, Query, quote},
    config::private_json,
    err, object,
    output::{pairs, schema_brief},
    text, truth,
};
use regex::Regex;
use serde_json::{Value, json};
use std::{collections::HashSet, fs, io::Read, time::Duration};

pub fn load(client: &mut Client, refresh: bool) -> Result<Value> {
    let cache = client.schema_path();
    if !refresh
        && fs::metadata(&cache)
            .ok()
            .and_then(|m| m.modified().ok())
            .is_some_and(|t| t.elapsed().unwrap_or_default() < Duration::from_secs(86400))
        && let Ok(v) = fs::read(&cache)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .ok_or(())
    {
        return Ok(v);
    }
    let candidates = if truth(&client.app["schema_path"]) {
        vec![text(&client.app["schema_path"])]
    } else {
        match client.kind.as_str() {
            "jellyfin" => vec![
                "/api-docs/openapi.json".into(),
                "/api-docs/swagger.json".into(),
            ],
            "bazarr" => vec!["/api/swagger.json".into()],
            "seerr" => vec![
                "/api-docs/swagger-ui-init.js".into(),
                "/api-docs/swagger.json".into(),
            ],
            "cleanuparr" => vec!["/swagger/v1/swagger.json".into(), "/openapi/v1.json".into()],
            "autobrr" => vec!["/api/swagger.json".into(), "/swagger/doc.json".into()],
            _ => vec![
                text(&client.app["api_prefix"]) + "/openapi.json",
                "/swagger/v1/swagger.json".into(),
                "/swagger.json".into(),
                "/openapi.json".into(),
            ],
        }
    };
    let swagger_options = Regex::new(r"var\s+options\s*=\s*").unwrap();
    for path in candidates {
        if let Ok(mut v) = client.request(
            "GET",
            &path,
            vec![],
            Data {
                site: true,
                ..Default::default()
            },
        ) {
            if let Some(js) = v.as_str()
                && path.ends_with("swagger-ui-init.js")
                && let Some(m) = swagger_options.find(js)
            {
                let mut de =
                    serde_json::Deserializer::from_str(&js[m.end()..]).into_iter::<Value>();
                if let Some(Ok(options)) = de.next() {
                    v = options["swaggerDoc"].clone();
                }
            }
            if v["paths"].is_object() {
                v["x-autoarr-source"] = json!({"type":"live","url":text(&client.app["url"])+&path});
                private_json(&cache, &v)?;
                return Ok(v);
            }
        }
    }
    let published = match client.kind.as_str() {
        "radarr" => Some(("Radarr/Radarr", "src/Radarr.Api.V3/openapi.json")),
        "sonarr" => Some(("Sonarr/Sonarr", "src/Sonarr.Api.V3/openapi.json")),
        "lidarr" => Some(("Lidarr/Lidarr", "src/Lidarr.Api.V1/openapi.json")),
        "prowlarr" => Some(("Prowlarr/Prowlarr", "src/Prowlarr.Api.V1/openapi.json")),
        "bookshelf" => Some(("pennydreadful/bookshelf", "src/Readarr.Api.V1/openapi.json")),
        "readarr" => Some(("Readarr/Readarr", "src/Readarr.Api.V1/openapi.json")),
        _ => None,
    };
    if let Some((repo, file)) = published {
        let version = text(&client.get("/system/status", vec![])?["version"]);
        let refs = if version.is_empty() {
            vec!["develop".into()]
        } else {
            vec![format!("v{version}"), "develop".into()]
        };
        let anonymous = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(20))
            .build()?;
        for reference in refs {
            let source = format!("https://raw.githubusercontent.com/{repo}/{reference}/{file}");
            if let Ok(response) = anonymous.get(&source).send() {
                if !response.status().is_success() {
                    continue;
                }
                let mut bytes = Vec::new();
                response.take(16 * 1024 * 1024).read_to_end(&mut bytes)?;
                if let Ok(mut v) = serde_json::from_slice::<Value>(&bytes)
                    && v["paths"].is_object()
                {
                    v["x-autoarr-source"] = json!({"type":"official-published","url":source,"serverVersion":version,"exactVersionTag":reference==format!("v{version}")});
                    private_json(&cache, &v)?;
                    return Ok(v);
                }
            }
        }
    }
    if !refresh && cache.exists() {
        return Ok(serde_json::from_slice(&fs::read(cache)?)?);
    }
    err("No OpenAPI schema found; use api for native endpoints or configure schema_path")
}
pub fn resolve(spec: &Value, value: &Value) -> Result<Value> {
    fn walk(spec: &Value, v: &Value, seen: &HashSet<String>, depth: usize) -> Result<Value> {
        if depth > 12 {
            return Ok(v.clone());
        }
        match v {
            Value::Array(a) => Ok(Value::Array(
                a.iter()
                    .map(|v| walk(spec, v, seen, depth + 1))
                    .collect::<Result<Vec<_>>>()?,
            )),
            Value::Object(m) => {
                if let Some(reference) = v["$ref"].as_str()
                    && reference.starts_with("#/")
                    && !seen.contains(reference)
                {
                    let target = spec.pointer(&reference[1..]).ok_or_else(|| {
                        Error::new(format!("Missing schema reference {reference}"))
                    })?;
                    let mut merged = object(target)?.clone();
                    for (k, v) in m {
                        if k != "$ref" {
                            merged.insert(k.clone(), v.clone());
                        }
                    }
                    let mut next = seen.clone();
                    next.insert(reference.into());
                    return walk(spec, &Value::Object(merged), &next, depth + 1);
                }
                Ok(Value::Object(
                    m.iter()
                        .map(|(k, v)| Ok((k.clone(), walk(spec, v, seen, depth + 1)?)))
                        .collect::<Result<_>>()?,
                ))
            }
            _ => Ok(v.clone()),
        }
    }
    walk(spec, value, &HashSet::new(), 0)
}
pub fn operations(
    spec: &Value,
    prefix: &str,
    search: &str,
    method: &str,
    details: bool,
) -> Result<Vec<Value>> {
    let mut rows = Vec::new();
    let generated = Regex::new("[^A-Za-z0-9]+").unwrap();
    for (path, methods) in object(&spec["paths"])? {
        for (verb, op) in object(methods)? {
            if !["get", "post", "put", "patch", "delete", "head", "options"]
                .contains(&verb.as_str())
                || !op.is_object()
            {
                continue;
            }
            let id = op["operationId"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(String::from)
                .unwrap_or_else(|| {
                    format!(
                        "{}_{}",
                        verb,
                        generated
                            .replace_all(path.strip_prefix(prefix).unwrap_or(path), "_")
                            .trim_matches('_')
                    )
                });
            let row = json!({"operationId":id,"method":verb.to_uppercase(),"path":path,"summary":op["summary"].as_str().unwrap_or(""),"tags":op["tags"].as_array().cloned().unwrap_or_default()});
            if !method.is_empty() && text(&row["method"]) != method.to_uppercase() {
                continue;
            }
            if !row
                .to_string()
                .to_lowercase()
                .contains(&search.to_lowercase())
            {
                continue;
            }
            rows.push(row);
        }
    }
    rows.sort_by_key(|v| (text(&v["path"]), text(&v["method"])));
    if details {
        for row in &mut rows {
            let detail = schema_brief(&operation_from_row(spec, row, false)?);
            for (k, v) in object(&detail)? {
                row[k] = v.clone();
            }
        }
    }
    Ok(rows)
}
fn operation_from_row(spec: &Value, row: &Value, responses: bool) -> Result<Value> {
    let path = &spec["paths"][text(&row["path"])];
    let raw = &path[text(&row["method"]).to_lowercase()];
    let mut row = row.clone();
    let mut parameters = path["parameters"].as_array().cloned().unwrap_or_default();
    parameters.extend(raw["parameters"].as_array().cloned().unwrap_or_default());
    row["parameters"] = resolve(spec, &json!(parameters))?;
    row["requestBody"] = resolve(spec, raw.get("requestBody").unwrap_or(&json!({})))?;
    if responses {
        row["responses"] = resolve(spec, raw.get("responses").unwrap_or(&json!({})))?;
    }
    row["schemaSource"] = spec["x-autoarr-source"].clone();
    Ok(row)
}
pub fn operation(spec: &Value, prefix: &str, id: &str, responses: bool) -> Result<Value> {
    let rows = operations(spec, prefix, "", "", false)?;
    let matches: Vec<_> = rows
        .iter()
        .filter(|r| text(&r["operationId"]).eq_ignore_ascii_case(id))
        .collect();
    if matches.is_empty() {
        return err("Unknown operation ID; use ops --search TERM --details");
    }
    if matches.len() != 1 {
        return err("Ambiguous operation ID; use api METHOD PATH");
    }
    operation_from_row(spec, matches[0], responses)
}
pub fn invoke(
    client: &mut Client,
    spec: &Value,
    id: &str,
    path_values: &[String],
    query: Query,
    mut data: Data,
) -> Result<Value> {
    let op = operation(spec, &text(&client.app["api_prefix"]), id, false)?;
    let mut path = text(&op["path"]);
    let parameters = pairs(path_values)?;
    let required: HashSet<String> = Regex::new(r"\{([^}]+)\}")
        .unwrap()
        .captures_iter(&path)
        .map(|c| c[1].into())
        .collect();
    if parameters
        .iter()
        .map(|(k, _)| k.clone())
        .collect::<HashSet<_>>()
        != required
    {
        return err("Path parameters must exactly match the discovered operation");
    }
    for (k, v) in parameters {
        path = path.replace(&format!("{{{k}}}"), &quote(&text(&v)));
    }
    let fields: HashSet<_> = data
        .form
        .as_ref()
        .unwrap_or(&vec![])
        .iter()
        .map(|(k, _)| k.clone())
        .chain(data.files.iter().map(|(k, _)| k.clone()))
        .collect();
    for p in op["parameters"].as_array().into_iter().flatten() {
        if p["required"] == true {
            let name = text(&p["name"]);
            if p["in"] == "query" && !query.iter().any(|(k, _)| k == &name) {
                return err(format!("Missing required query parameter: {name}"));
            }
            if p["in"] == "formData" && !fields.contains(&name) {
                return err(format!("Missing required form field: {name}"));
            }
        }
    }
    if op["requestBody"]["required"] == true
        && data.body.is_none()
        && data.form.is_none()
        && data.raw.is_none()
        && data.files.is_empty()
    {
        return err(
            "This operation requires --data, --form, or --upload; inspect schema OPERATION_ID",
        );
    }
    let mut qs = Vec::new();
    for (k, v) in query {
        let parameter = op["parameters"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|p| p["in"] == "query" && p["name"] == k);
        if let Some(p) = parameter {
            let schema = p.get("schema").unwrap_or(p);
            if schema["type"] == "array"
                && let Some(values) = v.as_array()
            {
                let explode = if spec.get("swagger").is_some() {
                    p["collectionFormat"] == "multi"
                } else {
                    p["explode"].as_bool().unwrap_or(true)
                };
                if explode {
                    qs.extend(values.iter().map(|v| (k.clone(), v.clone())));
                } else {
                    let sep = match text(&p["collectionFormat"]).as_str() {
                        "ssv" => " ",
                        "tsv" => "\t",
                        "pipes" => "|",
                        _ => ",",
                    };
                    qs.push((
                        k,
                        json!(values.iter().map(text).collect::<Vec<_>>().join(sep)),
                    ));
                }
                continue;
            }
        }
        qs.push((k, v));
    }
    let prefix = text(&client.app["api_prefix"]);
    data.site = !prefix.is_empty()
        && object(&spec["paths"])?
            .keys()
            .any(|p| p == &prefix || p.starts_with(&(prefix.clone() + "/")));
    client.request(&text(&op["method"]), &path, qs, data)
}
pub fn server_filters(client: &Client, path: &str, mut qs: Query, filters: &[String]) -> Query {
    if filters.is_empty() {
        return qs;
    }
    let Ok(bytes) = fs::read(client.schema_path()) else {
        return qs;
    };
    let Ok(spec) = serde_json::from_slice::<Value>(&bytes) else {
        return qs;
    };
    let full = text(&client.app["api_prefix"]) + path;
    let op = spec["paths"]
        .get(&full)
        .or_else(|| spec["paths"].get(path))
        .unwrap_or(&Value::Null);
    let mut parameters = op["parameters"].as_array().cloned().unwrap_or_default();
    parameters.extend(
        op["get"]["parameters"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
    );
    let Ok(parameters) = resolve(&spec, &json!(parameters)) else {
        return qs;
    };
    let allowed: HashSet<String> = parameters
        .as_array()
        .into_iter()
        .flatten()
        .filter(|p| p["in"] == "query")
        .map(|p| text(&p["name"]))
        .collect();
    let mut present: HashSet<_> = qs.iter().map(|(k, _)| k.to_lowercase()).collect();
    let equality = Regex::new(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*([^~=].*|)\s*$").unwrap();
    let ids = Regex::new(r"^\s*id\s+in\s+(\[.*\])\s*$").unwrap();
    for expression in filters {
        if let Some(c) = equality.captures(expression) {
            let name = c[1].to_string();
            let value = crate::output::parse_value(c[2].trim());
            if allowed.contains(&name)
                && !present.contains(&name.to_lowercase())
                && !value.is_array()
                && !value.is_object()
            {
                present.insert(name.to_lowercase());
                qs.push((name, value));
            }
            continue;
        }
        let alias = match path {
            "/episode" => "episodeIds",
            "/episodefile" => "episodeFileIds",
            "/moviefile" => "movieFileIds",
            "/trackfile" => "trackFileIds",
            _ => "",
        };
        if let Some(c) = ids.captures(expression)
            && allowed.contains(alias)
            && !present.contains(&alias.to_lowercase())
            && let Some(values) = crate::output::parse_value(&c[1]).as_array()
        {
            qs.extend(values.iter().map(|v| (alias.into(), v.clone())));
            present.insert(alias.to_lowercase());
        }
    }
    qs
}
