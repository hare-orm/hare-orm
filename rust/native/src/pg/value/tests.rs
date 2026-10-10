//! Binding and decoding across types.

use chrono::{NaiveDate, NaiveTime};
use tokio_postgres::types::{Kind as TypeShape, Type};

use crate::pg::value::decode::{decode_value, type_may_need_server_text_form, ServerTextForms};
use crate::pg::value::test_support::*;
use crate::pg::value::{RangeValue, Value};

#[test]
fn values_of_the_wrong_type_are_rejected_instead_of_written_as_raw_bytes() {
    let element = |value: Value| Value::Array(vec![value]);
    let datetime = NaiveDate::from_ymd_opt(2000, 1, 1).unwrap().and_hms_opt(0, 0, 1).unwrap();
    assert!(encode(&element(Value::Bytes(vec![0, 0, 0, 7])), &Type::INT4_ARRAY).is_err());
    assert!(encode(&element(Value::Date(NaiveDate::from_ymd_opt(2000, 1, 8).unwrap())), &Type::INT4_ARRAY).is_err());
    assert!(encode(&element(Value::Timestamp(datetime)), &Type::INT8_ARRAY).is_err());
    assert!(encode(&element(Value::Time(NaiveTime::from_hms_opt(0, 0, 1).unwrap())), &Type::INT8_ARRAY).is_err());
    assert!(encode(&element(Value::Uuid(uuid::Uuid::from_u128(5))), &Type::INTERVAL_ARRAY).is_err());
    assert!(encode(&Value::Timestamp(datetime), &Type::INT8).is_err());
    let range = Value::Range(RangeValue {
        lower: Some(Box::new(Value::Text("1".to_string()))),
        upper: None,
        lower_inc: true,
        upper_inc: false,
        empty: false,
    });
    assert!(encode(&range, &Type::INT4_RANGE).is_err());
}

#[test]
fn asyncpg_compatible_conversions_are_accepted() {
    let raw = encode(&Value::Array(vec![Value::Bool(true)]), &Type::INT4_ARRAY).expect("a bool binds to an integer");
    assert!(matches!(decode(&Type::INT4_ARRAY, &raw), Value::Array(items) if matches!(items[..], [Value::Int(1)])));
    let datetime = NaiveDate::from_ymd_opt(2020, 1, 2).unwrap().and_hms_opt(3, 4, 0).unwrap();
    let raw = encode(&Value::Timestamp(datetime), &Type::DATE).expect("a naive datetime binds to a date");
    assert!(
        matches!(decode(&Type::DATE, &raw), Value::Date(date) if date == NaiveDate::from_ymd_opt(2020, 1, 2).unwrap())
    );
    assert_eq!(encode(&Value::Decimal("1.5".to_string()), &Type::FLOAT8).unwrap(), 1.5f64.to_be_bytes().to_vec());
}

#[test]
fn domains_bind_and_decode_as_their_base_type() {
    let domain = Type::new("posint".to_string(), 16500, TypeShape::Domain(Type::INT4), "public".to_string());
    assert_eq!(encode(&Value::Int(5), &domain).unwrap(), 5i32.to_be_bytes().to_vec());
    assert!(matches!(decode(&domain, &5i32.to_be_bytes()), Value::Int(5)));
    let domain_array = Type::new("_posint".to_string(), 16501, TypeShape::Array(domain), "public".to_string());
    let raw = encode(&Value::Array(vec![Value::Int(5)]), &domain_array).unwrap();
    assert!(matches!(decode(&domain_array, &raw), Value::Array(items) if matches!(items[..], [Value::Int(5)])));
}

#[test]
fn integers_bind_to_oid_like_types() {
    assert_eq!(encode(&Value::Int(16384), &Type::OID).unwrap(), 16384u32.to_be_bytes().to_vec());
    assert_eq!(encode(&Value::Int(1259), &Type::REGCLASS).unwrap(), 1259u32.to_be_bytes().to_vec());
    assert!(encode(&Value::Int(-1), &Type::OID).is_err());
    assert!(matches!(decode(&Type::OID, &16384u32.to_be_bytes()), Value::Int(16384)));
    assert!(matches!(decode(&Type::XID, &1u32.to_be_bytes()), Value::Int(1)));
}

#[test]
fn catalog_and_pseudo_types_decode_natively() {
    let raw = encode(&Value::Array(vec![Value::Int(1), Value::Int(2)]), &Type::INT2_ARRAY).unwrap();
    assert!(
        matches!(decode(&Type::INT2_VECTOR, &raw), Value::Array(items) if matches!(items[..], [Value::Int(1), Value::Int(2)]))
    );
    assert!(matches!(decode(&Type::VOID, &[]), Value::Null));
    assert!(matches!(decode(&Type::CHAR, &[0xc3]), Value::Bytes(bytes) if bytes == vec![0xc3]));
}

#[test]
fn types_without_a_decoder_are_collected_for_their_server_text_form() {
    let mut forms = ServerTextForms::collecting();
    assert!(matches!(decode_value(&Type::POINT, &[1, 2], &mut forms).unwrap(), Value::Null));
    assert!(matches!(decode_value(&Type::REGCLASS, &[0, 0, 4, 235], &mut forms).unwrap(), Value::Null));
    let requests = forms.take_requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].0, Type::POINT);
    let mut forms = ServerTextForms::supplying(vec!["(1,2)".to_string()]);
    assert!(matches!(decode_value(&Type::POINT, &[1, 2], &mut forms).unwrap(), Value::Text(text) if text == "(1,2)"));
    assert!(type_may_need_server_text_form(&Type::POINT));
    assert!(type_may_need_server_text_form(&Type::RECORD));
    assert!(!type_may_need_server_text_form(&Type::INT4_ARRAY));
    assert!(!type_may_need_server_text_form(&Type::INT4MULTI_RANGE));
}

#[test]
fn anonymous_records_decode_as_tuples() {
    let mut raw = 2i32.to_be_bytes().to_vec();
    raw.extend_from_slice(&23u32.to_be_bytes());
    raw.extend_from_slice(&4i32.to_be_bytes());
    raw.extend_from_slice(&1i32.to_be_bytes());
    raw.extend_from_slice(&25u32.to_be_bytes());
    raw.extend_from_slice(&(-1i32).to_be_bytes());
    match decode(&Type::RECORD, &raw) {
        Value::Composite(items) => assert!(matches!(items[..], [Value::Int(1), Value::Null])),
        other => panic!("unexpected {other:?}"),
    }
}
