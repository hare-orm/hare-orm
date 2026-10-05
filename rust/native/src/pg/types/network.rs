//! `INET`/`CIDR`.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use tokio_postgres::types::Type;

use crate::pg::value::{BoxError, NetworkValue};

/// `INET`/`CIDR`: address family (2 = IPv4, 3 = IPv6), prefix length, is-cidr flag, address
/// length, then the address bytes.
pub(crate) fn decode_network(postgres_type: &Type, raw: &[u8]) -> Result<NetworkValue, BoxError> {
    if raw.len() < 4 {
        return Err("invalid inet/cidr payload: too short".into());
    }
    let family = raw[0];
    let prefix_length = raw[1];
    let address_length = raw[3] as usize;
    let address_bytes = &raw[4..];
    if address_bytes.len() != address_length {
        return Err(format!(
            "invalid inet/cidr payload: address length mismatch (header says {address_length}, got {})",
            address_bytes.len()
        )
        .into());
    }
    let (address, full_prefix_length) = match (family, address_length) {
        (2, 4) => {
            (IpAddr::V4(Ipv4Addr::new(address_bytes[0], address_bytes[1], address_bytes[2], address_bytes[3])), 32)
        }
        (3, 16) => {
            let mut octets = [0u8; 16];
            octets.copy_from_slice(address_bytes);
            (IpAddr::V6(Ipv6Addr::from(octets)), 128)
        }
        _ => return Err(format!("unrecognized inet/cidr address family {family} with length {address_length}").into()),
    };
    if prefix_length > full_prefix_length {
        return Err(format!("invalid inet/cidr prefix length {prefix_length}").into());
    }
    Ok(NetworkValue { address, prefix_length, is_cidr: *postgres_type == Type::CIDR })
}

#[cfg(test)]
mod tests {

    use tokio_postgres::types::Type;

    use crate::pg::value::decode::{decode_value, ServerTextForms};
    use crate::pg::value::test_support::*;
    use crate::pg::value::{NetworkValue, Value};

    #[test]
    fn inet_and_cidr_decode_address_prefix_and_type() {
        let host = decode(&Type::INET, &[2, 32, 0, 4, 192, 168, 1, 5]);
        assert!(matches!(host, Value::Network(NetworkValue { prefix_length: 32, is_cidr: false, .. })));
        let network = decode(&Type::CIDR, &[2, 24, 1, 4, 192, 168, 1, 0]);
        assert!(matches!(network, Value::Network(NetworkValue { prefix_length: 24, is_cidr: true, .. })));
        let mut raw = vec![3, 128, 0, 16];
        raw.extend_from_slice(&[0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]);
        match decode(&Type::INET, &raw) {
            Value::Network(network) => assert_eq!(network.address.to_string(), "2001:db8::1"),
            other => panic!("unexpected {other:?}"),
        }
        assert!(decode_value(&Type::INET, &[2, 33, 0, 4, 1, 2, 3, 4], &mut ServerTextForms::collecting()).is_err());
    }
}
