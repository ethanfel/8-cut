use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicU64, Ordering};
use serde_json::{json, Value};

pub struct Mpv {
    process: Option<Child>,
    writer: Option<UnixStream>,
    reader: Option<BufReader<UnixStream>>,
    socket_path: String,
    next_id: AtomicU64,
}

impl Mpv {
    pub fn new() -> Self {
        let socket_path = format!("/tmp/8cut-mpv-{}", std::process::id());
        Mpv {
            process: None,
            writer: None,
            reader: None,
            socket_path,
            next_id: AtomicU64::new(1),
        }
    }

    pub fn start(&mut self) -> Result<(), String> {
        self.stop();

        let child = Command::new("mpv")
            .args([
                "--idle=yes",
                "--force-window=no",
                "--vo=null",
                "--keep-open=yes",
                &format!("--input-ipc-server={}", self.socket_path),
            ])
            .spawn()
            .map_err(|e| format!("Failed to start mpv: {e}"))?;

        self.process = Some(child);

        // Wait for socket
        for _ in 0..50 {
            std::thread::sleep(std::time::Duration::from_millis(100));
            if let Ok(stream) = UnixStream::connect(&self.socket_path) {
                stream.set_nonblocking(false).ok();
                let reader_stream = stream.try_clone().map_err(|e| e.to_string())?;
                self.writer = Some(stream);
                self.reader = Some(BufReader::new(reader_stream));
                return Ok(());
            }
        }
        Err("Timeout waiting for mpv IPC socket".into())
    }

    pub fn stop(&mut self) {
        if let Some(ref mut child) = self.process {
            child.kill().ok();
            child.wait().ok();
        }
        self.process = None;
        self.writer = None;
        self.reader = None;
        std::fs::remove_file(&self.socket_path).ok();
    }

    /// Send a command and wait for the matching response (by request_id).
    /// Skips over asynchronous mpv events while waiting.
    fn send_and_recv(&mut self, cmd: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let writer = self.writer.as_mut().ok_or("mpv not running")?;
        let reader = self.reader.as_mut().ok_or("mpv not running")?;

        let mut msg_val = cmd;
        msg_val["request_id"] = json!(id);
        let mut msg = serde_json::to_string(&msg_val).unwrap();
        msg.push('\n');
        writer.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;

        // Read lines until we find the response matching our request_id
        let mut line = String::new();
        loop {
            line.clear();
            reader.read_line(&mut line).map_err(|e| e.to_string())?;
            let parsed: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
            // mpv events have "event" key, responses have "request_id"
            if parsed.get("request_id").and_then(|v| v.as_u64()) == Some(id) {
                return Ok(parsed);
            }
            // Otherwise it's an async event — skip it
        }
    }

    pub fn command(&mut self, args: &[&str]) -> Result<(), String> {
        let resp = self.send_and_recv(json!({ "command": args }))?;
        if resp.get("error").and_then(|e| e.as_str()) != Some("success") {
            return Err(format!("mpv error: {}", resp.get("error").unwrap_or(&Value::Null)));
        }
        Ok(())
    }

    pub fn set_property(&mut self, name: &str, value: Value) -> Result<(), String> {
        let resp = self.send_and_recv(json!({ "command": ["set_property", name, value] }))?;
        if resp.get("error").and_then(|e| e.as_str()) != Some("success") {
            return Err(format!("mpv error: {}", resp.get("error").unwrap_or(&Value::Null)));
        }
        Ok(())
    }

    pub fn get_property(&mut self, name: &str) -> Result<Value, String> {
        let resp = self.send_and_recv(json!({ "command": ["get_property", name] }))?;
        if resp.get("error").and_then(|e| e.as_str()) != Some("success") {
            return Err(format!("mpv error: {}", resp.get("error").unwrap_or(&Value::Null)));
        }
        Ok(resp.get("data").cloned().unwrap_or(Value::Null))
    }

    pub fn load_file(&mut self, video_url: &str, audio_url: &str) -> Result<(), String> {
        // Pass audio-file option during load so both streams sync from the start
        let options = format!("audio-file={}", audio_url);
        self.command(&["loadfile", video_url, "replace", &options])
    }

    pub fn seek(&mut self, time: f64) -> Result<(), String> {
        self.command(&["seek", &time.to_string(), "absolute"])
    }

    pub fn pause(&mut self) -> Result<(), String> {
        self.set_property("pause", json!(true))
    }

    pub fn resume(&mut self) -> Result<(), String> {
        self.set_property("pause", json!(false))
    }

    pub fn set_loop(&mut self, a: f64, b: f64) -> Result<(), String> {
        self.set_property("ab-loop-a", json!(a))?;
        self.set_property("ab-loop-b", json!(b))
    }

    pub fn clear_loop(&mut self) -> Result<(), String> {
        self.set_property("ab-loop-a", json!("no"))?;
        self.set_property("ab-loop-b", json!("no"))
    }

    pub fn time_pos(&mut self) -> Result<f64, String> {
        let val = self.get_property("time-pos")?;
        val.as_f64().ok_or("time-pos not a number".into())
    }

    pub fn get_duration(&mut self) -> Result<f64, String> {
        let val = self.get_property("duration")?;
        val.as_f64().ok_or("duration not a number".into())
    }
}

impl Drop for Mpv {
    fn drop(&mut self) {
        self.stop();
    }
}
