"""Write certifi's bundle plus the Windows certificate store to ca-bundle.pem.

For machines where antivirus HTTPS scanning or a proxy makes pip fail with
CERTIFICATE_VERIFY_FAILED before it can be upgraded to a truststore-aware version:

    python scripts/export_windows_certs.py
    .venv\\Scripts\\python -m pip install --cert ca-bundle.pem --upgrade pip
"""

import ssl
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ca-bundle.pem")
pems = []
try:
    import certifi

    pems.append(Path(certifi.where()).read_text())
except ImportError:
    pass
for store in ("ROOT", "CA"):
    for der, encoding, _trust in ssl.enum_certificates(store):
        if encoding == "x509_asn":
            pems.append(ssl.DER_cert_to_PEM_cert(der))
out.write_text("\n".join(pems))
print(f"Wrote {len(pems)} certificates to {out}")
