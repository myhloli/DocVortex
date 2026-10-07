import re
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_CODE_LANGUAGE = "txt"
_INVALID_SURROGATES = re.compile("[\ud800-\udfff]")
_CODE_LANGUAGE_MODEL_SCOPE = ContextVar("docvortex_code_language_model_scope", default=None)


@contextmanager
def _code_language_model_scope():
    """单次文档转换只复用标准识别器，退出或嵌套调用后立即恢复原上下文。"""
    token = _CODE_LANGUAGE_MODEL_SCOPE.set({})
    try:
        yield
    finally:
        _CODE_LANGUAGE_MODEL_SCOPE.reset(token)


def _code_language_classifier():
    """惰性加载当前模型；仅复用不可变会话，不缓存文字输入或识别结果。"""
    from .magika import Magika, _MAGIKA_REUSE_REFERENCES

    scope = _CODE_LANGUAGE_MODEL_SCOPE.get()
    if scope is None or Magika is not _MAGIKA_REUSE_REFERENCES[0]:
        return Magika()
    references = (Magika, Magika.__init__, getattr(Magika, "identify_bytes", None), getattr(Magika, "_init_onnx_session", None))
    if references != _MAGIKA_REUSE_REFERENCES:
        return Magika()
    if "classifier" not in scope:
        scope["classifier"] = Magika()
    return scope["classifier"]


def remove_invalid_surrogates(text: str) -> str:
    """等价移除代理码点；常见合法 Unicode 文本无需逐字符 Python 扫描。"""
    return _INVALID_SURROGATES.sub("", text)


def _normalize_text_for_language_guess(code: str) -> str:
    """移除孤立代理字符并还原合法代理对，供代码语言识别使用。"""
    if not code:
        return ""
    normalized: list[str] = []
    index = 0
    while index < len(code):
        current_char = code[index]
        current_ord = ord(current_char)
        if 0xD800 <= current_ord <= 0xDBFF:
            if index + 1 < len(code):
                next_char = code[index + 1]
                next_ord = ord(next_char)
                if 0xDC00 <= next_ord <= 0xDFFF:
                    pair = current_char + next_char
                    normalized.append(pair.encode("utf-16", "surrogatepass").decode("utf-16"))
                    index += 2
                    continue
            index += 1
            continue
        if 0xDC00 <= current_ord <= 0xDFFF:
            index += 1
            continue
        normalized.append(current_char)
        index += 1
    return "".join(normalized)


def guess_code_language(code: str) -> str:
    """使用 Magika 推断代码块语言，失败时返回纯文本类型。"""
    normalized_code = _normalize_text_for_language_guess(code)
    if not normalized_code:
        return DEFAULT_CODE_LANGUAGE
    try:
        lang = _code_language_classifier().identify_bytes(normalized_code.encode("utf-8", errors="replace")).prediction.output.label
    except Exception:
        return DEFAULT_CODE_LANGUAGE
    return lang if lang != "unknown" else DEFAULT_CODE_LANGUAGE


__all__ = ["DEFAULT_CODE_LANGUAGE", "guess_code_language", "remove_invalid_surrogates"]
