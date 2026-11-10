use crate::{
    Error, Result,
    args::Args,
    client::{Client, query},
    config::Results,
    err,
    output::records,
    text, truth,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
};
pub fn find(c: &mut Client, a: &Args) -> Result<Value> {
    if c.kind != "sonarr" {
        return err("Missing episodes requires Sonarr");
    }
    if a.b("series") == !a.v["series_id"].is_null() {
        return err("Supply either a series title or --series-id");
    }
    let show = if !a.v["series_id"].is_null() {
        c.get(&format!("/series/{}", a.s("series_id")), vec![])?
    } else {
        let rows = c.get("/series", vec![])?;
        let rows = records(&rows).ok_or_else(|| Error::new("Series API did not return records"))?;
        let title = a.s("series").to_lowercase();
        let mut matches: Vec<_> = rows
            .iter()
            .filter(|r| text(&r["title"]).to_lowercase() == title)
            .collect();
        if matches.is_empty() {
            matches = rows
                .iter()
                .filter(|r| text(&r["title"]).to_lowercase().contains(&title))
                .collect();
        }
        if matches.len() != 1 {
            return err(
                "Series title must match exactly one managed show; inspect IDs with list series",
            );
        }
        matches[0].clone()
    };
    let rows = c.get("/episode", query(&[("seriesId", show["id"].clone())]))?;
    let now = chrono::Utc::now();
    let mut episodes: Vec<Value> = records(&rows)
        .ok_or_else(|| Error::new("Episode API did not return records"))?
        .iter()
        .filter(|e| {
            if truth(&e["hasFile"])
                || !a.b("include_specials") && e["seasonNumber"].as_i64().unwrap_or(0) == 0
                || !a.v["season"].is_null() && e["seasonNumber"] != a.v["season"]
                || !a.b("include_unmonitored") && !truth(&e["monitored"])
            {
                return false;
            }
            chrono::DateTime::parse_from_rfc3339(&text(&e["airDateUtc"])).is_ok_and(|d| d <= now)
        })
        .cloned()
        .collect();
    episodes.sort_by_key(|e| {
        (
            e["seasonNumber"].as_i64().unwrap_or(0),
            e["episodeNumber"].as_i64().unwrap_or(0),
        )
    });
    if a.action == "missing" {
        return Ok(json!(episodes));
    }
    let loaded = if a.b("release_file") {
        let v: Value = serde_json::from_slice(&fs::read(a.s("release_file"))?)?;
        if !v.is_array() {
            return err("release-file must contain a native Sonarr release array");
        }
        Some(v)
    } else {
        None
    };
    let mut seasons: BTreeMap<i64, Vec<Value>> = BTreeMap::new();
    for e in &episodes {
        seasons
            .entry(e["seasonNumber"].as_i64().unwrap_or(0))
            .or_default()
            .push(e.clone());
    }
    let (mut report, mut snapshots, mut searches) = (Vec::new(), Vec::new(), 0);
    let label = regex::Regex::new("(?i)VOSTFR|SUBFRENCH").unwrap();
    for (season, episodes) in seasons {
        let releases = if let Some(v) = &loaded {
            v.clone()
        } else {
            searches += 1;
            c.get(
                "/release",
                query(&[
                    ("seriesId", show["id"].clone()),
                    ("seasonNumber", json!(season)),
                ]),
            )?
        };
        let releases = releases
            .as_array()
            .ok_or_else(|| Error::new("Unexpected Sonarr release response"))?;
        let key = if a.b("no_cache") {
            None
        } else {
            Some(Results::new(&c.state).save(&json!(releases), &c.name(), "releases")?)
        };
        if let Some(key) = &key {
            snapshots.push(key.clone());
        }
        for episode in episodes {
            let mut candidates = Vec::new();
            for (index, r) in releases.iter().enumerate() {
                let mapped = r["mappedSeriesId"].as_i64().unwrap_or(0);
                let release_season = r["mappedSeasonNumber"]
                    .as_i64()
                    .filter(|n| *n != 0)
                    .or(r["seasonNumber"].as_i64());
                if mapped != 0 && Some(mapped) != show["id"].as_i64()
                    || release_season != Some(season)
                    || !r["episodeNumbers"]
                        .as_array()
                        .is_some_and(|a| a.contains(&episode["episodeNumber"]))
                {
                    continue;
                }
                let labelled = label.is_match(&text(&r["title"]));
                let mut row = json!({"title":r["title"],"quality":r["quality"]["quality"]["name"],"seeders":r["seeders"],"sizeMiB":(r["size"].as_f64().unwrap_or(0.0)/1048576.0*10.0).round()/10.0,"indexer":r["indexer"],"score":r["customFormatScore"],"vostfrLabel":labelled,"frenchSubtitlesVerified":false,"rejections":r["rejections"].as_array().cloned().unwrap_or_default()});
                if let Some(key) = &key {
                    row["ref"] = json!(format!("{key}:{index}"));
                }
                candidates.push(row);
            }
            let strict = candidates
                .iter()
                .filter(|r| r["vostfrLabel"] == true)
                .count();
            let count = candidates.len();
            if a.b("vostfr") {
                candidates.retain(|r| r["vostfrLabel"] == true);
            }
            candidates.sort_by(|x, y| {
                (!truth(&x["vostfrLabel"]), truth(&x["rejections"]))
                    .cmp(&(!truth(&y["vostfrLabel"]), truth(&y["rejections"])))
                    .then_with(|| {
                        y["seeders"]
                            .as_f64()
                            .unwrap_or(0.0)
                            .total_cmp(&x["seeders"].as_f64().unwrap_or(0.0))
                    })
            });
            let mut seen = HashSet::new();
            candidates.retain(|r| {
                seen.insert(
                    text(&r["title"])
                        .to_lowercase()
                        .trim_end_matches(".mkv")
                        .to_string(),
                )
            });
            candidates.truncate(a.n("top") as usize);
            report.push(json!({"episode":format!("S{:02}E{:02}",season,episode["episodeNumber"].as_i64().unwrap_or(0)),"id":episode["id"],"title":episode["title"],"releases":count,"vostfrLabelled":strict,"candidates":candidates}));
        }
    }
    Ok(
        json!({"series":{"id":show["id"],"title":show["title"]},"missing":episodes.len(),"mode":if a.b("vostfr"){ "VOSTFR-labelled only" }else{"all languages; subtitles unverified"},"searches":searches,"fromFile":loaded.is_some(),"releaseResults":snapshots,"episodes":report,"downloadsStarted":0}),
    )
}
