//! 协议层的纯逻辑：请求行怎么拼、应答行怎么归类。
//!
//! 单独拆出来的原因：这部分**不碰操作系统**，可以被单元测试逐条锁死；
//! ipc.rs 只剩"开管道、起读线程、发信号"的胶水，由端到端 GUI 冒烟覆盖。
//! 覆盖率门禁（T7）因此只统计本文件，排除 ipc.rs / main.rs / gen/**。

use serde_json::{json, Value};

/// 一行应答的归类结果。
#[derive(Debug)]
pub enum Classified {
    /// 带 id：是对某个请求的应答，`text` 是原始 JSON（交给等待方解析）
    Reply { id: u64, text: String },
    /// 不带 id 且带 method：核主动推的事件
    Event(Value),
    /// 空行/坏 JSON/两种都不是：忽略
    Skip,
}

/// 拼一条请求行（单行 JSON + 一次性 token）。
pub fn build_request(id: u64, method: &str, params: Value, token: &str) -> String {
    json!({"id": id, "method": method, "params": params, "token": token}).to_string()
}

/// 把管道读到的一行归类。容忍前后空白；坏 JSON 一律 Skip，绝不 panic。
pub fn classify_line(line: &str) -> Classified {
    let text = line.trim();
    if text.is_empty() {
        return Classified::Skip;
    }
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return Classified::Skip;
    };
    match value.get("id").and_then(|id| id.as_u64()) {
        Some(id) => Classified::Reply {
            id,
            text: text.to_string(),
        },
        None => {
            if value.get("method").and_then(|m| m.as_str()).is_some() {
                Classified::Event(value)
            } else {
                Classified::Skip
            }
        }
    }
}

/// 应答体转交给前端前去掉 `id`（前端的 envelope 只有 ok/result/error）。
pub fn strip_id(mut value: Value) -> Value {
    if let Some(obj) = value.as_object_mut() {
        obj.remove("id");
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_request_keeps_token_and_params() {
        let line = build_request(7, "hello", json!({"a": 1}), "tok");
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["id"], 7);
        assert_eq!(v["method"], "hello");
        assert_eq!(v["token"], "tok");
        assert_eq!(v["params"]["a"], 1);
    }

    #[test]
    fn classify_reply_by_id() {
        match classify_line(r#"{"id":3,"ok":true,"result":{}}"#) {
            Classified::Reply { id, text } => {
                assert_eq!(id, 3);
                assert!(text.contains("\"ok\":true"));
            }
            other => panic!("expected reply, got {other:?}"),
        }
    }

    #[test]
    fn classify_event_without_id() {
        match classify_line(r#"{"method":"state","params":{"done":1}}"#) {
            Classified::Event(v) => assert_eq!(v["method"], "state"),
            other => panic!("expected event, got {other:?}"),
        }
    }

    #[test]
    fn classify_skips_noise() {
        assert!(matches!(classify_line(""), Classified::Skip));
        assert!(matches!(classify_line("   "), Classified::Skip));
        assert!(matches!(classify_line("not json"), Classified::Skip));
        assert!(matches!(classify_line(r#"{"foo":1}"#), Classified::Skip));
        assert!(matches!(classify_line(r#"{"id":"x"}"#), Classified::Skip));
    }

    #[test]
    fn classify_tolerates_surrounding_space() {
        match classify_line("  {\"id\":9,\"ok\":false}  ") {
            Classified::Reply { id, .. } => assert_eq!(id, 9),
            other => panic!("expected reply, got {other:?}"),
        }
    }

    #[test]
    fn strip_id_drops_only_id() {
        let out = strip_id(json!({"id": 5, "ok": true, "result": {"a": 1}}));
        assert!(out.get("id").is_none());
        assert_eq!(out["result"]["a"], 1);
        // 非对象输入原样返回，不 panic
        assert_eq!(strip_id(json!(1)), json!(1));
    }
}
