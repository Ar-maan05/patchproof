# Decoding our own session tokens fails randomly

`b64url_decode(b64url_encode(data))` works for some payloads and raises `binascii.Error: Incorrect padding` for others. For example `b64url_decode("YQ")` blows up, although `b64url_encode(b"a")` is what produced "YQ".

Expected: decode accepts unpadded URL-safe base64 and returns the original bytes.
