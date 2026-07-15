//! Smoke example: load a profile YAML and run one turn.
//!
//! ```sh
//! cargo run --example run_profile -- path/to/profile.yaml "your message"
//! ```

#[cfg(feature = "yaml")]
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    use agentproc::{run, Profile, RunOptions};

    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: run_profile <profile.yaml> <message>");
        std::process::exit(2);
    }
    let profile = Profile::from_path(&args[1])?;
    let opts = RunOptions::new(&args[2])
        .on_partial(|text, _| println!("[partial] {text}"))
        .on_session(|sid| eprintln!("[session] {sid}"));

    let result = run(&profile, opts).await?;
    eprintln!(
        "[result] exit={} session={} timed_out={}",
        result.exit_code,
        result.session_id,
        result.timed_out
    );
    if !result.reply.is_empty() {
        println!("{}", result.reply);
    }
    if !result.error.is_empty() {
        eprintln!("[error] {}", result.error);
        std::process::exit(1);
    }
    Ok(())
}

#[cfg(not(feature = "yaml"))]
fn main() {
    eprintln!("run_profile example requires the `yaml` feature");
}
