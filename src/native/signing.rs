//! Testnet-only L1 signing and interoperable durable action attempts.
use super::{Result, fail};
use k256::ecdsa::SigningKey;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::Sha256;
use sha3::{Digest, Keccak256};
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    path::Path,
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WireOrder {
    pub a: u64,
    pub b: bool,
    pub p: String,
    pub s: String,
    pub r: bool,
    pub t: LimitType,
    pub c: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LimitType {
    pub limit: TimeInForce,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TimeInForce {
    pub tif: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WireCancel {
    pub asset: u64,
    pub cloid: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Action {
    Order {
        #[serde(rename = "type")]
        kind: String,
        orders: Vec<WireOrder>,
        grouping: String,
    },
    Cancel {
        #[serde(rename = "type")]
        kind: String,
        cancels: Vec<WireCancel>,
    },
}
impl Action {
    pub fn order(order: WireOrder) -> Self {
        Self::Order {
            kind: "order".into(),
            orders: vec![order],
            grouping: "na".into(),
        }
    }
    pub fn cancel(asset: u64, cloid: String) -> Self {
        Self::Cancel {
            kind: "cancelByCloid".into(),
            cancels: vec![WireCancel { asset, cloid }],
        }
    }
}
fn hash(bytes: impl AsRef<[u8]>) -> [u8; 32] {
    Keccak256::digest(bytes.as_ref()).into()
}
pub fn action_hash(action: &Action, nonce: u64, expiry: u64) -> Result<[u8; 32]> {
    let mut data = rmp_serde::to_vec_named(action)?;
    data.extend(nonce.to_be_bytes());
    data.push(0);
    data.push(0);
    data.extend(expiry.to_be_bytes());
    Ok(hash(data))
}
pub fn digest(action: &Action, nonce: u64, expiry: u64) -> Result<[u8; 32]> {
    let mut domain = Vec::new();
    domain.extend(hash(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    ));
    domain.extend(hash(b"Exchange"));
    domain.extend(hash(b"1"));
    let mut chain = [0u8; 32];
    chain[24..].copy_from_slice(&1337u64.to_be_bytes());
    domain.extend(chain);
    domain.extend([0; 32]);
    let mut agent = Vec::new();
    agent.extend(hash(b"Agent(string source,bytes32 connectionId)"));
    agent.extend(hash(b"b"));
    agent.extend(action_hash(action, nonce, expiry)?);
    let mut bytes = vec![0x19, 0x01];
    bytes.extend(hash(domain));
    bytes.extend(hash(agent));
    Ok(hash(bytes))
}
pub struct Signer(SigningKey);
impl Signer {
    pub fn new(key: &str, agent: &str) -> Result<Self> {
        let bytes = hex::decode(key.strip_prefix("0x").unwrap_or(key))
            .map_err(|_| fail("invalid signing key"))?;
        let signing = SigningKey::from_slice(&bytes).map_err(|_| fail("invalid signing key"))?;
        let public = signing.verifying_key().to_encoded_point(false);
        let address = hash(&public.as_bytes()[1..]);
        if format!("0x{}", hex::encode(&address[12..])) != agent.to_ascii_lowercase() {
            return Err(fail("signer/agent mismatch"));
        }
        Ok(Self(signing))
    }
    pub fn sign(&self, action: &Action, nonce: u64, expiry: u64) -> Result<Value> {
        let (signature, recovery) = self
            .0
            .sign_prehash_recoverable(&digest(action, nonce, expiry)?)
            .map_err(|_| fail("signing failed"))?;
        let bytes = signature.to_bytes();
        Ok(
            json!({"r":format!("0x{}",hex::encode(&bytes[..32])),"s":format!("0x{}",hex::encode(&bytes[32..])),"v":27+recovery.to_byte()}),
        )
    }
}

pub struct Attempts {
    file: File,
    pub entries: BTreeMap<String, Value>,
    last_nonce: u64,
    chain: String,
    poisoned: bool,
}
impl Attempts {
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(path)?;
        file.try_lock()?;
        let mut out = Self {
            file,
            entries: BTreeMap::new(),
            last_nonce: 0,
            chain: "0".repeat(64),
            poisoned: false,
        };
        let mut reader = BufReader::new(out.file.try_clone()?);
        loop {
            let mut line = Vec::new();
            let n = reader.by_ref().take(65537).read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            if n > 65536 || line.last() != Some(&b'\n') || out.entries.len() >= 20000 {
                return Err(fail("action journal incomplete/capacity"));
            }
            let row: Value = serde_json::from_slice(&line)?;
            let p = &row["payload"];
            let key = p["key"].as_str().ok_or_else(|| fail("action key"))?;
            let nonce = p["nonce"].as_u64().ok_or_else(|| fail("action nonce"))?;
            let checksum = out.checksum(p)?;
            if row["hash"] != checksum || nonce <= out.last_nonce || out.entries.contains_key(key) {
                return Err(fail("action journal corrupt"));
            }
            out.chain = checksum;
            out.last_nonce = nonce;
            out.entries.insert(key.into(), p.clone());
        }
        out.file.sync_all()?;
        File::open(path.parent().ok_or_else(|| fail("action parent"))?)?.sync_all()?;
        Ok(out)
    }
    fn checksum(&self, p: &Value) -> Result<String> {
        let mut h = Sha256::new();
        h.update(self.chain.as_bytes());
        h.update(serde_json::to_vec(p)?);
        Ok(hex::encode(h.finalize()))
    }
    pub fn prepare(&mut self, key: &str, action: &Action, now: u64) -> Result<(u64, u64)> {
        self.prepare_until(
            key,
            action,
            now,
            now.checked_add(5000)
                .ok_or_else(|| fail("expiry overflow"))?,
        )
    }
    pub fn prepare_until(
        &mut self,
        key: &str,
        action: &Action,
        now: u64,
        expiry: u64,
    ) -> Result<(u64, u64)> {
        if expiry <= now || expiry - now > 5000 {
            return Err(fail("invalid/expired action window"));
        }
        if self.poisoned || self.entries.contains_key(key) || self.entries.len() >= 20000 {
            return Err(fail("action already attempted or unavailable"));
        }
        let nonce = now.max(
            self.last_nonce
                .checked_add(1)
                .ok_or_else(|| fail("nonce overflow"))?,
        );
        if nonce.saturating_sub(now) > 1000 || nonce > i64::MAX as u64 {
            return Err(fail("clock rollback/nonce budget"));
        }
        let payload = json!({"key":key,"action":action,"nonce":nonce,"expires_after":expiry});
        let checksum = self.checksum(&payload)?;
        let mut bytes = serde_json::to_vec(&json!({"payload":payload,"hash":checksum}))?;
        bytes.push(b'\n');
        self.poisoned = true;
        if bytes.len() > 65536 {
            return Err(fail("action too large"));
        }
        self.file.write_all(&bytes)?;
        self.file.sync_all()?;
        self.last_nonce = nonce;
        self.chain = checksum;
        self.entries.insert(key.into(), payload);
        self.poisoned = false;
        Ok((nonce, expiry))
    }
}
pub fn account_lock(account: &str) -> Result<File> {
    let dir = Path::new("/tmp/mininautilus-hyperliquid-locks");
    std::fs::create_dir_all(dir)?;
    let path = dir.join(hex::encode(Sha256::digest(
        account.to_ascii_lowercase().as_bytes(),
    )));
    let file = OpenOptions::new().create(true).append(true).open(path)?;
    file.try_lock()?;
    Ok(file)
}
