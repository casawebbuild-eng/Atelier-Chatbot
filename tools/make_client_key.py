#!/usr/bin/env python3
"""Make a client their own key for the admin page.

  py tools\\make_client_key.py casa-atelier             new key for that brand
  py tools\\make_client_key.py casa-atelier --rotate    replace an existing key (the old one stops working)

The key is printed ONCE. Only its SHA-256 hash is saved, in brands/<brand>.json, which is safe to commit.
A client key opens the admin page for that brand's client only: their documents, nothing else.
"""
import hashlib
import json
import pathlib
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rotate = "--rotate" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    path = ROOT / "brands" / f"{args[0]}.json"
    if not path.is_file():
        print(f"No brand file at {path}. Existing brands:",
              ", ".join(sorted(p.stem for p in (ROOT / "brands").glob("*.json"))) or "none")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    if not str(data.get("client", "")).strip():
        print(f'{path.name} has no "client" field, so there is nothing for the key to open. Add one first.')
        return 1
    if data.get("adminKeyHash") and not rotate:
        print(f"{path.name} already has a key. Use --rotate to replace it (the old key stops working).")
        return 1

    key = secrets.token_hex(24)  # 48 characters
    data["adminKeyHash"] = hashlib.sha256(key.encode()).hexdigest()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nKey for {data['client']} (brand {args[0]}). Copy it now, it is not stored anywhere:\n")
    print(f"    {key}\n")
    print("Give it to the client with the address  <your-site>/admin")
    print(f"Only its hash was saved, in brands/{args[0]}.json. Commit that file; the server reads it on the next request.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
