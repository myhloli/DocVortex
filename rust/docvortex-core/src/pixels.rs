//! 对独立位图字节执行裁剪、通道排列及直角旋转，不持有 PDFium 指针。

/// 校验步长和边界，按原方向分类语义返回 OpenCV 所需的连续 BGR 像素。
pub fn crop_bgr(
    data: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    mode: &str,
    bbox: (usize, usize, usize, usize),
    angle: u16,
) -> Result<(Vec<u8>, usize, usize), &'static str> {
    let (channels, rgb) = match mode {
        "BGR" => (3, false),
        "BGRX" | "BGRA" => (4, false),
        "RGB" => (3, true),
        "RGBX" | "RGBA" => (4, true),
        "L" => (1, false),
        _ => return Err("unsupported bitmap mode"),
    };
    if width.checked_mul(channels).is_none_or(|n| n > stride)
        || height.checked_mul(stride).is_none_or(|n| n > data.len())
    {
        return Err("invalid bitmap buffer or stride");
    }
    let (x0, y0, x1, y1) = bbox;
    if x0 >= x1 || y0 >= y1 || x1 > width || y1 > height {
        return Err("invalid crop bounds");
    }
    if ![0, 90, 180, 270].contains(&angle) {
        return Err("invalid crop angle");
    }
    let (w, h) = (x1 - x0, y1 - y0);
    let (ow, oh) = if angle == 90 || angle == 270 {
        (h, w)
    } else {
        (w, h)
    };
    let len = ow
        .checked_mul(oh)
        .and_then(|n| n.checked_mul(3))
        .ok_or("crop size overflow")?;
    let mut output = vec![0; len];
    if mode == "BGR" && angle == 0 {
        for y in 0..h {
            let start = (y0 + y) * stride + x0 * 3;
            output[y * w * 3..(y + 1) * w * 3].copy_from_slice(&data[start..start + w * 3]);
        }
    } else {
        for y in 0..h {
            for x in 0..w {
                let source = (y0 + y) * stride + (x0 + x) * channels;
                let (dx, dy) = match angle {
                    90 => (y, w - 1 - x),
                    180 => (w - 1 - x, h - 1 - y),
                    270 => (h - 1 - y, x),
                    _ => (x, y),
                };
                let target = (dy * ow + dx) * 3;
                let pixel = if channels == 1 {
                    [data[source]; 3]
                } else if rgb {
                    [data[source + 2], data[source + 1], data[source]]
                } else {
                    [data[source], data[source + 1], data[source + 2]]
                };
                output[target..target + 3].copy_from_slice(&pixel);
            }
        }
    }
    Ok((output, ow, oh))
}
