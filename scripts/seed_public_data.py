import argparse
import json
import requests

def main():
    parser = argparse.ArgumentParser(description="Seed public AI risk memory into ITTE")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--file", default="data/public_ai_risk_seed.jsonl")

    args = parser.parse_args()

    url = args.server.rstrip("/") + "/memory/seed"

    count = 0

    with open(args.file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            item = json.loads(line)
            res = requests.post(url, json=item, timeout=30)
            res.raise_for_status()

            count += 1

    print(f"Seeded {count} memory items.")

if __name__ == "__main__":
    main()
