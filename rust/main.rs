mod args;
mod batch;
mod catalog;
mod client;
mod config;
mod engine;
mod missing;
mod output;
#[cfg(test)]
mod tests;
mod waiting;
mod workflows;

use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};
pub static INTERRUPTED: AtomicBool = AtomicBool::new(false);
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone)]
pub struct Error {
    pub message: String,
    pub status: Option<u16>,
}
impl Error {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            status: None,
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.message.fmt(f)
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::new(e.to_string())
    }
}
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Self::new(format!("Invalid JSON: {e}"))
    }
}
impl From<reqwest::Error> for Error {
    fn from(e: reqwest::Error) -> Self {
        Self::new(e.without_url().to_string())
    }
}
impl From<url::ParseError> for Error {
    fn from(e: url::ParseError) -> Self {
        Self::new(e.to_string())
    }
}
impl From<regex::Error> for Error {
    fn from(e: regex::Error) -> Self {
        Self::new(format!("Invalid filter regex: {e}"))
    }
}
pub fn err<T>(message: impl Into<String>) -> Result<T> {
    Err(Error::new(message))
}
pub fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        _ => v.to_string(),
    }
}
pub fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
pub fn object(v: &Value) -> Result<&serde_json::Map<String, Value>> {
    v.as_object()
        .ok_or_else(|| Error::new("Expected a JSON object"))
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let global_end = argv.iter().position(|a| a == "--").unwrap_or(argv.len());
    if argv[..global_end].iter().any(|a| a == "--version") {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let mut redactor = config::Redactor::default();
    let result = (|| -> Result<i32> {
        let path = config::config_path(args::config_argument(&argv))?;
        let config = config::read_config(&path, false)?;
        redactor.collect(&config);
        for name in config["apps"]
            .as_object()
            .into_iter()
            .flat_map(|m| m.keys())
        {
            if let Ok(app) = config::app_config(&config, name) {
                redactor.collect(&app);
            }
        }
        if argv[..global_end]
            .iter()
            .any(|a| a == "--help" || a == "-h")
            || argv.is_empty()
        {
            println!("{}", args::help(&argv, &config));
            return Ok(0);
        }
        let options = args::parse(&argv, &config)?;
        let _ = ctrlc::set_handler(|| {
            INTERRUPTED.store(true, Ordering::SeqCst);
        });
        let engine = engine::Engine::new(config, path);
        let outcome = engine.run(options)?;
        redactor.collect(&outcome.data);
        println!(
            "{}",
            output::render(
                &redactor.clean(&outcome.data),
                &outcome.args,
                outcome.result.as_deref()
            )?
        );
        Ok(outcome.exit_code())
    })();
    let code = match result {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{}", json!({"error": redactor.clean(&json!(e.message))}));
            if INTERRUPTED.load(Ordering::SeqCst) {
                130
            } else {
                2
            }
        }
    };
    std::process::exit(code);
}
