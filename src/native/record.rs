//! Lossless cold-path observations. Secrets and signed request bodies are excluded.
use super::{Result, fail, wall_ms};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
    sync::{Arc, Mutex},
};

#[derive(Clone)]
pub struct Recorder(Arc<Mutex<File>>);
impl Recorder {
    pub fn create(path: &Path) -> Result<Self> {
        let file = OpenOptions::new().create_new(true).write(true).open(path)?;
        File::open(path.parent().ok_or_else(|| fail("recorder parent"))?)?.sync_all()?;
        Ok(Self(Arc::new(Mutex::new(file))))
    }
    pub fn append(&self, value: Value) -> Result<()> {
        let mut file = self.0.lock().map_err(|_| fail("recorder poisoned"))?;
        serde_json::to_writer(&mut *file, &value)?;
        file.write_all(b"\n")?;
        Ok(())
    }
    pub fn sync(&self) -> Result<()> {
        self.0
            .lock()
            .map_err(|_| fail("recorder poisoned"))?
            .sync_all()?;
        Ok(())
    }
    pub fn response(
        &self,
        path: &str,
        request: &Value,
        response: &Value,
        started_ms: u64,
        elapsed_ns: u64,
    ) -> Result<()> {
        // A signature is reusable authorization until expiry: never record it.
        let request = if path == "/exchange" {
            json!({"type":request["action"]["type"],"nonce":request["nonce"],"expiresAfter":request["expiresAfter"]})
        } else {
            request.clone()
        };
        self.append(json!({"kind":"http","path":path,"request":request,"response":response,"requested_ms":started_ms,"received_ms":wall_ms(),"elapsed_ns":elapsed_ns}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn records_response_without_reusable_signature() {
        let dir = std::env::temp_dir().join(format!("native-recorder-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("facts");
        let recorder = Recorder::create(&path).unwrap();
        recorder.response("/exchange", &json!({"action":{"type":"order"},"signature":{"r":"NEVER_RECORD"},"nonce":7,"expiresAfter":9}), &json!({"status":"ok"}), 1, 2).unwrap();
        recorder.sync().unwrap();
        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(!raw.contains("NEVER_RECORD"));
        assert!(!raw.contains("signature"));
        let row: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(row["request"]["nonce"], 7);
        assert_eq!(row["response"]["status"], "ok");
        assert!(Recorder::create(&path).is_err());
        drop(recorder);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
