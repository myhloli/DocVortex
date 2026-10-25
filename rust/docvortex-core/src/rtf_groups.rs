//! RTF 组范围扫描：完整校验词法边界，只保留按闭合顺序排列的子组偏移。

#[derive(Debug, PartialEq, Eq)]
pub struct ScanError {
    pub resource_limit: bool,
    pub message: String,
}

impl ScanError {
    /// 保留词法资源错误的分类和原始消息，绑定层不推断错误类型。
    fn resource(message: impl Into<String>) -> Self {
        Self {
            resource_limit: true,
            message: message.into(),
        }
    }

    /// 保留二进制长度错误的格式分类，避免变成通用原生异常。
    fn malformed(message: impl Into<String>) -> Self {
        Self {
            resource_limit: false,
            message: message.into(),
        }
    }
}

/// 兼容 Python 对两个 ASCII 字符调用 int(value, 16) 的接受范围。
fn valid_hex_pair(pair: &[u8]) -> bool {
    let whitespace = |value: u8| matches!(value, b' ' | b'\t'..=b'\r');
    let digit = |value: u8| value.is_ascii_hexdigit();
    (digit(pair[0]) && digit(pair[1]))
        || (digit(pair[1]) && (whitespace(pair[0]) || matches!(pair[0], b'+' | b'-')))
        || (digit(pair[0]) && whitespace(pair[1]))
}

