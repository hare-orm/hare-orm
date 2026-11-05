//! `INET`/`CIDR`.

use postgres_protocol::types::inet_from_sql;
use tokio_postgres::types::Type;

use crate::pg::value::{BoxError, NetworkValue};

/// `INET`/`CIDR`: address family (2 = IPv4, 3 = IPv6), prefix length, is-cidr flag, address
/// length, then the address bytes.
pub(crate) fn decode_network(postgres_type: &Type, raw: &[u8]) -> Result<NetworkValue, BoxError> {
    let value = inet_from_sql(raw)?;
    Ok(NetworkValue { address: value.addr(), prefix_length: value.netmask(), is_cidr: *postgres_type == Type::CIDR })
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
