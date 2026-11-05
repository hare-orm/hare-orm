//! JSON string literals shared by native codecs and SQL functions.

/// Writes `text` as `json.dumps(ensure_ascii=False)` writes a str.
pub(crate) fn write_json_string(text: &str, out: &mut String) {
    out.push_str(&serde_json::to_string(text).expect("serializing a string cannot fail"));
}