/// 扫描全部输入后返回子组，保持二进制跳过、单次预算、错误优先序和不平衡根组语义。
pub fn group_spans(
    data: &[u8],
    direct_only: bool,
    max_tokens: usize,
    max_depth: usize,
) -> Result<Vec<(usize, usize)>, ScanError> {
    let mut cursor = 0;
    let mut token_count = 0;
    let mut starts = Vec::new();
    let mut groups = Vec::new();
    while cursor < data.len() {
        let start = cursor;
        let value = data[cursor];
        cursor += 1;
        match value {
            b'{' => {
                if starts.len() >= max_depth {
                    return Err(ScanError::resource(format!(
                        "RTF group nesting exceeds max_group_depth={max_depth}"
                    )));
                }
            }
            b'}' => {}
            b'\\' if cursor == data.len() => {}
            b'\\' => match data[cursor] {
                b'\r' => {
                    cursor += 1;
                    if data.get(cursor) == Some(&b'\n') {
                        cursor += 1;
                    }
                }
                b'\n' => cursor += 1,
                b'\'' => {
                    cursor += 1;
                    if data.len() - cursor >= 2 && valid_hex_pair(&data[cursor..cursor + 2]) {
                        cursor += 2;
                    }
                }
                first if first.is_ascii_alphabetic() => {
                    let name_start = cursor;
                    while cursor < data.len() && data[cursor].is_ascii_alphabetic() {
                        cursor += 1;
                    }
                    let binary = data[name_start..cursor].eq_ignore_ascii_case(b"bin");
                    let negative = data.get(cursor) == Some(&b'-');
                    if negative {
                        cursor += 1;
                    }
                    let number_start = cursor;
                    while cursor < data.len() && data[cursor].is_ascii_digit() {
                        cursor += 1;
                    }
                    let digits = cursor - number_start;
                    if digits > 10 {
                        return Err(ScanError::resource(
                            "RTF control parameter exceeds max_digits=10",
                        ));
                    }
                    let parameter = if digits > 0 {
                        let mut number: i64 = 0;
                        for digit in &data[number_start..cursor] {
                            number = number * 10 + i64::from(digit - b'0');
                        }
                        if negative {
                            number = -number;
                        }
                        if number < i64::from(i32::MIN) || number > i64::from(i32::MAX) {
                            return Err(ScanError::resource(
                                "RTF control parameter exceeds signed 32-bit range",
                            ));
                        }
                        Some(number)
                    } else {
                        None
                    };
                    if data.get(cursor) == Some(&b' ') {
                        cursor += 1;
                    }
                    if binary {
                        let length = match parameter {
                            Some(value) if value >= 0 => value as usize,
                            _ => {
                                return Err(ScanError::malformed(
                                    "RTF bin control requires a non-negative length",
                                ));
                            }
                        };
                        let available = data.len() - cursor;
                        if length > available {
                            return Err(ScanError::malformed(format!(
                                "RTF bin payload is truncated: declared={length}, available={available}"
                            )));
                        }
                        cursor += length;
                    }
                }
                _ => cursor += 1,
            },
            _ => {
                while cursor < data.len() && !matches!(data[cursor], b'{' | b'}' | b'\\') {
                    cursor += 1;
                }
            }
        }
        // 与生成器一致：单个 token 的词法错误先检查，随后才检查计数并交给组收集器。
        if token_count >= max_tokens {
            return Err(ScanError::resource(format!(
                "RTF token count exceeds max_tokens={max_tokens}"
            )));
        }
        token_count += 1;
        if value == b'{' {
            starts.push(start);
        } else if value == b'}' {
            let depth = starts.len();
            if let Some(open) = starts.pop() {
                if (direct_only && depth == 2) || (!direct_only && depth >= 2) {
                    groups.push((open, cursor));
                }
            }
        }
    }
    Ok(groups)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 同时覆盖闭合顺序、直接子组和多余右括号后的新根组。
    #[test]
    fn closing_order_and_root_recovery() {
        let data = b"}{{a{b}}{c}}{d}";
        assert_eq!(
            group_spans(data, false, 100, 256).unwrap(),
            vec![(4, 7), (2, 8), (8, 11)]
        );
        assert_eq!(
            group_spans(data, true, 100, 256).unwrap(),
            vec![(2, 8), (8, 11)]
        );
        assert!(group_spans(b"{{unfinished", false, 100, 256)
            .unwrap()
            .is_empty());
    }

    /// 二进制内容中的结构字符和伪控制参数不得进入词法扫描。
    #[test]
    fn binary_payload_is_opaque() {
        assert_eq!(
            group_spans(br"{\BIN4 }{\x{ok}}", false, 100, 256).unwrap(),
            vec![(11, 15)]
        );
        assert!(group_spans(br"{\bin0 {ok}}", false, 100, 256).is_ok());
    }

    /// 固定参数限制在预算检查之前报告，长度错误不会被计数错误覆盖。
    #[test]
    fn lexical_errors_precede_token_budget() {
        for (data, resource, message) in [
            (
                br"\bin-1".as_slice(),
                false,
                "RTF bin control requires a non-negative length",
            ),
            (
                br"\bin3 a".as_slice(),
                false,
                "RTF bin payload is truncated: declared=3, available=1",
            ),
            (
                br"\x99999999999".as_slice(),
                true,
                "RTF control parameter exceeds max_digits=10",
            ),
            (
                br"\x2147483648".as_slice(),
                true,
                "RTF control parameter exceeds signed 32-bit range",
            ),
            (
                br"\x-2147483649".as_slice(),
                true,
                "RTF control parameter exceeds signed 32-bit range",
            ),
        ] {
            assert_eq!(
                group_spans(data, false, 0, 256).unwrap_err(),
                ScanError {
                    resource_limit: resource,
                    message: message.into()
                }
            );
        }
        assert!(group_spans(br"\x2147483647\y-2147483648", false, 2, 256).is_ok());
    }

    /// 分别检查恰好预算、超出预算和深度与计数同时越界时的顺序。
    #[test]
    fn exact_resource_limits() {
        assert!(group_spans(b"{{}}", false, 4, 2).is_ok());
        assert_eq!(
            group_spans(b"{{}}", false, 3, 2).unwrap_err().message,
            "RTF token count exceeds max_tokens=3"
        );
        assert_eq!(
            group_spans(b"{{}}", false, 1, 1).unwrap_err().message,
            "RTF group nesting exceeds max_group_depth=1"
        );
        assert!(group_spans(b"", false, 0, 0).is_ok());
    }

    /// 兼容十六进制解析对符号、空白、大小写和高位字节的既有接受范围。
    #[test]
    fn python_hex_pair_acceptance() {
        for pair in [b"ff", b"Af", b"+f", b"-f", b" f", b"f ", b"\tf", b"f\r"] {
            assert!(valid_hex_pair(pair));
            let mut data = br"\'".to_vec();
            data.extend(pair);
            assert!(group_spans(&data, false, 1, 256).is_ok());
        }
        for pair in [b"  ", b"0x", b"_f", b"f_", b"{f", b"\x1cf", b"\xfff"] {
            assert!(!valid_hex_pair(pair));
        }
    }
}
