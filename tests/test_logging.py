from cswapsync.logging_config import redact


def test_redacts_known_secret_shapes():
    text = ('sk-ant-oat01-ABCDEF ya29.a0AfH6 AGE-SECRET-KEY-1QQQ GOCSPX-abc '
            '"refreshToken": "1//0gSECRETSECRETSECRETSECRET" "client_secret": "zzz"')
    out = redact(text)
    for secret in ("ABCDEF", "a0AfH6", "1QQQ", "GOCSPX-abc", "SECRETSECRET", "zzz"):
        assert secret not in out
    assert out.count("[REDACTED]") >= 6
