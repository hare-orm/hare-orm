//! The text `json.dumps(ensure_ascii=False)` writes of a str - shared by the JSON path and array
//! text functions.

use std::fmt::Write;

/// Writes `text` as `json.dumps(ensure_ascii=False)` writes a str.
pub fn write_json_string(text: &str, out: &mut String) {
    out.push('"');
    let mut copied_up_to = 0;
    for (index, &byte) in text.as_bytes().iter().enumerate() {
        let escape = match byte {
            b'"' => "\\\"",
            b'\\' => "\\\\",
            b'\n' => "\\n",
            b'\r' => "\\r",
            b'\t' => "\\t",
            0x08 => "\\b",
            0x0c => "\\f",
            control if control < 0x20 => {
                out.push_str(&text[copied_up_to..index]);
                let _ = write!(out, "\\u{control:04x}");
                copied_up_to = index + 1;
                continue;
            }
            _ => continue,
        };
        out.push_str(&text[copied_up_to..index]);
        out.push_str(escape);
        copied_up_to = index + 1;
    }
    out.push_str(&text[copied_up_to..]);
    out.push('"');
}
