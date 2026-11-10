use crate::{args, batch, config, output, waiting};
use serde_json::{Value, json};
use std::collections::HashMap;

fn options(argv: &[&str]) -> args::Args {
    args::parse(
        &argv.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
        &json!({"apps":{}}),
    )
    .unwrap()
}

#[test]
fn wildcards_and_array_indices_preserve_values() {
    let v = json!({"languages":[{"name":"English"},{"name":"French"}]});
    assert_eq!(
        output::get_path(&v, "languages[].name"),
        json!(["English", "French"])
    );
    assert_eq!(output::get_path(&v, "languages[1].name"), "French");
    assert!(output::get_path(&v, "languages.9.name").is_null());
}
#[test]
fn filters_cover_arrays_ranges_missing_and_regex() {
    let v = json!({"id":7,"ep":[6,7],"title":"Show VOSTFR","language":null});
    for expression in [
        "id in [6,7]",
        "id between [5,8]",
        "ep contains 6",
        "ep overlaps [5,7]",
        "title =~ \"VOSTFR|SUBFRENCH\"",
        "language missing",
    ] {
        assert!(
            output::Predicate::parse(expression).unwrap().matches(&v),
            "{expression}"
        );
    }
    assert!(
        !output::Predicate::parse("ep notin [6]")
            .unwrap()
            .matches(&v)
    );
}
#[test]
fn invalid_filters_are_errors_not_executable_expressions() {
    for expression in ["bad expression", "x in 2", "x between [1]", "x =~ \"[\""] {
        assert!(output::Predicate::parse(expression).is_err());
    }
}
#[test]
fn negative_array_comparisons_require_all_elements() {
    let p = output::Predicate::parse("languages.*.name!=French").unwrap();
    assert!(!p.matches(&json!({"languages":[{"name":"English"},{"name":"French"}]})));
}
#[test]
fn nested_updates_preserve_siblings_and_validate_indices() {
    let mut v = json!({"fields":[{"name":"original","value":false}]});
    output::set_path(&mut v, "fields.0.value", json!(true)).unwrap();
    assert_eq!(v["fields"][0]["name"], "original");
    assert!(output::set_path(&mut v, "fields.3.value", json!(1)).is_err());
}
#[test]
fn original_row_references_survive_sort_and_filters() {
    let a = options(&[
        "result",
        "--file",
        "unused",
        "--sort=-seeders",
        "--filter",
        "seeders>=5",
        "--limit",
        "1",
    ]);
    let v = output::prepare(
        &json!([{"id":1,"seeders":1},{"id":2,"seeders":20},{"id":3,"seeders":10}]),
        &a,
        Some("abcdef123456"),
    )
    .unwrap();
    assert_eq!(v["rows"][0]["ref"], "abcdef123456:1");
}
#[test]
fn counts_and_groups_run_before_limits() {
    let a = options(&["result", "--file", "unused", "--count", "--limit", "1"]);
    assert_eq!(
        output::prepare(&json!([{"id":1},{"id":2}]), &a, None).unwrap()["matched"],
        2
    );
}
#[test]
fn unique_missing_identifiers_do_not_collapse_rows() {
    let a = options(&[
        "result",
        "--file",
        "unused",
        "--unique-by",
        "hash",
        "--sort=-seeders",
        "--full",
    ]);
    let v = output::prepare(
        &json!([{"hash":"a","seeders":2},{"hash":"a","seeders":10},{"hash":null},{"id":3}]),
        &a,
        None,
    )
    .unwrap();
    assert_eq!(v.as_array().unwrap().len(), 3);
    assert_eq!(v[0]["seeders"], 10);
}
#[test]
fn compact_budget_returns_valid_json_with_omissions() {
    let a = options(&["result", "--file", "unused", "--max-output", "256"]);
    let rows: Vec<_> = (0..100)
        .map(|i| json!({"id":i,"title":"x".repeat(150)}))
        .collect();
    let s = output::render(&json!(rows), &a, None).unwrap();
    assert!(s.chars().count() <= 256);
    assert_eq!(
        serde_json::from_str::<Value>(&s).unwrap()["outputLimited"],
        true
    );
}
#[test]
fn typed_json_substitution_preserves_quotes_null_and_boolean() {
    let values = HashMap::from([
        ("item.enabled".into(), json!(true)),
        ("item.name".into(), json!("quote \" $(not-a-command)")),
        ("item.optional".into(), Value::Null),
    ]);
    let argv = vec![
        json!({"enabled":"{item.enabled}","name":"{item.name}","optional":"{item.optional}"})
            .to_string(),
    ];
    let output = batch::substitute(&argv, &values, false).unwrap();
    let v: Value = serde_json::from_str(&output[0]).unwrap();
    assert_eq!(v["enabled"], true);
    assert!(v["optional"].is_null());
    assert_eq!(v["name"], values["item.name"]);
}
#[test]
fn secret_fields_tracker_values_and_urls_are_redacted() {
    let v = json!({"fields":[{"name":"passkey","value":"test-tracker-passkey"}],"downloadUrl":"https://host/test-tracker-passkey/download","api_key_env":"SONARR_API_KEY"});
    let mut r = config::Redactor::default();
    r.collect(&v);
    let cleaned = r.clean(&v);
    assert!(!cleaned.to_string().contains("test-tracker-passkey"));
    assert_eq!(cleaned["api_key_env"], "SONARR_API_KEY");
}
#[test]
fn result_snapshots_are_private_and_bound_to_source() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let store = config::Results::new(dir.path());
    let key = store
        .save(
            &json!([{"guid":"release","indexerId":1}]),
            "sonarr",
            "releases",
        )
        .unwrap();
    assert_eq!(
        store.reference(&format!("{key}:0"), "sonarr").unwrap()["guid"],
        "release"
    );
    assert!(store.reference(&format!("{key}:0"), "radarr").is_err());
    assert!(store.load("../outside").is_err());
    assert_eq!(
        store
            .directory
            .join(format!("{key}.json"))
            .metadata()
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}
#[test]
fn atomic_configuration_write_refuses_symlink() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("target");
    std::fs::write(&target, "unchanged").unwrap();
    let link = dir.path().join("link");
    std::os::unix::fs::symlink(&target, &link).unwrap();
    assert!(config::private_json(&link, &json!({"key":"value"})).is_err());
    assert_eq!(std::fs::read_to_string(target).unwrap(), "unchanged");
}
#[test]
fn wait_exit_codes_preserve_pending_work_state() {
    for (status, code) in [
        ("completed", 0),
        ("timeout", 3),
        ("failed", 2),
        ("interrupted", 130),
    ] {
        assert_eq!(
            waiting::exit_code(&json!({"wait":true,"status":status})),
            code
        );
    }
}
#[test]
fn api_and_wait_arguments_validate_before_requests() {
    for argv in [
        vec!["sonarr", "api", "GET"],
        vec![
            "sonarr",
            "command",
            "RefreshSeries",
            "--wait",
            "--poll-interval",
            "0",
        ],
        vec![
            "sonarr",
            "wait",
            "--command-id",
            "1",
            "--download-id",
            "abc",
        ],
    ] {
        assert!(
            args::parse(
                &argv.into_iter().map(String::from).collect::<Vec<_>>(),
                &json!({"apps":{}})
            )
            .is_err()
        );
    }
}
